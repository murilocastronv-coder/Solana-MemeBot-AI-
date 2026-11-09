"""Historical BTC/USDT data from Binance's public archive (data.binance.vision).

No API key is needed. Files are cached in data_cache/ (git-ignored).
- 5m klines: spot or USDT-M futures, monthly files + daily files for the current month;
- funding: USDT-M futures funding history (futures profile only).
A local CSV can be used instead (columns: open_time, open, high, low, close, volume).
"""
import csv
import datetime as dt
import io
import os
import zipfile

import requests

BASE = "https://data.binance.vision/data"
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_cache")


def _months(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _fetch_zip(url, cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, url.rsplit("/", 1)[1])
    if not os.path.exists(path):
        r = requests.get(url, timeout=60)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        with open(path + ".part", "wb") as f:
            f.write(r.content)
        os.replace(path + ".part", path)
    with zipfile.ZipFile(path) as z:
        return z.read(z.namelist()[0]).decode()


def _ts_seconds(raw):
    v = int(float(raw))
    if v > 10**14:      # microseconds (Binance spot files since 2025)
        return v // 1_000_000
    if v > 10**11:      # milliseconds
        return v // 1000
    return v


def parse_klines(text):
    out = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].strip().lstrip("-").isdigit():
            continue  # header line in newer files
        out.append({"t": _ts_seconds(row[0]), "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
                    "c": float(row[4]), "v": float(row[5])})
    return out


def _kline_path(market, kind, symbol, tag):
    prefix = "spot" if market == "spot" else "futures/um"
    return "%s/%s/%s/klines/%s/5m/%s-5m-%s.zip" % (BASE, prefix, kind, symbol, symbol, tag)


def load_klines_5m(start, end, market="futures", symbol="BTCUSDT", cache=CACHE):
    """5m candles with open time in [start, end), oldest first, deduplicated."""
    cache_dir = os.path.join(cache, market)
    today = dt.datetime.now(dt.timezone.utc).date()
    rows = []
    for y, m in _months(start, end - dt.timedelta(seconds=1)):
        text = _fetch_zip(_kline_path(market, "monthly", symbol, "%04d-%02d" % (y, m)), cache_dir)
        if text is None:  # month not archived yet -> daily files
            d = dt.date(y, m, 1)
            while d.month == m and d < today:
                t = _fetch_zip(_kline_path(market, "daily", symbol, d.isoformat()), cache_dir)
                if t:
                    rows += parse_klines(t)
                d += dt.timedelta(days=1)
        else:
            rows += parse_klines(text)
    lo, hi = int(start.timestamp()), int(end.timestamp())
    uniq = {c["t"]: c for c in rows if lo <= c["t"] < hi}
    return [uniq[t] for t in sorted(uniq)]


def load_funding(start, end, symbol="BTCUSDT", cache=CACHE):
    """{timestamp rounded to the 5m bar: funding rate} for USDT-M futures."""
    cache_dir = os.path.join(cache, "funding")
    out = {}
    for y, m in _months(start, end - dt.timedelta(seconds=1)):
        url = "%s/futures/um/monthly/fundingRate/%s/%s-fundingRate-%04d-%02d.zip" % (BASE, symbol, symbol, y, m)
        text = _fetch_zip(url, cache_dir)
        if text is None:
            continue
        for row in csv.reader(io.StringIO(text)):
            if not row or not row[0].strip().isdigit():
                continue
            t = _ts_seconds(row[0])
            out[t // 300 * 300] = float(row[-1])
    return out


def load_csv(path):
    with open(path) as f:
        return sorted({c["t"]: c for c in parse_klines(f.read())}.values(), key=lambda c: c["t"])


def gaps(c5):
    """Number of missing 5m bars inside the series."""
    return sum(max(0, (b["t"] - a["t"]) // 300 - 1) for a, b in zip(c5, c5[1:]))
