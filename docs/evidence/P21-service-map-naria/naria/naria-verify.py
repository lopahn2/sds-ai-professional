"""Strict, read-only naria conformance check for the dashboard CSS.

Every literal (non-var) value of a design property is classified:
  token  - naria token scale (space, radius, type size/weight/tracking, palette colour, shadow token value)
  naria  - the exact property:value pair exists in naria's component CSS (packages/ui/src/styles.css); line cited
  FAIL   - no naria basis
Usage: python3 -I naria-verify.py <css-dir> <naria-packages-ui-src-dir> [--all] [--palette=modern-palette.json]
modern-palette.json = paletteVariables("modern", light|dark) from @naria/ui 1.0.1 (the palette NariaProvider uses).
"""
import re, sys, pathlib, collections

src, ui = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
show_all = '--all' in sys.argv

SPACE = {0, 2, 4, 6, 8, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96}
RADII = {0, 6, 10, 16, 24, 999}
TYPE = {12: '0', 14: '0', 16: '0', 18: '-0.27px|-0.015em', 24: '-0.6px|-0.025em', 40: '-1.4px|-0.035em', 56: '-2.52px|-0.045em'}
WEIGHTS = {'400', '600', 'normal', 'inherit'}
SPACE_PROPS = re.compile(r'^(margin|padding)(-(top|right|bottom|left|inline|block)(-(start|end))?)?$|^(gap|row-gap|column-gap|top|right|bottom|left|inset)$')
COLOR_RE = re.compile(r'#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)|\b(white|black)\b')

# naria palette: every hex that appears in naria tokens / palettes / component CSS
naria_text = ''.join(p.read_text() for p in [ui / 'tokens.css', ui / 'palettes.ts', ui / 'styles.css'])
PALETTE = {c.lower() for c in re.findall(r'#[0-9a-fA-F]{3,8}\b', naria_text)}
# colours naria computes at runtime for the palette the dashboard uses (NariaProvider palette="modern"; paletteVariables)
import json
_pal = next((pathlib.Path(a.split('=', 1)[1]) for a in sys.argv if a.startswith('--palette=')), pathlib.Path(__file__).with_name('modern-palette.json'))
PALETTE |= {v.lower() for v in json.load(open(_pal)).values() if v.startswith('#')}

def blocks(css):
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    for m in re.finditer(r'([^{}]*)\{([^{}]*)\}', css):
        sel = ' '.join(m.group(1).split())
        line = css[:m.start(2)].count('\n') + 1
        for d in m.group(2).split(';'):
            if ':' in d:
                k, v = d.split(':', 1)
                yield sel, line, k.strip().lower(), ' '.join(v.split()).lower()

# naria component declarations: property -> {value: line}
NARIA = collections.defaultdict(dict)
for sel, line, k, v in blocks((ui / 'styles.css').read_text()):
    NARIA[k].setdefault(v.replace(' !important', ''), line)

def naria_pair(k, v):
    v = v.replace(' !important', '')
    if v in NARIA.get(k, {}):
        return NARIA[k][v]
    return None

def px_values(v):
    v = re.sub(r'var\([^()]*(\([^()]*\))?[^()]*\)', ' ', v)  # tokens are fine
    v = re.sub(r'calc\(.*\)|clamp\(.*\)|min\(.*\)|max\(.*\)', lambda m: m.group(0), v)
    return [float(x) for x in re.findall(r'(-?[0-9]*\.?[0-9]+)px', v)]

LINES = {'1', '1.18', '1.3', '1.4', '1.5', '1.6', '1.65', '1.7'}
TRACK = {18: {'-0.015em', '-0.27px'}, 24: {'-0.025em', '-0.6px'}, 17: {'-0.02em'}, 20: {'-0.025em'}, 32: {'-1px'}, 40: {'-0.035em', '-1.4px'}, 56: {'-0.045em', '-2.52px'}}
TRACK_SRC = {18: 'subheading (.nds-text--subheading, --nds-type-subheading-tracking)', 24: 'heading (.nds-text--heading, --nds-type-heading-tracking)', 17: '.nds-card h3', 20: '.nds-dialog-heading h2', 32: '.nds-stat > strong', 40: 'title', 56: 'display'}
TOKSIZE = {'var(--nds-type-subheading-size)': 18, 'var(--nds-type-heading-size)': 24, 'var(--nds-type-body-size)': 16, 'var(--nds-type-small-size)': 14, 'var(--nds-type-caption-size)': 12}
CONTROL = re.compile(r'(^|[\s>+~,(])(button|input|select|textarea)\b|\.btn\b|\.alert-button\b')
ICON = re.compile(r'(svg|::?before|::?after|\bi)\s*$')
RULE_SIZE = {}
for path in sorted(src.glob('*.css')):
    for sel, line, k, v in blocks(path.read_text()):
        if k == 'font-size':
            RULE_SIZE[(path.name, sel)] = TOKSIZE.get(v) or (int(float(v[:-2])) if v.endswith('px') else v)
out = collections.Counter(); fails = []; notes = []
for path in sorted(src.glob('*.css')):
    for sel, line, k, v in blocks(path.read_text()):
        where = f'{path.name}:{line}'
        verdict, why = None, ''
        if SPACE_PROPS.match(k):
            bad = [x for x in px_values(v) if abs(x) not in SPACE]
            if bad:
                ln = naria_pair(k, v)
                verdict, why = ('naria', f'styles.css:{ln}') if ln else ('FAIL', f'off-scale {bad}')
        elif k.startswith('border') and 'radius' in k:
            bad = [x for x in px_values(v) if x not in RADII]
            if bad:
                ln = naria_pair(k, v)
                verdict, why = ('naria', f'styles.css:{ln}') if ln else ('FAIL', f'radius {bad}')
        elif k == 'font-size' or k == 'font':
            sizes = px_values(v.split('/')[0]) if k == 'font' else px_values(v)
            bad = [x for x in sizes if int(x) not in TYPE]
            if bad:
                ln = naria_pair('font-size', f'{int(bad[0])}px') if k == 'font' else naria_pair(k, v)
                verdict, why = ('naria', f'styles.css:{ln}') if ln else ('FAIL', f'type size {bad}')
            if k == 'font' and '/' in v:
                lh = v.split('/')[1].split()[0]
                if lh.endswith('px') or (re.fullmatch(r'[0-9.]+', lh) and lh not in LINES):
                    verdict, why = 'FAIL', f'line-height {lh} in font shorthand'
        elif k in ('height', 'min-height') and CONTROL.search(sel) and not ICON.search(sel):
            allowed = {36, 44, 52}                                   # .nds-control--sm / md / lg
            if 'tab' in sel: allowed |= {46}                         # .nds-tablist > button
            if re.search(r'filter|tag|chip|history-results|cited-by', sel): allowed |= {32}   # .nds-tag
            if re.search(r'code-confirm input|\.check input', sel): allowed |= {20}          # .nds-check-mark
            if 'textarea' in sel: allowed |= {110}                    # .nds-textarea
            bad = [x for x in px_values(v) if x not in allowed]
            if bad:
                verdict, why = 'FAIL', f'control {k} {bad} (naria {sorted(allowed)})'
        elif k == 'line-height':
            if v.endswith('px') or (re.fullmatch(r'[0-9.]+', v) and v not in LINES):
                verdict, why = 'FAIL', f'line-height {v} (naria {sorted(LINES)} or tokens)'
        elif k == 'font-weight':
            if v not in WEIGHTS and not v.startswith('var('):
                ln = naria_pair(k, v)
                verdict, why = ('naria', f'styles.css:{ln}') if ln else ('FAIL', f'weight {v}')
        elif k == 'letter-spacing':
            if not v.startswith('var(') and v not in ('0', 'normal', '0px'):
                size = RULE_SIZE.get((path.name, sel))
                ok = TRACK.get(size, set())
                verdict, why = ('naria', f'{size} tracking {v} = naria {TRACK_SRC.get(size)}') if v in ok else ('FAIL', f'tracking {v} at font-size {size} (naria {sorted(ok) or "0"})')
        if k in ('color', 'background', 'background-color', 'border', 'border-color', 'border-top', 'border-bottom', 'border-left',
                 'border-right', 'outline', 'outline-color', 'fill', 'stroke', 'box-shadow', 'text-decoration-color', 'caret-color',
                 'accent-color', 'stop-color', 'border-inline-start', 'border-left-color') or 'color' in k:
            for c in COLOR_RE.findall(v) if False else [m.group(0) for m in COLOR_RE.finditer(v)]:
                c0 = c.lower()
                if c0 in ('white', 'black'):
                    verdict, why = 'FAIL', f'keyword colour {c0}'
                elif c0.startswith('#') and c0 not in PALETTE:
                    ln = naria_pair(k, v)
                    verdict, why = ('naria', f'styles.css:{ln}') if ln else ('FAIL', f'colour {c0} not in naria')
                elif c0.startswith('rgb') or c0.startswith('hsl'):
                    verdict, why = 'FAIL', f'functional colour {c0}'
        if verdict:
            out[verdict] += 1
            row = f'{verdict:5} {where:24} {k}: {v}   [{sel[-60:]}]  {why}'
            (fails if verdict == 'FAIL' else notes).append(row)

for path in sorted(list(src.glob('*.tsx')) + list(src.glob('*.ts'))):
    for n, line in enumerate(path.read_text().split('\n'), 1):
        for c in re.findall(r"['\"=]#([0-9a-fA-F]{3,8})\b", line):
            ok = '#' + c.lower() in PALETTE
            out['naria' if ok else 'FAIL'] += 1
            (notes if ok else fails).append(f"{'naria' if ok else 'FAIL '} {path.name}:{n}  colour #{c} {'in' if ok else 'not in'} naria palette (tokens.css / styles.css / modern palette)")
print(f'summary: {dict(out)}')
print('\n'.join(fails))
if show_all:
    print('--- verified against naria component CSS ---')
    print('\n'.join(notes))
