"""Complete deterministic OHLCV fixture recipe; synthetic, not source observations."""
import pandas as pd


def candles(resolution, count=80, constant=False):
    minutes = 1440 if resolution == "1D" else int(resolution)
    rows = []
    for i in range(count):
        opened = 100.0 if constant else 100 + ((i*17)%23-11)/10 + i/100
        closed = opened if constant else opened + ((i*7)%11-5)/100
        spread = 0.0 if constant else .2 + (i%5)/100
        rows.append({"timestamp":str(pd.Timestamp("2026-09-01T00:00:00Z") + pd.Timedelta(minutes=minutes*i)),
                     "open":opened,"high":max(opened,closed)+spread,"low":min(opened,closed)-spread,
                     "close":closed,"volume":1000+i})
    return pd.DataFrame(rows)
