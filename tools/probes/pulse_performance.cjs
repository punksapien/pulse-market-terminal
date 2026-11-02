/* Local sample-UI CPU/mutation benchmark, not exchange-to-screen latency. */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const html = fs.readFileSync(
  path.resolve(__dirname, "../../pulse-preview.html"),
  "utf8",
);
let browser;
(async () => {
  browser = await chromium.launch({ headless: true });
  const results = [];
  for (const count of [503, 5000]) {
    const page = await browser.newPage({
      viewport: { width: 375, height: 812 },
      deviceScaleFactor: 1,
      reducedMotion: "reduce",
    });
    await page.route("https://**", (route) => route.abort());
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const source = html.replace(
      "const snapshot = window.createPulseDesignFixture(M);",
      "const snapshot = window.createPulseDesignFixture(M); snapshot.rows = Array.from({length:" +
        count +
        '},(_,i)=>({...snapshot.rows[i%16],id:"B-T"+String(i).padStart(5,"0")+"_USDT",symbol:"T"+String(i).padStart(5,"0")}));',
    );
    await page.setContent(source, { waitUntil: "load" });
    await page.waitForFunction(() => Boolean(window.PulseDesign));
    const result = await page.evaluate(() => {
      const firstNav = document.querySelector('[data-metric="volatility"]');
      const observer = new MutationObserver(() => {});
      observer.observe(document.getElementById("pulse-app"), {
        subtree: true,
        childList: true,
        attributes: true,
        characterData: true,
      });
      const times = [],
        paints = [];
      let mutations = 0,
        navRemovals = 0;
      for (let i = 0; i < 120; i++) {
        const start = performance.now();
        document
          .querySelector('[data-window="' + (i % 2 ? "24H" : "1H") + '"]')
          .click();
        const elapsed = performance.now() - start;
        const records = observer.takeRecords();
        if (i >= 20) {
          times.push(elapsed);
          paints.push(PulseDesign.diagnostics().lastPaintMs);
          mutations += records.length;
          navRemovals += records
            .filter(
              (r) =>
                r.target.id === "metric-tabs" || r.target.id === "level-tabs",
            )
            .reduce((n, r) => n + r.removedNodes.length, 0);
        }
      }
      times.sort((a, b) => a - b);
      paints.sort((a, b) => a - b);
      observer.disconnect();
      const rowObserver = new MutationObserver(() => {});
      rowObserver.observe(document.getElementById("row-pool"), {
        subtree: true,
        childList: true,
        attributes: true,
        characterData: true,
      });
      document.querySelector('[data-level="0"]').click();
      const noopRowMutations = rowObserver.takeRecords().length;
      rowObserver.disconnect();
      return {
        rows: PulseDesign.diagnostics().totalRows,
        compileMs: PulseDesign.diagnostics().compileMs,
        clickP50Ms: times[50],
        clickP95Ms: times[95],
        paintP95Ms: paints[95],
        mutationsPer100WindowChanges: mutations,
        removedNavNodes: navRemovals,
        stableNavIdentity:
          firstNav === document.querySelector('[data-metric="volatility"]'),
        noopRowMutations,
        mountedRows: PulseDesign.diagnostics().visibleRows,
      };
    });
    assert.equal(errors.length, 0, errors.join("\n"));
    assert.ok(result.mountedRows <= 17);
    if (process.env.PULSE_ASSERT_INCREMENTAL) {
      assert.equal(result.stableNavIdentity, true);
      assert.equal(result.removedNavNodes, 0);
      assert.equal(result.noopRowMutations, 0);
    }
    results.push(result);
    await page.close();
  }
  console.log(
    JSON.stringify({
      sampleOnly: true,
      htmlBytes: Buffer.byteLength(html),
      gzip6Bytes: zlib.gzipSync(html, { level: 6 }).length,
      results,
    }),
  );
})()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(async () => {
    await browser?.close();
  });
