"""Build the pinned MiniLM cache with page-aligned, lossless FP32 weights."""
from argparse import ArgumentParser
from hashlib import file_digest
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory


REVISION = 'd13954661f83248295ba75c1ed411eef3b7b936e'
SOURCE_SHA256 = 'bbd7b466f6d58e646fdc2bd5fd67b2f5e93c0b687011bd4548c420f7bd46f0c5'
TOKENIZER_FILES = ('config.json', 'special_tokens_map.json', 'tokenizer.json', 'tokenizer_config.json', 'vocab.txt')
ALIGNMENT_BYTES = 16384


def prepare(source: Path, cache: Path) -> Path:
    import onnx
    from onnx.external_data_helper import set_external_data

    with (source / 'model.onnx').open('rb') as stream:
        if file_digest(stream, 'sha256').hexdigest() != SOURCE_SHA256:
            raise ValueError('unexpected MiniLM weights')
    model = onnx.load(str(source / 'model.onnx'))
    original = model.SerializeToString(deterministic=True)
    cache.mkdir(parents=True, exist_ok=True)
    output = cache / 'minilm-fp32-external'
    with TemporaryDirectory(prefix='minilm-prepare-', dir=cache) as tmp:
        stage = Path(tmp) / output.name
        stage.mkdir()
        for filename in TOKENIZER_FILES:
            shutil.copyfile(source / filename, stage / filename)
        with (stage / 'weights.bin').open('wb') as weights:
            for tensor in model.graph.initializer:
                if not tensor.HasField('raw_data') or len(tensor.raw_data) < 1024:
                    continue
                offset = ((weights.tell() + ALIGNMENT_BYTES - 1) // ALIGNMENT_BYTES) * ALIGNMENT_BYTES
                weights.seek(offset)
                length = weights.write(tensor.raw_data)
                set_external_data(tensor, location='weights.bin', offset=offset, length=length)
                tensor.ClearField('raw_data')
        graph = stage / 'model.onnx'
        onnx.save_model(model, str(graph))
        onnx.checker.check_model(str(graph))
        restored = onnx.load(str(graph), load_external_data=True)
        for tensor in restored.graph.initializer:
            tensor.ClearField('data_location')
        if restored.SerializeToString(deterministic=True) != original:
            raise ValueError('external weights changed the original graph or tensors')
        stage.rename(output)
    return output


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('cache', type=Path)
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    if args.source is not None:
        prepare(args.source, args.cache)
        return
    from huggingface_hub import snapshot_download

    with TemporaryDirectory(prefix='minilm-source-') as tmp:
        source = snapshot_download('Qdrant/all-MiniLM-L6-v2-onnx', revision=REVISION, cache_dir=tmp, token=False,
                                   allow_patterns=['model.onnx', *TOKENIZER_FILES])
        prepare(Path(source), args.cache)


if __name__ == '__main__':
    main()
