"""A real loopback gateway with disposable credentials for ASGI/browser integration checks."""
from contextlib import contextmanager
from dataclasses import replace
import importlib.util
from pathlib import Path
import threading
from app import human_sessions as H

spec = importlib.util.spec_from_file_location('operator_gateway', Path(__file__).resolve().parents[2] / 'scripts/liveprobe-local-gateway.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


@contextmanager
def local_gateway(harness, upstream_port):
    read, write = 'disposable-r-' + 'r'*40, 'disposable-w-' + 'w'*40
    read_path, write_path = harness.path/'view-key', harness.path/'operator-key'
    for path, key in ((read_path, read), (write_path, write)):
        path.write_text(key)
        path.chmod(0o600)
    harness.app.state.settings = replace(harness.app.state.settings, local_read_key_file=str(read_path),
                                         local_operator_key_file=str(write_path))
    harness.app.state.local_read_key = H.local_read_key(str(read_path))
    upstream = f'http://127.0.0.1:{upstream_port}'
    with gateway.server(upstream, read, 0) as reservation:
        port = reservation.server_port
    with gateway.server(upstream, read, port, operator_key=write) as proxy:
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{port}'
        finally:
            proxy.shutdown()
            thread.join(5)
