"""Price data for the MAc(7,10) tests.

1) The project's own file results/wfa_equity.csv (2019-2022, 5m). Its bh_equity column is
   price / price at 00:00 of Jan 1 of each test year (Bitstamp BTC/USD). The rule only uses
   the ratio of two moving averages, so the unknown price level does not matter.
2) Binance 5m klines + funding (2023 onwards = the only truly out-of-sample period), through
   btc_trend_pullback.data (public archive data.binance.vision, no key).
"""
import csv
import datetime as dt
import os

WFA_URL = ("https://raw.githubusercontent.com/octopus444/technical-trading-rules-based-strategies-btc/"
           "main/results/wfa_equity.csv")


def _ts(s):
    return int(dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp())


def load_wfa_years(path):
    """{year: [candles]} with o = previous close and h = l = c (only closes exist)."""
    if not os.path.exists(path):
        import requests
        r = requests.get(WFA_URL, timeout=120)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    years = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            years.setdefault(row["test_year"], []).append((_ts(row["date"]), float(row["bh_equity"])))
    out = {}
    for y, rows in years.items():
        t0 = rows[0][0] - 300
        prices = [(t0, 1.0)] + rows
        out[y] = [{"t": t, "o": prices[k - 1][1] if k else p, "h": p, "l": p, "c": p}
                  for k, (t, p) in enumerate(prices)]
    return out


def chain(years):
    """One continuous series. Year k+1 starts at year k's last price (the missing Dec 31 is ignored)."""
    out, mult = [], 1.0
    for y in sorted(years):
        bars = years[y]
        for k, b in enumerate(bars):
            o = out[-1]["c"] if (k == 0 and out) else b["o"] * mult
            out.append({"t": b["t"], "o": o, "h": b["h"] * mult, "l": b["l"] * mult, "c": b["c"] * mult})
        mult = out[-1]["c"]
    return out


def load_binance(start, end, market="futures"):
    from btc_trend_pullback import data as D
    c5 = D.load_klines_5m(start, end, market)
    funding = D.load_funding(start, end) if market == "futures" else None
    return c5, funding or None
