"""Run the bundled MiniLM text model with its serialized tokenizer settings."""
from collections.abc import Iterator
from hashlib import file_digest
from itertools import batched
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer


# These small files define the graph and preprocessing contract of the bundled
# model. Other prepared caches stay on FastEmbed's general-purpose loader.
_BUNDLED_FILES = {
    'config.json': '1b4d8e2a3988377ed8b519a31d8d31025a25f1c5f8606998e8014111438efcd7',
    'model.onnx': '70384473549f9d73e61b8e931bf6a22b47b12b96c72e55789ba5040ee787284e',
    'special_tokens_map.json': '5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a',
    'tokenizer.json': '851ca67100d372ca3ae031a6abd168f53489eebfd7d89523f35c5c9b4d372c3c',
    'tokenizer_config.json': 'abda01c8c14c5151ae498aceb30db406d6b91242c394fd88d3b6fd5a63a101e6',
}
_INFERENCE_BATCH = 16


def open_bundled(directory: Path) -> 'MiniLM | None':
    for name, expected in _BUNDLED_FILES.items():
        try:
            with (directory / name).open('rb') as source:
                if file_digest(source, 'sha256').hexdigest() != expected:
                    return None
        except FileNotFoundError:
            return None
    return MiniLM(directory)


class MiniLM:
    def __init__(self, directory: Path):
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.intra_op_num_threads = options.inter_op_num_threads = 2
        # Logs vary in length. Retaining their allocation patterns costs
        # memory after large requests without helping the common single search.
        options.enable_mem_pattern = False
        self._session = ort.InferenceSession(str(directory / 'model.onnx'), sess_options=options,
                                             providers=['CPUExecutionProvider'])
        self._tokenizer = Tokenizer.from_file(str(directory / 'tokenizer.json'))
        self._release_temporary_buffers = ort.RunOptions()
        self._release_temporary_buffers.add_run_config_entry('memory.enable_memory_arena_shrinkage', 'cpu:0')

    def embed(self, texts: list[str]) -> Iterator[np.ndarray]:
        for batch in batched(texts, 256):
            # Keep batch-wide padding fixed even when inference needs several calls.
            tokens = self._tokenizer.encode_batch(list(batch))
            ids = np.asarray([token.ids for token in tokens], dtype=np.int64)
            mask = np.asarray([token.attention_mask for token in tokens], dtype=np.int64)
            run_options = self._release_temporary_buffers if len(batch) > _INFERENCE_BATCH else None
            for start in range(0, len(batch), _INFERENCE_BATCH):
                end = start + _INFERENCE_BATCH
                yield from self._embed_rows(ids[start:end], mask[start:end], run_options)

    def _embed_rows(self, ids: np.ndarray, mask: np.ndarray, run_options: ort.RunOptions | None) -> np.ndarray:
        embeddings, *_ = self._session.run(None, {
            'input_ids': ids,
            'attention_mask': mask,
            'token_type_ids': np.zeros_like(ids),
        }, run_options=run_options)
        summed = (embeddings * mask[:, :, None]).sum(axis=1)
        pooled = summed / np.maximum(mask.sum(axis=1, keepdims=True), 1e-9)
        return pooled / np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12)
