/* ADR-0100: explicit live mode uses passive streaming; default remains the isolated sample. */
(function () {
  "use strict";
  const M = window.PulseModel;
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  // Live mode needs the loader modules and an explicit opt-in; the default page stays sample-only.
  const LIVE = params.get("source") === "live" && Boolean(window.PulseLoader);
  const copy = LIVE ? window.PulseCopy.liveCopy : (s) => s;
  // compact wire (core + the open metric's part) is the default live transport; ?transport=single keeps one document
  // price and price-change are live by default in live mode (project owner, 27 Oct): the price document is polled every
  // 2 s and returns are recomputed from the tick against the generation's ladder (ADR-0088). ?price=off opts out.
  const PRICE = LIVE && params.get("price") !== "off";
  const ticks = PRICE && window.PulseTicks ? window.PulseTicks.create() : null;
  let stream = null, ticksQueued = false, lastTickPaintMs = 0;
  let sortTimer = null, lastLiveSortMs = 0;
  const priceFrame = (r, now) => ticks ? ticks.display(r, now) : r;
  function paintField(node, value, reason, loading = false) {
    const missing=LIVE && value === "—", presentation=missing ? M.missingState(reason,loading) : null;
    text(node,presentation ? presentation.label : value);
    attr(node,"data-availability",presentation ? presentation.kind : "available");
    attr(node,"aria-busy",presentation?.kind === "loading" ? "true" : "false");
    attr(node,"title",presentation?.description || value);
    attr(node,"aria-label",presentation ? presentation.description : value);
  }
  function paintPrice(row, r) {
    const now=Date.now(), live = priceFrame(r, now);
    const change = live.changes[M.changeWindow(state.metric, state.window)];
    paintField(row.price,M.price(live.price),r.price_expires_ms && now >= r.price_expires_ms ? "expired" : "missing");
    if(Number.isFinite(live.price)) property(row.price, "title", row.price.textContent + (LIVE ? " · " + (stream?.state.status === "live" ? "streaming" : "delayed transport") : ""));
    const w=M.changeWindow(state.metric,state.window);
    paintField(row.change,M.percentage(change,true),r.change_expires_ms?.[w] && now>=r.change_expires_ms[w] ? "expired" : "baseline_missing");
    property(row.change, "className", "change-pill" + (!Number.isFinite(change)?" unavailable":change < 0 ? " negative" : change === 0 ? " zero" : ""));
    if (ticks && Number.isInteger(live.rev) && row.liveRevision !== live.rev) {
      ticks.recordPaint(live, Date.now());
      row.liveRevision = live.rev;
    }
  }
  function queueTicks() {
    if (ticksQueued || document.hidden) return;
    ticksQueued = true;
    requestAnimationFrame(() => {
      ticksQueued = false;
      const begin = performance.now();
      for (const row of pool) if (row.record) paintPrice(row,row.record);
      lastTickPaintMs = performance.now()-begin;
      scheduleLiveSort();
      if (detailId && ticks) {
        const r=snapshot.rows.find(r=>r.id===detailId), live=r&&priceFrame(r,Date.now()), target=dialog.querySelector(".detail-price"), pill=dialog.querySelector(".detail-header-change");
        if(live && target) paintField(target,Number.isFinite(live.price)?"$"+M.price(live.price):"—","expired");
        const move=live && live.changes[M.changeWindow(state.metric, state.window)];
        if(pill){ paintField(pill,M.percentage(move,true),"baseline_missing"); pill.style.color=Number.isFinite(move)?"var(--"+(move<0?"red":"green")+")":"var(--muted)"; }
      }
    });
  }
  function scheduleLiveSort() {
    if (!ticks || !["price","change"].includes(state.sort) || sortTimer !== null) return;
    sortTimer = setTimeout(() => {
      sortTimer = null;
      if (document.hidden || !["price","change"].includes(state.sort)) return;
      const next=index.select({...state,filters:state.filters[state.metric]},Date.now(),priceFrame).ids;
      lastLiveSortMs=performance.now();
      if (next.length===ids.length && next.every((v,i)=>v===ids[i])) return;
      ids=next;revision++;renderRows(true);
    }, Math.max(0,250-(performance.now()-lastLiveSortMs)));
  }
  function syncSocket() {
    if (stream) stream.setPairs(document.hidden ? [] : snapshot.rows.map(r=>r.id));
  }
  const WIRE = LIVE && Boolean(window.PulseWire) && params.get("transport") !== "single";
  const PART_OF = { volatility: "volatility", long_short: "ls", funding: "funding", funding_change: "funding_change", open_interest: "oi" };
  const fundingFamily = (id) => id === "funding" || id === "funding_change";
  // funding fields are not per-horizon, but their change column is the 24H return
  const wantedHorizon = () => (fundingFamily(state.metric) ? "24H" : state.window);
  let snapshot = LIVE
    ? { registry_version: M.VERSION, snapshot_id: "loading", generated_at: Date.now(), sample: false, rows: [] }
    : window.createPulseDesignFixture(M);
  const compileStart = performance.now();
  let index = new M.Index(snapshot);
  let compileMs = performance.now() - compileStart;
  const phases = ["ready", "loading", "empty", "error", "stale", "unavailable"];
  const state = {
    metric: "volatility",
    window: "24H",
    level: 0,
    sort: "design",
    descending: true,
    phase: LIVE ? "loading" : phases.includes(params.get("state")) ? params.get("state") : "ready",
    filters: Object.fromEntries(M.REGISTRY.map((m) => [m.id, M.defaults()])),
  };
  let attributionStorage;
  try { attributionStorage=window.localStorage; } catch (_) {}
  const navigation=window.PulseNavigation.create({href:location.href,storage:attributionStorage,
    navigator:window.navigator,navigate:url=>location.assign(url),
    gtag:(...args)=>window.gtag?.(...args),open:(...args)=>window.open(...args)});
  const track=(event,extra={})=>{if(LIVE)navigation.track(event,{...state,...extra});};
  track("view");
  function trade(id,surface){
    if(LIVE&&snapshot.rows.some(r=>r.id===id))navigation.trade(id,surface,state);
  }
  const viewport = $("market-viewport"),
    poolHost = $("row-pool"),
    space = $("virtual-space");
  const dialog = $("pulse-sheet"),
    content = $("sheet-content");
  let ids = [],
    revision = 0,
    paintQueued = false,
    lastPaint = "",
    pool = [],
    draft = null;
  let detailId = null,
    returnFocus = null,
    lastViewMs = 0,
    lastPaintMs = 0;
  // next moment any displayed value expires (metric cells and the live price frame)
  const deadline = (snap, after) => Math.min(M.nextExpiry(snap, after), Number.isFinite(snap.price_expires_at) && snap.price_expires_at > after ? snap.price_expires_at : Infinity);
  let expiry = deadline(snapshot, -Infinity);
  let metricButtons, levelButtons, appliedSignature;
  const windowButtons = document.querySelectorAll("[data-window]");
  const sortButtons = document.querySelectorAll("[data-sort]");
  const gaugePaths = {
    1: "M8 13L4.45 9.45",
    2: "M8 13V8",
    3: "M8 13L11.55 9.45",
    4: "M8 13H13",
  };
  function icon(m, level) {
    return gaugeIcons[level || 1];
  }
  function gaugeIcon(level) {
    const paths =
      '<path d="M1.5 13a6.5 6.5 0 0 1 13 0"/><path d="' +
      gaugePaths[level || 1] +
      '"/><circle cx="8" cy="13" r="1" fill="currentColor" stroke="none"/>';
    return '<svg viewBox="0 0 16 16" aria-hidden="true">' + paths + "</svg>";
  }
  const gaugeIcons = Object.fromEntries(
    [1, 2, 3, 4].map((level) => [level, gaugeIcon(level)]),
  );
  function el(tag, attrs = {}, ...children) {
    const n = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs))
      if (value !== null && value !== undefined)
        n.setAttribute(key, String(value));
    n.append(
      ...children.map((c) =>
        typeof c === "string" ? document.createTextNode(copy(c)) : c,
      ),
    );
    return n;
  }
  const button = (label, attrs = {}) =>
    el("button", { type: "button", ...attrs }, label);
  // ADR-0027: compare without layout reads; preserve unchanged DOM and focus.
  function text(n, value) {
    const next = copy(String(value));
    if (n.textContent !== next) n.textContent = next;
  }
  function attr(n, key, value) {
    const next = copy(String(value));
    if (n.getAttribute(key) !== next) n.setAttribute(key, next);
  }
  function property(n, key, value) {
    if (typeof value === "string") value = copy(value);
    if (n[key] !== value) n[key] = value;
  }
  const selectedMetric = () => M.metric(state.metric);
  function display(cell) {
    return M.effective(cell, Date.now(), state.phase);
  }
  const cellFor = (r, metric = state.metric, w = state.window) =>
    r.metrics[metric][w];
  const shown = (r, id = state.metric, w = state.window) =>
    display(cellFor(r, id, w)) === "unavailable"
      ? LIVE ? M.missingState(cellFor(r,id,w).reason || "expired").label : "—"
      : M.format(id, cellFor(r, id, w).value);
  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    $("preview-theme").value = theme;
    document.querySelector('meta[name="theme-color"]').content =
      theme === "dark" ? "#1F1F1F" : "#ffffff";
  }
  function avatar(symbol) {
    const a = el(
      "span",
      { class: "avatar", "aria-hidden": "true" },
      symbol.slice(0, 1),
    );
    const img = el("img", {
      src:
        "https://coindcx.s3.amazonaws.com/static/coins/" +
        symbol.toLowerCase() +
        ".webp",
      alt: "",
      width: 24,
      height: 24,
      loading: "lazy",
      decoding: "async",
    });
    img.addEventListener(
      "error",
      () => {
        img.hidden = true;
      },
      { once: true },
    );
    a.append(img);
    return a;
  }
  function metricHasLevels(id) {
    const key = id + ":" + state.window;
    const col = index.columns.get(key);
    const now=Date.now();
    return Boolean(col) && col.levels.some((l,i) => l > 0 && col.expiry[i] > now && Number.isFinite(col.values[i]));
  }
  function renderNavigation() {
    const m = selectedMetric();
    if (!metricButtons) {
      metricButtons = M.REGISTRY.map((reg) =>
        button(reg.label, { class: "metric-tab", "data-metric": reg.id }),
      );
      levelButtons = [0, 4, 3, 2, 1].map((level) =>
        button("", { class: "level-tab", "data-level": level }),
      );
      $("metric-tabs").append(...metricButtons);
      $("level-tabs").append(...levelButtons);
    }
    for (const b of metricButtons)
      attr(b, "aria-pressed", b.dataset.metric === state.metric);
    for (const b of levelButtons) {
      const level = Number(b.dataset.level),
        label = level ? m.levels[level - 1] : "All Signals";
      text(b, label);
      attr(b, "aria-pressed", state.level === level);
      attr(b, "aria-label", level ? label + (LIVE ? " level" : " · illustrative level") : label);
      if (LIVE) {
        // levels without approved cut-offs stay empty: the tab is disabled and says why
        const pending = level && !metricHasLevels(m.id);
        property(b, "disabled", Boolean(pending));
        property(b, "title", pending ? "No current classified values; calibration may be pending" : level && m.id !== "volatility" ? "Pilot band, not yet calibrated" : "");
      }
    }
    text($("metric-column"), m.column);
    attr($("metric-sort"), "aria-label", "Sort by " + m.label);
    text($("change-column"), M.changeWindow(m.id, state.window) + " % Chg");
    const labels = M.windowLabels(m.id);
    windowButtons.forEach((b, i) => {
      text(b, labels[i]);
      const active = b.dataset.window === state.window;
      if (b.classList.contains("selected") !== active)
        b.classList.toggle("selected", active);
      attr(b, "aria-pressed", active);
    });
    sortButtons.forEach((b) => {
      attr(b, "aria-pressed", b.dataset.sort === state.sort);
      property(
        b,
        "title",
        b.dataset.sort === state.sort
          ? state.descending
            ? "Sorted descending"
            : "Sorted ascending"
          : "Sort",
      );
    });
    renderAppliedFilters();
  }
  function renderAppliedFilters() {
    const signature =
      state.metric + ":" + JSON.stringify(state.filters[state.metric]);
    if (signature === appliedSignature) return;
    appliedSignature = signature;
    const f = state.filters[state.metric],
      count = M.filterCount(f),
      chips = [];
    $("filter-count").textContent = count;
    $("filter-count").hidden = !count;
    $("open-filters").setAttribute(
      "aria-label",
      "Open filters" + (count ? ", " + count + " applied" : ""),
    );
    const labels = {
      signal: f.signal,
      direction: f.direction,
      volume: "> " + M.compact(f.volume),
      availability: f.availability,
      search: "Search: " + f.search,
      threshold: mFilterLabel("threshold", f.threshold),
      interval: mFilterLabel("interval", f.interval),
    };
    for (const key of Object.keys(f)) {
      if (f[key] === M.defaults()[key]) continue;
      const b = button("", {
        "data-remove": key,
        "aria-label": "Remove " + labels[key] + " filter",
      });
      b.append(
        document.createTextNode(labels[key]),
        el("span", { "aria-hidden": "true" }, "×"),
      );
      chips.push(b);
    }
    $("applied-filters").replaceChildren(...chips);
    $("applied-filters").hidden = !chips.length;
  }
  function mFilterLabel(key, value) {
    return (
      selectedMetric().control.options.find((o) => o[0] === value)?.[1] || value
    );
  }
  function update(resetScroll = false) {
    const start = performance.now();
    renderNavigation();
    ids = index.select(
      { ...state, filters: state.filters[state.metric] },
      Date.now(),
      priceFrame,
    ).ids;
    if (
      state.phase === "empty" ||
      (["stale", "unavailable"].includes(state.phase) && state.level)
    )
      ids = [];
    revision++;
    if (resetScroll) viewport.scrollTop = 0;
    const notice =
      state.phase === "stale"
        ? "Sample delayed-data state · original observation age is retained."
        : state.phase === "unavailable"
          ? "Sample source-unavailable state · missing values are never zero."
          : "";
    $("state-notice").hidden = !notice;
    $("state-notice").textContent = copy(notice);
    $("preview-state").value = state.phase;
    $("pulse-announcement").textContent = copy(
      ids.length +
        " sample markets. " +
        selectedMetric().label +
        ", " +
        state.window +
        ".",
    );
    renderRows(true);
    lastViewMs = performance.now() - start;
  }
  function createRow() {
    const node = el("div", { class: "pulse-row", role: "listitem" });
    // Static trusted template; all fixture/user text is assigned with textContent.
    node.innerHTML =
      '<button class="row-hit" type="button"></button><div class="identity"><div class="identity-top"><span class="avatar-slot"></span><span class="identity-name"></span></div><div class="identity-meta"><span class="volume"></span><span class="trend-tag"></span></div></div><div class="row-metric"><span class="row-price"></span><span class="metric-value"><span class="metric-icon"></span><span class="metric-number"></span><span class="stale-indicator" hidden></span></span></div><div class="row-change"><span class="change-pill"></span></div><button class="row-trade" type="button" disabled title="Trading is disabled for sample data" aria-label="Trade unavailable in design preview"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 6.37h11.74m-3.91-3.91 3.91 3.91M2.26 9.63H14M2.26 9.63l3.91 3.91"/></svg></button>';
    const find = (cls) => node.querySelector("." + cls);
    return {
      node,
      hit: find("row-hit"),
      trade: find("row-trade"),
      avatar: find("avatar-slot"),
      name: find("identity-name"),
      volume: find("volume"),
      trend: find("trend-tag"),
      price: find("row-price"),
      number: find("metric-number"),
      value: find("metric-value"),
      icon: find("metric-icon"),
      stale: find("stale-indicator"),
      change: find("change-pill"),
      symbol: null,
    };
  }
  function patch(row, r, position) {
    const m = selectedMetric(),
      cell = cellFor(r),
      st = display(cell);
    const key = [r.id, m.id, state.window, st, ids.length, position].join(":");
    if (row.paintKey === key && row.record === r) return;
    row.paintKey = key;
    row.record = r;
    attr(row.node, "aria-setsize", ids.length);
    attr(row.node, "aria-posinset", position + 1);
    attr(row.hit, "data-pair", r.id);
    row.trade.disabled=!LIVE;
    attr(row.trade,"data-trade",r.id);
    attr(row.trade,"title",LIVE?"Open CoinDCX futures market in a new tab":"Trading is disabled for sample data");
    attr(row.trade,"aria-label",LIVE?"Trade "+r.symbol+" on CoinDCX (opens new tab)":"Trade unavailable in design preview");
    attr(
      row.hit,
      "aria-label",
      "View " + r.symbol + " " + m.label + " details",
    );
    if (row.symbol !== r.symbol) {
      row.avatar.replaceChildren(avatar(r.symbol));
      row.symbol = r.symbol;
    }
    text(row.name, r.symbol);
    paintField(row.volume,M.compact(r.volume),r.volume_expires_ms && Date.now()>=r.volume_expires_ms ? "expired" : "missing");
    text(row.volume,"Vol "+row.volume.textContent);
    text(row.trend, r.signal);
    attr(row.trend, "data-trend", r.signal);
    paintPrice(row, r);
    const label = st === "unavailable" ? "—" : M.format(m.id, cell.value);
    paintField(row.number,label,cell.reason || "expired",state.phase==="loading");
    const markup =
      st === "unavailable" || !cell.level ? "" : icon(m, cell.level);
    if (row.iconMarkup !== markup) {
      row.icon.innerHTML = markup;
      row.iconMarkup = markup;
    }
    const color =
      st === "unavailable"
        ? "var(--muted)"
        : M.COLORS[cell.level] || "var(--muted)";
    if (row.color !== color) {
      row.value.style.color = color;
      row.color = color;
    }
    const compact = label.length > 8;
    if (row.value.classList.contains("metric-compact") !== compact)
      row.value.classList.toggle("metric-compact", compact);
    property(
      row.value,
      "title",
      st === "unavailable"
        ? LIVE ? M.missingState(cell.reason || "expired").description : cell.reason || "Sample source unavailable"
        : m.headline +
            ": " +
            label +
            (st === "stale" ? " · stale" : " · sample"),
    );
    property(row.stale, "hidden", st !== "stale");
  }
  const emptyArt =
    '<svg viewBox="0 0 110 80" aria-hidden="true"><rect x="21" y="9" width="70" height="23" rx="6" fill="#e5ebff"/><circle cx="33" cy="20" r="5" fill="#9cadff"/><path d="M44 17h12m-12 5h22" stroke="#9cadff" stroke-width="2" stroke-linecap="round"/><rect x="73" y="15" width="13" height="11" rx="3" fill="#ccd7ff"/><rect x="7" y="43" width="96" height="28" rx="7" fill="#e5ebff"/><circle cx="23" cy="57" r="7" fill="#6781ff"/><path d="M36 53h13m-13 7h27" stroke="#6781ff" stroke-width="2.5" stroke-linecap="round"/><rect x="76" y="50" width="20" height="14" rx="3" fill="#b4c3ff"/></svg>';
  const errorArt =
    '<svg viewBox="0 0 110 80" aria-hidden="true"><path d="M0 46h22m66 0h22" stroke="#e5ebff" stroke-width="10"/><rect x="19" y="28" width="24" height="37" rx="7" fill="#c9d7ff"/><rect x="28" y="25" width="16" height="42" rx="3" fill="#e5ebff"/><path d="M44 36h10m-10 20h10" stroke="#6781ff" stroke-width="4" stroke-linecap="round"/><rect x="68" y="28" width="24" height="37" rx="7" fill="#d5e0ff"/><rect x="64" y="25" width="8" height="42" rx="2" fill="#e5ebff"/><path d="m49 15 3-7m5 9 5-6m0 11 6-4" stroke="#9cadff" stroke-width="2" stroke-linecap="round"/></svg>';
  function renderState() {
    const host = $("market-state");
    host.replaceChildren();
    if (state.phase === "loading") {
      host.className = "";
      host.setAttribute("role", "status");
      host.setAttribute("aria-label", "Loading sample markets");
      for (let i = 0; i < 8; i++)
        host.append(
          el(
            "div",
            { class: "skeleton-row", "aria-hidden": "true" },
            el("span"),
            el("span"),
            el("span"),
          ),
        );
      host.style.paddingTop = "12px";
      return;
    }
    host.className = "market-state";
    host.style.paddingTop = "";
    host.setAttribute("role", state.phase === "error" ? "alert" : "status");
    host.removeAttribute("aria-label");
    const art = el("div");
    art.innerHTML = state.phase === "error" ? errorArt : emptyArt;
    host.append(
      art,
      el(
        "h2",
        {},
        state.phase === "error"
          ? "Failed to load the section"
          : "No opportunities found",
      ),
    );
    if (state.phase === "error") {
      const retry = button("Retry", {
        class: "retry-button",
        id: "retry-sample",
      });
      retry.onclick = () => {
        track("retry");
        if (LIVE) return loader.wake();
        state.phase = "ready";
        update();
      };
      host.append(retry);
    } else
      host.append(
        el("p", {}, "No tokens found for the selected time frame."),
        el("p", {}, "Try adjusting your filters."),
      );
  }
  function renderRows(force = false) {
    const begin = performance.now();
    const showState = ["loading", "error"].includes(state.phase) || !ids.length;
    space.hidden = showState;
    $("market-state").hidden = !showState;
    if (showState) {
      if (force) renderState();
      poolHost.replaceChildren();
      pool = [];
      lastPaint = "";
      return;
    }
    const range = M.virtualRange(
      ids.length,
      viewport.scrollTop,
      viewport.clientHeight,
    );
    const signature = [revision, range.start, range.end].join(":");
    if (!force && signature === lastPaint) return;
    lastPaint = signature;
    property(space.style, "height", range.height + "px");
    property(poolHost.style, "transform", "translateY(" + range.offset + "px)");
    const needed = range.end - range.start;
    while (pool.length < needed) {
      const row = createRow();
      pool.push(row);
      poolHost.append(row.node);
    }
    while (pool.length > needed) pool.pop().node.remove();
    pool.forEach((row, i) =>
      patch(row, snapshot.rows[ids[range.start + i]], range.start + i),
    );
    lastPaintMs = performance.now() - begin;
    syncSocket();
  }
  function schedulePaint() {
    if (paintQueued) return;
    paintQueued = true;
    requestAnimationFrame(() => {
      paintQueued = false;
      renderRows();
    });
  }
  function choiceGroup(title, key, options) {
    const field = el(
      "fieldset",
      { class: "filter-section" },
      el("legend", {}, title),
    );
    const group = el("div", { class: "choice-group" });
    for (const [value, label] of options)
      group.append(
        button(label, {
          class: "choice",
          "data-filter-key": key,
          "data-value": value,
          "aria-pressed": String(draft[key]) === String(value),
        }),
      );
    field.append(group);
    return field;
  }
  function refreshDraftButtons() {
    content
      .querySelectorAll("[data-filter-key]")
      .forEach((b) =>
        b.setAttribute(
          "aria-pressed",
          String(draft[b.dataset.filterKey]) === b.dataset.value,
        ),
      );
    $("reset-filters").disabled = !M.filterCount(draft);
  }
  function openFilters() {
    track("filter_open");
    detailId = null;
    draft = { ...state.filters[state.metric] };
    returnFocus = document.activeElement;
    $("preview-controls").open = false;
    $("sheet-title").textContent = "Filters";
    dialog.dataset.kind = "filters";
    dialog.dataset.sheetMetric = state.metric;
    $("filter-footer").hidden = false;
    $("detail-footer").hidden = true;
    const m = selectedMetric();
    content.replaceChildren(
      choiceGroup(
        m.control.title +
          (m.id === "open_interest" ? " (" + state.window + " % chg)" : ""),
        m.control.key,
        m.control.options,
      ),
      choiceGroup("Trend", "signal", [
        ["all", "All Trends"],
        ...["Uptrend", "Downtrend", "Breakout", "Range", "Reversal"].map(
          (s) => [s, s],
        ),
      ]),
      choiceGroup("Minimum Volume", "volume", [
        [0, "Any"],
        [1e6, "> 1M"],
        [5e6, "> 5M"],
        [10e6, "> 10M"],
        [50e6, "> 50M"],
        [100e6, "> 100M"],
      ]),
    );
    refreshDraftButtons();
    content.scrollTop = 0;
    dialog.showModal();
  }
  const metricBox = (label, value) =>
    el(
      "div",
      { class: "detail-metric" },
      el("span", {}, label),
      el(
        "strong",
        {
          style:
            "color:" +
            (value === "—"
              ? "var(--muted)"
              : label === "RSI-14 momentum"
                ? "var(--blue)"
                : label === "Volatility momentum"
                  ? "var(--red)"
                  : label === "Volatility velocity"
                    ? "var(--green)"
                    : "var(--ink)"),
        },
        String(LIVE && value === "—" ? M.missingState("missing").label : value),
      ),
    );
  const finite = (v) => typeof v === "number" && Number.isFinite(v);
  function contextFields(r) {
    const c = r.context,
      dash = "—",
      volume = finite(r.volume) ? "$" + M.compact(r.volume) : dash,
      oiChange = shown(r, "open_interest"),
      current = cellFor(r, "funding", "1H"),
      fundingNow = shown(r, "funding", "1H"),
      interval = c.interval == null ? dash : c.interval + "H";
    switch (state.metric) {
      case "volatility":
        return [
          ["OI change · " + state.window, oiChange],
          ["Funding (" + interval + ")", fundingNow],
          ["RSI-14 momentum", finite(Number(c.rsi)) && c.rsi != null ? Number(c.rsi).toFixed(2) : dash],
          ["Volatility momentum", finite(c.momentum) ? M.percentage(c.momentum, true) : dash],
          ["Volatility velocity", c.velocity || dash],
          ["24H dollar volume", volume],
        ];
      case "long_short":
        return [
          ["Top accounts L/S", finite(c.topRatio) ? M.format("long_short", c.topRatio) : dash],
          ["Global long accounts · 5m", finite(c.longShare) ? M.percentage(c.longShare) : dash],
          // ADR-0022: the backend divergence uses a common source bucket.
          ["Top/global share gap", finite(c.shareGapPp) ? M.percentage(c.shareGapPp / 100, true, 2, " pp") : dash],
          ["OI change · " + state.window, oiChange],
          ["Funding (" + interval + ")", fundingNow],
          ["24H dollar volume", volume],
        ];
      case "funding":
        return [
          ["Native interval", interval],
          ["Next settlement", settlementTime(r)],
          ["Last settled rate", settledRate(r, (v) => M.percentage(v, false, 4))],
          ["8H-equivalent estimate", finite(current.value) && c.interval ? M.percentage((current.value * 8) / c.interval, false, 4) : dash],
          ["OI change · " + state.window, oiChange],
          ["24H dollar volume", volume],
        ];
      case "funding_change": {
        const delta = cellFor(r, "funding_change", "1H").current_delta;
        const bps = delta && delta.status === "available" && delta.bps != null ? (String(delta.bps).startsWith("-") ? "" : "+") + delta.bps + " bps" : dash;
        return [
          ["Live estimate", fundingNow],
          ["Last settled rate", settledRate(r, (v) => M.format("funding", v))],
          ["Current delta", bps],
          ["Next settlement", settlementTime(r)],
          ["Native interval", interval],
          ["24H dollar volume", volume],
        ];
      }
      default:
        return [
          ["OI quantity · symbol units", finite(c.quantity) ? M.compact(c.quantity) : dash],
          ["Price change · " + state.window, finite(r.changes[state.window]) ? M.percentage(r.changes[state.window], true) : dash],
          ["Global accounts L/S", shown(r, "long_short")],
          ["Funding (" + interval + ")", fundingNow],
          ["24H dollar volume", volume],
          ["Baseline timing error", baselineError(r)],
        ];
    }
  }
  // Live-mode detail values come from the adapter's real fields, never from derived stand-ins.
  function settlementTime(r) {
    const nft = r.funding_meta && r.funding_meta.nft;
    return Number.isSafeInteger(nft) ? new Date(nft).toISOString().slice(11, 16) + " UTC" : "—";
  }
  function settledRate(r, render) {
    const meta = r.funding_meta;
    const v = meta && meta.status === "available" ? Number(meta.settled_rate) : NaN;
    return Number.isFinite(v) && Date.now() < meta.nft ? render(v) : "Awaiting verification";
  }
  function baselineError(r) {
    const c = r.metrics.open_interest[state.window];
    return M.numeric(c && c.baseline_error_ms) ? Math.round(c.baseline_error_ms / 1000) + " s · " + (c.mode || "") : "—";
  }
  function breakdown(r, m, cell) {
    const valid = display(cell) !== "unavailable";
    let values;
    if (m.id === "volatility" || m.id === "open_interest")
      values = M.WINDOWS.map((w) => shown(r, m.id, w));
    else if (m.id === "long_short")
      values = M.WINDOWS.map((w) =>
        display(cellFor(r, m.id, w)) === "unavailable"
          ? "—"
          : M.percentage(cellFor(r, m.id, w).evolution, true, 2, " pp"),
      );
    else if (m.id === "funding")
      values = [
        shown(r, "funding", "1H"),
        valid ? M.format("funding", cell.cumulative1D) : "—",
        valid ? M.format("funding", cell.cumulative7D) : "—",
      ];
    else
      values = [
        shown(r, m.id, "1H"),
        valid ? M.format(m.id, cell.delta1D) : "—",
        valid ? M.format(m.id, cell.delta7D) : "—",
      ];
    return el(
      "div",
      { class: "breakdown-grid" },
      ...(m.id === "funding"
        ? ["Current", "1D Funding", "7D Funding"]
        : m.breakdown
      ).map((label, i) =>
        el(
          "div",
          {
            class: "breakdown-item",
            "data-active": M.WINDOWS[i] === state.window,
          },
          el("span", {}, label),
          el("strong", {}, values[i]),
        ),
      ),
    );
  }
  let historyClient = null;
  const historyRequests = new Map();
  const historyErrors = new Set();
  function historyBlock(r, m) {
    const slot = M.WINDOWS.indexOf(state.window);
    const period = ["funding", "funding_change"].includes(m.id)
      ? ["Current", "1D", "7D"][slot]
      : state.window;
    let retained = null;
    if (LIVE && historyClient) {
      const id = index.byId.get(r.id);
      const meta = loader.state.wire && loader.state.wire.universe[id];
      if (meta) {
        const saved = historyClient.peek(meta.id, snapshot.mapping_version);
        if (saved && saved.pair_id === meta.pair_id) retained = m.id === "volatility" ? saved.series.volatility[state.window] : saved.series[m.id];
        const key = snapshot.mapping_version + ":" + meta.id;
        if (!saved && (!historyRequests.has(key) || Date.now() - historyRequests.get(key) > 10000)) {
          historyRequests.set(key, Date.now());
          historyErrors.delete(key);
          historyClient.load(meta.id, snapshot.mapping_version).then(() => {
            detailSig = ""; refreshDetail();
          }).catch(() => {historyErrors.add(key);detailSig="";refreshDetail();});
        }
      }
    }
    const block = el(
      "section",
      { class: "detail-block" },
      el("h3", {}, retained ? retained.label : period + " " + m.label + " history"),
    );
    const cell = cellFor(r);
    if (!LIVE && display(cell) === "unavailable") {
      block.append(
        el("p", { class: "detail-context" }, "History unavailable."),
      );
      return block;
    }
    const values = LIVE ? retained && retained.points.map(p => Number(p[1])) : r.history[m.id];
    if (LIVE && (!Array.isArray(values) || values.length < 2)) {
      const failed=historyErrors.has(snapshot.mapping_version+":"+r.wire_id);
      block.append(el("p", { class: "detail-context" }, retained ? "Not enough verified history yet." : failed ? "History temporarily unavailable. Retrying…" : "Loading retained history…"));
      return block;
    }
    const low = Math.min(...values),
      high = Math.max(...values),
      range = high - low || 1;
    const points = values
      .map(
        (v, i) =>
          12 +
          (retained ? 306 * (retained.points[i][0] - retained.points[0][0]) / (retained.points.at(-1)[0] - retained.points[0][0]) : (i * 306) / (values.length - 1)) +
          "," +
          (112 - ((v - low) * 88) / range),
      )
      .join(" ");
    const frame = el("div", { class: "history-chart" });
    // Live points come only from the separately validated retained-history document.
    frame.innerHTML =
      '<svg viewBox="0 0 330 140" role="img" aria-label="' + (LIVE ? "Retained market history" : copy("Illustrative sample history")) + '"><path d="M12 24H318M12 68H318M12 112H318" stroke="var(--line)" fill="none"/><polyline points="' +
      points +
      '" fill="none" stroke="' +
      (M.COLORS[cell.level] || "var(--blue)") +
      '" stroke-width="1.5" stroke-linejoin="round"/></svg>';
    frame.append(
      el(
        "div",
        { class: "chart-axis" },
        el("span", {}, retained && retained.unit === "quantity" ? M.compact(low) : M.format(m.id, low)),
        el("span", {}, retained && retained.unit === "quantity" ? M.compact(high) : M.format(m.id, high)),
      ),
    );
    block.append(
      frame,
      el(
        "p",
        { class: "source-line" },
        LIVE ? new Date(retained.points[0][0]).toISOString().slice(5,16).replace("T"," ") + " — " + new Date(retained.points.at(-1)[0]).toISOString().slice(5,16).replace("T"," ") + " UTC" : "Illustrative sample series · not exchange history",
      ),
    );
    return block;
  }
  // A background refresh re-renders the open sheet; the reader's scroll position and focus must survive it.
  let detailSig = "";
  function refreshDetail() {
    if (!detailId || !dialog.open) return;
    const r = snapshot.rows[index.byId.get(detailId)];
    if (!r) return;
    const sig = state.metric + "|" + state.window + "|" + JSON.stringify(r);
    if (sig === detailSig) return;
    openDetail(detailId);
  }
  function openDetail(id) {
    const r = snapshot.rows[index.byId.get(id)];
    if (!r) return;
    const opening=detailId!==id||!dialog.open;
    const keepScroll = detailId === id && dialog.open ? content.scrollTop : 0;
    detailSig = state.metric + "|" + state.window + "|" + JSON.stringify(r);
    if (!keepScroll) returnFocus = document.activeElement;
    detailId = id;
    if(opening)track("detail_open",{pair:id});
    $("pulse-trade").disabled=!LIVE;
    $("pulse-trade").querySelector(".sr-only").textContent=LIVE?"Open CoinDCX futures market in a new tab":"Unavailable in design preview";
    if (LIVE && WIRE && loader) {
      const needed=M.detailParts(state.metric);
      if(needed.some(n=>!loader.state.parts.wanted.includes(n)))loader.ensureParts(needed);
    }
    draft = null;
    dialog.dataset.kind = "detail";
    dialog.dataset.sheetMetric = state.metric;
    $("preview-controls").open = false;
    $("filter-footer").hidden = true;
    $("detail-footer").hidden = false;
    const m = selectedMetric(),
      // the sheet's headline is the Current / Last value; 1D and 7D appear in its breakdown
      sheetWindow = ["funding", "funding_change"].includes(m.id) ? "1H" : state.window,
      cell = cellFor(r, m.id, sheetWindow),
      st = display(cell),
      liveNow = priceFrame(r, Date.now()),
      headline = {
        price: liveNow ? liveNow.price : r.price,
        change: (liveNow ? liveNow.changes : r.changes)[M.changeWindow(m.id, state.window)],
      };
    $("sheet-title").replaceChildren(
      el(
        "span",
        { class: "detail-identity" },
        avatar(r.symbol),
        el("span", { class: "detail-symbol" }, r.symbol),
      ),
      el("span", { class: "detail-price" }, Number.isFinite(headline.price)?"$"+M.price(headline.price):LIVE?M.missingState("expired").label:"—"),
      el(
        "span",
        {
          class: "detail-header-change",
          style:
            "color:var(--" +
            (!Number.isFinite(headline.change) ? "muted" : headline.change < 0 ? "red" : "green") +
            ")",
        },
        M.percentage(headline.change, true),
      ),
    );
    paintField(dialog.querySelector('.detail-price'),Number.isFinite(headline.price)?'$'+M.price(headline.price):'—','expired');
    paintField(dialog.querySelector('.detail-header-change'),M.percentage(headline.change,true),'baseline_missing');
    const heading = el("div", { class: "detail-headline" });
    heading.style.color =
      st === "unavailable"
        ? "var(--muted)"
        : M.COLORS[cell.level] || "var(--muted)";
    const meter = el("span");
    meter.innerHTML =
      st === "unavailable" || !cell.level ? "" : icon(m, cell.level);
    heading.append(
      meter,
      el("strong", { class: "large-value" }, shown(r, m.id, sheetWindow)),
      ...(m.id === "volatility"
        ? [
            el(
              "span",
              { class: "level-pill" },
              st === "unavailable" ? "Unavailable" : cell.direction,
            ),
          ]
        : []),
    );
    if (st === "stale")
      heading.append(
        el(
          "span",
          { class: "detail-status", role: "status" },
          "Delayed sample",
        ),
      );
    const overview = el(
      "section",
      { class: "detail-overview" },
      el("p", { class: "detail-label" }, m.headline),
      heading,
      breakdown(r, m, cell),
    );
    const fields = el(
      "section",
      { class: "detail-block" },
      el("h3", {}, "Market metrics"),
      el(
        "div",
        { class: "detail-metrics" },
        ...contextFields(r).map(([k, v]) => metricBox(k, v)),
      ),
    );
    const history = historyBlock(r, m);
    const isVol = m.id === "volatility" && st !== "unavailable";
    const outlook = el(
      "section",
      { class: "detail-block" },
      el(
        "div",
        { class: "outlook-heading" },
        el("h3", {}, "Volatility outlook"),
        el("span", {}, isVol ? r.outlook.label + " · " + Math.round(r.outlook.confidence * 100) + "%" : ""),
      ),
      el("p", { class: "detail-context" }, isVol ? r.outlook.description : ""),
    );
    if (isVol)
      outlook.append(
        el(
          "div",
          { class: "confidence-track" },
          el("span", {
            style: "width:" + r.outlook.confidence * 100 + "%;background:" + M.COLORS[cell.level],
          }),
        ),
      );
    content.replaceChildren(overview, fields, history, ...(isVol && r.outlook.label ? [outlook] : []));
    content.scrollTop = keepScroll;
    if (!dialog.open) dialog.showModal();
  }
  const GUIDE = [
    ["The five tabs", [
      ["Volatility", "Price dispersion over 1H, 4H or 24H, with its direction. It measures size of moves, not where price goes."],
      ["L/S Ratio", "Binance global accounts, net-long divided by net-short. It counts accounts, not position size."],
      ["Funding", "Current estimated rate per native interval. 1D and 7D are the funding paid over those periods."],
      ["Funding Chg", "Latest settled rate minus the one before, in bps. 1D and 7D compare further back."],
      ["Open Interest", "Change in open contracts over the selected window. Quantity-based, so price moves are removed."],
    ]],
    ["Levels", [
      ["Four bands", "Chips under the tabs rank markets from the lowest to the highest band of that tab. Volatility uses established regime rules. Funding, Funding Chg, L/S and OI are pilot bands, not yet calibrated."],
    ]],
    ["Using it", [
      ["Window", "Switch 1H, 4H or 24H at the bottom (Current, 1D, 7D on the funding tabs)."],
      ["Sort and filter", "Tap a column header to sort. The filter icon narrows by direction, trend and volume."],
      ["Details", "Tap a row for every figure of that market, and its history chart when history is available."],
    ]],
    ["Live data", [
      ["Freshness", "Price and % change stream live. Other metrics update on their own schedule. Loading means a part is downloading; Updating means the last observation expired; Pending means verification or history is missing; No data means no verified value is available. Missing values are never estimates or zero."],
    ]],
  ];
  function openGuide() {
    track("guide_open");
    detailId = null;
    returnFocus = document.activeElement;
    dialog.dataset.kind = "guide";
    $("sheet-title").textContent = "How to use";
    $("filter-footer").hidden = true;
    $("detail-footer").hidden = true;
    content.replaceChildren(
      el(
        "div",
        { class: "guide" },
        el("p", { class: "guide-intro" }, "Pulse screens CoinDCX USDT perpetual futures: one row per market, one tab per metric."),
        ...GUIDE.map(([heading, items]) =>
          el(
            "section",
            { class: "guide-section" },
            el("h3", {}, heading),
            ...items.map(([label, body]) => el("div", { class: "insight-row" }, el("strong", {}, label), el("p", {}, body))),
          ),
        ),
        el("p", { class: "guide-note" }, LIVE?"Statistical estimates. Not financial advice. Trade opens CoinDCX; Pulse does not place orders.":"Statistical estimates for information only. Not financial advice. Trading is disabled in this preview."),
      ),
    );
    content.scrollTop = 0;
    dialog.showModal();
  }
  $("metric-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-metric]");
    if (!b) return;
    state.metric = b.dataset.metric;
    track("metric_change");
    state.level = 0;
    state.sort = "design";
    state.descending = true;
    if (LIVE) ensureForView();
    update(true);
    const active = document.querySelector(
      '[data-metric="' + state.metric + '"]',
    );
    active.focus({ preventScroll: true });
    active.scrollIntoView({ inline: "nearest", block: "nearest" });
  });
  $("level-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-level]");
    if (b) {
      state.level = Number(b.dataset.level);
    track("level_change");
      update(true);
      document
        .querySelector('[data-level="' + state.level + '"]')
        .focus({ preventScroll: true });
    }
  });
  $("window-control").addEventListener("click", (e) => {
    const b = e.target.closest("[data-window]");
    if (b) {
      state.window = b.dataset.window;
      track("window_change");
      // one cacheable snapshot per horizon (contract); the metric windows are all inside each snapshot
      if (LIVE) ensureForView();
      update(true);
    }
  });
  document
    .querySelector(".pulse-table-header")
    .addEventListener("click", (e) => {
      const b = e.target.closest("[data-sort]");
      if (!b) return;
      state.descending =
        state.sort === b.dataset.sort
          ? !state.descending
          : b.dataset.sort !== "symbol";
      state.sort = b.dataset.sort;
      track("sort_change");
      update(true);
    });
  $("open-filters").onclick = openFilters;
  $("close-sheet").onclick = () => dialog.close();
  dialog.addEventListener("click", (e) => {
    if (
      e.target === dialog &&
      (e.clientX < dialog.getBoundingClientRect().left ||
        e.clientX > dialog.getBoundingClientRect().right ||
        e.clientY < dialog.getBoundingClientRect().top)
    )
      dialog.close();
  });
  dialog.addEventListener("close", () => {
    if(detailId)track("detail_close",{pair:detailId});
    detailId = null;
    draft = null;
    if(LIVE && WIRE && loader)ensureForView();
    if (returnFocus?.isConnected) returnFocus.focus();
    else document.querySelector('[data-metric="' + state.metric + '"]').focus();
  });
  content.addEventListener("click", (e) => {
    const b = e.target.closest("[data-filter-key]");
    if (b && draft) {
      draft[b.dataset.filterKey] =
        b.dataset.filterKey === "volume"
          ? Number(b.dataset.value)
          : b.dataset.value;
      refreshDraftButtons();
    }
  });
  $("reset-filters").onclick = () => {
    track("filter_reset");
    draft = M.defaults();
    refreshDraftButtons();
  };
  $("apply-filters").onclick = () => {
    track("filter_apply");
    state.filters[state.metric] = { ...draft };
    dialog.close();
    update(true);
  };
  $("applied-filters").addEventListener("click", (e) => {
    const b = e.target.closest("[data-remove]");
    if (b) {
      state.filters[state.metric][b.dataset.remove] =
        M.defaults()[b.dataset.remove];
      track("filter_remove");
      update(true);
    }
  });
  poolHost.addEventListener("click", (e) => {
    const c=e.target.closest("[data-trade]");
    if(c){trade(c.dataset.trade,"market_list");return;}
    const b = e.target.closest("[data-pair]");
    if (b) openDetail(b.dataset.pair);
  });
  $("pulse-trade").onclick=()=>trade(detailId,"detail_cta");
  viewport.addEventListener("scroll", schedulePaint, { passive: true });
  const resize = new ResizeObserver(() => {
    lastPaint = "";
    schedulePaint();
  });
  resize.observe(viewport);
  $("preview-state").onchange = (e) => {
    state.phase = e.target.value;
    state.level = 0;
    $("preview-controls").open = false;
    update(true);
  };
  $("preview-theme").onchange = (e) => setTheme(e.target.value);
  for (const nav of [$("metric-tabs"), $("level-tabs"), $("window-control")])
    nav.addEventListener("keydown", (e) => {
      if (
        !["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key) ||
        !e.target.matches("button")
      )
        return;
      e.preventDefault();
      const buttons = Array.from(nav.querySelectorAll("button")),
        at = buttons.indexOf(e.target);
      const next =
        e.key === "Home"
          ? 0
          : e.key === "End"
            ? buttons.length - 1
            : (at + (e.key === "ArrowRight" ? 1 : -1) + buttons.length) %
              buttons.length;
      const key =
        buttons[next].dataset.metric ||
        buttons[next].dataset.level ||
        buttons[next].dataset.window;
      buttons[next].click();
      Array.from(nav.querySelectorAll("button"))
        .find(
          (b) =>
            (b.dataset.metric || b.dataset.level || b.dataset.window) === key,
        )
        ?.focus();
    });
  const timer = setInterval(() => {
    if (document.hidden) return;
    if (ticks) queueTicks();
    if (Date.now() < expiry) return;
    const now = Date.now();
    if (LIVE) loader.tick();
    expiry = deadline(snapshot, now);
    update();
    refreshDetail();
  }, 1000);
  document.addEventListener("visibilitychange", () => {
    syncSocket();
    if (!document.hidden) {
      if (LIVE) loader.wake();
      update();
      refreshDetail();
    }
  });
  setTheme(params.has("dark") ? "dark" : "light");
  let loader = null;
  // the open view needs: the horizon's core, this metric's part (compact transport), nothing else
  function ensureForView() {
    const horizon = wantedHorizon();
    const part = WIRE ? [PART_OF[state.metric]] : undefined;
    if (horizon !== loader.state.horizon) loader.setHorizon(horizon, part);
    else if (WIRE) loader.ensureParts(part);
  }
  if (LIVE) {
    const endpoints = window.PULSE_ENDPOINTS || {};
    if (ticks && window.PulseBridge) stream = window.PulseBridge.create({
      WebSocketImpl:window.WebSocket, now:()=>Date.now(),
      setTimer:(fn,ms)=>window.setTimeout(fn,ms), clearTimer:id=>window.clearTimeout(id),
      url:"wss://"+location.host+"/api/pulse_stream", mapping:()=>snapshot.mapping_version,
      onTick:(pair,tick)=>{if(ticks.ingest(pair,tick,Date.now()))queueTicks();},
      onStatus:()=>queueTicks(),
    });
    historyClient = window.PulseHistory.create({fetch:window.fetch.bind(window),now:Date.now,
      url:id=>"/api/pulse?history="+id,digest:v=>window.PulseWire.digest(v,crypto.subtle)});
    loader = window.PulseLoader.create({
      fetch: window.fetch.bind(window),
      now: () => Date.now(),
      setTimer: (fn, ms) => setTimeout(fn, ms),
      clearTimer: (id) => clearTimeout(id),
      isVisible: () => !document.hidden,
      ...(WIRE
        ? {
            wire: window.PulseWire,
            digest: (v) => window.PulseWire.digest(v, crypto.subtle),
            artifactUrl: endpoints.artifact || ((h) => "/api/pulse?artifact=" + encodeURIComponent(h)),
          }
        : {}),
      snapshotUrl: endpoints.snapshot || ((h) => "/api/pulse?horizon=" + h),
      liveUrl: endpoints.live || (() => "/api/pulse?live=1"),
      livePriceUrl: endpoints.price || (() => "/api/pulse?live=price"),
      ...(ticks ? {priceSink:(doc,ls)=>{
        if(!ls.snapshot||!ls.wire)return 0;
        ticks.setFrame(ls.snapshot,ls.wire,Date.now());
        const changed=ticks.ingestDocument(doc,ls.wire.universe.map(u=>u.pair_id),Date.now());
        if(changed.length)queueTicks();
        return changed.length;
      }} : {}),
    });
    loader.subscribe((ls) => {
      // a tab whose part is still downloading shows the loading state, never "unavailable" values
      state.phase =
        WIRE && ls.phase !== "error" && ls.phase !== "empty" && !ls.parts.loaded.includes(PART_OF[state.metric]) ? "loading" : ls.phase;
      if (ls.snapshot && ls.snapshot !== snapshot) {
        const begin = performance.now();
        snapshot = ls.snapshot;
        index = new M.Index(snapshot);
        compileMs = performance.now() - begin;
        expiry = deadline(snapshot, -Infinity);
      }
      if(ticks && ls.snapshot && ls.wire) ticks.setFrame(ls.snapshot,ls.wire,Date.now());
      update();
      refreshDetail();
    });
    // live: the info icon opens the guide; the design-review popover is for fixture mode only
    $("preview-controls").querySelector("summary").addEventListener("click", (e) => {
      e.preventDefault();
      openGuide();
    });
    $("preview-controls").querySelector("summary").setAttribute("aria-label", "How to use");
    // the review popover stays in the DOM (its controls are wired) but is never shown live; its copy is replaced anyway
    $("preview-controls").querySelector("strong").textContent = "Live preview · staging data";
    $("preview-controls").querySelector("p").textContent = "Staging data from the Pulse backend. Trading is disabled.";
    $("preview-state").closest("label").hidden = true;
    for (const id of ["timeframe-risk-disclaimer", "detail-risk-disclaimer"])
      if ($(id)) $(id).textContent = copy($(id).textContent.trim());
    loader.start(wantedHorizon(), WIRE ? [PART_OF[state.metric]] : undefined);
    if (PRICE) loader.setPriceOverlay(true);
  }
  update();
  window.PulseDesign = Object.freeze({
    diagnostics: () => ({
      sample: !LIVE,
      live: LIVE ? loader.state.live : null,
      price: LIVE ? loader.state.price : null,
      socket: stream ? stream.state : null,
      ticks: ticks ? ticks.latency() : null,
      lastTickPaintMs,
      snapshot: snapshot.snapshot_id,
      metric: state.metric,
      window: state.window,
      phase: state.phase,
      pendingParts: LIVE && loader ? loader.state.parts.pending.slice() : [],
      visibleRows: pool.length,
      totalRows: snapshot.rows.length,
      filteredRows: ids.length,
      compileMs,
      lastViewMs,
      lastPaintMs,
      typedArrayBytes: Array.from(index.columns.values()).reduce(
        (n, c) =>
          n + c.values.byteLength + c.levels.byteLength + c.expiry.byteLength,
        0,
      ),
    }),
    destroy: () => {
      if(stream)stream.stop();
      if(sortTimer!==null)clearTimeout(sortTimer);
      if(loader)loader.stop();
      clearInterval(timer);
      resize.disconnect();
    },
  });
})();
