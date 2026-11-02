/* Live-mode UX regression against the local mock reader (no network): window toggles show different values,
   a refresh keeps the open sheet's scroll, RSI has two decimals. Run with PLAYWRIGHT_MODULE. */
"use strict";
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { server } = require("../frontend/mock_pulse_server.cjs");

(async () => {
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const base = "http://127.0.0.1:" + server.address().port;
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 375, height: 560 }, reducedMotion: "reduce" });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  for (const host of ["https://coindcx.s3.amazonaws.com/**", "https://fonts.googleapis.com/**", "https://fonts.gstatic.com/**"]) await page.route(host, (r) => r.abort());
  await page.goto(base + "/?source=live", { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => window.PulseDesign?.diagnostics().phase === "ready", null, { timeout: 15000 });
  const tab = (name) => page.locator("button, [role=tab]").filter({ hasText: new RegExp("^" + name + "$") }).first();
  const firstRow = () => page.locator(".pulse-row").first().innerText();

  // 1. Funding and Funding Chg: Current/Last, 1D and 7D are three different wire fields
  // Funding Chg windows are covered by the adapter unit test: the golden fixture repeats one settled value
  for (const [metric, labels] of [["Funding", ["Current", "1D", "7D"]]]) {
    await tab(metric).click();
    const seen = [];
    for (const label of labels) {
      await tab(label).click();
      await page.waitForTimeout(250);
      seen.push((await firstRow()).replace(/\s+/g, " "));
    }
    assert.equal(new Set(seen).size, 3, metric + " windows must show different values: " + JSON.stringify(seen));
  }

  // 2. The open sheet keeps its scroll through a refresh
  await tab("Volatility").click();
  await page.locator(".pulse-row").first().click();
  await page.waitForSelector("#pulse-sheet[open] .detail-block");
  const moved = await page.evaluate(() => { const c = document.getElementById("sheet-content"); c.scrollTop = 160; return c.scrollTop; });
  assert.ok(moved > 0, "sheet must be scrollable");
  const kept = await page.evaluate(async () => {
    document.dispatchEvent(new Event("visibilitychange"));
    await new Promise((r) => setTimeout(r, 2500));
    return document.getElementById("sheet-content").scrollTop;
  });
  assert.equal(kept, moved, "refresh moved the sheet scroll");

  // 3. RSI: two decimals
  const rsi = await page.evaluate(() => {
    const box = [...document.querySelectorAll(".detail-metric")].find((e) => /RSI/.test(e.textContent));
    return box && box.querySelector("strong").textContent;
  });
  assert.match(rsi, /^\d{1,3}\.\d{2}$/, "RSI text: " + rsi);
  // 4. The sheet carries no explanation/source/methodology blocks, only metrics, history and (volatility) outlook
  const sheetText = await page.evaluate(() => document.getElementById("sheet-content").innerText);
  for (const banned of ["Why this", "Understanding", "Methodology", "Source:", "Snapshot generated", "Sample", "Illustrative"])
    assert.ok(!sheetText.includes(banned), "sheet still contains: " + banned);
  assert.ok((await page.locator(".detail-metric").count()) === 6, "six market metric boxes");
  await page.click("#close-sheet");
  // 5. The info icon opens the Pulse guide that names every tab
  await page.locator("#preview-controls summary").click();
  await page.waitForSelector("#pulse-sheet[open][data-kind=guide] .guide");
  const guide = await page.evaluate(() => document.getElementById("sheet-content").innerText);
  for (const word of ["Volatility", "L/S Ratio", "Funding", "Funding Chg", "Open Interest", "Not financial advice"])
    assert.ok(guide.includes(word), "guide misses: " + word);
  assert.ok(guide.split(/\s+/).length < 330, "guide stays concise");
  assert.deepEqual(errors, []);
  console.log("Pulse live UX: three funding windows differ, sheet scroll kept (" + moved + "px), RSI " + rsi);
  await browser.close();
  server.close();
})().catch((e) => { console.error(e); process.exit(1); });
