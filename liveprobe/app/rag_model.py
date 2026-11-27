"""Run embedding inference in a process whose memory can be returned on idle."""
from multiprocessing.connection import Connection
from pathlib import Path
import socket
import subprocess
import sys
import threading
from time import monotonic


def open_embedding(model_name: str, cache_dir: str | None):
    model_path = {}
    if cache_dir and model_name.lower() == 'sentence-transformers/all-minilm-l6-v2':
        prepared = Path(cache_dir) / 'minilm-fp32-external'
        if (prepared / 'model.onnx').is_file() and (prepared / 'weights.bin').is_file():
            from .minilm import open_bundled

            model = open_bundled(prepared)
            if model is not None:
                return model
            model_path['specific_model_path'] = str(prepared)
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=model_name, cache_dir=cache_dir, threads=2, **model_path)


class IsolatedEmbedder:
    def __init__(self, model_name: str, cache_dir: str | None = None):
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._channel: Connection | None = None
        self._last_used_at: float | None = None
        self._closed = False

    def _start(self) -> None:
        local, remote = socket.socketpair()
        with remote:
            fd = remote.fileno()
            try:
                self._process = subprocess.Popen(
                    [sys.executable, '-m', 'app.rag_model', str(fd), self._model_name, self._cache_dir or ''],
                    pass_fds=(fd,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                )
            except BaseException:
                local.close()
                raise
        self._channel = Connection(local.detach())

    def _stop(self) -> None:
        if self._process is None or self._channel is None:
            return
        self._channel.close()
        try:
            self._process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        self._channel = self._process = None
        self._last_used_at = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            if self._closed:
                raise RuntimeError('embedding worker is closed')
            if self._channel is None:
                self._start()
            try:
                self._channel.send(texts)
                if not self._channel.poll(120):
                    raise RuntimeError('embedding worker timed out')
                ok, result = self._channel.recv()
                if not ok:
                    raise RuntimeError(f'embedding worker failed: {result}')
            except (EOFError, OSError, RuntimeError) as exc:
                self._stop()
                raise RuntimeError('embedding worker request failed') from exc
            self._last_used_at = monotonic()
            return result

    def release_if_idle(self, idle_seconds: float) -> None:
        with self._lock:
            if self._last_used_at is not None and monotonic() - self._last_used_at >= idle_seconds:
                self._stop()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop()


def _serve(fd: int, model_name: str, cache_dir: str | None) -> None:
    model = None
    with Connection(fd) as channel:
        while True:
            try:
                texts = channel.recv()
            except EOFError:
                return
            try:
                if model is None:
                    model = open_embedding(model_name, cache_dir)
                vectors = [value.tolist() for value in model.embed(texts)]
            except Exception as exc:
                channel.send((False, type(exc).__name__))
                return
            channel.send((True, vectors))


if __name__ == '__main__':
    _serve(int(sys.argv[1]), sys.argv[2], sys.argv[3] or None)
