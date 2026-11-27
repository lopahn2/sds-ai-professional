from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from evaluation import k8s_dev_pulse_artifact as pulse


CONTROLLER = pulse.APP_CLASSES + "inventory/ChaosV1Controller.class"
COMPANION = pulse.APP_CLASSES + "inventory/ChaosV1Controller$1.class"
BUSINESS = pulse.APP_CLASSES + "inventory/StockController.class"
LIBRARY = "BOOT-INF/lib/spring-boot.jar"


def make_archive(path, files):
    with ZipFile(path, "x") as archive:
        for name, text in files.items():
            archive.writestr(name, text, compress_type=ZIP_STORED if name == LIBRARY else ZIP_DEFLATED)


def test_only_selected_runtime_classes_are_replaced(tmp_path):
    base, compiled, output = (tmp_path / f"{name}.jar" for name in ("base", "compiled", "candidate"))
    original = {CONTROLLER: "old", COMPANION: "old generated", BUSINESS: "original business", LIBRARY: "original lib"}
    make_archive(base, original)
    make_archive(compiled, {CONTROLLER: "new longer class", COMPANION: "new generated", BUSINESS: "unrelated",
                            LIBRARY: "different dependency"})
    result = pulse.build("inventory", base, compiled, output)
    assert set(result["changed_classes"]) == {CONTROLLER, COMPANION}
    with ZipFile(output) as jar:
        assert jar.namelist() == list(original)
        assert jar.read(CONTROLLER) == b"new longer class"
        assert jar.read(COMPANION) == b"new generated"
        assert jar.read(BUSINESS) == b"original business"
        assert jar.read(LIBRARY) == b"original lib"
        assert jar.getinfo(LIBRARY).compress_type == ZIP_STORED
    with pytest.raises(FileExistsError):
        pulse.build("inventory", base, compiled, output)


def test_missing_companion_stops_candidate_publication(tmp_path):
    base, compiled, output = (tmp_path / f"{name}.jar" for name in ("base", "compiled", "candidate"))
    make_archive(base, {CONTROLLER: "old", COMPANION: "original generated"})
    make_archive(compiled, {CONTROLLER: "new"})
    with pytest.raises(ValueError, match="class group"):
        pulse.build("inventory", base, compiled, output)
    assert not output.exists()
