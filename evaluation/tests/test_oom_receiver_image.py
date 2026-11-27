from contextlib import contextmanager
from io import BytesIO
import json
import tarfile

import pytest

from evaluation import oom_receiver_image as receiver


@pytest.fixture
def layout(monkeypatch):
    oci = receiver.oci
    metadata = {}
    layers = {'sha256:' + digit * 64: 8 for digit in ('1', '2', '3', '4', '5')}
    old, shared, current, trailing, db = layers.keys()
    config = {**oci.ARCH, 'config': {'Env': ['A=B'], 'Cmd': ['uvicorn']},
              'rootfs': {'type': 'layers', 'diff_ids': [old, shared, current, trailing]},
              'history': [{'created_by': 'COPY @openai/codex'}, {'empty_layer': True},
                          {'created_by': 'RUN build'}, {'created_by': 'COPY @openai/codex'},
                          {'created_by': 'COPY app'}]}

    def image(value, blobs):
        return receiver.descriptor(metadata, {'schemaVersion': 2, 'mediaType': 'manifest',
                                   'config': receiver.descriptor(metadata, value, 'config'),
                                   'layers': [{'digest': key, 'size': layers[key]} for key in blobs]}, 'manifest')

    source = image(config, (old, shared, current, trailing))
    pg = image(oci.ARCH, (db,))
    monkeypatch.setattr(oci, 'RECEIVER_IMAGES', {receiver.SOURCE: source['digest'][7:], 'pg': pg['digest'][7:]})
    monkeypatch.setattr(oci, 'read_metadata', lambda key, size=None: metadata[key])
    return old, shared, current, trailing, db, config, pg


@pytest.mark.parametrize('variant', ['covered', 'missing', 'hardlink'])
def test_compaction_preserves_config_and_rejects_dependencies(monkeypatch, layout, variant):
    old, shared, current, trailing, db, config, pg = layout
    scope = {receiver.LAUNCHER: ('file', 0o755, 0, 0)}
    monkeypatch.setattr(receiver, 'package_layer', lambda desc: (
        scope if desc['digest'] == old or variant != 'missing' else {}, 42,
        '0.163.0' if desc['digest'] == old else '0.170.0'))

    @contextmanager
    def intermediate(_desc):
        entry = tarfile.TarInfo('extra-entrypoint')
        entry.type = tarfile.LNKTYPE
        entry.linkname = receiver.LAUNCHER.as_posix()
        yield [entry] if variant == 'hardlink' else []

    monkeypatch.setattr(receiver, 'checked_layer', intermediate)
    if variant != 'covered':
        with pytest.raises(ValueError, match='does not cover|intermediate layer links'):
            receiver.compact_receiver()
        return

    index, layers, metadata, report = receiver.compact_receiver()
    assert list(layers) == [shared, current, trailing, db]
    assert report['removed_regular_bytes'] == 42 and report['covered_paths'] == 1
    assert index['manifests'][0]['annotations']['org.opencontainers.image.ref.name'] == receiver.COMPACT
    assert index['manifests'][1]['digest'] == pg['digest']
    compact = json.loads(metadata[report['compact_config']['digest']])
    assert compact['config'] == config['config']
    assert compact['rootfs']['diff_ids'] == [shared, current, trailing]
    assert compact['history'] == config['history'][1:]
    assert json.loads(metadata[report['compact_manifest']['digest']])['layers'] == [
        {'digest': key, 'size': layers[key]} for key in (shared, current, trailing)]
    only_index, only_layers, only_metadata, only_report = receiver.compact_receiver(receiver_only=True)
    assert only_index['manifests'] == index['manifests'][:1]
    assert only_layers == {key: layers[key] for key in (shared, current, trailing)}
    assert only_metadata == {report[key]['digest']: metadata[report[key]['digest']]
                             for key in ('compact_manifest', 'compact_config')}
    assert only_report['receiver_stream_bytes'] == sum(only_layers.values()) + sum(map(len, only_metadata.values()))


@pytest.mark.parametrize('extra', ['other-file', '.wh.bin'])
def test_copy_review_rejects_entries_outside_ordinary_package_files(monkeypatch, extra):
    @contextmanager
    def checked_layer(_descriptor):
        raw = BytesIO()
        with tarfile.open(fileobj=raw, mode='w:') as archive:
            for name in [receiver.LAUNCHER.as_posix(), receiver.PACKAGE.joinpath(extra).as_posix()
                         if extra.startswith('.wh.') else extra]:
                archive.addfile(tarfile.TarInfo(name))
        raw.seek(0)
        with tarfile.open(fileobj=raw, mode='r:') as archive:
            yield archive

    monkeypatch.setattr(receiver, 'checked_layer', checked_layer)
    with pytest.raises(ValueError, match='unsupported path or entry'):
        receiver.package_layer({})
