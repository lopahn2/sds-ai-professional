"""Package checksum-pinned ARM64 CoreDNS releases for the offline KI-10 node."""
from argparse import ArgumentParser
from gzip import compress
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import tarfile


VERSIONS = ("1.5.2", "1.6.0")
ARCHIVES = {
    "1.5.2": "ffbe7b2451b9b604e26497948711e2eb0b0969e585fbdaf3cb3378cb75bfe9b0",
    "1.6.0": "25feb3b2f2698b172c0ac9d75278b9ae5f483f9779f59b96d242d3f13514c930",
}
IMAGE_REPOSITORY = "example.invalid/liveprobe-ki10/coredns"


def add_member(archive, name, data, mode=0o644):
    member = tarfile.TarInfo(name)
    member.size, member.mode = len(data), mode
    archive.addfile(member, BytesIO(data))


def image_archive(path, version, output):
    if sha256(path.read_bytes()).hexdigest() != ARCHIVES[version]:
        raise ValueError(f"CoreDNS archive checksum mismatch for {version}")
    with tarfile.open(path) as tar:
        member, = [m for m in tar if m.name in ("coredns", "./coredns") and m.isfile()]
        assert member.size < 75_000_000
        with tar.extractfile(member) as stream:
            binary = stream.read()
    assert binary[:4] == b"\x7fELF" and binary[18:20] == b"\xb7\x00"
    layer = BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as tar:
        add_member(tar, "coredns", binary, 0o555)
    content = layer.getvalue()
    layer_id = sha256(content).hexdigest()
    config = json.dumps({
        "architecture": "arm64", "os": "linux",
        "rootfs": {"type": "layers", "diff_ids": [f"sha256:{layer_id}"]},
        "config": {"Entrypoint": ["/coredns"], "User": "10001:10001"},
    }, sort_keys=True).encode()
    config_id = sha256(config).hexdigest()
    config_file, layer_file = f"{config_id}.json", f"{layer_id}/layer.tar"
    manifest = [{"Config": config_file, "RepoTags": [f"{IMAGE_REPOSITORY}:{version}"], "Layers": [layer_file]}]
    with output.open("xb") as raw:
        stream = BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as tar:
            add_member(tar, config_file, config)
            add_member(tar, layer_file, content)
            add_member(tar, "manifest.json", json.dumps(manifest).encode())
        raw.write(compress(stream.getvalue(), compresslevel=4, mtime=0))
    return {"version": version, "upstream_sha256": ARCHIVES[version], "binary_sha256": sha256(binary).hexdigest(),
            "image_config_sha256": config_id, "image_archive_sha256": sha256(output.read_bytes()).hexdigest()}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    args = parser.parse_args()
    versions = [image_archive(args.assets / f"coredns_{name}_linux_arm64.tgz", name,
                              args.assets / f"coredns-{name}.tar.gz") for name in VERSIONS]
    with (args.assets / "fixture-images.json").open("x") as out:
        json.dump(versions, out, indent=2)
    print(json.dumps(versions))


if __name__ == "__main__":
    main()
