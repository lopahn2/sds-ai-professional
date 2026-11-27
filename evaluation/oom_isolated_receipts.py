"""Validate completed OCI imports before deploying the dedicated dev fixtures."""
from argparse import ArgumentParser
import json
from pathlib import Path
import re

from evaluation.oom_isolated_images import IMAGES
from evaluation.oom_isolated_preflight import DIGEST, RECEIVER_COMPONENTS
from evaluation.oom_receiver_image import COMPACT


RECEIVER = frozenset(component[-1] for component in RECEIVER_COMPONENTS)
WORKLOADS = IMAGES.keys() - RECEIVER
FIELDS = ("source_digest", "linux_arm64_manifest", "configuration_digest")


def read_imports(directory, approved_receiver=None):
    images = {}
    expected = WORKLOADS if approved_receiver is None else WORKLOADS | RECEIVER
    approved = {} if approved_receiver is None else approved_receiver["images"]
    if approved_receiver is not None and (
            approved_receiver.get("streamed_and_verified") is not True or approved.keys() != RECEIVER):
        raise ValueError("receiver digests lack independent stream verification")
    for path in sorted(directory.glob("*.json")):
        if not re.fullmatch(r"[1-9][0-9]*\.json", path.name):
            continue
        receipt = json.loads(path.read_text())
        if receipt.get("streamed_and_verified") is not True:
            raise ValueError("an image import did not finish its verified stream")
        if receipt.get("compact_name") == COMPACT:
            manifest = receipt["compact_manifest"]["digest"]
            batch = {COMPACT: dict(zip(FIELDS, (manifest, manifest, receipt["compact_config"]["digest"])))}
        else:
            batch = receipt["images"]
        name, = batch
        digest = batch[name]
        if name not in expected or name in images or set(digest) != set(FIELDS) or not all(
                isinstance(value, str) and DIGEST.fullmatch(value) for value in digest.values()):
            raise ValueError("unexpected, duplicate or invalid imported OCI image")
        if name in approved:
            if digest != approved[name]:
                raise ValueError(f"imported receiver differs from independently verified source: {name}")
        elif digest["source_digest"] != "sha256:" + IMAGES[name]:
            raise ValueError(f"imported fixture differs from frozen source: {name}")
        images[name] = digest
    if images.keys() != expected:
        raise ValueError("the exact approved set of isolated images was not imported")
    return {"streamed_and_verified": True, "images": images}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("imports", type=Path)
    parser.add_argument("--approved-receiver", type=Path)
    args = parser.parse_args()
    try:
        approved = json.loads(args.approved_receiver.read_text()) if args.approved_receiver else None
        proof = read_imports(args.imports, approved)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(2, f"OCI import verification failed: {error}\n")
    print(json.dumps(proof, sort_keys=True))


if __name__ == "__main__":
    main()
