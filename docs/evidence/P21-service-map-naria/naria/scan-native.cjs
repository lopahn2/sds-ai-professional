// Finds form controls still drawn with browser defaults (no naria styling) on each page. Read-only.
const { chromium } = require('@playwright/test');
(async () => {
  const [,, base, routesArg, liveFlag] = process.argv;
  const routes = routesArg.split(',');
  const b = await chromium.launch(); const seen = new Map();
  for (const [w, h] of [[1440, 900], [390, 844]]) {
    const p = await b.newPage({ viewport: { width: w, height: h } });
    await p.route('**/*', r => ['GET', 'HEAD'].includes(r.request().method()) ? r.continue() : r.abort());
    for (const route of routes) {
      await p.goto(base + route, { waitUntil: 'domcontentloaded' }).catch(() => {}); await p.waitForTimeout(liveFlag ? 4000 : 1500);
      // open collapsed sections so hidden forms render
      await p.evaluate(() => document.querySelectorAll('details:not([open])').forEach(d => d.open = true));
      await p.waitForTimeout(300);
      const found = await p.evaluate(() => {
        const out = [];
        for (const e of document.querySelectorAll('input, textarea, select, button')) {
          if (e.type === 'hidden' || !e.getClientRects().length) continue;
          const s = getComputedStyle(e);
          const ua = [];
          if (['inset', 'outset', 'groove', 'ridge'].includes(s.borderTopStyle)) ua.push('border ' + s.borderTopStyle);
          if (s.borderTopColor === 'rgb(118, 118, 118)' || s.borderTopColor === 'rgb(133, 133, 133)') ua.push('UA border colour');
          if (s.backgroundColor === 'rgb(239, 239, 239)' || s.backgroundColor === 'rgb(240, 240, 240)') ua.push('UA button background');
          if (s.fontSize === '13.3333px') ua.push('UA font 13.33px');
          if (/Arial|Helvetica|system-ui/.test(s.fontFamily) && !/Wanted Sans/.test(s.fontFamily) && !/mono/i.test(s.fontFamily)) ua.push('UA font family');
          if (ua.length && !['checkbox', 'radio', 'range', 'color', 'file'].includes(e.type)) {
            const path = []; let n = e; for (let i = 0; i < 4 && n && n !== document.body; i++, n = n.parentElement) path.unshift(n.tagName.toLowerCase() + (n.className && typeof n.className === 'string' ? '.' + n.className.trim().split(/\s+/).join('.') : ''));
            out.push({ el: path.join(' > '), why: ua.join(', '), text: (e.textContent || e.placeholder || '').trim().slice(0, 30) });
          }
        }
        return out;
      });
      for (const f of found) { const k = f.el + ' | ' + f.why; if (!seen.has(k)) seen.set(k, `${route} @${w}: "${f.text}"`); }
    }
    await p.close();
  }
  for (const [k, v] of seen) console.log(k, '  <-', v);
  console.log('total distinct', seen.size); await b.close();
})().catch(e => { console.error(e); process.exit(1); });
