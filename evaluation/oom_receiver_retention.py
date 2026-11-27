"""Package the embedding lifecycle change for the frozen isolated OOM receiver."""

from argparse import ArgumentParser
import ast
from hashlib import sha256
import json
from pathlib import Path


BASE_IMAGE_ID = "sha256:8e76baf4e81e3416a02ee297f3f0c4d6060afd6ef5124fa93a2c457544df92eb"
BASE_MAIN_SHA256 = "c6542402c132b0cd50a90f73ae51002ed94dff9b94e008a61929a6d195450e9b"
FILES = ("main.py", "rag_model.py")


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError("the pinned RAG bootstrap differs from the expected source")
    return source.replace(before, after, 1)


def retained_main(source):
    if sha256(source).hexdigest() != BASE_MAIN_SHA256:
        raise ValueError("the receiver bootstrap differs from the pinned image")
    modified = replace_once(source.decode(),
                            "    from .rag import RAG, FastEmbedder, index_runbooks\n",
                            "    from .rag import RAG, index_runbooks\n    from .rag_model import IsolatedEmbedder\n")
    modified = replace_once(modified,
                            "    try:\n        emb = FastEmbedder(settings.embedding_model, settings.embedding_cache_dir)\n",
                            "    emb = IsolatedEmbedder(settings.embedding_model, settings.embedding_cache_dir)\n    try:\n")
    modified = replace_once(modified, "        def ingest_closed() -> None:\n",
                            "        emb.release_if_idle(0)\n\n        def ingest_closed() -> None:\n")
    modified = replace_once(modified, "                if rag_ingest_stop.wait(60):\n",
                            "                emb.release_if_idle(0)\n                if rag_ingest_stop.wait(60):\n")
    modified = replace_once(modified,
                            '    except Exception as e:  # noqa: BLE001\n        log.error("RAG init failed:',
                            '    except Exception as e:  # noqa: BLE001\n        emb.close()\n        log.error("RAG init failed:')
    # Close the embedder after the frozen ingestion worker observes shutdown.
    return replace_once(modified, '                    break\n\n        threading.Thread(target=ingest_closed',
                        '                    break\n            emb.close()\n\n        threading.Thread(target=ingest_closed')


def retained_model(source):
    """Keep the frozen model constructor unchanged; reuse the current worker lifecycle."""
    function, = (node for node in ast.parse(source).body
                 if isinstance(node, ast.FunctionDef) and node.name == "open_embedding")
    lines = source.splitlines(keepends=True)
    replacement = ('def open_embedding(model_name: str, cache_dir: str | None):\n'
                   '    from fastembed import TextEmbedding\n\n'
                   '    return TextEmbedding(model_name=model_name, cache_dir=cache_dir)\n')
    return "".join(lines[:function.lineno - 1] + [replacement] + lines[function.end_lineno:])


def receipt(files):
    digests = {name: sha256(files[name].encode()).hexdigest() for name in FILES}
    digest = sha256(json.dumps(digests, sort_keys=True).encode()).hexdigest()
    return {"base_image_id": BASE_IMAGE_ID, "base_main_sha256": BASE_MAIN_SHA256, "files": digests,
            "sha256": digest}


def load_bundle(path):
    data = json.loads(Path(path).read_bytes())
    files = data["files"]
    if set(files) != set(FILES) or data["receipt"] != receipt(files):
        raise ValueError("the isolated RAG bundle does not match its provenance")
    return files, data["receipt"]["sha256"]


def build(frozen, model):
    files = {"main.py": retained_main(frozen), "rag_model.py": retained_model(model)}
    for name, content in files.items():
        compile(content, name, "exec")
    return {"files": files, "receipt": receipt(files)}


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("frozen", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "liveprobe/app/rag_model.py"
    bundle = build(args.frozen.read_bytes(), source.read_text())
    with args.output.open("x") as stream:
        json.dump(bundle, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
