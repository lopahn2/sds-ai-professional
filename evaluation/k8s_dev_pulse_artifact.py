"""Preserve the running-app archive except for the compiled fault-reset classes."""
from argparse import ArgumentParser
from copy import copy
from hashlib import sha256
import json
from os import link
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile


APP_CLASSES = "BOOT-INF/classes/io/liveprobe/target/"
RESET_TYPES = {
    "inventory": ("inventory/ChaosV1Controller",),
    "order": ("order/chaos/ChaosV1Controller", "order/chaos/ChaosFaultService"),
}


def members(archive):
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ValueError("duplicate archive entries")
    return set(names)


def changed_types(base, compiled, service):
    existing = members(base)
    available = members(compiled)
    if any(name.startswith("META-INF/") and name.upper().endswith((".SF", ".RSA", ".DSA", ".EC"))
           for name in existing):
        raise ValueError("archive has a signature")
    selected = set()
    for type_name in RESET_TYPES[service]:
        prefix = APP_CLASSES + type_name
        outer = prefix + ".class"
        old_group = {name for name in existing if name == outer or name.startswith(prefix + "$") and name.endswith(".class")}
        new_group = {name for name in available if name == outer or name.startswith(prefix + "$") and name.endswith(".class")}
        if outer not in old_group or old_group != new_group:
            raise ValueError("compiled reset class group does not match the base archive")
        selected.update(old_group)
    return {name for name in selected if base.read(name) != compiled.read(name)}


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def build(service, base_path, compiled_path, output):
    if output.exists():
        raise FileExistsError(output)
    with TemporaryDirectory(dir=output.parent, prefix=".pulse-archive-") as folder:
        staged = Path(folder) / "candidate.jar"
        with ZipFile(base_path) as base, ZipFile(compiled_path) as compiled:
            updates = changed_types(base, compiled, service)
            if not updates:
                raise ValueError("no compiled reset class differs")
            with ZipFile(staged, "x") as target:
                for entry in base.infolist():
                    source = compiled if entry.filename in updates else base
                    target.writestr(copy(entry), source.read(entry.filename))
            with ZipFile(staged) as target:
                if target.namelist() != base.namelist() or target.testzip() is not None:
                    raise ValueError("candidate archive layout or CRC changed")
                for entry in target.infolist():
                    name = entry.filename
                    source = compiled if name in updates else base
                    if target.read(name) != source.read(name) or entry.compress_type != base.getinfo(name).compress_type:
                        raise ValueError("candidate altered contents or member encoding outside the selected classes")
        link(staged, output)
    return {"service": service, "base_sha256": digest(base_path), "compiled_sha256": digest(compiled_path),
            "candidate_sha256": digest(output), "changed_classes": sorted(updates)}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True, choices=tuple(RESET_TYPES))
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--compiled", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.service, args.base, args.compiled, args.output)))


if __name__ == "__main__":
    main()
