"""P18 campaign: automatic code-fix change requests can be switched off by config (fault campaigns)."""
from types import SimpleNamespace

from app.changes import auto_create_for_incident
from app.config import AppConfig


class _Recorder:
    def __init__(self):
        self.calls = []

    def create_for_incident(self, iid, actor):
        self.calls.append((iid, actor))


def test_default_keeps_automatic_change_requests_on():
    assert AppConfig().change_auto_create is True


def test_disabled_flag_skips_change_request_without_touching_the_database(monkeypatch):
    import app.changes as changes

    def _no_db(*a, **k):
        raise AssertionError("database must not be touched when change_auto_create is off")

    monkeypatch.setattr(changes.db, "session", _no_db)
    rec = _Recorder()
    engine = SimpleNamespace(capture=None, changes=rec, cfg=AppConfig(change_auto_create=False))
    auto_create_for_incident(engine, 42)
    assert rec.calls == []
