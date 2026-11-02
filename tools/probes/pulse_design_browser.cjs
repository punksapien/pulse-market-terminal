/* Run with PLAYWRIGHT_MODULE pointing at an installed Playwright module. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const html = fs.readFileSync(path.join(root, "pulse-preview.html"), "utf8");
const output = process.env.PULSE_SCREENSHOTS || "/private/tmp";
const previousLight = process.env.PULSE_VERIFY_SCREENSHOT
  ? fs.readFileSync(path.join(output, "pulse-mobile-light.png"))
  : null;

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({
    viewport: { width: 375, height: 812 },
    deviceScaleFactor: 2,
    reducedMotion: "reduce",
  });
  const errors = [],
    requests = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => requests.push(request.url()));
  // Offline, deterministic logo failures exercise the visible initial fallback.
  // No third-party availability or personal browser profile is used by this test.
  await page.route("https://coindcx.s3.amazonaws.com/**", (route) =>
    route.abort(),
  );
  await page.route("https://fonts.googleapis.com/**", (route) => route.abort());
  await page.setContent(html, { waitUntil: "load" });
  await page.waitForFunction(() => Boolean(window.PulseDesign));
  const light = await page.screenshot({
    path: path.join(output, "pulse-mobile-light.png"),
  });
  if (previousLight)
    assert.ok(
      light.equals(previousLight),
      "Initial design screenshot changed during performance-only work",
    );
  const originalNav = await page
    .locator('[data-metric="volatility"]')
    .elementHandle();
  await page.locator('[data-window="1H"]').click();
  assert.ok(
    await originalNav.evaluate(
      (n) => n === document.querySelector('[data-metric="volatility"]'),
    ),
  );
  await page.locator('[data-window="24H"]').click();
  const initial = await page.evaluate(() => window.PulseDesign.diagnostics());
  assert.equal(initial.totalRows, 16);
  assert.ok(initial.visibleRows <= 17);
  const geometry = await page.evaluate(() =>
    Object.fromEntries(
      [
        ".metric-nav",
        ".level-strip",
        ".pulse-table-header",
        ".pulse-row",
        ".identity",
        ".change-pill",
        ".row-trade",
      ].map((s) => {
        const b = document.querySelector(s).getBoundingClientRect();
        return [s, { x: b.x, y: b.y, width: b.width, height: b.height }];
      }),
    ),
  );
  assert.equal(geometry[".metric-nav"].height, 50);
  assert.equal(geometry[".level-strip"].height, 44);
  assert.equal(geometry[".pulse-table-header"].height, 32);
  assert.equal(geometry[".pulse-row"].y, 138);
  assert.equal(geometry[".identity"].height, 44);
  assert.deepEqual(
    [geometry[".change-pill"].width, geometry[".change-pill"].height],
    [70, 28],
  );
  assert.deepEqual(
    [geometry[".row-trade"].width, geometry[".row-trade"].height],
    [28, 28],
  );
  assert.equal(
    await page.locator(".identity-name").first().innerText(),
    "BABYDOGE",
  );
  assert.equal(
    await page.locator(".metric-number").first().innerText(),
    "67.67%",
  );
  assert.equal(
    await page.locator(".change-pill").first().innerText(),
    "+120.21%",
  );
  for (const id of [
    "volatility",
    "long_short",
    "funding",
    "funding_change",
    "open_interest",
  ]) {
    await page.locator('[data-metric="' + id + '"]').click();
    assert.equal(await page.locator("#level-tabs button").count(), 5);
    await page.locator('[data-level="3"]').click();
    assert.ok(
      (await page.evaluate(() => PulseDesign.diagnostics())).filteredRows > 0,
    );
    await page.locator('[data-level="0"]').click();
    await page.locator("#row-pool .row-hit").first().click();
    assert.equal(
      await page.locator("#pulse-sheet").evaluate((n) => n.open),
      true,
    );
    assert.ok(
      (await page.locator("#sheet-content").innerText()).includes(
        "Market metrics",
      ),
    );
    assert.equal(
      await page.locator("#detail-footer button").isDisabled(),
      true,
    );
    if (id === "funding_change")
      await page.screenshot({
        path: path.join(output, "pulse-funding-change-detail.png"),
      });
    assert.equal(
      await page.locator(".large-value").innerText(),
      await page.locator(".metric-number").first().innerText(),
    );
    assert.equal(await page.locator(".history-chart").count(), 1);
    await page.keyboard.press("Escape");
    await page.locator("#open-filters").click();
    assert.ok(
      (await page.locator("#sheet-content").innerText()).includes("Trend"),
    );
    assert.equal(await page.locator(".filter-section").count(), 3);
    assert.equal(await page.locator("#filter-search").count(), 0);
    assert.equal(
      await page
        .locator(".choice-group")
        .first()
        .evaluate((n) => getComputedStyle(n).gap),
      "12px",
    );
    await page.locator("#close-sheet").click();
  }
  await page.locator('[data-metric="funding"]').click();
  await page.locator("#open-filters").click();
  await page.locator('[data-filter-key="interval"][data-value="4"]').click();
  await page
    .locator('[data-filter-key="volume"][data-value="5000000"]')
    .click();
  await page.screenshot({
    path: path.join(output, "pulse-funding-filters.png"),
  });
  await page.locator("#apply-filters").click();
  assert.equal(await page.locator("#filter-count").innerText(), "2");
  assert.ok(await page.locator("#applied-filters").isVisible());
  const filtered = await page.evaluate(
    () => PulseDesign.diagnostics().filteredRows,
  );
  assert.ok(filtered > 0 && filtered < 16);
  await page.locator("#open-filters").click();
  await page.locator('[data-filter-key="interval"][data-value="1"]').click();
  await page.keyboard.press("Escape");
  assert.equal(await page.locator("#filter-count").innerText(), "2");
  await page.locator('[data-metric="long_short"]').click();
  assert.equal(await page.locator("#filter-count").isVisible(), false);
  await page.locator('[data-metric="funding"]').click();
  assert.equal(await page.locator("#filter-count").innerText(), "2");
  await page.locator("#open-filters").click();
  await page.locator("#reset-filters").click();
  await page.locator("#close-sheet").click(); // Cancel must preserve applied filters.
  assert.equal(await page.locator("#filter-count").innerText(), "2");
  await page.locator("#open-filters").click();
  await page.locator("#reset-filters").click();
  await page.locator("#apply-filters").click();
  assert.equal(await page.locator("#filter-count").isVisible(), false);
  const fundingHeadline = await page
    .locator(".metric-number")
    .first()
    .innerText();
  for (const w of ["1H", "4H", "24H"]) {
    await page.locator('[data-window="' + w + '"]').click();
    assert.equal(await page.locator("#change-column").innerText(), "24H % Chg");
    assert.equal(
      await page.locator(".metric-number").first().innerText(),
      fundingHeadline,
    );
  }
  assert.deepEqual(await page.locator("[data-window]").allInnerTexts(), [
    "Current",
    "1D",
    "7D",
  ]);
  await page.locator('[data-metric="open_interest"]').click();
  await page.locator('[data-window="1H"]').click();
  assert.equal(await page.locator("#change-column").innerText(), "1H % Chg");
  await page.locator("#open-filters").click();
  assert.ok(
    (await page.locator(".filter-section").first().innerText()).includes(
      "1H % chg",
    ),
  );
  await page
    .locator('[data-filter-key="threshold"][data-value="lt:-0.1"]')
    .click();
  await page.locator("#apply-filters").click();
  await page.locator("#open-filters").click();
  await page.locator("#reset-filters").click();
  await page.locator("#apply-filters").click();
  await page.locator('[data-metric="long_short"]').click();
  await page.locator("#open-filters").click();
  await page
    .locator('[data-filter-key="threshold"][data-value="gt:1.5"]')
    .click();
  await page.locator("#apply-filters").click();
  const ratios = await page.locator(".metric-number").allInnerTexts();
  ratios.forEach((v) => assert.ok(parseFloat(v) > 1.5));
  await page.locator("#open-filters").click();
  await page.locator("#reset-filters").click();
  await page.locator("#apply-filters").click();
  for (const phase of [
    "loading",
    "empty",
    "error",
    "stale",
    "unavailable",
    "ready",
  ]) {
    await page.locator("#preview-controls summary").click();
    await page.locator("#preview-state").selectOption(phase);
    assert.equal(
      await page.evaluate(() => PulseDesign.diagnostics().phase),
      phase,
    );
    if (phase === "stale" || phase === "unavailable") {
      await page.locator(".row-hit").first().click();
      if (phase === "stale")
        assert.equal(
          await page.locator(".detail-status").innerText(),
          "Delayed sample",
        );
      else {
        assert.equal(await page.locator(".large-value").innerText(), "—");
        assert.equal(await page.locator(".history-chart").count(), 0);
        assert.equal(await page.locator(".signal-since-line").count(), 0);
      }
      await page.keyboard.press("Escape");
    }
    if (phase === "error") {
      assert.ok(await page.getByText("Failed to load the section").isVisible());
      await page.locator("#retry-sample").click();
      assert.equal(
        await page.evaluate(() => PulseDesign.diagnostics().phase),
        "ready",
      );
    }
  }
  await page.locator("#preview-controls summary").click();
  await page.locator("#preview-theme").selectOption("dark");
  await page.locator("#preview-controls summary").click();
  assert.equal(
    await page.evaluate(
      () =>
        getComputedStyle(document.querySelector(".pulse-app")).backgroundColor,
    ),
    "rgb(31, 31, 31)",
  );
  await page.screenshot({ path: path.join(output, "pulse-mobile-dark.png") });
  for (const width of [320, 375, 430, 1440]) {
    await page.setViewportSize({ width, height: 812 });
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      ),
      false,
    );
    const clip = await page
      .locator(".table-scroll")
      .evaluate((n) => ({ client: n.clientWidth, scroll: n.scrollWidth }));
    if (width === 320) assert.ok(clip.scroll > clip.client);
  }
  // Stress the SAME controller with 5,000 fixture identities; no server involved.
  const stressed = html.replace(
    "const snapshot = window.createPulseDesignFixture(M);",
    'const snapshot = window.createPulseDesignFixture(M); snapshot.rows = Array.from({length:5000},(_,i)=>({...snapshot.rows[i%16],id:"B-T"+String(i).padStart(5,"0")+"_USDT",symbol:"T"+String(i).padStart(5,"0")}));',
  );
  assert.notEqual(stressed, html);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.evaluate(() => PulseDesign.destroy());
  await page.setContent(stressed, { waitUntil: "load" });
  await page.waitForFunction(
    () => window.PulseDesign?.diagnostics().totalRows === 5000,
  );
  await page.locator("#market-viewport").evaluate((n) => {
    n.scrollTop = n.scrollHeight / 2;
  });
  await page.waitForTimeout(100);
  const stress = await page.evaluate(() => PulseDesign.diagnostics());
  assert.ok(stress.visibleRows <= 17);
  assert.equal(stress.totalRows, 5000);
  const expiringPage = await browser.newPage({
    viewport: { width: 375, height: 812 },
    reducedMotion: "reduce",
  });
  expiringPage.on("pageerror", (error) => errors.push(error.message));
  await expiringPage.route("https://coindcx.s3.amazonaws.com/**", (route) =>
    route.abort(),
  );
  await expiringPage.route("https://fonts.googleapis.com/**", (route) =>
    route.abort(),
  );
  const expiring = html.replace(
    "const snapshot = window.createPulseDesignFixture(M);",
    "const snapshot = window.createPulseDesignFixture(M); snapshot.rows.forEach((r,i)=>M.WINDOWS.forEach(w=>{r.metrics.funding[w].expires_at=Date.now()+(i%2?2200:700)}));",
  );
  await expiringPage.setContent(expiring, { waitUntil: "load" });
  await expiringPage.locator('[data-metric="funding"]').click();
  await expiringPage.waitForFunction(
    () =>
      Array.from(document.querySelectorAll(".metric-number")).filter(
        (n) => n.textContent === "—",
      ).length >= 8,
  );
  const firstExpired = await expiringPage
    .locator(".metric-number")
    .evaluateAll((nodes) => nodes.filter((n) => n.textContent === "—").length);
  assert.ok(
    firstExpired < 16,
    "Two different expiry deadlines must not be collapsed",
  );
  await expiringPage.waitForFunction(() =>
    Array.from(document.querySelectorAll(".metric-number")).every(
      (n) => n.textContent === "—",
    ),
  );
  await expiringPage.close();
  assert.equal(errors.length, 0, errors.join("\n"));
  assert.equal(
    requests.some((url) => /\/api(?:[/?]|$)/.test(url)),
    false,
  );
  console.log(
    JSON.stringify({
      browser: "chromium",
      interactions: "passed",
      staggeredIdleExpiry: "passed",
      sourceRequests: 0,
      pageErrors: errors,
      mobile: initial,
      svgGeometry: geometry,
      stress5000: stress,
      screenshots: output,
    }),
  );
  await browser.close();
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
