"""Regenerate the common Korean glyphs from the locked Naria CSS using fonttools[woff]."""
import base64
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re

from fontTools import subset
from fontTools.ttLib import TTFont


def common(code: int) -> bool:
    if code < 0x3000 or 0x3130 <= code <= 0x318F:
        return True
    if not 0xAC00 <= code <= 0xD7A3:
        return False
    # A two-byte EUC-KR representation identifies the KS X 1001 syllables.
    try:
        return len(chr(code).encode('euc_kr')) == 2
    except UnicodeEncodeError:
        return False


def ranges(codes: set[int]) -> str:
    intervals = []
    for code in sorted(codes):
        if intervals and code == intervals[-1][1] + 1:
            intervals[-1][1] = code
        else:
            intervals.append([code, code])
    return ','.join(f'U+{lo:X}' if lo == hi else f'U+{lo:X}-{hi:X}' for lo, hi in intervals)


def prepare() -> None:
    directory = Path(__file__).resolve().parent
    package = directory.parents[1] / 'node_modules/@naria/ui'
    stylesheet = (package / 'dist/styles.css').read_text(encoding='utf-8')
    encoded_faces = re.findall(r'data:font/woff2;base64,([A-Za-z0-9+/]+=*)', stylesheet)
    if len(encoded_faces) != 2:
        raise ValueError('Expected two Wanted Sans faces in the locked Naria package')
    manifest = {'fonts': {}}
    coverage = None
    for encoded in encoded_faces:
        raw = base64.b64decode(encoded, validate=True)
        digest = sha256(raw).hexdigest()
        font = TTFont(BytesIO(raw), recalcTimestamp=False)
        original = font.getBestCmap()
        selected = {code for code in original if common(code)}
        current = (selected, original.keys() - selected)
        if coverage is not None and current != coverage:
            raise ValueError('The two font weights have different character coverage')
        coverage = current
        metrics = {code: font['hmtx'].metrics[original[code]] for code in selected}
        opts = subset.Options()
        opts.layout_features = ['*']
        opts.name_IDs = ['*']
        opts.name_legacy = True
        builder = subset.Subsetter(options=opts)
        builder.populate(unicodes=selected)
        builder.subset(font)
        filename = f'wanted-sans-{digest[:16]}-core.woff2'
        target = directory / filename
        font.save(target)
        prepared = TTFont(target)
        cmap = prepared.getBestCmap()
        if cmap.keys() != selected or any(prepared['hmtx'].metrics[glyph] != metrics[code] for code, glyph in cmap.items()):
            raise ValueError('Subset must preserve the selected glyphs and their advances')
        manifest['fonts'][digest] = filename

    selected, remaining = coverage
    manifest.update(core=ranges(selected), fallback=ranges(remaining))
    (directory / 'coverage.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    license_text = (package / 'LICENSES.md').read_text(encoding='utf-8').split('## Wanted Sans\n', 1)[1].lstrip()
    license_path = directory.parents[1] / 'public/licenses/wanted-sans.txt'
    license_path.parent.mkdir(parents=True, exist_ok=True)
    license_path.write_text(license_text, encoding='utf-8')


if __name__ == '__main__':
    prepare()
