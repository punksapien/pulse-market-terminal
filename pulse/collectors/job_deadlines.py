"""ADR-0180: pure scheduling deadlines; never changes observation expiry."""
from pulse.observation import MAX_SOURCE_TIME_MS


def priority(item,record):
    due,symbol,kind=item;record=record or {}
    def deadline(key,margin):
        value=record.get(key)
        return min(MAX_SOURCE_TIME_MS,value+margin) if type(value) is int and 0<value<=MAX_SOURCE_TIME_MS else due
    if kind=='oi':end=deadline('last_hot_ms',90000)
    elif kind=='instrument':end=deadline('instrument_ms',120000)
    elif kind=='global':end=min(deadline('global_source_ms',900000),deadline('global_tip_refreshed_ms',360000))
    elif kind=='detail':
        changes=record.get('derived',{}).get('changes',{})
        end=due if any(v.get('status')!='available' for v in changes.values()) else min(
            deadline('top_source_ms',2100000),deadline('top_refreshed_ms',960000))
    elif kind=='legacy':end=deadline('legacy_source_ms',900000)
    elif kind=='references':end=deadline('references_prefetch_until_ms',-60000)
    elif kind=='funding':end=deadline('nft',900000)
    elif kind.startswith('period_'):
        value=record.get('ls_periods',{}).get(kind[7:],{}).get('expires_ms')
        end=value if type(value) is int and 0<value<=MAX_SOURCE_TIME_MS else due
    else:end=due
    return end,due,symbol,kind


def providers(kind):
    if kind in ('oi','global','detail') or kind.startswith('period_'):return ('binance',)
    if kind in ('instrument','references'):return ('coindcx',)
    return ('binance','coindcx')
