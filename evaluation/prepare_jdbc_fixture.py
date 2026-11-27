"""Materialize one blinded Spring source tree in a clean external directory."""

from argparse import ArgumentParser
import json
import os
from pathlib import Path
import secrets
import shutil

from evaluation.matched_recovery_campaign import ARMS, canonical_digest


HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "jdbc-recovery"
VARIANTS = ("a7e3", "b26d", "c483", "ea51")
JAVA_PACKAGE = Path("src/main/java/io/liveprobe/trial")


def freeze_assignments(destination: Path) -> str:
    """Commit a private draw before any arm receives source or benchmark observations."""
    cases = list(VARIANTS)
    secrets.SystemRandom().shuffle(cases)
    assignments = {arm: case for arm, case in zip(ARMS, cases, strict=True)}
    with open(destination, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
        json.dump(assignments, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    return canonical_digest(assignments)


def materialize(variant: str, destination: Path) -> str:
    if variant not in VARIANTS:
        raise ValueError("unregistered fixture")
    dest = destination.resolve()
    if dest.is_relative_to(HERE.parent) or not dest.parent.is_dir():
        raise ValueError("fixture source must be in a new, external directory")
    shutil.copytree(FIXTURE / "common", dest)
    shutil.copyfile(FIXTURE / "variants" / variant / "ReservationQuery.java", dest / JAVA_PACKAGE / "ReservationQuery.java")
    return canonical_digest({str(path.relative_to(dest)): path.read_text() for path in sorted(dest.rglob("*")) if path.is_file()})


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("variant", choices=VARIANTS)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps({"fixture_digest": materialize(args.variant, args.destination)}))
