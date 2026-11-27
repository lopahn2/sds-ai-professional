"""Stream a compact, frozen ARM64 receiver image for the isolated OOM campaign."""
from argparse import ArgumentParser
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import PurePosixPath
import subprocess
import sys
import tarfile

from evaluation import oom_isolated_images as oci


SOURCE = 'docker.io/liveprobe/liveprobe:dev-prd-scope-api-20261124'
COMPACT = 'docker.io/liveprobe/liveprobe:oom-dev-receiver-compact'
DATABASE = 'docker.io/pgvector/pgvector:0.8.6-pg17-bookworm'
PACKAGE = PurePosixPath('usr/local/lib/node_modules/@openai/codex')
LAUNCHER = PACKAGE / 'bin/codex.js'
VERSIONS = ('0.163.0', '0.170.0')


@contextmanager
def checked_layer(descriptor):
    digest = descriptor['digest']
    with subprocess.Popen(oci.blob_command(digest, 'cat'), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE) as process:
        source = oci.HashedReader(process.stdout)
        try:
            with tarfile.open(fileobj=source, mode='r|gz') as archive:
                yield archive
            while source.read(1 << 20):
                pass
            if process.wait(timeout=10) or source.hash.hexdigest() != digest[7:]:
                raise ValueError('layer digest mismatch')
        except BaseException:
            process.kill()
            raise


def package_layer(descriptor):
    """Read a complete COPY layer. Reject metadata with effects outside the covered paths."""
    paths, regular_bytes, package_version = {}, 0, None
    with checked_layer(descriptor) as archive:
        for entry in archive:
            path = PurePosixPath(entry.name)
            package = path.is_relative_to(PACKAGE)
            ordinary = entry.isdir() or entry.type in (tarfile.REGTYPE, tarfile.AREGTYPE)
            if (path.is_absolute() or '..' in path.parts or path in paths or
                    any(part.startswith('.wh.') for part in path.parts) or
                    set(entry.pax_headers) - {'path', 'mtime', 'atime', 'ctime'} or
                    not (entry.isdir() and path in PACKAGE.parents or package and ordinary)):
                raise ValueError('Codex COPY layer contains an unsupported path or entry')
            paths[path] = (entry.type, entry.mode, entry.uid, entry.gid)
            if entry.isfile():
                regular_bytes += entry.size
            if path == PACKAGE / 'package.json':
                if not entry.isfile() or entry.size > oci.MAX_METADATA:
                    raise ValueError('invalid Codex package metadata')
                package_version = json.load(archive.extractfile(entry))['version']
    if LAUNCHER not in paths:
        raise ValueError('Codex launcher absent from COPY layer')
    return paths, regular_bytes, package_version


def descriptor(metadata, value, media_type):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    digest = 'sha256:' + sha256(raw).hexdigest()
    metadata[digest] = raw
    return {'mediaType': media_type, 'digest': digest, 'size': len(raw)}


def compact_receiver(*, receiver_only=False):
    index, layers, metadata, original = oci.inspect_layout(oci.RECEIVER_IMAGES)
    ids = original['images'][SOURCE]
    manifest = json.loads(metadata[ids['linux_arm64_manifest']])
    config = json.loads(metadata[ids['configuration_digest']])
    history = [i for i, row in enumerate(config['history']) if not row.get('empty_layer')]
    api_layers = manifest['layers']
    diff_ids = config['rootfs']['diff_ids']
    if len(history) != len(api_layers) or len(diff_ids) != len(api_layers):
        raise ValueError('image history does not match the root layers')
    copies = [i for i, position in enumerate(history)
              if 'COPY' in config['history'][position].get('created_by', '')
              and '@openai/codex' in config['history'][position]['created_by']]
    if len(copies) != 2:
        raise ValueError('expected both Codex COPY layers')

    earlier, active = (api_layers[i] for i in copies)
    before, removed_bytes, before_version = package_layer(earlier)
    after, _, after_version = package_layer(active)
    if (before_version, after_version) != VERSIONS or not before or not before.items() <= after.items():
        raise ValueError('the active Codex installation does not cover the complete original layer')
    for layer in api_layers[copies[0] + 1:copies[1]]:
        with checked_layer(layer) as archive:
            if any(entry.islnk() and PurePosixPath(entry.linkname.lstrip('/')) in before for entry in archive):
                raise ValueError('an intermediate layer links to a file in the original Codex COPY')

    pos = copies[0]
    omitted = earlier['digest']
    db_layers = {layer['digest'] for name, db in original['images'].items() if name != SOURCE
                 for layer in json.loads(metadata[db['linux_arm64_manifest']])['layers']}
    if omitted in db_layers or sum(layer['digest'] == omitted for layer in api_layers) != 1:
        raise ValueError('the original Codex COPY is used in another root layer position')
    del api_layers[pos], diff_ids[pos], config['history'][history[pos]], layers[omitted]
    new_config = descriptor(metadata, config, manifest['config']['mediaType'])
    manifest['config'] = new_config
    new_image = descriptor(metadata, manifest, manifest['mediaType'])
    for item in index['manifests']:
        if item['annotations']['org.opencontainers.image.ref.name'] == SOURCE:
            item.update(**new_image, platform=oci.ARCH,
                        annotations={'org.opencontainers.image.ref.name': COMPACT, 'io.containerd.image.name': COMPACT})
    for obsolete in set(ids.values()):
        if any(obsolete in row.values() for name, row in original['images'].items() if name != SOURCE):
            raise ValueError('obsolete receiver metadata is used by another image')
        del metadata[obsolete]

    if receiver_only:
        index['manifests'] = [item for item in index['manifests']
                              if item['annotations']['org.opencontainers.image.ref.name'] == COMPACT]
        layers = {layer['digest']: layer['size'] for layer in api_layers}
        metadata = {digest: metadata[digest] for digest in (new_image['digest'], new_config['digest'])}

    payload = sum(layers.values()) + sum(map(len, metadata.values()))
    if max(layers.values()) > oci.MAX_BLOB or payload > oci.MAX_ARCHIVE:
        raise ValueError('compact receiver exceeds the separate stream budget')
    report = {'source': ids, 'compact_name': COMPACT, 'compact_manifest': new_image, 'compact_config': new_config,
              'removed_layer': earlier, 'retained_codex_layer': active, 'covered_paths': len(before),
              'removed_regular_bytes': removed_bytes, 'before_version': before_version,
              'after_version': after_version, 'receiver_stream_bytes': payload, 'linux_layers': len(layers)}
    return index, layers, metadata, report


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--stream', action='store_true', help='stream the separately bounded receiver OCI archive to stdout')
    parser.add_argument('--image', choices=(COMPACT, DATABASE), help='emit only one receiver component for sequential import')
    args = parser.parse_args()
    if args.image == DATABASE:
        index, layers, metadata, report = oci.layout({DATABASE: oci.RECEIVER_IMAGES[DATABASE]})
    else:
        index, layers, metadata, report = compact_receiver(receiver_only=args.image == COMPACT)
    if args.stream:
        oci.stream_layout(sys.stdout.buffer, index, layers, metadata)
        report['streamed_and_verified'] = True
    print(json.dumps(report), file=sys.stderr if args.stream else sys.stdout)


if __name__ == '__main__':
    main()
