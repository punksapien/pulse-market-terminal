"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { performance } = require("node:perf_hooks");
const M = require("../frontend/src/pulse/pulse_model.js");
const fixture = { Date };
vm.createContext(fixture);
vm.runInContext(
  fs.readFileSync(path.join(__dirname, "../frontend/src/pulse/pulse_fixture.js"), "utf8"),
  fixture,
);
const now = 1000000;
const snapshot = fixture.createPulseDesignFixture(M, now);
const index = new M.Index(snapshot);
const view = (patch = {}, clock = now) =>
  index.select(
    { metric: "volatility", window: "24H", filters: M.defaults(), ...patch },
    clock,
  );
let checks = 0;
const check = (fn) => {
  fn();
  checks++;
};
check(() => assert.equal(M.REGISTRY.length, 5));
check(() => assert.equal(M.nextExpiry({ rows: [] }), Infinity));
check(() => assert.equal(M.nextExpiry(snapshot), now + 3600000));
check(() => assert.equal(M.nextExpiry(snapshot, now + 3600000), Infinity));
check(() => {
  const rows = [
    {
      metrics: {
        volatility: {
          "1H": { expires_at: 10 },
          "4H": { expires_at: 20 },
          "24H": { expires_at: NaN },
        },
      },
    },
  ];
  assert.equal(M.nextExpiry({ rows }, 10), 20);
  assert.equal(M.nextExpiry({ rows }, 20), Infinity);
});
check(() =>
  assert.equal(
    M.nextExpiry({ rows: Array(50000).fill(snapshot.rows[0]) }),
    now + 3600000,
  ),
);
check(() =>
  assert.ok(Object.isFrozen(index.snapshot.rows[0].metrics.funding["1H"])),
);
check(() => assert.equal(M.format("funding", 0), "0.00 bps"));
check(() => assert.equal(M.format("funding", 0.00015), "1.50 bps"));
check(() => assert.equal(M.format("funding", -0.00015), "-1.50 bps"));
for (const [input, expected] of [
  [0.00028696,"2.87 bps"],[-0.00500151,"-50.02 bps"],
  [-0.01386383,"-138.64 bps"],[0.0000005,"0.01 bps"],[-0.0000005,"-0.01 bps"],
  [-1e-12,"0.00 bps"],
]) check(() => assert.equal(M.format("funding",input),expected));
for (const [input, expected] of [
  [0.00005, "+0.50 bps"], [-0.00004, "-0.40 bps"],
  [-0.0001, "-1.00 bps"], [0, "0.00 bps"], [-0, "0.00 bps"],
  [1e-12, "0.00 bps"], [-1e-12,"0.00 bps"], [null, "—"], [NaN, "—"],
]) check(() => assert.equal(M.format("funding_change", input), expected));
check(() => assert.equal(M.format("long_short", 2), "2x"));
check(() => assert.equal(M.format("open_interest", 0.1), "+10.00%"));
check(() => assert.equal(M.format("volatility", null), "—"));
check(() => assert.notEqual(M.format("open_interest", 0.0000001), "+0.00%"));
check(() => assert.equal(M.price(0.0000000175), "0.0000000175"));
check(() => assert.notEqual(M.format("funding", 1e-15), "0.00000000%"));
check(() => assert.equal(M.format("funding", 1e-15), "0.00 bps"));
for (const [value, threshold, expected] of [
  [1, "gt:1", false],
  [1.01, "gt:1", true],
  [-0.2, "lt:-0.1", true],
  [-0.1, "lt:-0.1", false],
  [null, "gt:1", false],
  [1, "bad", false],
  [0, "all", true],
])
  check(() => assert.equal(M.matchesThreshold(value, threshold), expected));
check(() =>
  assert.deepEqual(M.windowLabels("funding"), ["Current", "1D", "7D"]),
);
check(() =>
  assert.deepEqual(M.windowLabels("funding_change"), ["Last", "1D", "7D"]),
);
check(() => assert.equal(M.changeWindow("funding", "4H"), "24H"));
check(() => assert.equal(M.changeWindow("open_interest", "4H"), "4H"));
for (const id of ["long_short", "funding_change", "open_interest"]) {
  for (const [threshold] of M.metric(id).control.options.slice(1))
    check(() => {
      view({ metric: id, filters: { ...M.defaults(), threshold } }).ids.forEach(
        (i) =>
          assert.ok(
            M.matchesThreshold(
              snapshot.rows[i].metrics[id]["24H"].value,
              threshold,
            ),
          ),
      );
      assert.equal(
        view(
          { metric: id, filters: { ...M.defaults(), threshold } },
          now + 3600001,
        ).ids.length,
        0,
      );
    });
}
for (const interval of ["1", "4", "8"])
  check(() => {
    const ids = view({
      metric: "funding",
      filters: { ...M.defaults(), interval },
    }).ids;
    assert.ok(ids.length > 0);
    ids.forEach((i) =>
      assert.equal(String(snapshot.rows[i].context.interval), interval),
    );
  });
check(() => assert.equal(view({ sort: "design" }).ids[0], 0));
check(() =>
  assert.equal(
    M.filterCount({ ...M.defaults(), signal: "Uptrend", volume: 5e6 }),
    2,
  ),
);
check(() =>
  assert.equal(
    M.effective(
      { value: 0, status: "available", asof: now, expires_at: now + 1 },
      now,
    ),
    "available",
  ),
);
for (const c of [
  { value: NaN },
  { value: true },
  { value: 0, asof: now + 5001 },
  { value: 0, expires_at: now },
]) {
  check(() =>
    assert.equal(
      M.effective(
        { value: 0, status: "available", asof: now, expires_at: now + 1, ...c },
        now,
      ),
      "unavailable",
    ),
  );
}
for (const metric of M.REGISTRY) {
  for (const window of M.WINDOWS) {
    const options = { metric: metric.id, window };
    for (const level of [1, 2, 3, 4])
      check(() => {
        const result = view({ ...options, level });
        assert.ok(result.ids.length > 0);
        result.ids.forEach((i) =>
          assert.equal(
            snapshot.rows[i].metrics[metric.id][window].level,
            level,
          ),
        );
      });
    check(() => {
      const result = view({
        ...options,
        filters: { ...M.defaults(), availability: "unavailable" },
      });
      assert.equal(result.ids.length, 1);
    });
  }
}
check(() => assert.equal(view({}, now + 3600001).ids.length, 16));
check(() => assert.equal(view({ level: 3 }, now + 3600001).ids.length, 0));
check(() =>
  assert.equal(
    view({
      phase: "stale",
      filters: { ...M.defaults(), availability: "stale" },
    }).ids.length,
    15,
  ),
);
check(() =>
  assert.equal(
    view({
      phase: "unavailable",
      filters: { ...M.defaults(), availability: "unavailable" },
    }).ids.length,
    16,
  ),
);
check(() =>
  assert.equal(
    view({ filters: { ...M.defaults(), search: "<script>" } }).ids.length,
    0,
  ),
);
check(() =>
  assert.equal(
    view({ filters: { ...M.defaults(), search: "BTC" } }).ids.length,
    1,
  ),
);
check(() =>
  view({
    filters: { ...M.defaults(), signal: "Uptrend", volume: 5e6 },
  }).ids.forEach((i) => {
    assert.equal(snapshot.rows[i].signal, "Uptrend");
    assert.ok(snapshot.rows[i].volume > 5e6);
  }),
);
check(() =>
  assert.equal(
    view({ sort: "symbol", descending: false }).ids[0],
    snapshot.rows.findIndex((r) => r.symbol === "1000PEPE"),
  ),
);
check(() => assert.deepEqual(view().ids, view().ids));
// ADR-0027: independent, straightforward filtering/partitioning parity oracle.
for (const m of M.REGISTRY)
  for (const w of M.WINDOWS)
    for (const phase of ["ready", "stale", "unavailable"]) {
      for (const clock of [now, now + 3600001])
        for (const f of [
          M.defaults(),
          { ...M.defaults(), signal: "Uptrend" },
          { ...M.defaults(), direction: m.directions[0] },
          { ...M.defaults(), volume: 5e6 },
          { ...M.defaults(), availability: "unavailable" },
          { ...M.defaults(), threshold: "gt:0.01" },
          { ...M.defaults(), threshold: "lt:-0.01" },
          { ...M.defaults(), interval: "4" },
          { ...M.defaults(), search: "DOG" },
        ])
          check(() => {
            const order = Array.from(index.orders.get(m.id + ":" + w));
            const good = order.filter((i) => {
              const r = snapshot.rows[i],
                c = r.metrics[m.id][w],
                st = M.effective(c, clock, phase);
              return (
                (f.signal === "all" || f.signal === r.signal) &&
                (f.direction === "all" || f.direction === c.direction) &&
                (!f.volume || r.volume > f.volume) &&
                (f.availability === "all" || f.availability === st) &&
                (f.threshold === "all" ||
                  (st === "available" &&
                    M.matchesThreshold(c.value, f.threshold))) &&
                (f.interval === "all" ||
                  String(r.context.interval) === f.interval) &&
                (!f.search || r.symbol.includes(f.search))
              );
            });
            const present = good.filter(
              (i) =>
                M.effective(snapshot.rows[i].metrics[m.id][w], clock, phase) !==
                "unavailable",
            );
            const absent = good.filter(
              (i) =>
                M.effective(snapshot.rows[i].metrics[m.id][w], clock, phase) ===
                "unavailable",
            );
            const counts = [0, 0, 0, 0, 0];
            for (const i of good) {
              const c = snapshot.rows[i].metrics[m.id][w];
              if (M.effective(c, clock, phase) === "available" && c.level)
                counts[c.level]++;
            }
            assert.deepEqual(
              index.select(
                { metric: m.id, window: w, phase, filters: f },
                clock,
              ),
              { ids: present.concat(absent), counts },
            );
          });
    }
check(() =>
  assert.equal(
    view({ filters: { ...M.defaults(), threshold: "invalid" } }).ids.length,
    0,
  ),
);
check(() =>
  assert.throws(
    () => new M.Index({ ...snapshot, registry_version: "wrong" }),
    /mismatch/,
  ),
);
check(() =>
  assert.throws(
    () =>
      new M.Index({ ...snapshot, rows: [snapshot.rows[0], snapshot.rows[0]] }),
    /duplicate/,
  ),
);
check(() => {
  const broken = JSON.parse(JSON.stringify(snapshot));
  broken.rows[0].metrics.funding["1H"].unit = "percent";
  assert.throws(() => new M.Index(broken), /unit/);
});
for (const total of [0, 1, 16, 503, 5000])
  for (const scroll of [0, 1200, 1000000])
    check(() => {
      const range = M.virtualRange(total, scroll, 650);
      assert.ok(range.end - range.start <= 17);
      assert.ok(range.start >= 0 && range.end <= total);
    });
for (const count of [503, 5000]) {
  const rows = Array.from({ length: count }, (_, i) => ({
    ...snapshot.rows[i % 16],
    id: "B-T" + String(i).padStart(5, "0") + "_USDT",
    symbol: "T" + String(i).padStart(5, "0"),
  }));
  const start = performance.now();
  const scale = new M.Index({ ...snapshot, rows });
  const compileMs = performance.now() - start;
  const times = [];
  for (let i = 0; i < 100; i++) {
    const t = performance.now();
    scale.select(
      {
        metric: M.REGISTRY[i % 5].id,
        window: M.WINDOWS[i % 3],
        level: i % 5,
        filters: M.defaults(),
      },
      now,
    );
    times.push(performance.now() - t);
  }
  times.sort((a, b) => a - b);
  console.log(
    JSON.stringify({
      rows: count,
      compileMs: +compileMs.toFixed(3),
      viewP50Ms: +times[50].toFixed(3),
      viewP95Ms: +times[95].toFixed(3),
      viewP99Ms: +times[99].toFixed(3),
      maxDomRowsAt650px: M.virtualRange(count, 0, 650).end,
    }),
  );
}
console.log(
  "Pulse design model:",
  checks,
  "checks passed. No live classifications or exchange requests.",
);
