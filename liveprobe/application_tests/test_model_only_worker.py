"""The model-only process uses the existing durable claim. No external runner or HTTP server is started."""
from dataclasses import replace
from threading import Event

from fastapi.testclient import TestClient

from app.application import model_worker
from app.application.control import ControlPlane
from app.application.main import create_app
from app.application.worker import AnalysisWorker
from test_core_analysis import SyntheticTransport
from test_core_support import Harness, PREFIX


def test_model_only_reuses_claim_without_runner_or_api_credentials(tmp_path, monkeypatch):
    harness = Harness()
    try:
        iid = harness.seed_source()
        scoped = replace(harness.settings, identity_file=None, machine_identity_file=None,
                         worker_enabled=True, codex_home=str(tmp_path), github_read_token_file=None)
        login = tmp_path/'auth.json'
        login.touch(mode=0o600)
        control = ControlPlane(harness.app.state.store, scoped, lambda: harness.now)
        transport = SyntheticTransport(level=2)
        worker = AnalysisWorker(control, transport)
        stop = Event()
        class Once:
            def run_once(self):
                try:
                    assert worker.run_once()
                finally:
                    stop.set()
        monkeypatch.setattr(model_worker, 'cli_configured', lambda _: True)
        model_worker.run(control, stop, worker=Once(), marker=tmp_path/'ready')
        assert [call['role'] for call in transport.calls] == ['classifier','analyst','specialist','critic','verifier','patch']
        assert harness.detail(iid)['state'] == 'test_build_queued'
        assert not worker.run_once() and not (tmp_path/'ready').exists()
        # A current model pulse never impersonates the host runner's independent identity.
        assert model_worker.observed(control.store, harness.now)
        assert not model_worker.observed(control.store, harness.now + model_worker.FRESH_SECONDS)
        assert not control.runner_recent('qa')
        api = create_app(replace(scoped, worker_enabled=False, external_model_worker=True, codex_home=None),
                         clock=lambda: harness.now)
        client = TestClient(api)
        try:
            checks = client.get(PREFIX+'/readyz').json()['checks']
            assert checks['model'] and not checks['machine_identity']
        finally:
            client.close()
            api.state.store.engine.dispose()
    finally:
        harness.close()
