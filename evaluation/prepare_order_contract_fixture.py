"""Prepare the dev order image candidate whose readiness still passes but stock decoding fails."""

from argparse import ArgumentParser
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZipFile


REPO = Path(__file__).resolve().parents[1]
CONTROLLER = Path("src/main/java/io/liveprobe/target/order/OrderController.java")
ORIGINAL = "public record InventoryStock(String item, int quantity, boolean available) {}"
REPLACEMENT = "public record InventoryStock(String item, java.util.List<Integer> quantity, boolean available) {}"
DATATYPE_ENTRY = "BOOT-INF/classes/io/liveprobe/target/order/OrderController$InventoryStock.class"


def prepare(destination: Path) -> dict:
    """Create an isolated source snapshot for both images while retaining all existing order code."""
    base = destination.resolve()
    if base.is_relative_to(REPO) or not base.parent.is_dir():
        raise ValueError("fixture build must use a new external directory")
    source = REPO / "services/order"
    old = (source / CONTROLLER).read_text()
    if old.count(ORIGINAL) != 1:
        raise ValueError("order contract code changed; review the expected stock response before building")
    base.mkdir(exist_ok=False)
    healthy = base / "healthy"
    fault = base / "candidate"
    for target in (healthy, fault):
        target.mkdir()
        shutil.copyfile(source / "pom.xml", target / "pom.xml")
        shutil.copytree(source / "src/main", target / "src/main")
    changed = old.replace(ORIGINAL, REPLACEMENT, 1)
    (fault / CONTROLLER).write_text(changed)
    return {"path": str(CONTROLLER), "baseline_sha256": sha256(old.encode()).hexdigest(),
            "candidate_sha256": sha256(changed.encode()).hexdigest(),
            "request": "/api/orders/check?item=a", "readiness": "/actuator/health/readiness"}


def build_from_baseline_jar(baseline: Path, candidate: Path) -> dict:
    """Change one compiled DTO in the actual healthy release, preserving all other archive entries."""
    with TemporaryDirectory(prefix="lp-contract-dto-") as directory:
        root = Path(directory)
        source = root / "OrderController.java"
        source.write_text("package io.liveprobe.target.order;\npublic class OrderController {\n    "
                          + REPLACEMENT + "\n}\n")
        subprocess.run(["javac", "--release", "21", "-d", directory, str(source)], check=True, timeout=30)
        binary = (root / DATATYPE_ENTRY.removeprefix("BOOT-INF/classes/")).read_bytes()
        with ZipFile(baseline) as original:
            entries = original.infolist()
            names = {entry.filename for entry in entries}
            if (len(names) != len(entries) or DATATYPE_ENTRY not in names
                    or DATATYPE_ENTRY.split("$")[0] + ".class" not in names or original.testzip() is not None):
                raise ValueError("baseline is not the expected intact order release")
            before = original.read(DATATYPE_ENTRY)
            if binary == before:
                raise ValueError("replacement already belongs to the baseline")
            with ZipFile(candidate, "x") as output:
                output.comment = original.comment
                for info in entries:
                    output.writestr(info, binary if info.filename == DATATYPE_ENTRY else original.read(info))
    return {"entry": DATATYPE_ENTRY, "baseline_class_sha256": sha256(before).hexdigest(),
            "candidate_class_sha256": sha256(binary).hexdigest(), "entries": len(entries)}


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--baseline-jar", type=Path, help="write a candidate JAR from the exact healthy release")
    args = parser.parse_args()
    receipt = build_from_baseline_jar(args.baseline_jar, args.destination) if args.baseline_jar else prepare(args.destination)
    print(json.dumps(receipt))
