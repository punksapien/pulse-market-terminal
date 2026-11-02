(function () {
  'use strict';

  const D = document;
  const Q = (selector, root = D) => root.querySelector(selector);
  const QA = (selector, root = D) => Array.from(root.querySelectorAll(selector));

  const API_ENDPOINT = window.VOL_DASHBOARD_API || '/api';
  const REPORT_CACHE_TTL = 120000;
  const REPORT_HARD_MAX_AGE = 3 * 60 * 60 * 1000;
  const FAST_REFRESH_MS = 10000;
  const FULL_REFRESH_MS = 60000;
  const IS_LOCAL_DEV = window.location.protocol === 'file:' ||
    /^(localhost|127\.0\.0\.1|\[::1\])$/.test(window.location.hostname);

  const REGIME_COLORS = {
    Calm: '#4965D2',
    Low: '#4965D2',
    Active: '#37B271',
    Hot: '#F7931A',
    Extreme: '#F13B4D'
  };

  const DIRECTION_COLORS = {
    Surging: '#F7931A',
    Rising: '#37B271',
    Stable: '#4965D2',
    Falling: '#F13B4D',
    Contracting: '#4965D2'
  };

  const SIGNAL_LABELS = {
    MomentumLong: 'Uptrend',
    MomentumShort: 'Downtrend',
    ReversalLong: 'Reversal',
    ReversalShort: 'Reversal',
    BreakoutWatch: 'Breakout',
    Range: 'Range',
    Avoid: 'Avoid'
  };
  const SIGNAL_SINCE_STATES = new Set([
    'MomentumLong',
    'MomentumShort',
    'BreakoutWatch',
    'Range'
  ]);

  const state = {
    timeframe: '1D',
    regimeGroup: 'all',
    signalType: 'all',
    filters: {
      trendDirection: 'all',
      minVolume: 'all'
    },
    draftFilters: {
      trendDirection: 'all',
      minVolume: 'all'
    },
    sort: {
      field: 'volatility_score',
      direction: 'desc'
    }
  };

  let report = null;
  let tokens = [];
  let tokenByInstrument = new Map();
  let activeDetailInstrument = null;
  let fullRequestController = null;
  let fastRequestController = null;
  let requestSequence = 0;
  let fastTimer = null;
  let fullTimer = null;
  let freshnessTimer = null;
  let lastSuccessfulFetchAt = 0;
  let lastModelRebuiltAt = 0;
  let lastMarketHydratedAt = 0;
  let lastFocusedElement = null;

  const reportCache = new Map();
  const realtimeCache = new Map();
  const activeGenerationByTimeframe = new Map();

  function applyPageBackground() {
    const params = new URLSearchParams(window.location.search);
    const background = params.has('dark')
      ? '#1F1F1F'
      : params.has('light') ? '#d4d4d4' : null;
    if (!background) return;
    D.documentElement.dataset.pageTheme = params.has('dark') ? 'dark' : 'light';
    D.documentElement.style.background = background;
    D.body.style.background = background;
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function cleanText(value) {
    if (typeof value !== 'string') return '';
    return value
      .replace(/\u2014/g, ' - ')
      .replace(/\u2013/g, '-')
      .replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/gu, '')
      .trim();
  }

  function finiteNumber(value, fallback = null) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function formatPercent(value, digits = 2, signed = false) {
    const number = finiteNumber(value);
    if (number == null) return '—';
    const scaled = number * 100;
    const prefix = signed && scaled > 0 ? '+' : '';
    return `${prefix}${scaled.toFixed(digits)}%`;
  }

  function formatCompactNumber(value, currency = false) {
    const number = finiteNumber(value);
    if (number == null || number < 0) return '—';
    const prefix = currency ? '$' : '';
    if (number >= 1e12) return `${prefix}${(number / 1e12).toFixed(1)}T`;
    if (number >= 1e9) return `${prefix}${(number / 1e9).toFixed(1)}B`;
    if (number >= 1e6) return `${prefix}${(number / 1e6).toFixed(number >= 1e8 ? 0 : 1)}M`;
    if (number >= 1e3) return `${prefix}${(number / 1e3).toFixed(number >= 1e5 ? 0 : 1)}K`;
    return `${prefix}${number.toFixed(number < 10 ? 2 : 0)}`;
  }

  function formatMarketVolume(value) {
    const number = finiteNumber(value);
    if (number == null || number < 0) return '—';
    if (number >= 1e12) return `${(number / 1e12).toFixed(2)}T`;
    if (number >= 1e9) return `${(number / 1e9).toFixed(2)}B`;
    if (number >= 1e8) return `${(number / 1e6).toFixed(0)}M`;
    if (number >= 1e7) return `${(number / 1e6).toFixed(1)}M`;
    if (number >= 1e6) return `${(number / 1e6).toFixed(2)}M`;
    if (number >= 1e3) return `${(number / 1e3).toFixed(1)}K`;
    return number.toFixed(0);
  }

  function formatPrice(value) {
    const number = finiteNumber(value);
    if (number == null || number <= 0) return '—';
    let maximumFractionDigits = 2;
    if (number < 1) maximumFractionDigits = 6;
    if (number < 0.01) maximumFractionDigits = 8;
    return number.toLocaleString(undefined, {
      minimumFractionDigits: number >= 1 ? 2 : 0,
      maximumFractionDigits
    });
  }

  function formatFunding(value) {
    const number = finiteNumber(value);
    return number == null ? '—' : `${(number * 100).toFixed(3)}%`;
  }

  function normalizeRegime(value) {
    const name = String(value || '').toLowerCase();
    if (name.includes('extreme')) return 'Extreme';
    if (name.includes('hot') || name.includes('high risk')) return 'Hot';
    if (name.includes('calm') || name.includes('quiet')) return 'Calm';
    if (name.includes('low')) return 'Low';
    return 'Active';
  }

  function normalizeDirection(value) {
    const name = String(value || '').toLowerCase();
    if (name === 'surging') return 'Surging';
    if (name === 'rising') return 'Rising';
    if (name === 'falling') return 'Falling';
    if (name === 'contracting') return 'Contracting';
    return 'Stable';
  }

  function signalLabel(value) {
    return SIGNAL_LABELS[value] || 'Watch';
  }

  function volatilityBandColor(regime) {
    return REGIME_COLORS[normalizeRegime(regime)] || REGIME_COLORS.Active;
  }

  function getHorizonForTimeframe(timeframe) {
    if (timeframe === '60') return '1H';
    if (timeframe === '240') return '4H';
    return '24H';
  }

  function getRegimeGroup(regime) {
    if (regime === 'Extreme') return 'extreme';
    if (regime === 'Hot') return 'hot';
    if (regime === 'Calm' || regime === 'Low') return 'calm';
    return 'active';
  }

  function volatilityMeterIcon(regime, className = 'vol-gauge') {
    const meterType = getRegimeGroup(normalizeRegime(regime));
    const needles = {
      calm: 'M8 13L4.45 9.45',
      active: 'M8 13V8',
      hot: 'M8 13L11.55 9.45',
      extreme: 'M8 13H13'
    };
    return `
      <svg class="${className}" viewBox="0 0 16 16" data-meter-type="${meterType}" aria-hidden="true">
        <path d="M2 13a6 6 0 0 1 12 0"></path>
        <path d="${needles[meterType] || needles.active}"></path>
        <circle cx="8" cy="13" r="1"></circle>
      </svg>`;
  }

  function volumeThreshold(value) {
    return {
      '1M': 1e6,
      '5M': 5e6,
      '10M': 10e6,
      '50M': 50e6,
      '100M': 100e6
    }[value] || 0;
  }

  function avatarColor(symbol) {
    const palette = ['#4965D2', '#37B271', '#F7931A', '#F13B4D'];
    let hash = 0;
    for (const char of symbol) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0;
    return palette[Math.abs(hash) % palette.length];
  }

  function tokenIconSlug(symbol) {
    return String(symbol || '')
      .toLowerCase()
      .replace(/^1000000/, '')
      .replace(/^1000/, '')
      .replace(/[^a-z0-9]/g, '');
  }

  function tokenIconHtml(token, className = 'token-avatar') {
    const safeSymbol = escapeHtml(token.symbol);
    const initial = escapeHtml(token.symbol.slice(0, 1) || '?');
    const iconUrl = `https://coindcx.s3.amazonaws.com/static/coins/${tokenIconSlug(token.symbol)}.webp`;
    return `
      <span class="${className}" style="--avatar-fallback:${avatarColor(token.symbol)}" aria-hidden="true">
        <img src="${escapeHtml(iconUrl)}" alt="" loading="lazy" decoding="async"
          onerror="this.hidden=true;this.nextElementSibling.hidden=false">
        <span class="token-avatar-fallback" hidden>${initial}</span>
      </span>`;
  }

  function inferOiAvailability(token) {
    if (Array.isArray(token.oi_history) && token.oi_history.length > 0) return true;
    if (finiteNumber(token.oi_usd, 0) > 0) return true;
    return finiteNumber(token.oi_24h_change, 0) !== 0;
  }

  function normalizeToken(raw, instrument) {
    const symbol = String(raw.symbol || instrument.replace('B-', '').split('_')[0] || '')
      .toUpperCase()
      .replace(/^(?:1000000|1000|1M)(?=[A-Z])/, '');
    const normalized = Object.assign({}, raw, {
      instrument,
      symbol,
      regime: normalizeRegime(raw.regime || raw.score_band),
      vol_direction: normalizeDirection(raw.vol_direction),
      volatility_score: finiteNumber(raw.volatility_score, finiteNumber(raw.sigma_base, 0)),
      dollar_vol_24h: finiteNumber(raw.dollar_vol_24h, finiteNumber(raw.daily_notional_24h, 0)),
      price_last: finiteNumber(raw.price_last),
      price_change_24h: finiteNumber(raw.price_change_24h),
      oi_24h_change: finiteNumber(raw.oi_24h_change),
      signal_since_price: raw.signal_since_price == null
        ? null
        : finiteNumber(raw.signal_since_price),
      signal_since_at: raw.signal_since_at == null
        ? null
        : finiteNumber(raw.signal_since_at)
    });
    normalized.oi_available = inferOiAvailability(normalized);
    normalized.card_rationale = cleanText(normalized.card_rationale);
    normalized.vol_outlook_reasons = Array.isArray(normalized.vol_outlook_reasons)
      ? normalized.vol_outlook_reasons.map(cleanText).filter(Boolean)
      : [];
    normalized.quant_reasons = Array.isArray(normalized.quant_reasons)
      ? normalized.quant_reasons.map((reason) => ({
          icon: cleanText(reason && reason.icon),
          text: cleanText(reason && reason.text),
          confidence: cleanText(reason && reason.confidence)
        })).filter((reason) => reason.text)
      : [];
    return normalized;
  }

  function realtimeCacheKey(instrument, timeframe = state.timeframe) {
    return `${timeframe}:${instrument}`;
  }

  function clearRealtimeCacheForTimeframe(timeframe) {
    const prefix = `${timeframe}:`;
    for (const key of realtimeCache.keys()) {
      if (key.startsWith(prefix)) realtimeCache.delete(key);
    }
  }

  function applyRealtimeValues(token) {
    const cached = realtimeCache.get(realtimeCacheKey(token.instrument));
    if (!cached) return token;
    Object.assign(token, cached);
    token.oi_available = token.oi_available || inferOiAvailability(token);
    return token;
  }

  function processReport(nextReport) {
    report = nextReport;
    const meta = nextReport.meta || {};
    const generationId = String(meta.generation_id || '');
    const priorGenerationId = activeGenerationByTimeframe.get(state.timeframe);
    const generationChanged = Boolean(
      generationId && priorGenerationId && generationId !== priorGenerationId
    );
    const needsImmediateSignalHydration = Boolean(
      generationId && (!priorGenerationId || generationChanged)
    );
    if (generationChanged) clearRealtimeCacheForTimeframe(state.timeframe);
    if (generationId) activeGenerationByTimeframe.set(state.timeframe, generationId);
    const rebuiltAt = finiteNumber(meta.last_rebuilt);
    const hydratedAt = finiteNumber(meta.last_hydrated);
    if (rebuiltAt != null && rebuiltAt > 0) lastModelRebuiltAt = rebuiltAt * 1000;
    if (hydratedAt != null && hydratedAt > 0) lastMarketHydratedAt = hydratedAt * 1000;
    const instruments = nextReport.instruments || {};
    tokens = [];
    tokenByInstrument = new Map();

    for (const summaryRow of nextReport.summary || []) {
      const instrument = summaryRow.instrument || '';
      if (!instrument || summaryRow.error) continue;
      const detail = instruments[instrument] || {};
      if (detail.error) continue;
      const token = applyRealtimeValues(normalizeToken(Object.assign({}, detail, summaryRow), instrument));
      tokens.push(token);
      tokenByInstrument.set(instrument, token);
    }
    updateMarketContext();
    if (needsImmediateSignalHydration && tokens.length) {
      window.setTimeout(fetchFastDelta, 0);
    }
  }

  function tokenPassesFilters(token) {
    if (state.regimeGroup !== 'all' && getRegimeGroup(token.regime) !== state.regimeGroup) return false;
    if (state.signalType !== 'all' && token.trade_signal !== state.signalType) return false;
    if (state.filters.trendDirection !== 'all' &&
        token.vol_direction.toLowerCase() !== state.filters.trendDirection) return false;
    if (token.dollar_vol_24h < volumeThreshold(state.filters.minVolume)) return false;
    return true;
  }

  function compareTokens(a, b) {
    const field = state.sort.field;
    let left = a[field];
    let right = b[field];
    if (field === 'symbol') {
      const result = String(left || '').localeCompare(String(right || ''));
      return state.sort.direction === 'asc' ? result : -result;
    }
    left = finiteNumber(left, Number.NEGATIVE_INFINITY);
    right = finiteNumber(right, Number.NEGATIVE_INFINITY);
    if (left === right) return a.symbol.localeCompare(b.symbol);
    const result = left < right ? -1 : 1;
    return state.sort.direction === 'asc' ? result : -result;
  }

  function filteredTokens() {
    return tokens.filter(tokenPassesFilters).sort(compareTokens);
  }

  function changeClass(value) {
    const number = finiteNumber(value, 0);
    if (Math.abs(number) < 0.003) return 'neutral';
    return number < 0 ? 'down' : 'up';
  }

  function marketRowHtml(token) {
    const volatilityColor = volatilityBandColor(token.regime);
    const safeInstrument = escapeHtml(token.instrument);
    const safeSymbol = escapeHtml(token.symbol);
    const priceChange = token.price_change_24h == null ? '—' : formatPercent(token.price_change_24h, 2, true);
    const signal = signalLabel(token.trade_signal);
    const signalClass = String(token.trade_signal || 'Watch').toLowerCase();

    return `
      <article class="market-row" id="market-${safeInstrument}" data-instrument="${safeInstrument}">
        <button class="row-open-button" type="button" data-detail-instrument="${safeInstrument}" aria-label="Open ${safeSymbol} volatility detail"></button>
        <div class="token-cell">
          <span class="token-primary">
            ${tokenIconHtml(token)}
            <span class="token-name">${safeSymbol}</span>
          </span>
          <span class="token-meta">
            <span>Vol ${escapeHtml(formatMarketVolume(token.dollar_vol_24h))}</span>
            <span class="signal-tag signal-${escapeHtml(signalClass)}">${escapeHtml(signal)}</span>
          </span>
        </div>
        <div class="market-metric-cell">
          <span class="price-value">${escapeHtml(formatPrice(token.price_last))}</span>
          <span class="vol-score" style="color:${volatilityColor}">
            ${volatilityMeterIcon(token.regime)}
            ${escapeHtml(formatPercent(token.volatility_score))}
          </span>
        </div>
        <div class="change-cell"><span class="change-badge ${changeClass(token.price_change_24h)}">${escapeHtml(priceChange)}</span></div>
        <div class="trade-cell">
          <button class="trade-button" type="button" data-trade-instrument="${safeInstrument}" aria-label="Trade ${safeSymbol} futures" aria-describedby="timeframe-risk-disclaimer">
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="M2 6.37h11.74"></path>
              <path d="M9.83 2.46l3.91 3.91"></path>
              <path d="M14 9.63H2.26"></path>
              <path d="M6.17 13.54L2.26 9.63"></path>
            </svg>
          </button>
        </div>
      </article>`;
  }

  function renderMarketList() {
    const list = Q('#token-list');
    if (!list) return;
    const visible = filteredTokens();
    Q('#result-count').textContent = `${visible.length} of ${tokens.length} markets`;

    if (!visible.length) {
      list.innerHTML = `
        <div class="section-state empty-state" role="status">
          <div class="section-state-content">
            <svg class="state-illustration empty-illustration" viewBox="0 0 112 76" aria-hidden="true">
              <rect x="25" y="3" width="74" height="26" rx="7" fill="#EDF2FF"/>
              <rect x="25" y="3" width="5" height="26" rx="2.5" fill="#D5E1FF"/>
              <circle cx="43" cy="16" r="7" fill="#8EA8FF"/>
              <path d="M57 12h14M57 17h28" stroke="#8EA8FF" stroke-width="2" stroke-linecap="round"/>
              <rect x="76" y="9" width="17" height="13" rx="3" fill="#C9D6FF"/>
              <rect x="8" y="39" width="96" height="32" rx="8" fill="#E7EEFF"/>
              <rect x="8" y="39" width="5" height="32" rx="2.5" fill="#C9D9FF"/>
              <circle cx="29" cy="55" r="9" fill="#5D7DFF"/>
              <path d="M46 50h15M46 57h31" stroke="#5D7DFF" stroke-width="2.5" stroke-linecap="round"/>
              <rect x="78" y="47" width="20" height="16" rx="3" fill="#AFC0FA"/>
            </svg>
            <h2>No opportunities found</h2>
            <p>No tokens found for the selected time frame.<br>Try adjusting your filters.</p>
          </div>
        </div>`;
      return;
    }
    list.innerHTML = visible.map(marketRowHtml).join('');
  }

  function renderErrorState() {
    const list = Q('#token-list');
    if (!list) return;
    Q('#result-count').textContent = 'Market section unavailable';
    list.innerHTML = `
      <div class="section-state error-state" role="alert">
        <div class="section-state-content">
          <svg class="state-illustration error-illustration" viewBox="0 0 132 82" aria-hidden="true">
            <path d="M3 46h19M110 46h19" stroke="#EEF3FF" stroke-width="10" stroke-linecap="round"/>
            <rect x="20" y="28" width="39" height="36" rx="8" fill="#D9E5FF"/>
            <rect x="15" y="33" width="13" height="26" rx="6" fill="#C8D8FF"/>
            <rect x="26" y="24" width="34" height="44" rx="7" fill="#D2DFFF"/>
            <rect x="52" y="20" width="10" height="52" rx="2" fill="#EDF2FF"/>
            <rect x="73" y="28" width="39" height="36" rx="8" fill="#D9E5FF"/>
            <rect x="104" y="33" width="13" height="26" rx="6" fill="#C8D8FF"/>
            <rect x="72" y="24" width="34" height="44" rx="7" fill="#D2DFFF"/>
            <rect x="70" y="20" width="10" height="52" rx="2" fill="#EDF2FF"/>
            <path d="M59 39h7M59 57h7" stroke="#5D7DFF" stroke-width="5" stroke-linecap="round"/>
            <path d="M64 11l2-8M72 13l5-7M78 18l7-4" stroke="#AFC4FF" stroke-width="3" stroke-linecap="round"/>
          </svg>
          <h2>Failed to load the section</h2>
          <button class="state-retry-button" id="error-retry" type="button">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M20 11a8 8 0 1 0-2.3 5.7"></path>
              <path d="M20 5v6h-6"></path>
            </svg>
            <span>Retry</span>
          </button>
        </div>
      </div>`;
    const retry = Q('#error-retry');
    if (retry) {
      retry.onclick = () => {
        renderSkeletons();
        fetchReport(true);
      };
    }
  }

  function updateTimeframeLabels() {
    const horizon = getHorizonForTimeframe(state.timeframe);
    const volumeLabel = Q('#timeframe-volume-label');
    const changeLabel = Q('#timeframe-change-label');
    if (volumeLabel) volumeLabel.textContent = `${horizon} Vol`;
    if (changeLabel) changeLabel.textContent = `${horizon} % Chg`;
  }

  function renderSkeletons() {
    const list = Q('#token-list');
    if (!list) return;
    list.innerHTML = Array.from({ length: 9 }, () => `
      <div class="skeleton-row" aria-hidden="true">
        <div class="shimmer" style="width:118px;height:40px"></div>
        <div class="shimmer" style="width:68px;height:40px;justify-self:end"></div>
        <div class="shimmer" style="width:68px;height:28px;justify-self:end"></div>
        <div class="shimmer" style="width:28px;height:28px"></div>
      </div>`).join('');
  }

  function updateMarketContext() {
    const context = report && report.market_context;
    const label = Q('#market-context');
    if (!label) return;
    label.textContent = cleanText(context && context.market_context_text) || 'CoinDCX futures';
  }

  function updateFilterNavigation() {
    QA('#regime-tabs .regime-tab').forEach((button) => {
      const active = button.dataset.regimeGroup === state.regimeGroup;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    QA('#signal-strip .signal-chip').forEach((button) => {
      const active = button.dataset.signal === state.signalType;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });

    const activeFilterCount =
      (state.filters.trendDirection !== 'all' ? 1 : 0) +
      (state.filters.minVolume !== 'all' ? 1 : 0);
    const badge = Q('#filter-badge');
    badge.hidden = activeFilterCount === 0;
    badge.textContent = String(activeFilterCount);

    const applied = Q('#applied-filter-chips');
    const chips = [];
    if (state.filters.trendDirection !== 'all') {
      const direction = state.filters.trendDirection;
      chips.push(`<button class="applied-chip" type="button" data-remove-filter="trend">${escapeHtml(direction[0].toUpperCase() + direction.slice(1))}<span>×</span></button>`);
    }
    if (state.filters.minVolume !== 'all') {
      chips.push(`<button class="applied-chip" type="button" data-remove-filter="volume">&gt; ${escapeHtml(state.filters.minVolume)}<span>×</span></button>`);
    }
    applied.innerHTML = chips.join('');
  }

  function updateSortHeader() {
    QA('#col-header-row [data-field]').forEach((heading) => {
      const active = heading.dataset.field === state.sort.field;
      heading.classList.toggle('active', active);
      heading.classList.toggle('asc', active && state.sort.direction === 'asc');
      heading.classList.toggle('desc', active && state.sort.direction === 'desc');
      heading.setAttribute('aria-sort', active ? (state.sort.direction === 'asc' ? 'ascending' : 'descending') : 'none');
    });
  }

  function resetAllFilters() {
    state.regimeGroup = 'all';
    state.signalType = 'all';
    state.filters = { trendDirection: 'all', minVolume: 'all' };
    state.draftFilters = Object.assign({}, state.filters);
    updateFilterNavigation();
    syncFilterSheet();
    renderMarketList();
  }

  function syncFilterSheet() {
    QA('#filter-trend-row [data-trend]').forEach((button) => {
      button.classList.toggle('active', button.dataset.trend === state.draftFilters.trendDirection);
    });
    QA('#filter-volume-row [data-volume]').forEach((button) => {
      button.classList.toggle('active', button.dataset.volume === state.draftFilters.minVolume);
    });
  }

  function setSheetOpen(sheet, overlay, open, focusTarget) {
    if (!sheet || !overlay) return;
    if (overlay._hideTimer) {
      window.clearTimeout(overlay._hideTimer);
      overlay._hideTimer = null;
    }
    if (open) {
      lastFocusedElement = D.activeElement;
      overlay.hidden = false;
      sheet.setAttribute('aria-hidden', 'false');
      requestAnimationFrame(() => {
        overlay.classList.add('open');
        sheet.classList.add('open');
        const focusable = Q('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])', sheet);
        if (focusable) focusable.focus();
      });
    } else {
      overlay.classList.remove('open');
      sheet.classList.remove('open');
      sheet.setAttribute('aria-hidden', 'true');
      overlay._hideTimer = window.setTimeout(() => {
        overlay.hidden = true;
        overlay._hideTimer = null;
      }, 290);
      const target = focusTarget || lastFocusedElement;
      if (target && typeof target.focus === 'function') target.focus();
    }
  }

  function openFilterSheet() {
    state.draftFilters = Object.assign({}, state.filters);
    syncFilterSheet();
    setSheetOpen(Q('#filter-sheet'), Q('#filter-overlay'), true);
    trackInteraction('filter_sheet_open', { utm_medium: 'filter_strip' });
  }

  function closeFilterSheet() {
    setSheetOpen(Q('#filter-sheet'), Q('#filter-overlay'), false, Q('#btn-filters'));
  }

  function formatOi(token) {
    if (!token.oi_available) return '—';
    return formatPercent(token.oi_24h_change, 1, true);
  }

  function signalSinceText(token) {
    if (
      !token
      || !SIGNAL_SINCE_STATES.has(token.trade_signal)
      || token.signal_since_price == null
    ) {
      return '';
    }
    const sincePrice = finiteNumber(token.signal_since_price);
    if (sincePrice == null || sincePrice <= 0) return '';
    return `${signalLabel(token.trade_signal)} since $${formatPrice(sincePrice)}`;
  }

  function populateDetail(token) {
    const regimeColor = volatilityBandColor(token.regime);
    const directionColor = DIRECTION_COLORS[token.vol_direction] || '#8a8a8a';
    const detailAvatar = Q('#detail-token-icon');
    detailAvatar.outerHTML = tokenIconHtml(token, 'detail-token-icon').replace(
      'class="detail-token-icon"',
      'class="detail-token-icon" id="detail-token-icon"'
    );
    Q('#detail-symbol').textContent = token.symbol;
    Q('#detail-price').textContent = token.price_last == null ? '—' : `$${formatPrice(token.price_last)}`;
    const detailChange = Q('#detail-change');
    detailChange.textContent = token.price_change_24h == null ? '—' : formatPercent(token.price_change_24h, 2, true);
    detailChange.style.color = finiteNumber(token.price_change_24h, 0) < 0 ? 'var(--negative)' : 'var(--positive)';
    Q('#detail-vol-score').textContent = formatPercent(token.volatility_score);
    Q('#detail-vol-score').style.color = regimeColor;
    const detailVolMeter = Q('#detail-vol-meter');
    detailVolMeter.style.color = regimeColor;
    detailVolMeter.innerHTML = volatilityMeterIcon(token.regime, 'detail-gauge-icon');
    const directionBadge = Q('#detail-dir-badge');
    directionBadge.textContent = token.vol_direction;
    directionBadge.style.color = directionColor;
    directionBadge.style.background = `${directionColor}18`;

    Q('#comp-1h').textContent = formatPercent(finiteNumber(token.vol_1h));
    Q('#comp-4h').textContent = formatPercent(finiteNumber(token.vol_4h));
    Q('#comp-24h').textContent = formatPercent(finiteNumber(token.vol_24h));

    const oi = Q('#metric-oi-val');
    oi.textContent = formatOi(token);
    oi.style.color = !token.oi_available ? 'var(--ink-33)' :
      finiteNumber(token.oi_24h_change, 0) < 0 ? 'var(--negative)' : 'var(--positive)';
    Q('#metric-funding-val').textContent = formatFunding(token.funding_rate);
    Q('#metric-rsi-val').textContent = finiteNumber(token.rsi_14) == null ? '—' : finiteNumber(token.rsi_14).toFixed(0);
    Q('#metric-vol-mom-val').textContent = formatPercent(finiteNumber(token.vol_momentum), 1, true);
    const velocityLabel = cleanText(token.vol_velocity_label || token.vol_direction || '—');
    Q('#metric-velocity-val').textContent = velocityLabel
      ? velocityLabel[0].toUpperCase() + velocityLabel.slice(1)
      : '—';
    Q('#metric-volume-val').textContent = formatCompactNumber(token.dollar_vol_24h, true);

    const reasonList = Q('#detail-why-box');
    const reasons = token.quant_reasons.length
      ? token.quant_reasons
      : [{ icon: 'Volatility', text: token.card_rationale || 'No explanatory factors are available for this market.' }];
    reasonList.innerHTML = reasons.slice(0, 5).map((reason) => `
      <div class="reason-row">
        <span class="reason-label">${escapeHtml(reason.icon || 'Market signal')}</span>
        <span class="reason-text">${escapeHtml(reason.text)}</span>
      </div>`).join('');

    const outlook = cleanText(token.vol_outlook || 'Mixed');
    const confidence = Math.max(0, Math.min(100, finiteNumber(token.vol_outlook_confidence, 0)));
    Q('#outlook-status').textContent = `${outlook} · ${confidence}%`;
    Q('#outlook-text').textContent = token.vol_outlook_reasons.length
      ? token.vol_outlook_reasons.join(' · ')
      : 'Current inputs do not provide a high-conviction volatility path.';
    const signalSinceLine = Q('#signal-since-line');
    const signalSince = signalSinceText(token);
    signalSinceLine.textContent = signalSince;
    signalSinceLine.hidden = !signalSince;
    Q('#outlook-bar-fill').style.width = `${confidence}%`;
    Q('#outlook-bar-fill').style.background = regimeColor;

    drawVolatilityChart(token, regimeColor);
    Q('#btn-detail-trade').onclick = () => tradeToken(token, 'detail_cta');
  }

  function openDetailSheet(instrument) {
    const token = tokenByInstrument.get(instrument);
    if (!token) return;
    activeDetailInstrument = instrument;
    populateDetail(token);
    setSheetOpen(Q('#detail-sheet'), Q('#detail-overlay'), true);
    trackInteraction('detail_sheet_open', { utm_medium: 'market_row', utm_term: token.symbol });
  }

  function closeDetailSheet() {
    setSheetOpen(Q('#detail-sheet'), Q('#detail-overlay'), false);
    activeDetailInstrument = null;
  }

  function drawVolatilityChart(token, color) {
    const canvas = Q('#detail-chart');
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(280, rect.width || 335);
    const height = Math.max(120, rect.height || 148);
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const context = canvas.getContext('2d');
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, width, height);
    context.fillStyle = '#ffffff';
    context.fillRect(0, 0, width, height);

    const values = (token.vol_sparkline_24h || []).map(Number).filter(Number.isFinite);
    if (values.length < 3) {
      context.fillStyle = 'rgba(0,0,0,.33)';
      context.font = '10px Inter';
      context.textAlign = 'center';
      context.fillText('Insufficient volatility history', width / 2, height / 2);
      return;
    }

    const padding = { top: 18, right: 12, bottom: 18, left: 44 };
    const chartWidth = width - padding.left - padding.right;
    const chartHeight = height - padding.top - padding.bottom;
    const minimum = Math.min(...values);
    const maximum = Math.max(...values);
    const range = maximum - minimum || 1;

    context.strokeStyle = 'rgba(0,0,0,.05)';
    context.lineWidth = 1;
    for (let index = 0; index < 3; index += 1) {
      const y = padding.top + (chartHeight / 2) * index;
      context.beginPath();
      context.moveTo(padding.left, y);
      context.lineTo(width - padding.right, y);
      context.stroke();
    }

    const points = values.map((value, index) => ({
      x: padding.left + (index / (values.length - 1)) * chartWidth,
      y: padding.top + chartHeight - ((value - minimum) / range) * chartHeight
    }));

    const gradient = context.createLinearGradient(0, padding.top, 0, height - padding.bottom);
    gradient.addColorStop(0, `${color}35`);
    gradient.addColorStop(1, `${color}00`);
    context.beginPath();
    points.forEach((point, index) => index ? context.lineTo(point.x, point.y) : context.moveTo(point.x, point.y));
    context.lineTo(points[points.length - 1].x, height - padding.bottom);
    context.lineTo(points[0].x, height - padding.bottom);
    context.closePath();
    context.fillStyle = gradient;
    context.fill();

    context.beginPath();
    points.forEach((point, index) => index ? context.lineTo(point.x, point.y) : context.moveTo(point.x, point.y));
    context.strokeStyle = color;
    context.lineWidth = 2;
    context.lineJoin = 'round';
    context.lineCap = 'round';
    context.stroke();

    context.fillStyle = 'rgba(0,0,0,.54)';
    context.font = '9px Inter';
    context.textAlign = 'left';
    context.fillText(formatPercent(maximum), 7, padding.top + 3);
    context.fillText(formatPercent(minimum), 7, height - padding.bottom + 3);
  }

  function activeUtms(medium, content, term) {
    let stored = {};
    try { stored = JSON.parse(localStorage.getItem('volatility_active_utms') || '{}'); } catch (_) {}
    return {
      utm_source: stored.utm_source || 'volsignal_terminal',
      utm_medium: medium || stored.utm_medium || 'dashboard',
      utm_campaign: stored.utm_campaign || 'coindcx_volatility_redesign',
      utm_content: content || stored.utm_content || '',
      utm_term: term || stored.utm_term || ''
    };
  }

  function captureUtms() {
    try {
      const params = new URLSearchParams(location.search);
      const stored = JSON.parse(localStorage.getItem('volatility_active_utms') || '{}');
      ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'].forEach((key) => {
        if (params.get(key)) stored[key] = params.get(key);
      });
      localStorage.setItem('volatility_active_utms', JSON.stringify(stored));
    } catch (_) {}
  }

  function trackInteraction(event, overrides = {}) {
    const utms = activeUtms(overrides.utm_medium, overrides.utm_content, overrides.utm_term);
    if (typeof window.gtag === 'function') {
      window.gtag('event', event, Object.assign({ event_category: 'volatility_dashboard' }, utms));
    }
    try {
      fetch('/api/telemetry', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.assign({ timestamp: new Date().toISOString(), event }, utms)),
        keepalive: true
      }).catch(() => {});
    } catch (_) {}
  }

  function tradeToken(token, medium) {
    const utms = activeUtms(medium, `trade_${token.symbol.toLowerCase()}`, token.symbol);
    trackInteraction('trade_cta_click', utms);
    const query = new URLSearchParams(Object.assign({ instrument: token.instrument }, utms)).toString();
    const mobile = /android|iphone|ipad|ipod|mobile/i.test(navigator.userAgent) ||
      (navigator.maxTouchPoints > 1 && /Macintosh/.test(navigator.userAgent));
    if (mobile) {
      location.href = `dcxgo://go.coindcx.com/futures?${query}`;
    } else {
      window.open(`https://coindcx.com/futures/${encodeURIComponent(token.instrument)}?${new URLSearchParams(utms)}`, '_blank', 'noopener');
    }
  }

  function updateFreshness() {
    const label = Q('#freshness-label');
    if (!label) return;
    if (expireUnsafeReport()) return;
    if (!lastSuccessfulFetchAt) {
      label.textContent = 'Loading market scan';
      return;
    }
    const now = Date.now();
    const marketReference = lastMarketHydratedAt || lastSuccessfulFetchAt;
    const marketSeconds = Math.max(0, Math.floor((now - marketReference) / 1000));
    const modelMinutes = lastModelRebuiltAt
      ? Math.max(0, Math.floor((now - lastModelRebuiltAt) / 60000))
      : null;
    let marketLabel = 'Prices live';
    if (marketSeconds >= 60) marketLabel = `Prices ${Math.floor(marketSeconds / 60)}m old`;
    else if (marketSeconds >= 10) marketLabel = `Prices ${marketSeconds}s old`;
    label.textContent = modelMinutes == null
      ? marketLabel
      : `${marketLabel} · Scan ${modelMinutes}m old`;
  }

  function reportMatchesRequest(data, timeframe, horizon) {
    const meta = data && data.meta;
    if (!meta) return true;
    return String(meta.resolution) === String(timeframe) &&
      String(meta.scale_horizon || '').toUpperCase() === String(horizon).toUpperCase();
  }

  function reportModelTimestamp(data) {
    const meta = data && data.meta;
    if (!meta) return 0;
    const rebuilt = finiteNumber(meta.last_rebuilt);
    if (rebuilt != null && rebuilt > 0) {
      return rebuilt > 1e12 ? rebuilt : rebuilt * 1000;
    }
    const created = Date.parse(meta.created_at_utc || '');
    return Number.isFinite(created) ? created : 0;
  }

  function reportIsSafeToDisplay(data) {
    if (IS_LOCAL_DEV) return Boolean(data && Array.isArray(data.summary));
    const modelTimestamp = reportModelTimestamp(data);
    return modelTimestamp > 0 && Date.now() - modelTimestamp <= REPORT_HARD_MAX_AGE;
  }

  function expireUnsafeReport() {
    if (!report || reportIsSafeToDisplay(report)) return false;
    report = null;
    tokens = [];
    tokenByInstrument = new Map();
    reportCache.delete(state.timeframe);
    try { localStorage.removeItem('volatility_cached_report'); } catch (_) {}
    lastModelRebuiltAt = 0;
    const label = Q('#freshness-label');
    if (label) label.textContent = 'Market scan unavailable';
    const dot = Q('#live-dot');
    if (dot) dot.className = 'live-dot dead';
    renderErrorState();
    return true;
  }

  function cacheReport(timeframe, data) {
    if (!reportIsSafeToDisplay(data)) return false;
    reportCache.set(timeframe, { data, timestamp: Date.now() });
    try {
      localStorage.setItem('volatility_cached_report', JSON.stringify({
        timeframe,
        data,
        timestamp: Date.now()
      }));
    } catch (_) {}
    return true;
  }

  function readCachedReport(timeframe) {
    const memory = reportCache.get(timeframe);
    if (memory && reportIsSafeToDisplay(memory.data)) return memory;
    if (memory) reportCache.delete(timeframe);
    try {
      const parsed = JSON.parse(localStorage.getItem('volatility_cached_report') || 'null');
      if (parsed && parsed.timeframe === timeframe && reportIsSafeToDisplay(parsed.data)) {
        return parsed;
      }
      if (parsed) localStorage.removeItem('volatility_cached_report');
    } catch (_) {}
    return null;
  }

  async function fetchReport(force = false) {
    const timeframe = state.timeframe;
    const horizon = getHorizonForTimeframe(timeframe);
    const sequence = ++requestSequence;
    const dot = Q('#live-dot');
    dot.className = 'live-dot loading';

    if (!force) {
      const cached = readCachedReport(timeframe);
      if (cached) {
        processReport(cached.data);
        renderMarketList();
        lastSuccessfulFetchAt = cached.timestamp;
        updateFreshness();
        dot.className = 'live-dot';
        if (Date.now() - cached.timestamp < REPORT_CACHE_TTL) {
          backgroundFetch(timeframe, horizon);
          return;
        }
      } else if (!tokens.length) {
        renderSkeletons();
      }
    }

    if (fullRequestController) fullRequestController.abort();
    fullRequestController = new AbortController();
    let url = `${API_ENDPOINT}?json=1&timeframe=${encodeURIComponent(timeframe)}&horizon=${encodeURIComponent(horizon)}&utm_source=volsignal_terminal&utm_medium=api_client`;
    if (force) url += '&refresh=1';

    try {
      const response = await fetch(url, { signal: fullRequestController.signal, cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!data || !Array.isArray(data.summary)) throw new Error('Invalid report payload');
      if (!reportMatchesRequest(data, timeframe, horizon)) {
        throw new Error('Report timeframe metadata mismatch');
      }
      if (!reportIsSafeToDisplay(data)) throw new Error('Report exceeded hard freshness limit');
      if (sequence !== requestSequence || timeframe !== state.timeframe) return;
      cacheReport(timeframe, data);
      processReport(data);
      renderMarketList();
      lastSuccessfulFetchAt = Date.now();
      updateFreshness();
      dot.className = 'live-dot';
      prewarmTimeframes(timeframe);
      if (activeDetailInstrument && tokenByInstrument.has(activeDetailInstrument)) {
        populateDetail(tokenByInstrument.get(activeDetailInstrument));
      }
    } catch (error) {
      if (error.name === 'AbortError') return;
      console.error('Report fetch failed:', error);
      dot.className = 'live-dot dead';
      Q('#freshness-label').textContent = tokens.length ? 'Showing cached market scan' : 'Market scan unavailable';
      expireUnsafeReport();
      if (!tokens.length) renderErrorState();
    }
  }

  async function backgroundFetch(timeframe, horizon) {
    try {
      const response = await fetch(`${API_ENDPOINT}?json=1&timeframe=${encodeURIComponent(timeframe)}&horizon=${encodeURIComponent(horizon)}&utm_source=volsignal_terminal&utm_medium=background_refresh`);
      if (!response.ok) {
        expireUnsafeReport();
        return;
      }
      const data = await response.json();
      if (!data || !Array.isArray(data.summary)) return;
      if (!reportMatchesRequest(data, timeframe, horizon)) return;
      if (!reportIsSafeToDisplay(data)) {
        expireUnsafeReport();
        return;
      }
      cacheReport(timeframe, data);
      if (state.timeframe === timeframe) {
        processReport(data);
        renderMarketList();
        lastSuccessfulFetchAt = Date.now();
        updateFreshness();
      }
    } catch (_) {}
  }

  let prewarmed = false;
  function prewarmTimeframes(current) {
    if (prewarmed) return;
    prewarmed = true;
    ['60', '240', '1D'].filter((timeframe) => timeframe !== current).forEach((timeframe, index) => {
      window.setTimeout(() => backgroundFetch(timeframe, getHorizonForTimeframe(timeframe)), 1500 + index * 1500);
    });
  }

  async function fetchFastDelta() {
    if (!tokens.length) return;
    if (fastRequestController) fastRequestController.abort();
    fastRequestController = new AbortController();
    const timeframe = state.timeframe;
    const horizon = getHorizonForTimeframe(timeframe);
    try {
      const response = await fetch(`${API_ENDPOINT}?fast=1&json=1&timeframe=${encodeURIComponent(timeframe)}&horizon=${encodeURIComponent(horizon)}&utm_source=volsignal_terminal&utm_medium=fast_refresh`, {
        signal: fastRequestController.signal,
        cache: 'no-store'
      });
      if (!response.ok) return;
      const payload = await response.json();
      if (payload.status === 'unavailable') {
        expireUnsafeReport();
        return;
      }
      if (timeframe !== state.timeframe || payload.type !== 'delta' || !payload.delta) return;
      const activeGeneration = activeGenerationByTimeframe.get(timeframe);
      const payloadGeneration = String(payload.generation_id || '');
      if (activeGeneration && payloadGeneration && activeGeneration !== payloadGeneration) {
        backgroundFetch(timeframe, horizon);
        return;
      }
      applyDelta(payload.delta, timeframe);
      const deltaTimestamp = finiteNumber(payload.ts);
      if (deltaTimestamp != null && deltaTimestamp > 0) {
        lastMarketHydratedAt = deltaTimestamp * 1000;
      }
      lastSuccessfulFetchAt = Date.now();
      updateFreshness();
    } catch (error) {
      if (error.name !== 'AbortError') console.warn('Fast refresh failed:', error);
    }
  }

  function applyDelta(delta, timeframe = state.timeframe) {
    for (const [instrument, updates] of Object.entries(delta)) {
      const cacheKey = realtimeCacheKey(instrument, timeframe);
      const existingRealtime = realtimeCache.get(cacheKey) || {};
      const nextRealtime = Object.assign(existingRealtime, updates);
      realtimeCache.set(cacheKey, nextRealtime);
      const token = tokenByInstrument.get(instrument);
      if (!token) continue;
      Object.assign(token, updates);
      token.regime = normalizeRegime(token.regime);
      token.vol_direction = normalizeDirection(token.vol_direction);
      token.oi_available = token.oi_available || inferOiAvailability(token);
    }
    renderMarketList();
    if (activeDetailInstrument && tokenByInstrument.has(activeDetailInstrument)) {
      populateDetail(tokenByInstrument.get(activeDetailInstrument));
    }
  }

  function restartPolling() {
    if (fastTimer) clearInterval(fastTimer);
    if (fullTimer) clearInterval(fullTimer);
    fastTimer = window.setInterval(fetchFastDelta, FAST_REFRESH_MS);
    fullTimer = window.setInterval(() => fetchReport(false), FULL_REFRESH_MS);
  }

  function trapDialogFocus(event) {
    if (event.key !== 'Tab') return;
    const openSheet = Q('.bottom-sheet.open');
    if (!openSheet) return;
    const focusable = QA('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])', openSheet);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && D.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && D.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function bindEvents() {
    const refreshButton = Q('#btn-refresh');
    if (refreshButton) {
      refreshButton.onclick = () => {
        trackInteraction('manual_refresh', { utm_medium: 'header' });
        fetchReport(true);
      };
    }

    Q('#regime-tabs').onclick = (event) => {
      const button = event.target.closest('[data-regime-group]');
      if (!button) return;
      state.regimeGroup = button.dataset.regimeGroup;
      updateFilterNavigation();
      renderMarketList();
      trackInteraction('regime_filter', { utm_medium: 'regime_tabs', utm_content: state.regimeGroup });
    };

    Q('#signal-strip').onclick = (event) => {
      const signalButton = event.target.closest('[data-signal]');
      if (signalButton) {
        state.signalType = signalButton.dataset.signal;
        updateFilterNavigation();
        renderMarketList();
        trackInteraction('signal_filter', { utm_medium: 'signal_strip', utm_content: state.signalType });
        return;
      }
      const remove = event.target.closest('[data-remove-filter]');
      if (remove) {
        if (remove.dataset.removeFilter === 'trend') state.filters.trendDirection = 'all';
        if (remove.dataset.removeFilter === 'volume') state.filters.minVolume = 'all';
        state.draftFilters = Object.assign({}, state.filters);
        updateFilterNavigation();
        syncFilterSheet();
        renderMarketList();
      }
    };

    Q('#btn-filters').onclick = openFilterSheet;
    Q('#btn-filter-close').onclick = closeFilterSheet;
    Q('#filter-overlay').onclick = closeFilterSheet;
    Q('#filter-trend-row').onclick = (event) => {
      const button = event.target.closest('[data-trend]');
      if (!button) return;
      state.draftFilters.trendDirection = button.dataset.trend;
      syncFilterSheet();
    };
    Q('#filter-volume-row').onclick = (event) => {
      const button = event.target.closest('[data-volume]');
      if (!button) return;
      state.draftFilters.minVolume = button.dataset.volume;
      syncFilterSheet();
    };
    Q('#btn-filter-reset').onclick = () => {
      state.draftFilters = { trendDirection: 'all', minVolume: 'all' };
      syncFilterSheet();
    };
    Q('#btn-filter-apply').onclick = () => {
      state.filters = Object.assign({}, state.draftFilters);
      updateFilterNavigation();
      renderMarketList();
      closeFilterSheet();
      trackInteraction('filters_apply', {
        utm_medium: 'filter_sheet',
        utm_content: `${state.filters.trendDirection}_${state.filters.minVolume}`
      });
    };

    Q('#col-header-row').onclick = (event) => {
      const heading = event.target.closest('[data-field]');
      if (!heading) return;
      if (state.sort.field === heading.dataset.field) {
        state.sort.direction = state.sort.direction === 'desc' ? 'asc' : 'desc';
      } else {
        state.sort.field = heading.dataset.field;
        state.sort.direction = heading.dataset.field === 'symbol' ? 'asc' : 'desc';
      }
      updateSortHeader();
      renderMarketList();
      trackInteraction('column_sort', { utm_medium: 'market_header', utm_content: `${state.sort.field}_${state.sort.direction}` });
    };

    Q('#token-list').onclick = (event) => {
      const trade = event.target.closest('[data-trade-instrument]');
      if (trade) {
        event.stopPropagation();
        const token = tokenByInstrument.get(trade.dataset.tradeInstrument);
        if (token) tradeToken(token, 'market_list');
        return;
      }
      const detail = event.target.closest('[data-detail-instrument]');
      if (detail) openDetailSheet(detail.dataset.detailInstrument);
    };

    Q('#timeframe-control').onclick = (event) => {
      const button = event.target.closest('[data-timeframe]');
      if (!button || button.dataset.timeframe === state.timeframe) return;
      state.timeframe = button.dataset.timeframe;
      updateTimeframeLabels();
      QA('#timeframe-control [data-timeframe]').forEach((item) => {
        const active = item.dataset.timeframe === state.timeframe;
        item.classList.toggle('active', active);
        item.setAttribute('aria-pressed', String(active));
      });
      if (fullRequestController) fullRequestController.abort();
      if (fastRequestController) fastRequestController.abort();
      activeDetailInstrument = null;
      closeDetailSheet();
      tokens = [];
      tokenByInstrument = new Map();
      renderSkeletons();
      fetchReport(false);
      restartPolling();
      trackInteraction('timeframe_change', { utm_medium: 'timeframe_dock', utm_content: state.timeframe });
    };

    Q('#btn-detail-close').onclick = closeDetailSheet;
    Q('#detail-overlay').onclick = closeDetailSheet;

    D.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        if (Q('#detail-sheet').classList.contains('open')) closeDetailSheet();
        else if (Q('#filter-sheet').classList.contains('open')) closeFilterSheet();
      }
      trapDialogFocus(event);
    });

    window.addEventListener('resize', () => {
      if (activeDetailInstrument && tokenByInstrument.has(activeDetailInstrument)) {
        drawVolatilityChart(tokenByInstrument.get(activeDetailInstrument), REGIME_COLORS[tokenByInstrument.get(activeDetailInstrument).regime]);
      }
    });
  }

  function loadEmbeddedReport() {
    const embedded = Q('#vd');
    if (!embedded || !embedded.textContent.trim()) return false;
    try {
      const data = JSON.parse(embedded.textContent);
      if (!data || !Array.isArray(data.summary)) return false;
      processReport(data);
      renderMarketList();
      lastSuccessfulFetchAt = Date.now();
      return true;
    } catch (error) {
      console.warn('Embedded report could not be parsed:', error);
      return false;
    }
  }

  function init() {
    applyPageBackground();
    captureUtms();
    bindEvents();
    updateFilterNavigation();
    updateSortHeader();
    updateTimeframeLabels();
    const embeddedLoaded = loadEmbeddedReport();
    if (!embeddedLoaded) {
      const cached = readCachedReport(state.timeframe);
      if (cached) {
        processReport(cached.data);
        renderMarketList();
        lastSuccessfulFetchAt = cached.timestamp;
      } else {
        renderSkeletons();
      }
    }
    updateFreshness();
    fetchReport(false);
    restartPolling();
    freshnessTimer = window.setInterval(updateFreshness, 10000);
  }

  if (D.readyState === 'loading') {
    D.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
