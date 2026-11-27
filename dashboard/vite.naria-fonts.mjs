import { Buffer } from 'node:buffer';
import { createHash, randomUUID } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const nariaStylesheet = /(?:^|\/)@naria\/ui\/dist\/styles\.css$/;
const fontFace = /@font-face\s*\{[^}]*\}/g;
const inlineWoff2 = /url\((["']?)data:font\/woff2;base64,([A-Za-z0-9+/]+={0,2})\1\)/g;
const coreDirectory = fileURLToPath(new URL('./vendor/wanted-sans-core/', import.meta.url));
const coverage = JSON.parse(readFileSync(join(coreDirectory, 'coverage.json'), 'utf8'));

function cssUrl(stylesheet, asset) {
  const path = relative(dirname(stylesheet), asset).split(sep).join('/');
  return `url("${path.startsWith('.') ? path : './' + path}")`;
}

/** Load common glyphs first and retain the matching complete face for uncommon text. */
export default function nariaFontAssets() {
  let directory;
  return {
    name: 'liveprobe-naria-font-assets',
    enforce: 'pre',
    configResolved(config) {
      directory = join(config.cacheDir, 'naria-fonts');
    },
    transform(source, id) {
      const stylesheet = id.split('?', 1)[0];
      if (!nariaStylesheet.test(stylesheet)) return null;
      const css = source.replace(fontFace, face => {
        let subset;
        const full = face.replace(inlineWoff2, (_url, _quote, base64) => {
          const bytes = Buffer.from(base64, 'base64');
          const digest = createHash('sha256').update(bytes).digest('hex');
          subset = coverage.fonts[digest];
          const asset = join(directory, `wanted-sans-${digest.slice(0, 16)}.woff2`);
          if (!existsSync(asset)) {
            mkdirSync(directory, { recursive: true });
            const temporary = asset + '.' + randomUUID();
            try {
              writeFileSync(temporary, bytes);
              renameSync(temporary, asset);
            } finally {
              rmSync(temporary, { force: true });
            }
          }
          return cssUrl(stylesheet, asset);
        });
        if (!subset || /\bunicode-range\s*:/.test(face)) return full;
        const core = face.replace(inlineWoff2, () => cssUrl(stylesheet, join(coreDirectory, subset)));
        return `${full.slice(0, -1)};unicode-range:${coverage.fallback}}${core.slice(0, -1)};unicode-range:${coverage.core}}`;
      });
      return css === source ? null : css;
    },
  };
}
