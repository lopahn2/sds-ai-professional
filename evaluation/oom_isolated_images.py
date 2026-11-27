"""Stream the frozen ARM64 OOM fixture images from the existing read-only cache."""
from argparse import ArgumentParser
from hashlib import sha256
from io import BytesIO
import json
import re
import subprocess
import sys
import tarfile


STORE = "/var/lib/docker/containerd/daemon/io.containerd.content.v1.content/blobs/sha256"
ARCH = {"os": "linux", "architecture": "arm64"}
IMAGES = {
    "docker.io/liveprobe/order:dev-pulse-reset-20261124": "b3f5e4118cf0127d61a9c7c598e2bb1273fb6841b37029afd20944ef2138e175",
    "docker.io/liveprobe/inventory:dev-pulse-reset-20261124": "28d9d201f374e453b229ffc63979a7a3126f40a6977b05e75a949666d63c26ce",
    "docker.io/library/postgres:lp-oom-source": "d4bb0a8c1b7bb2e29f976d099e7bfb9a5d8858cffe9e46b35cd302cd1f1f8168",
    "docker.io/rancher/mirrored-coredns-coredns:1.14.4": "3e98f280fd601b37411c5fb7075fd9f337833c480f1644970b727ae0af067782",
    "docker.io/rancher/mirrored-pause:3.10.2": "f548e0e8e3dc1896ca956272154dde3314e8cc4fde0a57577ee9fa1c63f5baf4",
    "docker.io/prom/prometheus:v3.15.0": "efd719c99d83b060d9daefdcf00360461adf279f45ef5391f8d111892118753e",
    "docker.io/prom/alertmanager:v0.34.1": "e9733bafb1bdef9b00e25a21f8f99dc26a22224bf16641ad754d1649f4c3357a",
    "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.20.0": "42cfe3723a5f058171c627537fb57a3ea0f26e4380fa18555a95cb1a1b4cfc5b",
}
RECEIVER_IMAGES = {
    "docker.io/liveprobe/liveprobe:dev-prd-scope-api-20261124": "8e76baf4e81e3416a02ee297f3f0c4d6060afd6ef5124fa93a2c457544df92eb",
    "docker.io/pgvector/pgvector:0.8.6-pg17-bookworm": "cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f",
}
MAX_METADATA = 64 << 10
MAX_BLOB = 300 << 20
MAX_ARCHIVE = 900 << 20
IMAGE_INDEX = "application/vnd.oci.image.index.v1+json"


def blob_command(digest: str, *command: str) -> list[str]:
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("invalid OCI blob digest")
    return ["rdctl", "shell", "sudo", *command, f"{STORE}/{digest[7:]}"]


def read_metadata(digest: str, expected_size: int | None = None) -> bytes:
    raw = subprocess.check_output(blob_command(digest, "head", "-c", str(MAX_METADATA + 1)),
                                  stdin=subprocess.DEVNULL, timeout=10)
    if (not 0 < len(raw) <= MAX_METADATA or (expected_size is not None and len(raw) != expected_size)
            or sha256(raw).hexdigest() != digest[7:]):
        raise ValueError(f"unexpected size or digest for OCI metadata {digest}")
    return raw


def inspect_layout(references):
    index = {"schemaVersion": 2, "mediaType": IMAGE_INDEX, "manifests": []}
    blobs, metadata, images = {}, {}, {}

    def load(digest, size=None):
        if digest not in metadata:
            metadata[digest] = read_metadata(digest, size)
        raw = metadata[digest]
        if size is not None and len(raw) != size:
            raise ValueError(f"conflicting OCI metadata size for {digest}")
        return json.loads(raw)

    for name, fingerprint in references.items():
        root = "sha256:" + fingerprint
        data = load(root)
        leaf, manifest = root, data
        if "manifests" in data:
            matching = [row for row in data["manifests"]
                        if all((row.get("platform") or {}).get(key) == val for key, val in ARCH.items())]
            if len(matching) != 1:
                raise ValueError(f"{name} does not have a unique Linux ARM64 manifest")
            leaf = matching[0]["digest"]
            manifest = load(leaf, matching[0]["size"])
        if manifest["schemaVersion"] != 2:
            raise ValueError(f"invalid image manifest for {name}")
        config = load(manifest["config"]["digest"], manifest["config"]["size"])
        if any(config.get(key) != val for key, val in ARCH.items()):
            raise ValueError(f"{name} does not have a Linux ARM64 configuration")
        for layer in manifest["layers"]:
            digest, size = layer["digest"], layer["size"]
            if (type(size) is not int or size <= 0 or blobs.get(digest, size) != size
                    or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)):
                raise ValueError(f"invalid layer descriptor for {name}")
            blobs[digest] = size
        index["manifests"].append({
            "digest": root, "size": len(metadata[root]), "mediaType": data["mediaType"],
            "annotations": {"org.opencontainers.image.ref.name": name, "io.containerd.image.name": name},
        })
        images[name] = {"source_digest": root, "linux_arm64_manifest": leaf,
                        "configuration_digest": manifest["config"]["digest"]}

    total = sum(blobs.values()) + sum(len(raw) for raw in metadata.values())
    return index, blobs, metadata, {"images": images, "bytes": total, "linux_layers": len(blobs)}


def layout(references=None):
    index, blobs, metadata, report = inspect_layout(IMAGES if references is None else references)
    if max(blobs.values(), default=0) > MAX_BLOB:
        raise ValueError("OCI layer exceeds the fixture budget")
    if report["bytes"] > MAX_ARCHIVE:
        raise ValueError("deduplicated OCI content exceeds the fixture budget")
    return index, blobs, metadata, report


def qualification_budget():
    _, _, _, fixture = inspect_layout(IMAGES)
    _, layers, _, combined = inspect_layout(IMAGES | RECEIVER_IMAGES)
    largest_layer = max(layers.values(), default=0)
    return {
        "fixture": fixture,
        "fixture_and_incident_receiver": combined,
        "additional_bytes": combined["bytes"] - fixture["bytes"],
        "largest_layer_bytes": largest_layer,
        "archive_limit_bytes": MAX_ARCHIVE,
        "layer_limit_bytes": MAX_BLOB,
        "within_stream_limits": combined["bytes"] <= MAX_ARCHIVE and largest_layer <= MAX_BLOB,
        "layer_contents_read": False,
    }


class HashedReader:
    def __init__(self, stream):
        self.stream, self.hash = stream, sha256()

    def read(self, count):
        piece = self.stream.read(count)
        self.hash.update(piece)
        return piece


def stream_layout(out, index, blobs, metadata):
    with tarfile.open(fileobj=out, mode="w|", format=tarfile.USTAR_FORMAT) as archive:
        def tar_entry(name, size):
            entry = tarfile.TarInfo(name)
            entry.size = size
            entry.mode = 0o600
            return entry

        def write(name, raw):
            archive.addfile(tar_entry(name, len(raw)), BytesIO(raw))

        write("oci-layout", b'{"imageLayoutVersion":"1.0.0"}\n')
        write("index.json", json.dumps(index, sort_keys=True).encode())
        for digest, raw in sorted(metadata.items()):
            write(f"blobs/sha256/{digest[7:]}", raw)
        for digest, size in sorted(blobs.items()):
            with subprocess.Popen(blob_command(digest, "cat"), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE) as source:
                reader = HashedReader(source.stdout)
                try:
                    archive.addfile(tar_entry(f"blobs/sha256/{digest[7:]}", size), reader)
                    if source.stdout.read(1) or source.wait(timeout=10) or reader.hash.hexdigest() != digest[7:]:
                        raise ValueError(f"wrong size or digest while streaming layer {digest}")
                except BaseException:
                    source.kill()
                    raise


def main():
    parser = ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--describe", action="store_true", help="report the planned image digests without reading layers")
    mode.add_argument("--describe-qualification", action="store_true",
                      help="measure fixture plus incident receiver images without reading layers")
    parser.add_argument("--image", choices=IMAGES, help="emit one frozen fixture image for sequential import")
    args = parser.parse_args()
    if args.describe_qualification:
        if args.image:
            parser.error("--describe-qualification covers the full campaign")
        print(json.dumps(qualification_budget()), file=sys.stderr)
        return
    selection = {args.image: IMAGES[args.image]} if args.image else None
    index, blobs, metadata, report = layout(selection)
    if not args.describe:
        stream_layout(sys.stdout.buffer, index, blobs, metadata)
        report["streamed_and_verified"] = True
    print(json.dumps(report), file=sys.stderr)


if __name__ == "__main__":
    main()
