"""Pure multilingual RAG contracts; no conftest, embedder download or database."""
import unicodedata

from app import rag
from sqlalchemy.dialects import postgresql
from types import SimpleNamespace


def stored_hit(provenance=None, document=True):
    ch = SimpleNamespace(id=7, doc="결제.md", source="internal-runbook", chunk_ix=2,
                         content="10:00 결제 실패\n10:01 결제 정상: DB 장애 아님", provenance=provenance,
                         section="반대 근거", char_start=100, char_end=150,
                         line_start=8, line_end=9, locator="repo://old/결제.md#L8-L9",
                         document_id=1 if document else None)
    doc = SimpleNamespace(kind="runbook", title="수정된 제목", source_uri="repo://new/결제.md",
                          version_hash="f" * 64, retrieved_at=None, license="internal", attribution="새 값") if document else None

    class StoredRows:
        """Only replace SQL transport; production ranking and row conversion run."""
        def __init__(self):
            self.results = iter([[SimpleNamespace(id=7)], [SimpleNamespace(id=7)], [(ch, doc, 0.2)]])
            self.statements = []

        def execute(self, statement):
            self.statements.append(statement)
            rows = next(self.results)
            return SimpleNamespace(all=lambda: rows)

    class StoredVector:
        def embed(self, texts):
            return [[0.0] * 384]

    session = StoredRows()
    hit = rag.search(session, StoredVector(), "결제 오류", k=1)[0]
    assert len(session.statements) == 3
    return hit


def test_search_hit_preserves_immutable_source_version_and_location():
    snapshot = {"source_uri": "repo://old/결제.md", "title": "과거 제목", "license": "internal",
                "attribution": "과거 값", "kind": "runbook", "version_hash": "a" * 64,
                "retrieved_at": "2026-10-01T00:00:00+00:00"}
    hit = stored_hit(snapshot)
    assert getattr(hit, "version_hash", None) == snapshot["version_hash"]
    assert getattr(hit, "retrieved_at", None) == snapshot["retrieved_at"]
    assert (hit.source, hit.source_uri, hit.title, hit.attribution) == (
        "internal-runbook", snapshot["source_uri"], "과거 제목", "과거 값")
    assert (hit.locator, hit.section, hit.char_start, hit.char_end, hit.line_start, hit.line_end) == (
        "repo://old/결제.md#L8-L9", "반대 근거", 100, 150, 8, 9)
    assert hit.content.endswith("DB 장애 아님")
    provenance = hit.provenance()
    assert provenance["source"] == hit.source
    assert provenance["version_hash"] == snapshot["version_hash"]
    assert provenance["source_uri"] == snapshot["source_uri"]
    assert provenance["locator"] == hit.locator


def test_snapshot_missing_fields_do_not_acquire_mutable_document_provenance():
    hit = stored_hit({"title": "과거 제목", "source_uri": None, "license": None, "attribution": None})
    assert hit.source_uri is None
    assert hit.license is None
    assert hit.attribution is None
    assert getattr(hit, "version_hash", None) is None
    legacy = stored_hit(None, document=False)
    assert legacy.source_uri is None
    assert getattr(legacy, "version_hash", None) is None


def test_chunk_bound_keeps_original_timeline_and_late_counter_evidence():
    raw = ("---\ntitle: 사건\nsource: internal-runbook\n---\n\n"
           "## 관측\n2026-11-02T10:00:00Z 결제 실패 " + "반복 관측 " * 80 +
           "\n2026-11-02T10:01:00Z 결제 정상, DB 장애 아님\n")
    chunks = rag.chunk_markdown("사건.md", raw, size=96)
    assert all(len(c.content) <= 96 for c in chunks), "one long paragraph bypasses the character limit"
    assert [c.chunk_ix for c in chunks] == list(range(len(chunks)))
    assert [c.char_start for c in chunks] == sorted(c.char_start for c in chunks)
    joined = "".join(raw[c.char_start:c.char_end] for c in chunks)
    assert joined.index("10:00:00Z") < joined.index("10:01:00Z")
    assert joined.endswith("결제 정상, DB 장애 아님")
    assert "".join(joined.split()) == "".join(rag.front_matter(raw)[1].split())
    for c in chunks:
        assert c.content == f"[사건]\n{raw[c.char_start:c.char_end]}"
        assert c.line_start == raw.count("\n", 0, c.char_start) + 1
        assert c.line_end == raw.count("\n", 0, c.char_end) + 1
        assert c.section == "관측"


def test_ingest_provenance_and_locator_match_original_korean_text():
    from app.rag_ingest import Document, _locator, _provenance
    raw = "# 결제\n\n## 증거\n10:00 실패\n10:01 회복: DB 장애 아님\n"
    document = Document(key="결제.md", title="결제", kind="runbook", source="internal-runbook",
                        source_uri="repo://runbooks/결제.md", license="internal", text=raw)
    snapshot = _provenance(document)
    assert snapshot["version_hash"] == document.version_hash
    assert snapshot["source"] == document.source
    assert snapshot["source_uri"] == document.source_uri
    for chunk in rag.chunk_markdown(document.key, raw, size=48):
        assert _locator(document, chunk) == f"repo://runbooks/결제.md#L{chunk.line_start}-L{chunk.line_end}"
        assert chunk.content.endswith(raw[chunk.char_start:chunk.char_end])


def test_zero_hit_limit_does_not_embed_or_query():
    calls = []

    class LocalVector:
        def embed(self, texts):
            calls.append("embed")
            return [[0.0] * 384]

    class NoRows:
        def execute(self, statement):
            calls.append("sql")
            return SimpleNamespace(all=lambda: [])

    assert rag.search(NoRows(), LocalVector(), "결제 오류", k=0) == []
    assert calls == [], "a zero result limit must not trigger embedding or SQL"


def test_rrf_tie_order_and_hit_cap_do_not_truncate_counter_evidence():
    def run(reverse_rows=False):
        chunks = []
        for ident, name, content, distance in (
            (1, "support.md", "10:00 DB 장애 추정", 0.3),
            (2, "counter.md", "10:00 실패\n10:01 결제 정상, DB 장애 아님", 0.1),
            (3, "other.md", "기타 근거", 0.0),
        ):
            chunks.append((SimpleNamespace(id=ident, doc=name, source="internal-runbook", chunk_ix=0,
                content=content, provenance={}, section="관측", char_start=0, char_end=len(content),
                line_start=1, line_end=2, locator=f"repo://{name}#L1-L2", document_id=None), None, distance))
        results = iter([[SimpleNamespace(id=i) for i in (1, 2, 3)],
                        [SimpleNamespace(id=i) for i in (2, 1, 3)], list(reversed(chunks)) if reverse_rows else chunks])

        class Rows:
            def execute(self, statement):
                return SimpleNamespace(all=lambda: next(results))

        class Vector:
            def embed(self, texts):
                return [[0.0] * 384]

        return rag.search(Rows(), Vector(), "결제 장애", k=2)

    hits = run()
    assert [h.chunk_id for h in hits] == [2, 1]
    assert [h.chunk_id for h in run(True)] == [2, 1]
    assert len(hits) == 2
    assert hits[0].content == "10:00 실패\n10:01 결제 정상, DB 장애 아님"
    assert hits[0].hybrid_score == hits[1].hybrid_score


def test_lexical_statement_binds_korean_terms_and_filters_without_database():
    assert hasattr(rag, "lexical_statement"), "DB-free lexical SQL builder is missing"
    query = "결제 오류'; DROP TABLE rag_chunks; -- 503"
    statement = rag.lexical_statement(query, kinds=["runbook", "public_doc"], candidates=7)
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "to_tsquery" in sql and "@@" in sql
    assert "retired_at IS NULL" in sql
    assert "ORDER BY" in sql and "rag_chunks.id" in sql
    assert "결제" not in sql and "DROP TABLE" not in sql
    assert compiled.params["lq"] == rag.lexical_query(query)
    assert ["runbook", "public_doc"] in compiled.params.values()
    assert 7 in compiled.params.values()
    assert rag.lexical_statement("그리고 the !!!") is None
    # The runtime search uses this same pure builder, not a second SQL path.
    import inspect
    assert "lexical_statement(" in inspect.getsource(rag.search)


def test_lexical_query_keeps_hangul_english_digits_and_negation():
    query = "결제 오류 그리고 THE OOMKilled 결제 503 0 DB 연결 없음 NOT no; & ! ' :* <->"
    assert rag.lexical_query(query).split(" | ") == [
        "결제", "오류", "oomkilled", "503", "0", "db", "연결", "없음", "not", "no"]
    assert rag.lexical_query(unicodedata.normalize("NFD", "결제 오류")) == "결제 | 오류"
    assert rag.lexical_query("그리고 및 또는 the and or !!! 😀") == ""
    tokens = rag.lexical_query(" ".join(f"검색{i}" for i in range(40))).split(" | ")
    assert tokens == [f"검색{i}" for i in range(32)]
