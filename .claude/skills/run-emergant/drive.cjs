// Drive the Bounty Hunter web UI with Playwright (headless Chromium).
//
//   node .claude/skills/run-emergant/drive.cjs [--owner] [--out DIR] [--click "Text"] [route ...]
//
// For each route (default "/"): load it, dismiss the guided tour, optionally click a
// button/link by visible text, screenshot to DIR/<route>.png, and print the page heading,
// uncaught page errors and failing /api responses. --owner signs in as the local owner
// seeded by seed_owner.py (reads $RUN_DIR/session_token).
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const req = (m) => { try { return require(m); } catch { return require(path.join(execSync('npm root -g').toString().trim(), m)); } };
const { chromium } = req('playwright');

const args = process.argv.slice(2);
const flag = (n) => { const i = args.indexOf(n); if (i < 0) return null; const v = args[i + 1]; args.splice(i, 2); return v; };
const owner = args.includes('--owner'); if (owner) args.splice(args.indexOf('--owner'), 1);
const out = flag('--out') || path.join(process.env.RUN_DIR || '/tmp/emergant-run', 'shots');
const click = flag('--click');
const routes = args.length ? args : ['/'];
const BASE = process.env.FRONTEND_URL || 'http://localhost:3000';
fs.mkdirSync(out, { recursive: true });

(async () => {
  const browser = await chromium.launch({ executablePath: fs.existsSync('/opt/pw-browsers/chromium') ? '/opt/pw-browsers/chromium' : undefined });
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  // Tour modal opens on first visit unless this localStorage key is set.
  await ctx.addInitScript(() => localStorage.setItem('bh_walkthrough_done_v1', '1'));
  if (owner) {
    const token = fs.readFileSync(path.join(process.env.RUN_DIR || '/tmp/emergant-run', 'session_token'), 'utf8').trim();
    // Backend is :8001, frontend :3000 — same site (localhost), so one cookie on host "localhost" covers both.
    await ctx.addCookies([{ name: 'session_token', value: token, domain: 'localhost', path: '/', httpOnly: true, secure: true, sameSite: 'None' }]);
  }
  const page = await ctx.newPage();
  const errors = []; const apiFails = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('response', (r) => { if (r.url().includes('/api/') && r.status() >= 400) apiFails.push(`${r.status()} ${new URL(r.url()).pathname}`); });

  for (const route of routes) {
    errors.length = 0; apiFails.length = 0;
    await page.goto(BASE + route, { waitUntil: 'networkidle' });
    await page.waitForTimeout(800);
    if (click) {
      await page.getByRole('button', { name: click }).or(page.getByRole('link', { name: click })).first().click();
      await page.waitForLoadState('networkidle'); await page.waitForTimeout(800);
    }
    const file = path.join(out, (route.replace(/[^a-z0-9]+/gi, '_').replace(/^_|_$/g, '') || 'home') + '.png');
    await page.screenshot({ path: file, fullPage: true });
    const h1 = await page.locator('h1').first().innerText().catch(() => '(no h1)');
    const signedIn = await page.locator('[data-testid="account-menu-button"]').count() > 0;
    console.log(JSON.stringify({ route, url: page.url(), h1, signedIn, screenshot: file, pageErrors: [...errors], apiFails: [...new Set(apiFails)] }));
  }
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
