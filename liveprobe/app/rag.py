"""Lightweight RAG: fastembed (all-MiniLM-L6-v2, 384-d, local/offline) + pgvector + Postgres FTS hybrid search.

Every chunk carries its provenance (document, source URI, licence) and its position in the original file
(section heading, character span and line span), so a citation can always be traced back to the source text.
Ingestion of documents (runbooks, licensed public excerpts, closed incidents) lives in `rag_ingest.py`.

Ranking policy (hybrid, see tests/test_rag.py):
  1. candidate set = top `CANDIDATES` active chunks by cosine distance  UNION  top `CANDIDATES` by ts_rank_cd
     of an OR-query of normalized Hangul/English/digit tokens (SQL parameters, never raw query operators);
  2. fused score = sum over lists of 1 / (RRF_K + rank)  (reciprocal rank fusion, rank starts at 1);
  3. ties broken by smaller cosine distance, then smaller chunk id (deterministic);
  4. diversity: at most one chunk per document; at most `MAX_PER_KIND[kind]` hits of one kind
     (past incidents are capped at 1 so repeated similar incidents cannot push runbooks out of top-k).
`Hit.score` stays the cosine similarity (MVP-0 citation contract); `hybrid_score` is the fused score.

`mode` (hybrid | vector | lexical), `rrf_k`, `candidates`, `k` and the per-kind caps are parameters: the active
values come from the retrieval configuration that `rag_tuning` evaluates and selects. The defaults are the
ranking above. `pool` / `fuse` / `diversify` are shared by live search and offline evaluation.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml
from sqlalchemy import bindparam, func, literal_column, select, text
from sqlalchemy.orm import Session

from .models import RagChunk, RagDocument

log = logging.getLogger(__name__)

CANDIDATES = 30
RRF_K = 60
MODES = ("hybrid", "vector", "lexical")
# Verified-resolution memory of evaluation runs gets the same cap as production memory.
MAX_PER_KIND = {"past_incident": 1, "eval_memory": 1}
_WORD = re.compile(r"[a-z0-9\u1100-\u11ff\u3130-\u318f\uac00-\ud7a3]+")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "with", "by", "at",
         "be", "it", "as", "from", "this", "that", "그리고", "또는", "및", "에서", "으로", "대한", "관련"}


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    def __init__(self, model_name: str, cache_dir: str | None = None):
        from fastembed import TextEmbedding
        self._model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            return [v.tolist() for v in self._model.embed(texts)]


@dataclass
class Chunk:
    doc: str
    source: str
    chunk_ix: int
    content: str
    section: str | None = None
    char_start: int | None = None   # offsets into the original file text (front matter included)
    char_end: int | None = None
    line_start: int | None = None   # 1-based, inclusive
    line_end: int | None = None


@dataclass
class Hit:
    chunk_id: int
    doc: str
    source: str
    chunk_ix: int
    content: str
    score: float                         # cosine similarity (MVP-0 contract)
    hybrid_score: float = 0.0
    vector_rank: int | None = None
    lexical_rank: int | None = None
    section: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    locator: str | None = None
    kind: str | None = None
    title: str | None = None
    source_uri: str | None = None
    license: str | None = None
    attribution: str | None = None
    version_hash: str | None = None
    retrieved_at: str | None = None

    def provenance(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("doc", "source", "chunk_id", "chunk_ix", "version_hash", "retrieved_at",
                                               "kind", "title", "source_uri", "license", "attribution", "section",
                                               "locator", "line_start", "line_end", "char_start", "char_end")}


def front_matter(text: str) -> tuple[dict[str, Any], str, int]:
    """Return (meta, body, body_offset). YAML front matter; falls back to `key: value` lines."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            raw = text[3:end]
            try:
                meta = yaml.safe_load(raw) or {}
                if not isinstance(meta, dict):
                    meta = {}
            except yaml.YAMLError:
                meta = {}
                for line in raw.strip().splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip()
            off = end + 4
            while off < len(text) and text[off] in "\r\n \t":
                off += 1
            return meta, text[off:].rstrip(), off
    stripped = text.lstrip()
    return {}, stripped.rstrip(), len(text) - len(stripped)


def _front_matter(text: str) -> tuple[dict[str, str], str]:  # backwards compatible helper
    meta, body, _ = front_matter(text)
    return {k: str(v) for k, v in meta.items()}, body


_PARA = re.compile(r"\S.*?(?=\n[ \t]*\n|\Z)", re.S)
_HEADING = re.compile(r"#{1,6}\s+(.*)")


def chunk_markdown(doc: str, text: str, size: int = 500) -> list[Chunk]:
    """Pack original-order spans, including the title within the hard character cap.

    Oversized paragraphs are split, never tail-truncated: late observations and
    counter-evidence remain indexable with original char/line positions.
    """
    meta, body, off = front_matter(text)
    source = str(meta.get("source", "internal-runbook"))
    title = str(meta.get("title", doc))
    capacity = size - len(f"[{title}]\n")
    if isinstance(size, bool) or not isinstance(size, int) or capacity < 1:
        raise ValueError("chunk size must leave room for title and source text")
    out: list[Chunk] = []
    section: str | None = None
    buf: list[tuple[int, int]] = []   # spans (relative to body) packed into the current chunk
    buf_section: str | None = None

    def flush() -> None:
        if not buf:
            return
        a, b = buf[0][0], buf[-1][1]
        s, e = off + a, off + b
        out.append(Chunk(doc=doc, source=source, chunk_ix=len(out), content=f"[{title}]\n{body[a:b]}",
                         section=buf_section, char_start=s, char_end=e,
                         line_start=text.count("\n", 0, s) + 1, line_end=text.count("\n", 0, e) + 1))
        buf.clear()

    for m in _PARA.finditer(body):
        a, b = m.start(), m.end()
        para = body[a:b].rstrip()
        b = a + len(para)
        h = _HEADING.match(para)
        if h and para.startswith("##"):          # a new section starts a new chunk (as in MVP-0)
            flush()
            section = h.group(1).strip()
        elif h and section is None:
            section = h.group(1).strip()
        while a < b:
            end = min(b, a + capacity)
            if buf and (end - buf[0][0]) > capacity:
                flush()
            if not buf:
                buf_section = section
            buf.append((a, end))
            a = end
            if a < b:
                flush()
    flush()
    return out


def load_runbooks(directory: str) -> list[Chunk]:
    out: list[Chunk] = []
    for p in sorted(Path(directory).glob("*.md")):
        out.extend(chunk_markdown(p.name, p.read_text()))
    return out


def index_runbooks(s: Session, embedder: Embedder, directory: str) -> int:
    """MVP-0 entry point, now provenance-aware (idempotent per document). Returns number of new chunks."""
    from .rag_ingest import index_documents, load_runbook_documents
    return index_documents(s, embedder, load_runbook_documents(directory)).new_chunks


def lexical_query(query: str) -> str:
    """Bounded OR-query: normalized Hangul/English/digits, no tsquery operators.

    This is exact token matching, not Korean morphological analysis. Keep
    negation and one-character numbers/Hangul; discard short Latin noise only.
    """
    words, seen = [], set()
    for w in _WORD.findall(unicodedata.normalize("NFKC", query).lower()):
        if w in _STOP or (len(w) < 2 and w.isascii() and w.isalpha()) or w in seen:
            continue
        seen.add(w)
        words.append(w)
    return " | ".join(words[:32])


def lexical_statement(query: str, *, kinds: list[str] | None = None, candidates: int = CANDIDATES):
    """Build parameterized PostgreSQL lexical SQL without executing any I/O.

    Keep the same English configuration as the stored tsvector. Hangul tokens
    are retained by the PostgreSQL parser; DB-level recall is an integration check.
    """
    lq = lexical_query(query)
    if not lq:
        return None
    tsq = func.to_tsquery("english", bindparam("lq", lq))
    rank = func.ts_rank_cd(RagChunk.tsv, tsq)
    filters = [RagChunk.retired_at.is_(None), RagChunk.tsv.op("@@")(tsq)]
    if kinds:
        filters.append(func.coalesce(RagDocument.kind, literal_column("'runbook'")).in_(kinds))
    return (select(RagChunk.id, rank.label("r"))
            .outerjoin(RagDocument, RagDocument.id == RagChunk.document_id)
            .where(*filters).order_by(rank.desc(), RagChunk.id).limit(candidates))


def pool(s: Session, qv: list[float], query: str, *, kinds: list[str] | None = None,
         candidates: int = CANDIDATES, mode: str = "hybrid") -> tuple[list[int], list[int], dict[int, tuple]]:
    """Ranked vector and lexical candidate ids and, for their union, (doc, kind, document incident id, distance)."""
    kind_col = func.coalesce(RagDocument.kind, literal_column("'runbook'"))
    base_filter = [RagChunk.retired_at.is_(None)]
    if kinds:
        base_filter.append(kind_col.in_(kinds))
    dist = RagChunk.embedding.cosine_distance(qv)
    vec_ids: list[int] = []
    if mode != "lexical":
        vec_ids = [r.id for r in s.execute(
            select(RagChunk.id, dist.label("d"))
            .outerjoin(RagDocument, RagDocument.id == RagChunk.document_id)
            .where(*base_filter).order_by(dist, RagChunk.id).limit(candidates)).all()]
    lex_ids: list[int] = []
    lexical = lexical_statement(query, kinds=kinds, candidates=candidates) if mode != "vector" else None
    if lexical is not None:
        lex_ids = [r.id for r in s.execute(lexical).all()]
    ids = set(vec_ids) | set(lex_ids)
    meta: dict[int, tuple] = {}
    if ids:
        for r in s.execute(select(RagChunk.id, RagChunk.doc, kind_col.label("kind"), RagDocument.incident_id,
                                  dist.label("d"))
                           .outerjoin(RagDocument, RagDocument.id == RagChunk.document_id)
                           .where(RagChunk.id.in_(ids))).all():
            meta[r.id] = (r.doc, r.kind, r.incident_id, float(r.d))
    return vec_ids, lex_ids, meta


def fuse(vec_ids: list[int], lex_ids: list[int], dist: dict[int, float], *, mode: str = "hybrid",
         rrf_k: int = RRF_K, candidates: int = CANDIDATES) -> tuple[list[tuple[float, float, int]], dict, dict]:
    """Reciprocal rank fusion over the first `candidates` of each list. Ties: smaller distance, then smaller id."""
    if mode not in MODES:
        raise ValueError(f"unknown retrieval mode {mode!r}")
    vec = vec_ids[:candidates] if mode != "lexical" else []
    lex = lex_ids[:candidates] if mode != "vector" else []
    vec_rank = {cid: i + 1 for i, cid in enumerate(vec)}
    lex_rank = {cid: i + 1 for i, cid in enumerate(lex)}
    scored = [(sum(1.0 / (rrf_k + r[cid]) for r in (vec_rank, lex_rank) if cid in r), dist[cid], cid)
              for cid in set(vec_rank) | set(lex_rank)]
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    return scored, vec_rank, lex_rank


def diversify(scored: list[tuple[float, float, int]], doc_of, kind_of, k: int,
              max_per_kind: dict[str, int]) -> list[tuple[float, float, int]]:
    """At most one chunk per document and at most `max_per_kind[kind]` documents of one kind, in fused order."""
    out: list[tuple[float, float, int]] = []
    per_kind: dict[str, int] = {}
    seen_docs: set[str] = set()
    for item in scored:
        cid = item[2]
        doc, kind = doc_of(cid), kind_of(cid)
        if doc in seen_docs or (kind in max_per_kind and per_kind.get(kind, 0) >= max_per_kind[kind]):
            continue
        seen_docs.add(doc)
        per_kind[kind] = per_kind.get(kind, 0) + 1
        out.append(item)
        if len(out) >= k:
            break
    return out


def search(s: Session, embedder: Embedder, query: str, k: int = 3, *, kinds: list[str] | None = None,
           candidates: int = CANDIDATES, max_per_kind: dict[str, int] | None = None, mode: str = "hybrid",
           rrf_k: int = RRF_K) -> list[Hit]:
    if k <= 0:
        return []
    if mode not in MODES:
        raise ValueError(f"unknown retrieval mode {mode!r}")
    qv = embedder.embed([query])[0]
    max_per_kind = MAX_PER_KIND if max_per_kind is None else max_per_kind
    vec_ids, lex_ids, meta = pool(s, qv, query, kinds=kinds, candidates=candidates, mode=mode)
    if not meta:
        return []
    scored, vec_rank, lex_rank = fuse(vec_ids, lex_ids, {cid: m[3] for cid, m in meta.items()}, mode=mode,
                                      rrf_k=rrf_k, candidates=candidates)
    chosen = diversify(scored, lambda cid: meta[cid][0], lambda cid: meta[cid][1], k, max_per_kind)
    if not chosen:
        return []
    rows = {ch.id: (ch, doc) for ch, doc in s.execute(
        select(RagChunk, RagDocument).outerjoin(RagDocument, RagDocument.id == RagChunk.document_id)
        .where(RagChunk.id.in_([cid for _, _, cid in chosen]))).all()}
    hits: list[Hit] = []
    for fused, d, cid in chosen:
        ch, doc = rows[cid]
        kind = meta[cid][1]
        # A stored chunk snapshot (even an empty/partial one) is authoritative.
        # Never fill its missing historical values from a mutable document row.
        prov = ch.provenance
        if prov is None:
            prov = {"title": doc.title, "source_uri": doc.source_uri,
                    "license": doc.license, "attribution": doc.attribution} if doc and ch.document_id else {}
        hits.append(Hit(
            chunk_id=ch.id, doc=ch.doc, source=ch.source, chunk_ix=ch.chunk_ix, content=ch.content,
            score=round(1.0 - d, 4), hybrid_score=round(fused, 6),
            vector_rank=vec_rank.get(ch.id), lexical_rank=lex_rank.get(ch.id),
            section=ch.section, char_start=ch.char_start, char_end=ch.char_end,
            line_start=ch.line_start, line_end=ch.line_end, locator=ch.locator, kind=kind,
            title=prov.get("title"), source_uri=prov.get("source_uri"),
            license=prov.get("license"), attribution=prov.get("attribution"),
            version_hash=prov.get("version_hash"), retrieved_at=prov.get("retrieved_at")))
    return hits


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


class RAG:
    """Holds the embedder; search runs in a caller-provided session."""

    def __init__(self, embedder: Embedder):
        self.embedder = embedder

    def search(self, s: Session, query: str, k: int = 3, **kw) -> list[Hit]:
        return search(s, self.embedder, query, k, **kw)


__all__ = ["Chunk", "Embedder", "FastEmbedder", "Hit", "RAG", "chunk_markdown", "diversify", "front_matter", "fuse",
           "index_runbooks", "lexical_query", "lexical_statement", "load_runbooks", "pool", "search", "content_hash",
           "field", "text"]
