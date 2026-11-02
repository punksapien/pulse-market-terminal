/* Regression: a background refresh must not reset the open detail sheet's scroll. Run with PLAYWRIGHT_MODULE. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const html = fs.readFileSync(path.resolve(__dirname, "../../pulse-preview.html"), "utf8");
(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 375, height: 500 }, reducedMotion: "reduce" });
  for (const host of ["https://coindcx.s3.amazonaws.com/**", "https://fonts.googleapis.com/**"]) await page.route(host, (r) => r.abort());
  await page.setContent(html, { waitUntil: "load" });
  await page.waitForFunction(() => Boolean(window.PulseDesign));
  await page.waitForSelector(".pulse-row");
  await page.click(".pulse-row .row-main, .pulse-row");
  await page.waitForSelector("#pulse-sheet[open] .detail-block");
  const moved = await page.evaluate(() => { const c = document.getElementById("sheet-content"); c.scrollTop = 200; return c.scrollTop; });
  assert.ok(moved > 0, "sheet content must be scrollable in this viewport");
  const afterRefresh = await page.evaluate(async () => {
    document.dispatchEvent(new Event("visibilitychange"));
    await new Promise((r) => setTimeout(r, 1500));
    return document.getElementById("sheet-content").scrollTop;
  });
  assert.equal(afterRefresh, moved, "background refresh reset the sheet scroll");
  console.log("Pulse sheet scroll: position kept across refresh (" + moved + "px)");
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
