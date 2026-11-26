"""Source and configuration evidence: the code and settings behind the running workload, its callers and dependencies.

A source store maps image repositories to source roots (`/etc/liveprobe/sources/registry.json`). Everything here
is read-only and bounded. Secret-looking keys and values are redacted; nothing is executed or built.

What it looks for is generic:
- JDBC connections acquired by hand (not try-with-resources) and not closed in a `finally`, with the exception
  paths that skip the close, plus the call sites of the leaking class (guards, swallowed exceptions);
- the effective configuration: properties files and non-secret env of the Deployment;
- retry settings without backoff;
- access-control lines in the scripts and configuration a dependency's Pods mount (who may connect, as which role).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REGISTRY = Path("/etc/liveprobe-sources/registry.json")  # mounted next to /etc/liveprobe, not inside it
SECRET = re.compile(r"(?i)(pass(word)?|secret|token|credential|api[_-]?key|private)")
MAX_FILES = 400
MAX_EXCERPT = 25
ACCESS = re.compile(r"(?i)(pg_hba|hba\.conf|\bhost\s+\w+\s+\w+|grant |revoke |allow|deny|only the \w+ role|role may connect|listen_addresses)")


def load_registry(path: Path = REGISTRY) -> list[dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [s for s in data.get("sources") or [] if isinstance(s, dict) and s.get("image") and s.get("root")]


def source_for_image(image: str, registry: list[dict]) -> dict | None:
    repo, _, tag = (image or "").rpartition(":") if "@" not in (image or "") else ((image or "").split("@")[0], "", "")
    repo = repo or image
    best = None
    for s in registry:
        if repo.startswith(s["image"]) and (best is None or len(s["image"]) > len(best["image"])):
            best = s
    if not best:
        return None
    return {**best, "image": image, "tag": tag}


def _redact_line(line: str) -> str:
    key = line.split("=", 1)[0] if "=" in line else line
    return f"{key}=<redacted>" if SECRET.search(key) else line


def _java_files(root: Path) -> list[Path]:
    return sorted(root.glob("src/main/java/**/*.java"))[:MAX_FILES]


def pom_version(root: Path) -> str | None:
    try:
        text = (root / "pom.xml").read_text()
    except OSError:
        return None
    text = re.sub(r"<parent>.*?</parent>", "", text, flags=re.S)
    m = re.search(r"<version>([^<]+)</version>", text)
    return m.group(1).strip() if m else None


# ------------------------------------------------------------------ Java parsing (lightweight, brace matched)
METHOD = re.compile(r"^[ \t]*(?:(?:public|protected|private|static|final|synchronized)\s+)*[\w<>\[\], ?]+\s+(\w+)\s*"
                    r"\([^;{)]*\)\s*(?:throws\s+[\w., ]+)?\s*\{", re.M)


def _block(text: str, open_at: int) -> int:
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(text) - 1


def methods(text: str) -> list[dict]:
    out = []
    for m in METHOD.finditer(text):
        if m.group(1) in ("if", "for", "while", "switch", "catch", "synchronized", "try"):
            continue
        brace = text.index("{", m.start(), m.end())
        end = _block(text, brace)
        out.append({"name": m.group(1), "start": m.start(), "end": end, "body": text[brace:end + 1]})
    return out


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def connection_leaks(root: Path) -> list[dict]:
    """Connections obtained by hand whose close is not guaranteed on every exit."""
    out = []
    for path in _java_files(root):
        text = path.read_text(errors="replace")
        for meth in methods(text):
            body = meth["body"]
            for acq in re.finditer(r"(?:Connection\s+)?(\w+)\s*=\s*[\w.]+\.getConnection\(\s*\)", body):
                head = body[max(0, acq.start() - 20):acq.start()]
                if re.search(r"try\s*\(\s*(?:\w+\s+)?$", head) or re.search(r"try\s*\([^)]*$", body[:acq.start()][-160:]):
                    continue  # try-with-resources closes it
                var = acq.group(1)
                rest = body[acq.end():]
                if re.search(r"finally\s*\{[^}]*\b" + re.escape(var) + r"\.close\(\)", rest, re.S):
                    continue
                closes = [_line_of(text, meth["start"]) + body[:acq.end()].count("\n") + rest[:c.start()].count("\n")
                          for c in re.finditer(r"\b" + re.escape(var) + r"\.close\(\)", rest)]
                caught = sorted({t.strip() for m in re.finditer(r"catch\s*\(([\w.| ]+)\s+\w+\)", rest)
                                 for t in m.group(1).split("|")})
                thrown = sorted({m.group(1) for m in re.finditer(r"throw\s+new\s+(\w+)", rest)} - set(caught))
                start_line = _line_of(text, meth["start"])
                lines = text[meth["start"]:meth["end"] + 1].splitlines()
                out.append({
                    "file": str(path.relative_to(root)), "method": meth["name"], "line": start_line,
                    "acquired": var, "closed_at_lines": closes, "caught": caught,
                    "exits_without_close": [f"throw new {t}" for t in thrown] or
                                           ["any exception not caught by " + (", ".join(caught) or "a catch")],
                    "excerpt": [f"{start_line + i}: {l}" for i, l in enumerate(lines[:MAX_EXCERPT])],
                })
    return out


def call_sites(root: Path, leak: dict) -> list[dict]:
    """Where the leaking class is used: guards and handlers around the call (≤ 2 sites)."""
    leak_file = root / leak["file"]
    text = leak_file.read_text(errors="replace")
    cls = leak_file.stem
    public = [m["name"] for m in methods(text) if m["name"] != leak["method"]]
    callers_in_class = [m["name"] for m in methods(text) if re.search(r"\b" + leak["method"] + r"\(", m["body"])
                        and m["name"] != leak["method"]]
    names = set(callers_in_class or public)
    out = []
    for path in _java_files(root):
        if path == leak_file:
            continue
        src = path.read_text(errors="replace")
        if cls not in src:
            continue
        lines = src.splitlines()
        for i, line in enumerate(lines):
            if any(re.search(r"\.\s*" + n + r"\s*\(", line) for n in names):
                lo, hi = max(0, i - 4), min(len(lines), i + 5)
                out.append({"file": str(path.relative_to(root)), "line": i + 1,
                            "excerpt": [f"{j + 1}: {lines[j]}" for j in range(lo, hi)]})
                break
        if len(out) >= 2:
            break
    return out


def active_profiles(env: dict[str, str]) -> list[str]:
    text = " ".join([env.get("args", ""), env.get("SPRING_PROFILES_ACTIVE", "")])
    m = re.search(r"spring\.profiles\.active=([\w,-]+)", text)
    found = (m.group(1) if m else env.get("SPRING_PROFILES_ACTIVE", "")).split(",")
    return [f for f in (x.strip() for x in found) if f]


def properties(root: Path, profiles: list[str] | None = None) -> dict[str, list[str]]:
    """application.properties plus the active profiles' files (all profiles when none is known)."""
    out = {}
    for path in sorted(root.glob("src/main/resources/application*.properties")):
        profile = path.stem.partition("-")[2]
        if profiles and profile and profile not in profiles:
            continue
        rows = [l.strip() for l in path.read_text(errors="replace").splitlines() if l.strip() and not l.startswith("#")]
        out[path.name] = [_redact_line(l) for l in rows][:60]
    return out


def deployment_env(template: dict) -> dict[str, str]:
    out = {}
    for c in (template.get("spec") or {}).get("containers") or []:
        for e in c.get("env") or []:
            name = e.get("name") or ""
            if "value" in e:
                out[name] = "<redacted>" if SECRET.search(name) else str(e["value"])[:200]
            elif e.get("valueFrom"):
                out[name] = "<from reference>"
        for a in c.get("args") or []:
            if isinstance(a, str) and a.startswith("--") and not SECRET.search(a):
                out.setdefault("args", "")
                out["args"] = (out["args"] + " " + a)[:300].strip()
    return out


def retry_settings(props: dict[str, list[str]], env: dict[str, str]) -> list[dict]:
    """Retries configured without backoff (properties overridden by env, Spring relaxed names)."""
    values: dict[str, str] = {}
    for rows in props.values():
        for row in rows:
            k, _, v = row.partition("=")
            values[k.strip().lower()] = v.strip()
    for k, v in env.items():
        values[k.lower().replace("_", ".")] = v
    out = []
    for key, v in values.items():
        if not re.search(r"retr(y|ies)$", key) or not v.isdigit() or int(v) <= 0:
            continue
        prefix = key.rsplit(".", 1)[0]
        backoff = next((bv for bk, bv in values.items() if bk.startswith(prefix) and "backoff" in bk), None)
        if backoff is None or (backoff.isdigit() and int(backoff) == 0):
            out.append({"setting": key, "retries": int(v), "backoff": backoff, "kind": "retry_without_backoff"})
    return out


def access_lines(text: str, limit: int = 12) -> list[str]:
    lines = text.splitlines()
    keep = set()
    for i, line in enumerate(lines):
        if ACCESS.search(line) and not SECRET.search(line.split("=", 1)[0]):
            keep.update({i - 1, i, i + 1})
            # the enclosing condition says when the line applies (e.g. only on replicas)
            for j in range(i - 1, max(-1, i - 8), -1):
                if re.match(r"\s*(if|elif|case|when|for|while)\b", lines[j]):
                    keep.add(j)
                    break
    return [f"{i + 1}: {lines[i][:240]}" for i in sorted(k for k in keep if 0 <= k < len(lines))][:limit]


def workload_source(image: str, template: dict, registry: list[dict]) -> dict:
    src = source_for_image(image, registry)
    view: dict = {"image": image, "env": deployment_env(template)}
    if not src:
        view["source"] = "no registered source for this image"
        return view
    root = Path(src["root"])
    if not root.is_dir():
        view["source"] = "registered source root is not mounted"
        return view
    version = pom_version(root)
    leaks = connection_leaks(root)
    view.update({"source_root": src.get("name") or str(root), "source_version": version,
                 "version_matches_tag": bool(version and src.get("tag", "").startswith(version)),
                 "properties": properties(root, active_profiles(view["env"])), "connection_leaks": leaks,
                 "leak_call_sites": [site for leak in leaks[:2] for site in call_sites(root, leak)]})
    view["retry_settings"] = retry_settings(view["properties"], view["env"])
    return view


def source_findings(view: dict | None) -> list[dict]:
    if not view:
        return []
    out = []
    w = view.get("workload") or {}
    for leak in w.get("connection_leaks") or []:
        out.append({"kind": "source_connection_leak", "file": leak["file"], "method": leak["method"],
                    "line": leak["line"], "exits_without_close": leak["exits_without_close"],
                    "version_matches_tag": w.get("version_matches_tag")})
    for name, c in (view.get("callers") or {}).items():
        for r in c.get("retry_settings") or []:
            out.append({"kind": "caller_retry_without_backoff", "caller": name, **{k: r[k] for k in ("setting", "retries", "backoff")}})
    for svc, d in (view.get("dependencies") or {}).items():
        for cm, lines in (d.get("access_config") or {}).items():
            if lines:
                out.append({"kind": "dependency_access_config", "service": svc, "configmap": cm, "lines": lines[:6]})
    return out


def dependency_access(kube_raw, ns: str, pod_specs: list[dict], deadline_check=lambda: None) -> dict[str, list[str]]:
    """Access-control lines in the ConfigMaps a dependency's Pods mount (scripts, server configuration)."""
    names = []
    for spec in pod_specs:
        for v in (spec.get("volumes") or []):
            name = (v.get("configMap") or {}).get("name")
            if name and name not in names:
                names.append(name)
    out = {}
    for name in names[:4]:
        deadline_check()
        try:
            cm = kube_raw.to_dict(kube_raw.core.read_namespaced_config_map(name, ns, _request_timeout=6))
        except Exception:  # noqa: BLE001 - optional read
            continue
        for key, text in (cm.get("data") or {}).items():
            lines = access_lines(str(text))
            if lines:
                out[f"{name}/{key}"] = lines
    return out


def caller_config(image: str, template: dict, registry: list[dict]) -> dict:
    """A caller's effective client settings: env, active-profile properties, retries without backoff."""
    env = deployment_env(template)
    view: dict = {"image": image, "env": env}
    src = source_for_image(image, registry)
    root = Path(src["root"]) if src else None
    if root and root.is_dir():
        view["properties"] = properties(root, active_profiles(env))
    view["retry_settings"] = retry_settings(view.get("properties") or {}, env)
    return view
