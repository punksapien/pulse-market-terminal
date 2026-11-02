/* ADR-0025: FICTIONAL, explicitly labelled design fixtures. Not live/calibration data. */
(function (root) {
  "use strict";
  root.createPulseDesignFixture = function (model, now = Date.now()) {
    const source = [
      ["BABYDOGE", 17.502, 1.1e9, "Breakout", 120.21],
      ["DOGE", 0.07215, 669e6, "Uptrend", 60.21],
      ["HYPE", 67.846, 669e6, "Breakout", 110.42],
      ["APE", 0.16289, 669e6, "Range", -1.35],
      ["JASMY", 0.00439, 669e6, "Uptrend", 50.16],
      ["1000PEPE", 0.002615, 669e6, "Downtrend", -99.21],
      ["ARB", 0.08515, 669e6, "Range", 3.72],
      ["BTC", 67284.2, 1.42e9, "Uptrend", 4.21],
      ["ETH", 3512.42, 987e6, "Uptrend", 2.86],
      ["XAU", 2846.1, 93.4e6, "Range", -0.34],
      ["SOL", 172.42, 446e6, "Downtrend", -4.7],
      ["SUI", 0.757, 158e6, "Reversal", -1.02],
      ["ALLO", 0.45141, 446e6, "Uptrend", 5.35],
      ["PIPPIN", 0.02267, 28.8e6, "Uptrend", 16.86],
      ["MOVE", 0.01494, 224e6, "Downtrend", -2.26],
      ["MEW", 0.00036, 2.6e6, "Avoid", -0.56],
    ];
    const rows = source.map(([symbol, price, volume, signal, change], i) => {
      const row = {
        id: "B-" + symbol + "_USDT",
        symbol,
        price,
        volume,
        signal,
        changes: {
          "1H": change / 500,
          "4H": change / 250,
          "24H": change / 100,
        },
        metrics: {},
        history: {},
        outlook: {
          label: "Fade",
          confidence: 0.45,
          description:
            "Mixed signals — low conviction outlook (illustrative sample).",
        },
        signal_since:
          i < 7
            ? {
                state: signal,
                price: [17.11, 0.07, 64.8, 0.16, 0.0041, 0.0029, 0.082][i],
              }
            : null,
        context: {
          interval: i % 3 === 1 ? 4 : i % 3 === 2 ? 1 : 8,
          rsi: 14 + ((i * 7) % 70),
          quantity: 94000 + i * 817,
          topRatio: 1.45 + i * 0.07,
          topShare: 0.61 + i * 0.003,
          longShare: 0.58 + i * 0.004,
          momentum: 0.189 - i * 0.017,
          velocity: i % 2 ? "Decelerating" : "Accelerating",
        },
      };
      model.REGISTRY.forEach((m) => {
        row.history[m.id] = [
          0.44, 0.8, 0.52, 0.62, 0.48, 0.71, 0.59, 0.67, 0.84, 0.73, 0.9, 1,
        ].map(
          (v) =>
            v *
            {
              volatility: 0.1545,
              long_short: 1.8,
              funding: 0.003,
              funding_change: 0.00015,
              open_interest: 0.12,
            }[m.id] *
            (1 + i / 10),
        );
        row.metrics[m.id] = {};
        model.WINDOWS.forEach((w, wi) => {
          const sign = i % 3 === 1 ? -1 : 1;
          const referenceVol = [
            0.6767, 0.5551, 0.8172, 0.1545, 0.3192, 0.7526, 0.1291,
          ];
          const base = {
            volatility: [0.01545, 0.00675, 0.0042, 0.0011][i % 4],
            long_short: i % 6 === 0 ? 1 : [2.16, 0.72, 1.32, 0.96][i % 4],
            funding:
              i === 4
                ? 0
                : sign * [0.00042, 0.00014, 0.000065, 0.0000125][i % 4],
            funding_change:
              i === 4
                ? 0
                : sign * [0.00016, 0.000075, 0.000025, 0.000002][i % 4],
            open_interest:
              i === 4 ? 0 : sign * [0.0841, 0.032, 0.012, 0.0014][i % 4],
          }[m.id];
          let value = ["volatility", "open_interest"].includes(m.id)
            ? base * [1, 1.7, 2.8][wi]
            : base;
          if (i < 7) {
            if (m.id === "volatility")
              value = referenceVol[i] * [0.4, 0.7, 1][wi];
            if (m.id === "long_short")
              value = [1.8, 0.4, 1.5, 1, 0.5, 1.9, 1.1][i];
            if (m.id === "funding")
              value = [30, 20, 40, 15, 25, 35, 15][i] / 10000;
          }
          const direction =
            m.id === "volatility"
              ? m.directions[i % m.directions.length]
              : m.id === "long_short"
                ? value > 1
                  ? "Long dominant"
                  : value < 1
                    ? "Short dominant"
                    : "Balanced"
                : m.id === "funding"
                  ? value > 0
                    ? "Longs pay"
                    : value < 0
                      ? "Shorts pay"
                      : "Zero"
                  : m.id === "funding_change"
                    ? value > 0
                      ? "Rising"
                      : value < 0
                        ? "Falling"
                        : "Unchanged"
                    : value > 0
                      ? "Increasing"
                      : value < 0
                        ? "Decreasing"
                        : "Unchanged";
          row.metrics[m.id][w] = {
            value: i === 15 ? null : value,
            unit: m.unit,
            status: i === 15 ? "unavailable" : "available",
            reason: i === 15 ? "Sample missing-source state" : null,
            asof: now - 30000,
            expires_at: now + 3600000,
            // Design assignments only, not thresholds or live classification.
            level:
              i === 14 && m.id !== "volatility"
                ? null
                : (m.id === "long_short" && value === 1) ||
                    (m.id !== "volatility" && value === 0)
                  ? 1
                  : i < 7
                    ? [3, 2, 3, 1, 2, 3, 1][i]
                    : 4 - (i % 4),
            direction,
            evolution: (i % 2 ? -1 : 1) * [0.013, 0.026, 0.061][wi],
            cumulative1D: value * 3,
            cumulative7D: value * 21,
            delta1D: base * 1.4,
            delta7D: base * 2.1,
            source: m.source,
            definition_version: m.id + ".design-only",
            provisional: true,
          };
        });
      });
      return row;
    });
    return {
      registry_version: model.VERSION,
      snapshot_id: "illustrative-svg-design-v2",
      generated_at: now,
      sample: true,
      rows,
    };
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
