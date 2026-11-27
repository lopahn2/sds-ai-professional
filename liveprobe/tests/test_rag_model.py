import os
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import rag_model


def test_prepared_cache_is_selected_only_for_the_default_model(monkeypatch, tmp_path):
    import fastembed

    prepared = tmp_path / 'minilm-fp32-external'
    prepared.mkdir()
    (prepared / 'model.onnx').touch()
    opened = []
    monkeypatch.setattr(fastembed, 'TextEmbedding', lambda **kwargs: opened.append(kwargs))
    default = 'sentence-transformers/all-MiniLM-L6-v2'
    rag_model.open_embedding(default, str(tmp_path))
    (prepared / 'weights.bin').touch()
    rag_model.open_embedding(default, str(tmp_path))
    rag_model.open_embedding(default.lower(), str(tmp_path))
    rag_model.open_embedding('other-model', str(tmp_path))
    assert [opts.get('specific_model_path') for opts in opened] == [None, str(prepared), str(prepared), None]
    assert all(opts['threads'] == 2 and opts['cache_dir'] == str(tmp_path) for opts in opened)


def test_bundled_loader_requires_the_matching_graph_and_tokenizer(monkeypatch, tmp_path):
    from app import minilm

    prepared = tmp_path / 'minilm-fp32-external'
    prepared.mkdir()
    bundled = {'model.onnx': b'graph', 'tokenizer.json': b'tokenizer'}
    for name, data in bundled.items():
        (prepared / name).write_bytes(data)
    (prepared / 'weights.bin').touch()
    monkeypatch.setattr(minilm, '_BUNDLED_FILES', {name: sha256(data).hexdigest() for name, data in bundled.items()})
    loaded = object()
    monkeypatch.setattr(minilm, 'MiniLM', lambda directory: loaded if directory == prepared else None)
    default = 'sentence-transformers/all-minilm-l6-v2'
    assert rag_model.open_embedding(default, str(tmp_path)) is loaded
    (prepared / 'tokenizer.json').write_bytes(b'custom tokenizer')
    assert minilm.open_bundled(prepared) is None


def test_bundled_model_splits_inference_without_repadding_or_reordering(monkeypatch, tmp_path):
    import numpy as np
    from app import minilm

    observed = []

    def run(_outputs, inputs, *, run_options):
        ids = inputs['input_ids']
        observed.append((ids.shape[0], ids.shape[1], None if run_options is None else
                         run_options.get_run_config_entry('memory.enable_memory_arena_shrinkage')))
        return [np.stack((ids, np.ones_like(ids)), axis=-1).astype(np.float32)]

    def encode(texts):
        width = max(map(int, texts))
        return [SimpleNamespace(ids=[int(text)] * width, attention_mask=[1] * width) for text in texts]

    monkeypatch.setattr(minilm.ort, 'InferenceSession', lambda *_args, **_kwargs: SimpleNamespace(run=run))
    monkeypatch.setattr(minilm, 'Tokenizer', SimpleNamespace(from_file=lambda _path: SimpleNamespace(encode_batch=encode)))
    model = minilm.MiniLM(tmp_path)
    assert list(model.embed([])) == []
    assert len(list(model.embed(['1']))) == 1
    rows = np.asarray(list(model.embed([str(i) for i in range(1, 34)])))
    assert np.allclose(rows[:, 0] / rows[:, 1], range(1, 34))
    assert observed == [(1, 1, None), (16, 33, 'cpu:0'), (16, 33, 'cpu:0'), (1, 33, 'cpu:0')]


@pytest.fixture
def isolated_embedder(monkeypatch, tmp_path):
    (tmp_path / 'fastembed.py').write_text('''
import os

class Vector:
    def __init__(self, text):
        self.text = text

    def tolist(self):
        return [float(len(self.text))]

class TextEmbedding:
    def __init__(self, *, model_name, cache_dir, threads):
        assert model_name == 'fixture-model' and threads == 2

    def embed(self, texts):
        if 'crash' in texts:
            os._exit(2)
        if 'failure' in texts:
            raise ValueError('fixture embedding failure')
        return [Vector(text) for text in texts]
''')
    package = str(Path(__file__).resolve().parent.parent)
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join([str(tmp_path), package]))
    worker = rag_model.IsolatedEmbedder('fixture-model')
    try:
        yield worker
    finally:
        worker.close()


def test_child_is_reaped_when_idle_and_reopened_on_new_input(isolated_embedder, monkeypatch):
    now = 0.0
    monkeypatch.setattr(rag_model, 'monotonic', lambda: now)
    embedder = isolated_embedder
    assert embedder.embed([]) == [] and embedder._process is None
    assert embedder.embed(['first', 'second']) == [[5.0], [6.0]]
    worker = embedder._process
    now = 299.0
    embedder.release_if_idle(300)
    assert embedder._process is worker and worker.poll() is None
    now = 300.0
    embedder.release_if_idle(300)
    assert worker.poll() == 0 and embedder._process is None
    assert embedder.embed(['third']) == [[5.0]]
    fresh = embedder._process
    assert fresh is not worker and fresh.poll() is None
    embedder.close()
    assert fresh.poll() == 0
    with pytest.raises(RuntimeError, match='closed'):
        embedder.embed(['fourth'])


@pytest.mark.parametrize('error', ['crash', 'failure'])
def test_broken_worker_is_reaped_and_next_search_can_retry(isolated_embedder, error):
    embedder = isolated_embedder
    assert embedder.embed(['first']) == [[5.0]]
    worker = embedder._process
    with pytest.raises(RuntimeError, match='request failed'):
        embedder.embed([error])
    assert worker.poll() is not None and embedder._process is None
    assert embedder.embed(['retry']) == [[5.0]]
