"""A standalone model's source and regression tools for one blinded reservation variant."""

from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from xml.etree import ElementTree

from evaluation.prepare_jdbc_fixture import JAVA_PACKAGE, materialize
from evaluation.reservation_runtime_image import package


TEST_ROOT = Path("src/test/java/io/liveprobe/trial")
TEST_NAME = re.compile(r"Reservation[A-Za-z0-9]*Test\.java")
SOURCE_NAMES = frozenset({"ReservationApplication.java", "ReservationEndpoint.java",
                          "ReservationQuery.java", "ReservationRows.java"})
MAVEN_IMAGE = "maven:3.9-eclipse-temurin-21"


def java_path(name, *, test):
    directory = TEST_ROOT if test else JAVA_PACKAGE
    if not isinstance(name, str):
        raise ValueError("the requested file must be identified by its name or listed path")
    name = name.removeprefix(directory.as_posix() + "/")
    if test and TEST_NAME.fullmatch(name):
        return TEST_ROOT / name
    if not test and name in SOURCE_NAMES:
        return JAVA_PACKAGE / name
    raise ValueError("the requested file is outside the reservation sources and local regression tests")


def digest(path):
    return sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def test_totals(workspace):
    counts = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
    target = workspace / "target"
    reports = target / "surefire-reports"
    if target.is_symlink() or reports.is_symlink():
        raise ValueError("unexpected test output directory")
    for report in reports.glob("TEST-*.xml"):
        if report.is_symlink() or not report.is_file() or report.stat().st_size > 2_000_000:
            raise ValueError("unexpected test output")
        fields = ElementTree.parse(report).getroot().attrib
        for key in counts:
            counts[key] += int(fields.get(key, 0))
    return counts


class SourceTrial:
    def __init__(self, variant, folder, maven_cache):
        self.folder = folder
        self.cache = maven_cache.resolve(strict=True)
        self.initial = folder / "original"
        self.working = folder / "working"
        self.original_digest = materialize(variant, self.initial)
        shutil.copytree(self.initial, self.working)

    def index(self):
        files = {}
        for path in [*self.working.joinpath(JAVA_PACKAGE).glob("*.java"), *self.working.joinpath(TEST_ROOT).glob("*.java")]:
            files[path.relative_to(self.working).as_posix()] = digest(path)
        return files

    def read(self, name, *, test=False):
        target = self.working / java_path(name, test=test)
        return {"name": name, "sha256": digest(target),
                "text": target.read_text() if target.is_file() else None}

    def replace(self, name, expected, text, *, test=False):
        target = self.working / java_path(name, test=test)
        if not isinstance(text, str) or not 1 <= len(text) <= 50_000 or "\x00" in text:
            raise ValueError("replacement must be bounded Java source")
        if target.is_symlink() or digest(target) != expected:
            return {"applied": False, "reason": "source changed since the provided digest"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return {"applied": True, "sha256": digest(target)}

    def _maven(self, path, phase):
        command = [
            "docker", "run", "--rm", "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--tmpfs", "/tmp:size=96m", "--cpus=1", "--memory=512m",
            "--pids-limit=128", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
            "-e", "MAVEN_OPTS=-Xmx192m -Duser.home=/tmp",
            "--mount", f"type=bind,src={self.cache},dst=/deps,readonly",
            "--mount", f"type=bind,src={path},dst=/work", "--workdir", "/work",
            "--entrypoint", "mvn", MAVEN_IMAGE, "--offline", "--batch-mode", "--no-transfer-progress",
            "-Dmaven.repo.local=/deps", phase,
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=110)
        return {"exit_code": result.returncode, "tests": test_totals(path),
                "output": (result.stdout + result.stderr)[-4500:]}

    def verify_patch(self):
        tests = self.working / TEST_ROOT
        if not any(tests.glob("*.java")):
            raise ValueError("create a test for the failure before packaging")
        with TemporaryDirectory(prefix="reservation-compare-", dir=self.folder) as directory:
            baseline = Path(directory) / "baseline"
            candidate = Path(directory) / "candidate"
            shutil.copytree(self.initial, baseline)
            shutil.copytree(tests, baseline / TEST_ROOT)
            shutil.copytree(self.working, candidate)
            before = self._maven(baseline, "test")
            after = self._maven(candidate, "package")
            reproduced = before["exit_code"] != 0 and before["tests"]["tests"] > 0 and (
                before["tests"]["failures"] + before["tests"]["errors"] > 0)
            passed = after["exit_code"] == 0 and after["tests"]["tests"] > 0 and (
                after["tests"]["failures"] == after["tests"]["errors"] == after["tests"]["skipped"] == 0)
            receipt = {"before": before, "after": after, "reproduced": reproduced, "passed": passed}
            if reproduced and passed:
                jar = candidate / "target/reservation-1.0.0.jar"
                if jar.parent.is_symlink() or jar.is_symlink():
                    raise ValueError("unexpected build artifact path")
                receipt["artifact"] = package(jar)
            return receipt
