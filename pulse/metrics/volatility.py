"""ADR-0053: reuse v1 executable evaluation order, never reimplement its formula."""


def evaluate_v1(candles_by_pair, config):
    # Lazy imports: no scorer dependency load on unrelated metrics or HTTP import.
    from pulse.legacy.scoring import analyse_instrument_lw, score_cross_section

    results = {pair: analyse_instrument_lw(pair, candles, config)
               for pair, candles in candles_by_pair.items()}
    rows, summary = score_cross_section(results, config)
    return results, rows, summary
