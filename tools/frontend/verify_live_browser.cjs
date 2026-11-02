/* Browser verification of live mode against the mock reader. PLAYWRIGHT_MODULE=/path/to/node_modules/playwright */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { server, control } = require("./mock_pulse_server.cjs");
const assert = require("node:assert");
const out = process.env.SHOT_DIR || "/tmp";
// the committed wire fixtures are fictional by design; ignore their own data strings, not the UI copy
const clean = (t) => t.replace(/Fictional protocol fixture; not a market outlook\./g, "");
(async () => {
  // VERIFY_BASE points at the real Preview reader harness (tools/frontend/serve_real_reader.py); default is the mock
  let base = process.env.VERIFY_BASE;
  if (!base) {
    await new Promise((r) => server.listen(0, "127.0.0.1", r));
    base = "http://127.0.0.1:" + server.address().port;
  }
  const requests = [];
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 375, height: 710 }, deviceScaleFactor: 2, reducedMotion: "reduce" });
  const errors = [];
  const encodings = new Set();
  page.on("response", (res) => { if (res.url().includes("/api/pulse")) { requests.push(res.url()); const e = res.headers()["content-encoding"]; if (res.url().includes("artifact=")) encodings.add(e || "identity"); } });
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => m.type() === "error" && !/Failed to load resource/.test(m.text()) && errors.push(m.text()));
  await page.route("https://coindcx.s3.amazonaws.com/**", (r) => r.abort());
  await page.route("https://fonts.googleapis.com/**", (r) => r.abort());
  await page.route("https://fonts.gstatic.com/**", (r) => r.abort());
  // This reader/UI harness has no TLS/socket server. Socket behavior has its own browser test.
  if(new URL(base).hostname==="127.0.0.1") await page.routeWebSocket("**/api/pulse_stream",()=>{});
  await page.goto(base + "/?source=live", { waitUntil: "load" });
  await page.waitForSelector(".pulse-row", { timeout: 5000 });
  const d = await page.evaluate(() => PulseDesign.diagnostics());
  const artifacts = () => requests.filter((c) => c.includes("artifact=")).length;
  // compact transport: the first view downloads the manifest, core and the open metric only
  assert.strictEqual(artifacts(), 3, "initial view downloads core, volatility and (price is live by default) references: " + requests.join(" "));
  assert.strictEqual(d.sample, false);
  assert.strictEqual(d.totalRows, 3);
  assert.strictEqual(d.phase, "ready");
  await page.waitForFunction(() => PulseDesign.diagnostics().live && PulseDesign.diagnostics().live.status === "ok", null, { timeout: 8000 });
  const results = {};
  for (const m of ["volatility", "long_short", "funding", "funding_change", "open_interest"]) {
    const before = artifacts();
    await page.locator('[data-metric="' + m + '"]').click();
    await page.waitForFunction(() => PulseDesign.diagnostics().phase === "ready", null, { timeout: 5000 });
    results.fetched = (results.fetched || 0) + (artifacts() - before);
    const tabs = await page.$$eval("#level-tabs button", (b) => b.map((x) => ({ level: x.dataset.level, disabled: x.disabled })));
    results[m] = { rows: await page.locator(".pulse-row").count(), levelsDisabled: tabs.filter((t) => t.level !== "0" && t.disabled).length };
    await page.screenshot({ path: out + "/live-" + m + ".png" });
    await page.locator(".row-hit").first().click();
    await page.waitForSelector("#pulse-sheet[open]");
    const sheet = clean(await page.locator("#pulse-sheet").innerText());
    assert.ok(!/sample|illustrative|fictional|fixture/i.test(sheet), m + " detail contains sample wording: " + sheet.match(/.{0,30}(sample|illustrative|fictional|fixture).{0,30}/i));
    await page.screenshot({ path: out + "/live-" + m + "-detail.png" });
    await page.keyboard.press("Escape");
  }
  assert.ok(artifacts() <= 8, "at most one artifact per part plus a horizon switch: " + artifacts());
  const body = clean(await page.locator("#pulse-app").innerText());
  assert.ok(!/sample|illustrative|fictional|fixture/i.test(body), "page text contains sample wording: " + ((body.match(/.{0,40}(sample|illustrative|fictional|fixture).{0,40}/i) || [""])[0]));
  assert.ok(await page.locator("#timeframe-risk-disclaimer").innerText().then((t) => !/sample|not live/i.test(t)));
  // funding detail shows the real settlement time and settled rate, not stand-ins
  await page.locator('[data-metric="funding"]').click();
  await page.locator(".row-hit").first().click();
  await page.waitForSelector("#pulse-sheet[open]");
  const funding = await page.locator("#pulse-sheet").innerText();
  assert.ok(/Next settlement/.test(funding) && /UTC/.test(funding), "next settlement shown");
  await page.keyboard.press("Escape");
  // horizon switch requests the 4H snapshot
  await page.locator('[data-metric="open_interest"]').click();
  await page.locator('[data-window="4H"]').click();
  await page.waitForFunction(() => PulseDesign.diagnostics().phase === "ready" && true, null, { timeout: 5000 });
  assert.ok(requests.some((c) => c.includes("horizon=4H")), "4H snapshot requested: " + requests.join(" "));
  // backend failure: data stays, state becomes stale after repeated failures; then recovers
  if (!process.env.VERIFY_BASE) await fetch(base + "/control?set=" + encodeURIComponent(JSON.stringify({ fail: 503 })));
  await page.evaluate(() => 0);
  // overflow and geometry checks
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow <= 0, "horizontal page overflow " + overflow);
  await page.emulateMedia({ colorScheme: "dark" });
  await page.screenshot({ path: out + "/live-dark.png" });
  // live price overlay (opt-in): a second page with ?price=live
  if (process.env.VERIFY_BASE) {
    const overlay = await browser.newPage({ viewport: { width: 375, height: 710 }, deviceScaleFactor: 2, reducedMotion: "reduce" });
    const oRequests = [];
    overlay.on("response", (res) => res.url().includes("/api/pulse") && oRequests.push(res.url()));
    overlay.on("pageerror", (e) => errors.push(e.message));
    await overlay.route("https://coindcx.s3.amazonaws.com/**", (r) => r.abort());
    await overlay.route("https://fonts.googleapis.com/**", (r) => r.abort());
    await overlay.route("https://fonts.gstatic.com/**", (r) => r.abort());
    await overlay.goto(base + "/?source=live&price=live", { waitUntil: "load" });
    await overlay.waitForSelector(".pulse-row", { timeout: 5000 });
    await overlay.waitForFunction(() => PulseDesign.diagnostics().price && PulseDesign.diagnostics().price.status === "ok" && PulseDesign.diagnostics().price.applied > 0, null, { timeout: 12000 });
    const firstPrice = await overlay.locator(".row-price").first().innerText();
    assert.ok(/103\.125/.test(firstPrice), "live price shown with its return: " + firstPrice);
    const firstChange = await overlay.locator(".change-pill").first().innerText();
    assert.ok(/3\.13/.test(firstChange), "live return shown with the live price: " + firstChange);
    assert.strictEqual(oRequests.filter((u) => u.includes("artifact=")).length, 3, "core, volatility and references (lazy, once)");
    await overlay.screenshot({ path: out + "/live-price-overlay.png" });
    await overlay.close();
  }
  console.log("results", JSON.stringify(results), "artifact encodings", JSON.stringify([...encodings]), "errors", JSON.stringify(errors));
  assert.deepStrictEqual(errors, []);
  await browser.close();
  if (!process.env.VERIFY_BASE) server.close();
  console.log("Live browser verification passed");
})().catch((e) => { console.error(e); process.exit(1); });
