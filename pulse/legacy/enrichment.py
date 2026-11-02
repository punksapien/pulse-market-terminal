"""ADR-0033: legacy enrichment using the single service-owned OI cache."""
import time

from pulse.config import OI_CACHE_TTL


def _get_cached_oi(
    inst, market_symbol='', price_hint=None, session=None, *,
    cache_state, fetch, unavailable,
):
    _OI_CACHE = cache_state
    fetch_oi_metrics = fetch
    unavailable_oi_metrics = unavailable
    now = time.time()
    with _OI_CACHE["lock"]:
        entry = _OI_CACHE["store"].get(inst)
        if entry and (now - entry["ts"]) < OI_CACHE_TTL:
            return entry["data"]

    # Cache miss or expired
    try:
        metrics = fetch_oi_metrics(
            inst,
            market_symbol=market_symbol,
            price_hint=price_hint,
            period="1h",
            limit=25,
            session=session,
        )
    except Exception as exc:
        metrics = unavailable_oi_metrics(f"provider_error:{type(exc).__name__}")

    with _OI_CACHE["lock"]:
        _OI_CACHE["store"][inst] = {"data": metrics, "ts": now}
    return metrics


def _enrich_with_signals(report, session=None, *, http, get_oi, classify, market_context, provider_order, unavailable):
    """Run signal engine over lightweight report: OI, regime, direction, signals."""
    requests = http
    _get_cached_oi = get_oi
    enrich_token = classify
    compute_market_context = market_context
    get_oi_provider_order = provider_order
    unavailable_oi_metrics = unavailable
    instruments = report.get("instruments", {})
    summary = report.get("summary", [])
    target_secs = report.get("meta", {}).get("target_seconds", 86400)

    # 1. Bulk fetch CoinDCX V3 public futures prices & stats (highly efficient!)
    v3_data = {}
    try:
        sess = session or requests.Session()
        # Configure robust connection pooling to prevent pool exhaustion in concurrent threads
        adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        resp = sess.get("https://public.coindcx.com/market_data/v3/current_prices/futures/rt", timeout=8)
        if resp.status_code == 200:
            v3_data = resp.json().get("prices", {})
    except Exception as e:
        print(f"Error fetching CoinDCX V3 public stats: {e}")
        sess = session or requests.Session()

    from pulse.legacy.signal_config import VolSignalConfig
    cfg = VolSignalConfig()

    from concurrent.futures import ThreadPoolExecutor, as_completed

    valid_insts = [(inst, info) for inst, info in instruments.items() if "error" not in info]
    enriched_tokens = []
    def enrich_single_token(inst, info):
        token = dict(info)
        # Populate from CoinDCX V3 bulk public futures data
        v3_info = v3_data.get(inst)
        market_symbol = v3_info.get("mkt", "") if isinstance(v3_info, dict) else ""
        oi_price_hint = (
            v3_info.get("ls")
            if isinstance(v3_info, dict) and v3_info.get("ls") is not None
            else token.get("price_last")
        )
        try:
            oi_metrics = _get_cached_oi(
                inst,
                market_symbol=market_symbol,
                price_hint=oi_price_hint,
                session=sess,
            )
        except Exception as exc:
            oi_metrics = unavailable_oi_metrics(
                f"provider_error:{type(exc).__name__}"
            )
        token.update(oi_metrics)

        if v3_info:
            funding_val = v3_info.get("efr")
            if funding_val is None:
                funding_val = v3_info.get("fr")
            if funding_val is not None:
                token["funding_rate"] = float(funding_val)

            price_last_val = v3_info.get("ls")
            price_last = None
            if price_last_val is not None:
                price_last = float(price_last_val)
                token["price_last"] = price_last

            price_change = v3_info.get("pc")
            if price_change is not None:
                token["price_change_24h"] = float(price_change) / 100.0

            volume_val = v3_info.get("v")
            if volume_val is not None:
                dollar_vol = float(volume_val)
                token["dollar_vol_24h"] = dollar_vol
                latest_p = price_last or token.get("price_last")
                if latest_p and latest_p > 0:
                    token["volume_24h"] = dollar_vol / latest_p
                else:
                    token["volume_24h"] = dollar_vol
                token["daily_volume"] = token["volume_24h"]
            high_val = v3_info.get("h")
            if high_val is not None:
                token["high_24h"] = float(high_val)

            low_val = v3_info.get("l")
            if low_val is not None:
                token["low_24h"] = float(low_val)

        token.setdefault("funding_rate", 0)
        if "oi_available" not in token:
            token.update(unavailable_oi_metrics("missing_oi_result"))
        token.setdefault("price_change_24h", 0)
        token.setdefault("price_change_1h", 0)

        # Run signal engine
        enrich_token(token, target_secs, cfg)
        return inst, token

    # Concurrently enrich all valid instruments (fast thread-pooled requests)
    max_workers = min(16, len(valid_insts) or 1)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(enrich_single_token, inst, info): inst for inst, info in valid_insts}
        for future in as_completed(futures):
            try:
                inst, token = future.result()
                instruments[inst] = token
                enriched_tokens.append(token)
            except Exception as exc:
                print(f"[CONCURRENT ENRICH FAILED] for {futures[future]}: {exc}")

    # Update summary rows with signal data
    for row in summary:
        inst = row.get("instrument", "")
        if inst in instruments:
            info = instruments[inst]
            for key in ("regime", "regime_icon", "regime_color", "regime_band_pct",
                        "vol_direction", "direction_icon", "direction_color",
                        "vol_velocity", "vol_acceleration", "vol_momentum",
                        "trade_signal", "signal_spec", "signal_readiness",
                        "vol_outlook", "vol_outlook_confidence", "vol_outlook_reasons",
                        "sizing", "card_rationale", "quant_reasons",
                        "oi_available", "oi_24h_change", "oi_usd", "oi_trend", "oi_velocity",
                        "oi_acceleration", "oi_source", "oi_source_symbol", "oi_asof",
                        "oi_error",
                        "funding_rate", "long_pct", "short_pct",
                        "vol_1h", "vol_4h", "vol_24h", "vol_7d", "vol_7d_avg",
                        "vol_percentile_30d", "rsi_14",
                        "price_change_24h", "price_change_1h", "price_tightening",
                        "vol_sparkline_24h", "symbol", "price_last", "oi_history",
                        "dollar_vol_24h", "volume_24h", "daily_volume", "volume_vs_30d_avg",
                        "trend_label", "playbook_action", "playbook_strategy", "playbook_tactics"):
                if key in info:
                    row[key] = info[key]

    # Market context
    report["market_context"] = compute_market_context(enriched_tokens, cfg)
    report_meta = report.setdefault("meta", {})
    oi_available_count = sum(
        1 for token in enriched_tokens if token.get("oi_available")
    )
    report_meta["signal_engine"] = "vol_signal_v1"
    report_meta["oi_provider_order"] = get_oi_provider_order()
    report_meta["oi_available_count"] = oi_available_count
    report_meta["oi_unavailable_count"] = max(
        0, len(enriched_tokens) - oi_available_count
    )
    report_meta["oi_coverage_ratio"] = (
        round(oi_available_count / len(enriched_tokens), 4)
        if enriched_tokens else 0.0
    )
    return report

