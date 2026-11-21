"""C3 `dispatch/v1`: the public record of what one agent call was asked to do.

Computed from the final prompt right before the provider call and stored on `agent_started.detail.dispatch`.
Only identifiers, the role's public job description and a fingerprint are kept: never the prompt body, quotes,
operator text, secrets or reasoning.

`evidence_refs` are read only from the formats the prompt builders actually write, and only for the
collected-evidence keys of `evidence.compact_summary` whose block is non-empty:

* agent roles (`analysis_context.bounded_view`, used by every group role and the SIMPLE/HIGH_RISK workflows):
  a section header line that is exactly the key, followed on the next line by its JSON body;
* the router (`router.ROUTER_PROMPT`): a key of the JSON-encoded `compact_summary` (`"E1_deployment": ...`),
  which the router prompt truncates — keys cut off by the truncation are not counted.

Mentions inside instructions ("a source id shown below (E1_deployment, ...)") or inside earlier agents' JSON
outputs (`"ref": "E2_pods"`, `"evidence_refs": ["E1"]`) are values, not block headers, and are never counted.
Operator instructions are appended after `instructions_at` and are excluded from the scan.
"""
from __future__ import annotations

import hashlib
import re

from .roles_ko import role_info

VERSION = "dispatch/v1"
MAX_REFS = 50
_EMPTY = {"null", "[]", "{}", '""'}


def _known_keys() -> tuple[str, ...]:
    from .evidence import Bundle, compact_summary
    return tuple(k for k in compact_summary(Bundle("", "")) if re.match(r"^E\d+_", k))


KNOWN_EVIDENCE_KEYS = _known_keys()
_KEY = r"E\d+_[A-Za-z0-9_]+"
_HEADER = re.compile(rf"(?m)^({_KEY})\n([^\n]*)")
# An escaped quote (\") inside a JSON string value never starts a key.
_JSON_KEY = re.compile(rf'(?<!\\)"({_KEY})":\s*(null|\[\]|\{{\}}|"")?')


def evidence_refs(text: str, *, router: bool = False) -> list[str]:
    found: list[str] = []
    if router:
        hits = [(m.group(1), m.group(2) is None) for m in _JSON_KEY.finditer(text)]
    else:
        hits = [(m.group(1), m.group(2).strip() not in _EMPTY) for m in _HEADER.finditer(text)]
    for key, has_body in hits:
        if has_body and key in KNOWN_EVIDENCE_KEYS and key not in found:
            found.append(key)
    return found[:MAX_REFS]


def rag_refs(hits: list[dict], text: str) -> list[str]:
    """Retrieved chunks whose header and body excerpt are both in the prompt (same rule as the audit's `delivered`)."""
    from .analysis_context import rag_usage
    out: list[str] = []
    for ref in rag_usage(hits or [], text, {})["delivered"]:
        if ref not in out:
            out.append(ref)
    return out[:MAX_REFS]


def build(role: str, prompt: str, hits: list[dict] | None, message_ids: list[int] | None, *,
          instructions_at: int | None = None) -> dict:
    """Public items of one call. `prompt` is the exact final prompt sent to the provider; `instructions_at` is where the
    appended operator-instruction block starts (identifiers are only read before it)."""
    scan = prompt if instructions_at is None else prompt[:max(0, instructions_at)]
    info = role_info(role)
    ids: list[int] = []
    for m in message_ids or []:
        if isinstance(m, int) and not isinstance(m, bool) and m not in ids:
            ids.append(m)
    return {"version": VERSION, "role_ko": info["role_ko"], "task_ko": info["job_ko"],
            "evidence_refs": evidence_refs(scan, router=role == "router"), "rag_refs": rag_refs(hits or [], scan),
            "operator_message_ids": ids[:MAX_REFS],
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "prompt_chars": len(prompt)}
