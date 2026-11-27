"""Package one sealed Spring reservation JAR using the existing local Java runtime."""

from argparse import ArgumentParser
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
from uuid import uuid4
from zipfile import ZipFile

from evaluation.jdbc_dev_manifest import IMAGE
from evaluation.matched_recovery_campaign import write_new


RUNTIME = "eclipse-temurin@sha256:49e21e16e3c86eb7816a44a67549910ed090fbeb40c29c525d58bf5e02e91b0f"
LABEL = "liveprobe.io/eval-artifact"


def docker(*args, timeout=40):
    result = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ValueError(f"local Docker operation failed ({args[0]})")
    return result.stdout.strip()


def inspect(image):
    data, = json.loads(docker("image", "inspect", image))
    return data


def package(jar: Path) -> dict:
    jar = jar.resolve(strict=True)
    if jar.suffix != ".jar" or not 100_000 < jar.stat().st_size < 48 * 1024 * 1024:
        raise ValueError("expected one bounded Spring Boot JAR")
    with ZipFile(jar) as archive:
        main = "BOOT-INF/classes/io/liveprobe/trial/ReservationApplication.class"
        workload = "BOOT-INF/classes/io/liveprobe/trial/ReservationQuery.class"
        if archive.namelist().count(main) != 1 or archive.namelist().count(workload) != 1 or archive.testzip():
            raise ValueError("the expected reservation application archive is absent")
    digest = sha256(jar.read_bytes()).hexdigest()
    base = inspect(RUNTIME)
    if RUNTIME not in base["RepoDigests"]:
        raise ValueError("the sealed local runtime image is unavailable")
    tag = f"liveprobe/eval-reservation:artifact-{digest[:20]}"
    container = f"lp-reservation-package-{uuid4().hex[:12]}"
    try:
        image = inspect(tag)
    except ValueError:
        docker("create", "--name", container, "--network", "none", "--user", "10001",
               "--workdir", "/", "--expose", "8080", "--entrypoint", "java",
               RUNTIME, "-XX:ActiveProcessorCount=2", "-jar", "/app.jar")
        try:
            docker("cp", str(jar), f"{container}:/app.jar")
            docker("commit", "--change", f"LABEL {LABEL}={digest}", container, tag, timeout=90)
            image = inspect(tag)
        finally:
            docker("rm", container)
    config = image["Config"]
    image_ids = [name for name in image.get("RepoDigests", []) if IMAGE.fullmatch(name)]
    if (len(image_ids) != 1 or config["Labels"].get(LABEL) != digest or config["Entrypoint"] != ["java"]
            or config["Cmd"] != ["-XX:ActiveProcessorCount=2", "-jar", "/app.jar"] or config["User"] != "10001"
            or image["RootFS"]["Layers"][:-1] != base["RootFS"]["Layers"]
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", image["Id"])):
        raise ValueError("the local image does not prove the expected artifact and base")
    return {"image": image_ids[0], "runtime_image_id": image["Id"], "jar_sha256": digest, "source_runtime": RUNTIME}


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("jar", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    row = package(args.jar)
    write_new(args.receipt, row)
    print(json.dumps(row))
