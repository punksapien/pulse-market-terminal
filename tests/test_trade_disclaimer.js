const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const sourceRoot = path.join(root, 'frontend/src/volatility');
const source = fs.readFileSync(path.join(sourceRoot, 'vs.js'), 'utf8');
const html = fs.readFileSync(path.join(sourceRoot, 'vs.html'), 'utf8');
const css = fs.readFileSync(path.join(sourceRoot, 'vs.css'), 'utf8');
const warning = 'Disclaimer: Volatility signals are statistical estimates, not financial advice.';
const pulseHtml = fs.readFileSync(path.join(root, 'frontend/src/pulse/pulse.html'), 'utf8');
assert.ok(pulseHtml.includes('Disclaimer: Pulse signals are statistical estimates, not a financial advice.'));
const dock = html.match(/<nav class="timeframe-dock"[\s\S]*?<\/nav>/)[0];
const footer = html.match(/<footer class="detail-actions"[\s\S]*?<\/footer>/)[0];
assert.equal((html.match(new RegExp(warning.replace('.', '\\.'), 'g')) || []).length, 2);
assert.equal((dock.match(/data-timeframe=/g) || []).length, 3);
assert.ok(dock.indexOf('timeframe-risk-disclaimer') > dock.indexOf('24H Chg'));
assert.ok(footer.indexOf('detail-risk-disclaimer') < footer.indexOf('btn-detail-trade'));
assert.ok(footer.includes('aria-describedby="detail-risk-disclaimer"'));
assert.ok(footer.includes('class="detail-disclaimer"'));
assert.ok(footer.includes('disclaimer-info-dot'));
assert.ok(css.includes('.detail-disclaimer {'));
assert.ok(css.includes('background: var(--surface-soft);'));
assert.ok(source.includes('aria-describedby="timeframe-risk-disclaimer"'));
assert.ok(!source.includes('window.confirm'));
assert.ok(!source.includes('confirmAndTrade'));
assert.ok(css.includes('max-width: 375px;'));
assert.ok(css.includes('padding-bottom: calc(100px + env(safe-area-inset-bottom, 0px));'));

const opened = [];
const telemetry = [];
const location = { protocol: 'http:', hostname: 'localhost', href: '', search: '' };
const context = {
  document: { readyState: 'loading', addEventListener() {} },
  window: { location, confirm() { throw Error('Confirmation popup must not run'); },
    open(...args) { opened.push(args); } },
  location,
  navigator: { userAgent: 'Macintosh desktop browser', maxTouchPoints: 0 },
  localStorage: { getItem: () => JSON.stringify({ utm_source: 'test-source' }) },
  fetch: async (url, options) => { telemetry.push({ url, options }); return { ok: true }; },
  URLSearchParams, console, Date, setInterval, clearInterval
};
vm.createContext(context);
vm.runInContext(source.replace("  if (D.readyState === 'loading') {", `
  globalThis.testTrade = tradeToken;
  if (D.readyState === 'loading') {`), context);
const token = { instrument: 'B-BTC_USDT', symbol: 'BTC' };
for (const medium of ['market_list', 'detail_cta']) {
  context.testTrade(token, medium);
  const [url, target, features] = opened.at(-1);
  const destination = new URL(url);
  assert.equal(destination.origin, 'https://coindcx.com');
  assert.equal(destination.pathname, '/futures/B-BTC_USDT');
  assert.equal(destination.searchParams.get('utm_source'), 'test-source');
  assert.equal(destination.searchParams.get('utm_medium'), medium);
  assert.equal(target, '_blank');
  assert.equal(features, 'noopener');
}
context.navigator.userAgent = 'iPhone';
context.navigator.maxTouchPoints = 5;
context.testTrade(token, 'detail_cta');
assert.ok(location.href.startsWith('dcxgo://go.coindcx.com/futures?'));
assert.ok(location.href.includes('instrument=B-BTC_USDT'));
assert.equal(opened.length, 2);
context.navigator.userAgent = 'Macintosh';
context.testTrade(token, 'market_list');
assert.equal(opened.length, 2); // Touch-enabled iPad uses the existing mobile route.
assert.equal(telemetry.length, 4);
for (const file of ['frontend/src/volatility/vs.js', 'index.html']) {
  const built = fs.readFileSync(path.join(root, file), 'utf8');
  assert.ok(!built.includes('window.confirm'), `${file} must not retain the popup`);
}
for (const file of ['frontend/src/volatility/vs.html', 'index.html']) {
  const built = fs.readFileSync(path.join(root, file), 'utf8');
  assert.ok(built.includes('id="timeframe-risk-disclaimer"'));
  assert.ok(built.includes('id="detail-risk-disclaimer"'));
}
console.log('Trade disclaimer: footer placement, accessibility, no popup, desktop/mobile routing and built assets passed.');
const production = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
assert.ok(production.includes('id="app"'));
assert.ok(production.includes('googletagmanager'));
assert.ok(!production.includes('noindex'));
assert.ok(!production.includes('id="pulse-app"'));
