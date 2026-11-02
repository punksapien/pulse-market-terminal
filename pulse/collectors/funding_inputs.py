"""ADR-0070 exact public funding source parsing; no network or storage access."""
from decimal import Decimal, InvalidOperation

from pulse.metrics.funding import FundingMetadata, FundingEstimate, Settlement, MAX_EVENTS
from pulse.observation import MAX_SOURCE_TIME_MS


def fraction(value):
    if isinstance(value,bool) or isinstance(value,float) or not isinstance(value,(str,int,Decimal)):
        raise ValueError("funding_decimal_source_required")
    try:
        result = Decimal(value)
    except (InvalidOperation,ValueError):
        raise ValueError("funding_invalid_decimal") from None
    if (not result.is_finite() or len(result.as_tuple().digits)>28
            or not -28<=result.as_tuple().exponent<=0):
        raise ValueError("funding_invalid_decimal")
    return result


def timestamp(value):
    if isinstance(value,str) and value.isascii() and value.isdigit() and len(value)<=16:
        value = int(value)
    if type(value) is not int or not 0<=value<=MAX_SOURCE_TIME_MS:
        raise ValueError("funding_invalid_timestamp")
    return value


def hours(value):
    result = fraction(value)
    if result not in (Decimal(1),Decimal(4),Decimal(8)):
        raise ValueError("funding_interval_unsupported")
    return int(result)


def info_index(payload):
    if type(payload) is not list or len(payload)>4096:
        raise ValueError("funding_info_not_complete_list")
    result = {}
    for row in payload:
        if type(row) is not dict or not isinstance(row.get("symbol"),str):
            raise ValueError("funding_info_malformed")
        symbol = row["symbol"]
        if symbol in result:
            raise ValueError("funding_info_duplicate")
        result[symbol] = row
    return result


def history(payload,pair,symbol,*,start_ms,end_ms):
    timestamp(start_ms); timestamp(end_ms)
    if start_ms>end_ms or end_ms-start_ms>8*86400000:
        raise ValueError("funding_history_scope")
    if type(payload) is not list or len(payload)>=MAX_EVENTS:
        raise ValueError("funding_history_saturated_or_invalid")
    result = {}
    for row in payload:
        if type(row) is not dict or row.get("symbol")!=symbol:
            raise ValueError("funding_history_identity")
        stamp = timestamp(row.get("fundingTime"))
        if not start_ms<=stamp<=end_ms:
            raise ValueError("funding_history_outside_scope")
        kind = row.get("rateType","Regular")
        if not isinstance(kind,str) or not 0<len(kind)<=32:
            raise ValueError("funding_invalid_rate_type")
        event = Settlement(pair,stamp,fraction(row.get("fundingRate")),kind)
        key = stamp,kind
        if key in result and result[key]!=event:
            raise ValueError("funding_history_conflicting_duplicate")
        result[key] = event
    return tuple(sorted(result.values(),key=lambda e:(e.time_ms,e.rate_type)))


def inputs(pair,symbol,instrument,ticker,info,premium,*,instrument_ms,info_ms,mapping="v1"):
    if (type(instrument) is not dict or instrument.get("pair")!=pair
            or instrument.get("quote_currency_short_name")!="USDT"
            or instrument.get("settle_currency_short_name")!="USDT"
            or type(premium) is not dict or premium.get("symbol")!=symbol):
        raise ValueError("funding_metadata_identity")
    if type(ticker) is not dict or type(ticker.get("prices")) is not dict:
        raise ValueError("funding_ticker_shape")
    row = ticker["prices"].get(pair)
    if type(row) is not dict or row.get("mkt")!=symbol:
        raise ValueError("funding_ticker_identity")
    h = hours(instrument.get("funding_frequency"))
    entry = info.get(symbol)
    native = None if entry is None else hours(entry.get("fundingIntervalHours"))
    source = min(timestamp(ticker.get("ts")),timestamp(row.get("bmST")))
    nft = timestamp(premium.get("nextFundingTime"))
    settled = fraction(row.get("fr"))
    cap = None if entry is None or entry.get("adjustedFundingRateCap") is None else fraction(entry["adjustedFundingRateCap"])
    floor = None if entry is None or entry.get("adjustedFundingRateFloor") is None else fraction(entry["adjustedFundingRateFloor"])
    metadata = FundingMetadata(pair,mapping,h,native,nft,settled,
        min(source,timestamp(instrument_ms),timestamp(info_ms),timestamp(premium.get("time"))),cap,floor)
    estimate = FundingEstimate(pair,mapping,h,nft,settled,fraction(row.get("efr")),source)
    return metadata,estimate
