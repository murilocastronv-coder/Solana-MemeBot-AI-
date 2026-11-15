"""Disciplined search for a leveraged BTC strategy (research only: places no orders).

Tests ~2,200 variants of the best-known classic, publicly documented strategies on 1 year of real
BTC candles, on 3 timeframes (15 min, 1 h, 4 h, built from the 15-minute candles):
- breakout:    Turtle / Donchian: close beyond the high/low of the last N candles (± EMA200 trend)
- ema_cross:   moving-average crosses, including the 50/200 "golden cross"
- macd:        MACD line crosses its signal line (± EMA200 trend)
- supertrend:  Supertrend flips (ATR 10, multiplier 2 or 3)
- rsi_rev:     RSI / Larry Connors RSI(2) oversold-overbought (± EMA200 trend)
- bb_rev:      close outside the Bollinger band, betting on the return (± trend)
- squeeze:     Bollinger bands unusually narrow, then a close outside the band
- keltner:     close outside the Keltner channel (EMA20 ± ATR), trend following (± trend)
- psar:        Parabolic SAR flips (Wilder 0.02 / 0.2)
- ichimoku:    Tenkan/Kijun cross, optionally only on the right side of the cloud
- heikin:      Heikin-Ashi candle colour flips (± trend)
- adx:         DI+/DI- cross with ADX above 20 or 25 (Wilder)
- williams:    Larry Williams volatility breakout (day open ± k × previous day's range)
each with exits: stop 1 or 2 ATR, target 1/2/3x the stop or none, time limit 24 or 96 candles,
and exit on the opposite signal.

Honest selection (testing many variants on one history always finds lucky ones; with ~2,200 tests,
even more of them):
1. development = first 8 months; out of sample = last 4 months, never used to choose;
2. a variant only qualifies with enough trades (60 on 15m, 40 on 1h, 20 on 4h), profit factor >= 1.15,
   profit in BOTH halves of the development period and max drawdown < 40%;
3. the winner is picked on development only; the 4 out-of-sample months then decide.
Costs: Jupiter-like 0.06% per side + borrow; 20% of the account as collateral; 2x for the test.
"""
import bisect
import json
import time

import requests

TF = 900
TFS = (900, 3600, 14400)
TF_NAME = {900: "15m", 3600: "1h", 14400: "4h"}
MIN_TRADES_TF = {900: 60, 3600: 40, 14400: 20}
FEE = 0.0006
BORROW_H = 0.00003
LIQ_LOSS = 0.90
FRACTION = 0.20
START = 50.0
TEST_LEVERAGE = 2
LEVERAGES = (2, 3, 5, 10, 20)
MIN_TRADES = 60
MIN_PF = 1.15
MAX_DD = 0.40
WARM = 300                      # candles of warm-up (indicators) on every timeframe
WARM_15M = 16 * 330             # enough 15m history for 330 warm-up candles of 4 h

_http = requests.Session()


# ------------------------------------------------------------------ data
def _get(url):
    r = _http.get(url, timeout=15, headers={"User-Agent": "BTCResearch/1.0"})
    r.raise_for_status()
    return r.json()


def fetch_year(days=365, warm=WARM_15M, now=None):
    now = int(now or time.time())
    end = now // TF * TF
    start = end - (days * 96 + warm) * TF
    rows = []
    try:
        cursor = start * 1000
        while cursor < end * 1000:
            page = _get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=15m&limit=1000&startTime=" +
                        str(cursor) + "&endTime=" + str(end * 1000 - 1))
            if not page:
                break
            rows += [{"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                      "c": float(r[4]), "v": float(r[5])} for r in page]
            cursor = (rows[-1]["t"] + TF) * 1000
    except Exception:
        rows, cursor = [], start
        while cursor < end:
            stop = min(cursor + 300 * TF, end)
            for r in _get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=900&start=" +
                          time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cursor)) + "&end=" +
                          time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stop))):
                rows.append({"t": int(r[0]), "l": float(r[1]), "h": float(r[2]), "o": float(r[3]), "c": float(r[4]),
                             "v": float(r[5])})
            cursor = stop
    uniq = {c["t"]: c for c in rows if c["t"] + TF <= now}
    return [uniq[t] for t in sorted(uniq)]


def resample(cs, tf):
    """Build complete `tf`-second candles from 15-minute ones (incomplete groups are dropped)."""
    if tf == TF:
        return cs
    need, groups, out = tf // TF, {}, []
    for c in cs:
        groups.setdefault(c["t"] // tf * tf, []).append(c)
    for t in sorted(groups):
        g = groups[t]
        if len(g) == need:
            out.append({"t": t, "o": g[0]["o"], "h": max(x["h"] for x in g), "l": min(x["l"] for x in g),
                        "c": g[-1]["c"], "v": sum(x["v"] for x in g)})
    return out


# ------------------------------------------------------------------ indicators (index i uses data <= i)
def ema(v, n):
    out, k = [None] * len(v), 2 / (n + 1)
    j = next((i for i, x in enumerate(v) if x is not None), len(v))
    if len(v) - j < n:
        return out
    p = sum(v[j:j + n]) / n
    out[j + n - 1] = p
    for i in range(j + n, len(v)):
        p = v[i] * k + p * (1 - k)
        out[i] = p
    return out


def rsi(c, n):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = sum(max(c[i] - c[i - 1], 0) for i in range(1, n + 1)) / n
    l = sum(max(c[i - 1] - c[i], 0) for i in range(1, n + 1)) / n
    for i in range(n, len(c)):
        if i > n:
            d = c[i] - c[i - 1]
            g = (g * (n - 1) + max(d, 0)) / n
            l = (l * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def true_range(cs):
    return [cs[0]["h"] - cs[0]["l"]] + [max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"]))
                                        for p, c in zip(cs, cs[1:])]


def atr(cs, n=14):
    out = [None] * len(cs)
    tr = true_range(cs)
    if len(cs) <= n:
        return out
    p = sum(tr[1:n + 1]) / n
    out[n] = p
    for i in range(n + 1, len(cs)):
        p = (p * (n - 1) + tr[i]) / n
        out[i] = p
    return out


def bollinger(c, n=20, k=2.0):
    mid, up, lo, width = [None] * len(c), [None] * len(c), [None] * len(c), [None] * len(c)
    s = s2 = 0.0
    for i, x in enumerate(c):
        s += x
        s2 += x * x
        if i >= n:
            s -= c[i - n]
            s2 -= c[i - n] ** 2
        if i >= n - 1:
            m = s / n
            sd = max(s2 / n - m * m, 0) ** 0.5
            mid[i], up[i], lo[i] = m, m + k * sd, m - k * sd
            width[i] = (2 * k * sd) / m if m else None
    return mid, up, lo, width


def midpoint(cs, n):
    """(highest high + lowest low) / 2 of the last n candles, current one included (Ichimoku lines)."""
    out = [None] * len(cs)
    for i in range(n - 1, len(cs)):
        w = cs[i - n + 1:i + 1]
        out[i] = (max(x["h"] for x in w) + min(x["l"] for x in w)) / 2
    return out


def supertrend(cs, n=10, m=3.0):
    """+1 / -1 trend direction of the classic Supertrend (None during warm-up)."""
    a, out = atr(cs, n), [None] * len(cs)
    fub = flb = None
    d = 1
    for i, c in enumerate(cs):
        if a[i] is None:
            continue
        hl2 = (c["h"] + c["l"]) / 2
        ub, lb = hl2 + m * a[i], hl2 - m * a[i]
        pc = cs[i - 1]["c"]
        fub = ub if fub is None or ub < fub or pc > fub else fub
        flb = lb if flb is None or lb > flb or pc < flb else flb
        if d == 1 and c["c"] < flb:
            d = -1
        elif d == -1 and c["c"] > fub:
            d = 1
        out[i] = d
    return out


def psar(cs, step=0.02, mx=0.2):
    """+1 / -1 side of Wilder's Parabolic SAR."""
    out = [None] * len(cs)
    if len(cs) < 3:
        return out
    up = cs[1]["c"] >= cs[0]["c"]
    sar = cs[0]["l"] if up else cs[0]["h"]
    ep = cs[1]["h"] if up else cs[1]["l"]
    af = step
    for i in range(2, len(cs)):
        c = cs[i]
        sar += af * (ep - sar)
        if up:
            sar = min(sar, cs[i - 1]["l"], cs[i - 2]["l"])
            if c["l"] < sar:
                up, sar, ep, af = False, ep, c["l"], step
            elif c["h"] > ep:
                ep, af = c["h"], min(af + step, mx)
        else:
            sar = max(sar, cs[i - 1]["h"], cs[i - 2]["h"])
            if c["h"] > sar:
                up, sar, ep, af = True, ep, c["h"], step
            elif c["l"] < ep:
                ep, af = c["l"], min(af + step, mx)
        out[i] = 1 if up else -1
    return out


def heikin_green(cs):
    """True when the Heikin-Ashi candle is green."""
    out = [None] * len(cs)
    ho, hc = (cs[0]["o"] + cs[0]["c"]) / 2, (cs[0]["o"] + cs[0]["h"] + cs[0]["l"] + cs[0]["c"]) / 4
    for i, c in enumerate(cs):
        if i:
            ho = (ho + hc) / 2
            hc = (c["o"] + c["h"] + c["l"] + c["c"]) / 4
        out[i] = hc > ho
    return out


def adx(cs, n=14):
    """Wilder's DI+, DI-, ADX."""
    L = len(cs)
    pdi, mdi, ax = [None] * L, [None] * L, [None] * L
    if L <= 2 * n:
        return pdi, mdi, ax
    tr = true_range(cs)
    pdm, mdm = [0.0] * L, [0.0] * L
    for i in range(1, L):
        upm, dnm = cs[i]["h"] - cs[i - 1]["h"], cs[i - 1]["l"] - cs[i]["l"]
        pdm[i] = upm if upm > dnm and upm > 0 else 0.0
        mdm[i] = dnm if dnm > upm and dnm > 0 else 0.0
    st, sp, sm = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(mdm[1:n + 1])
    dxs, a = [], None
    for i in range(n, L):
        if i > n:
            st, sp, sm = st - st / n + tr[i], sp - sp / n + pdm[i], sm - sm / n + mdm[i]
        p, m = (100 * sp / st, 100 * sm / st) if st else (0.0, 0.0)
        pdi[i], mdi[i] = p, m
        dx = 100 * abs(p - m) / (p + m) if p + m else 0.0
        if a is None:
            dxs.append(dx)
            if len(dxs) == n:
                a = sum(dxs) / n
                ax[i] = a
        else:
            a = (a * (n - 1) + dx) / n
            ax[i] = a
    return pdi, mdi, ax


# ------------------------------------------------------------------ entry signals per family
FAMILY_NAMES = {"breakout": "Turtle/Donchian", "ema_cross": "cruzamento de médias", "macd": "MACD",
                "supertrend": "Supertrend", "rsi_rev": "RSI / RSI2 Connors", "bb_rev": "Bollinger volta à média",
                "squeeze": "Bollinger squeeze", "keltner": "Keltner", "psar": "Parabolic SAR",
                "ichimoku": "Ichimoku", "heikin": "Heikin-Ashi", "adx": "ADX/DI", "williams": "Larry Williams"}


def entry_configs():
    out = []
    for n in (20, 48, 96):
        for f in (False, True):
            out.append({"family": "breakout", "n": n, "trend": f})
    for fs in ((5, 13), (9, 21), (12, 26), (20, 50), (50, 200)):
        out.append({"family": "ema_cross", "fast": fs[0], "slow": fs[1]})
    for f in (False, True):
        out.append({"family": "macd", "fast": 12, "slow": 26, "sig": 9, "trend": f})
    for m in (2.0, 3.0):
        out.append({"family": "supertrend", "n": 10, "mult": m})
    for per, lv in ((2, (10, 90)), (2, (5, 95)), (2, (20, 80)), (14, (30, 70)), (14, (25, 75)), (14, (20, 80))):
        for f in (False, True):
            out.append({"family": "rsi_rev", "period": per, "low": lv[0], "high": lv[1], "trend": f})
    for k in (2.0, 2.5):
        for f in (False, True):
            out.append({"family": "bb_rev", "k": k, "trend": f})
    for pct in (0.10, 0.20):
        out.append({"family": "squeeze", "pct": pct})
    for m in (1.5, 2.0):
        for f in (False, True):
            out.append({"family": "keltner", "mult": m, "trend": f})
    out.append({"family": "psar", "step": 0.02, "max": 0.2})
    for cloud in (False, True):
        out.append({"family": "ichimoku", "cloud": cloud})
    for f in (False, True):
        out.append({"family": "heikin", "trend": f})
    for thr in (20, 25):
        out.append({"family": "adx", "thr": thr})
    for k in (0.5, 0.7):
        out.append({"family": "williams", "k": k})
    return out


def exit_configs():
    return [{"sl": sl, "rr": rr, "max_bars": mb} for sl in (1.0, 2.0) for rr in (1.0, 2.0, 3.0, None) for mb in (24, 96)]


class Data:
    def __init__(self, cs, tf=TF):
        self.cs, self.tf = cs, tf
        self.c = [x["c"] for x in cs]
        self.atr = atr(cs)
        self._cache = {}

    def get(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def ema(self, n):
        return self.get(("ema", n), lambda: ema(self.c, n))

    def rsi(self, n):
        return self.get(("rsi", n), lambda: rsi(self.c, n))

    def bb(self, k):
        return self.get(("bb", k), lambda: bollinger(self.c, 20, k))


def _flip(out, d, first):
    """Signal on every change of a +1/-1 direction series."""
    for i in range(max(first, 1), len(d)):
        if d[i] is not None and d[i - 1] is not None and d[i] != d[i - 1]:
            out[i] = "long" if d[i] > 0 else "short"


def _cross(out, a, b, first, ok=lambda i, side: True):
    for i in range(max(first, 1), len(a)):
        if None in (a[i], b[i], a[i - 1], b[i - 1]):
            continue
        if a[i - 1] <= b[i - 1] and a[i] > b[i] and ok(i, "long"):
            out[i] = "long"
        elif a[i - 1] >= b[i - 1] and a[i] < b[i] and ok(i, "short"):
            out[i] = "short"


def signals(data, e):
    """'long' / 'short' / None for each closed candle (no look-ahead)."""
    cs, c, n_all = data.cs, data.c, len(data.c)
    out = [None] * n_all
    trend = data.ema(200)
    fam = e["family"]

    def with_trend(i, side):
        if not e.get("trend"):
            return True
        return trend[i] is not None and (c[i] > trend[i] if side == "long" else c[i] < trend[i])

    if fam == "breakout":
        n = e["n"]
        for i in range(max(n, 200), n_all):
            hi = max(x["h"] for x in cs[i - n:i])
            lo = min(x["l"] for x in cs[i - n:i])
            if c[i] > hi and with_trend(i, "long"):
                out[i] = "long"
            elif c[i] < lo and with_trend(i, "short"):
                out[i] = "short"
    elif fam == "ema_cross":
        _cross(out, data.ema(e["fast"]), data.ema(e["slow"]), e["slow"] + 1)
    elif fam == "macd":
        line, sig = data.get(("macd", e["fast"], e["slow"], e["sig"]), lambda: _macd(c, e["fast"], e["slow"], e["sig"]))
        _cross(out, line, sig, 200, with_trend)
    elif fam == "supertrend":
        _flip(out, data.get(("st", e["n"], e["mult"]), lambda: supertrend(cs, e["n"], e["mult"])), 200)
    elif fam == "rsi_rev":
        r = data.rsi(e["period"])
        for i in range(200, n_all):
            if r[i] is None:
                continue
            if r[i] < e["low"] and with_trend(i, "long"):
                out[i] = "long"
            elif r[i] > e["high"] and with_trend(i, "short"):
                out[i] = "short"
    elif fam == "bb_rev":
        _, up, lo, _ = data.bb(e["k"])
        for i in range(200, n_all):
            if c[i] < lo[i] and with_trend(i, "long"):
                out[i] = "long"
            elif c[i] > up[i] and with_trend(i, "short"):
                out[i] = "short"
    elif fam == "squeeze":
        _, up, lo, width = data.bb(2.0)
        for i in range(300, n_all):
            past = sorted(w for w in width[i - 100:i] if w)
            if len(past) < 50:
                continue
            narrow = width[i - 1] is not None and width[i - 1] <= past[int(len(past) * e["pct"])]
            if narrow and c[i] > up[i] and c[i] > trend[i]:
                out[i] = "long"
            elif narrow and c[i] < lo[i] and c[i] < trend[i]:
                out[i] = "short"
    elif fam == "keltner":
        mid, a = data.ema(20), data.get(("atr", 10), lambda: atr(cs, 10))
        for i in range(200, n_all):
            if mid[i] is None or a[i] is None:
                continue
            if c[i] > mid[i] + e["mult"] * a[i] and with_trend(i, "long"):
                out[i] = "long"
            elif c[i] < mid[i] - e["mult"] * a[i] and with_trend(i, "short"):
                out[i] = "short"
    elif fam == "psar":
        _flip(out, data.get(("psar", e["step"], e["max"]), lambda: psar(cs, e["step"], e["max"])), 200)
    elif fam == "ichimoku":
        tk, kj, sb = (data.get(("mid", n), lambda n=n: midpoint(cs, n)) for n in (9, 26, 52))

        def cloud_ok(i, side):
            if not e["cloud"]:
                return True
            j = i - 26
            if j < 0 or None in (tk[j], kj[j], sb[j]):
                return False
            sa = (tk[j] + kj[j]) / 2
            return c[i] > max(sa, sb[j]) if side == "long" else c[i] < min(sa, sb[j])
        _cross(out, tk, kj, 200, cloud_ok)
    elif fam == "heikin":
        g = data.get(("ha",), lambda: heikin_green(cs))
        for i in range(200, n_all):
            if g[i] and not g[i - 1] and with_trend(i, "long"):
                out[i] = "long"
            elif not g[i] and g[i - 1] and with_trend(i, "short"):
                out[i] = "short"
    elif fam == "adx":
        p, m, ax = data.get(("adx", 14), lambda: adx(cs, 14))
        _cross(out, p, m, 200, lambda i, side: ax[i] is not None and ax[i] > e["thr"])
    elif fam == "williams":
        days = {}
        for x in cs:
            d = days.setdefault(x["t"] // 86400, [x["o"], x["h"], x["l"]])
            d[1], d[2] = max(d[1], x["h"]), min(d[2], x["l"])
        fired = set()
        for i in range(200, n_all):
            day = cs[i]["t"] // 86400
            prev = days.get(day - 1)
            if prev is None:
                continue
            rng, op = prev[1] - prev[2], days[day][0]
            if c[i] > op + e["k"] * rng and (day, 1) not in fired:
                out[i] = "long"
                fired.add((day, 1))
            elif c[i] < op - e["k"] * rng and (day, -1) not in fired:
                out[i] = "short"
                fired.add((day, -1))
    return out


def _macd(c, f, s, g):
    fa, sl = ema(c, f), ema(c, s)
    line = [None if a is None or b is None else a - b for a, b in zip(fa, sl)]
    return line, ema(line, g)


# ------------------------------------------------------------------ engine
def backtest(data, sig, x, lev, first, last, mid=None, start=START):
    """Entry at the next open; stop first if stop and target fall in the same candle; exit on the
    opposite signal or the time limit. Returns stats (+ equity at `mid` for the two-halves check)."""
    cs, a = data.cs, data.atr
    tf_h = getattr(data, "tf", TF) / 3600
    eq = peak = start
    dd = 0.0
    pos = pend = None
    trades = wins = 0
    gw = gl = 0.0
    eq_mid = None
    liq_move = LIQ_LOSS / lev

    def close(price, i, liq=False):
        nonlocal eq, pos, trades, wins, gw, gl
        hours = (i - pos["i"] + 1) * tf_h
        mv = (price - pos["e"]) / pos["e"] * pos["s"]
        pnl = -pos["col"] if liq else max(pos["size"] * mv - pos["size"] * (2 * FEE + BORROW_H * hours), -pos["col"])
        eq += pnl
        trades += 1
        if pnl > 0:
            wins += 1
            gw += pnl
        else:
            gl -= pnl
        pos = None

    for i in range(first, last):
        c = cs[i]
        if mid is not None and i == mid:
            eq_mid = eq
        if pend is not None:
            act, side, ai = pend
            pend = None
            if act == "exit" and pos:
                close(c["o"], i)
            if act == "enter" and pos is None and eq > 1 and ai:
                col = eq * FRACTION
                e = c["o"]
                s = 1 if side == "long" else -1
                sl_d = min(x["sl"] * ai, 0.7 * liq_move * e)
                pos = {"e": e, "s": s, "col": col, "size": col * lev, "i": i, "sl": e - s * sl_d,
                       "tp": None if x["rr"] is None else e + s * x["rr"] * sl_d, "liq": e * (1 - s * liq_move)}
        if pos:
            s = pos["s"]
            adverse, favour = (c["l"], c["h"]) if s > 0 else (c["h"], c["l"])
            if (adverse <= pos["sl"]) if s > 0 else (adverse >= pos["sl"]):
                gap = (c["o"] <= pos["sl"]) if s > 0 else (c["o"] >= pos["sl"])
                hit_liq = (adverse <= pos["liq"]) if s > 0 else (adverse >= pos["liq"])
                close(pos["liq"] if (gap and hit_liq) else (c["o"] if gap else pos["sl"]), i, liq=gap and hit_liq)
            elif pos["tp"] is not None and ((favour >= pos["tp"]) if s > 0 else (favour <= pos["tp"])):
                close(pos["tp"], i)
            elif i - pos["i"] >= x["max_bars"]:
                close(c["c"], i)
        sg = sig[i]
        if pos and sg and (sg == "long") != (pos["s"] > 0):
            pend = ("exit", None, None)
        elif pos is None and sg:
            pend = ("enter", sg, a[i])
        mtm = eq + (pos["size"] * (c["c"] - pos["e"]) / pos["e"] * pos["s"] if pos else 0)
        peak = max(peak, mtm)
        dd = max(dd, (peak - mtm) / peak if peak > 0 else 0)
        if eq <= 1:
            break
    if pos:
        close(cs[last - 1]["c"], last - 1)
    return {"final": eq, "ret": (eq / start - 1) * 100, "dd": dd * 100, "trades": trades,
            "win": wins / trades if trades else 0.0, "pf": gw / gl if gl else (99.0 if gw else 0.0),
            "eq_mid": eq_mid if eq_mid is not None else start}


def describe(e, x, tf=TF):
    fam = e["family"]
    tr = " a favor da EMA200" if e.get("trend") else ""
    if fam == "breakout":
        s = "Turtle: rompimento de " + str(e["n"]) + " velas" + tr
    elif fam == "ema_cross":
        s = ("golden cross " if (e["fast"], e["slow"]) == (50, 200) else "") + "cruzamento EMA " + str(e["fast"]) + "/" + str(e["slow"])
    elif fam == "macd":
        s = "MACD " + str(e["fast"]) + "/" + str(e["slow"]) + "/" + str(e["sig"]) + tr
    elif fam == "supertrend":
        s = "Supertrend " + str(e["n"]) + "/" + format(e["mult"], "g")
    elif fam == "rsi_rev":
        s = ("RSI2 Connors " if e["period"] == 2 else "RSI" + str(e["period"]) + " ") + str(e["low"]) + "/" + str(e["high"]) + tr
    elif fam == "bb_rev":
        s = "volta à média (Bollinger " + format(e["k"], "g") + ")" + tr
    elif fam == "squeeze":
        s = "squeeze de Bollinger (" + format(e["pct"] * 100, ".0f") + "% mais estreitas)"
    elif fam == "keltner":
        s = "rompimento do canal de Keltner " + format(e["mult"], "g") + " ATR" + tr
    elif fam == "psar":
        s = "Parabolic SAR " + format(e["step"], "g") + "/" + format(e["max"], "g")
    elif fam == "ichimoku":
        s = "Ichimoku Tenkan/Kijun" + (" com a nuvem" if e["cloud"] else "")
    elif fam == "heikin":
        s = "Heikin-Ashi virada de cor" + tr
    elif fam == "adx":
        s = "ADX > " + str(e["thr"]) + " com cruzamento DI"
    else:
        s = "Larry Williams rompimento de volatilidade k=" + format(e["k"], "g")
    hours = x["max_bars"] * tf / 3600
    span = format(hours, "g") + " h" if hours < 48 else format(hours / 24, "g") + " dias"
    return ("[" + TF_NAME.get(tf, str(tf // 60) + "m") + "] " + s + " · stop " + format(x["sl"], "g") + " ATR · alvo " +
            ("sem alvo" if x["rr"] is None else format(x["rr"], "g") + "× o stop") + " · máx. " + span)


# ------------------------------------------------------------------ search
def search(candles=None, progress=None, tfs=TFS):
    cs15 = candles or fetch_year()
    end_t = cs15[-1]["t"] + TF
    t0 = max(end_t - 365 * 86400, cs15[min(WARM, len(cs15) - 1)]["t"])
    split_t = t0 + (end_t - t0) * 2 // 3
    mid_t = t0 + (split_t - t0) // 2
    entries, exits = entry_configs(), exit_configs()
    tested, qualified, per_tf, ctx = 0, [], {}, {}
    step, total = 0, len(entries) * len(tfs)
    for tf in tfs:
        cs = resample(cs15, tf)
        times = [x["t"] for x in cs]
        first = max(bisect.bisect_left(times, t0), WARM)
        split = bisect.bisect_left(times, split_t)
        mid = bisect.bisect_left(times, mid_t)
        if split - first < 20 or len(cs) - split < 10:
            continue
        data = Data(cs, tf)
        ctx[tf] = (data, split)
        count = ok_count = 0
        for e in entries:
            sig = signals(data, e)
            for x in exits:
                r = backtest(data, sig, x, TEST_LEVERAGE, first, split, mid)
                count += 1
                ok = (r["trades"] >= MIN_TRADES_TF.get(tf, MIN_TRADES) and r["pf"] >= MIN_PF and r["eq_mid"] > START and
                      r["final"] > r["eq_mid"] and r["dd"] < MAX_DD * 100)
                if ok:
                    ok_count += 1
                    qualified.append((tf, e, x, r, sig))
            step += 1
            if progress:
                progress(step, total)
        tested += count
        per_tf[TF_NAME.get(tf, str(tf))] = {"tested": count, "qualified": ok_count}
    qualified.sort(key=lambda row: row[3]["final"], reverse=True)
    report = {"tested": tested, "qualified": len(qualified), "per_tf": per_tf,
              "families": len({e["family"] for e in entries}), "first": t0, "split": split_t,
              "last": cs15[-1]["t"], "top": [], "winner": None}
    # how much of the "edge" is luck: share of qualified variants that also make money out of sample
    oos_hits = 0
    best_oos = None
    for tf, e, x, r, sig in qualified:
        data, split = ctx[tf]
        o = backtest(data, sig, x, TEST_LEVERAGE, split, len(data.cs))
        oos_hits += o["final"] > START
        if best_oos is None or o["final"] > best_oos["oos"]["final"]:
            best_oos = {"desc": describe(e, x, tf), "dev": r, "oos": o}
        if len(report["top"]) < 5:
            report["top"].append({"desc": describe(e, x, tf), "dev": r, "oos": o})
    report["oos_hit_rate"] = oos_hits / len(qualified) if qualified else 0.0
    report["best_oos"] = best_oos
    if qualified:
        tf, e, x, r, sig = qualified[0]
        data, split = ctx[tf]
        o = report["top"][0]["oos"]
        passed = o["final"] > START and o["pf"] >= 1.1 and o["dd"] < 35
        report["winner"] = {"desc": describe(e, x, tf), "timeframe": TF_NAME.get(tf), "entry": e, "exit": x, "dev": r,
                            "oos": o, "passed": passed,
                            "by_leverage": {lev: backtest(data, sig, x, lev, split, len(data.cs)) for lev in LEVERAGES}}
    return report


def report_text(rep):
    day = lambda t: time.strftime("%d/%m/%Y", time.gmtime(t))
    pf = lambda v: "∞" if v >= 99 else format(v, ".2f")
    line = lambda r: (format(r["ret"], "+.0f") + "% · queda máx. " + format(r["dd"], ".0f") + "% · " + str(r["trades"]) +
                      " ops · acerto " + format(r["win"] * 100, ".0f") + "% · fator " + pf(r["pf"]))
    per = rep.get("per_tf") or {}
    L = ["🔬 PESQUISA BTC ALAVANCADO — " + format(rep["tested"], ",").replace(",", ".") + " testes de " +
         str(rep.get("families", 5)) + " estratégias clássicas" + (" em " + "/".join(per) if per else ""),
         "Tipos: " + ", ".join(FAMILY_NAMES.values()),
         "Escolha: " + day(rep["first"]) + " → " + day(rep["split"]) + " (8 meses) · Conferência: " + day(rep["split"]) +
         " → " + day(rep["last"]) + " (4 meses que a escolha não viu) · teste em " + str(TEST_LEVERAGE) + "x, " +
         format(FRACTION * 100, ".0f") + "% da conta, com taxas",
         "Passaram nos filtros dos 8 meses (ops mínimas 60/40/20 em 15m/1h/4h, fator ≥ " + format(MIN_PF, "g") +
         ", lucro nas duas metades, queda < " + format(MAX_DD * 100, ".0f") + "%): " + str(rep["qualified"]) + " de " +
         str(rep["tested"])]
    if per:
        L.append("Por gráfico: " + " · ".join(k + " " + str(v["qualified"]) + "/" + str(v["tested"]) for k, v in per.items()))
    if rep["qualified"]:
        L.append("Dessas, " + format(rep["oos_hit_rate"] * 100, ".0f") + "% também deram lucro nos 4 meses seguintes "
                 "(perto de 50% = é sorte, não vantagem; com tantos testes, várias passam só por sorte).")
        L += ["", "Top 5 nos 8 meses → resultado nos 4 meses de conferência:"]
        for t in rep["top"]:
            L.append("• " + t["desc"] + "\n  escolha: " + line(t["dev"]) + "\n  conferência: " + line(t["oos"]))
        b = rep.get("best_oos")
        if b:
            L += ["", "💰 Maior lucro nos 4 meses entre as aprovadas: " + b["desc"] + "\n  " + line(b["oos"]) +
                  "\n  (só informação: escolher pelo resultado já visto engana — no futuro tende a ir pior)"]
        w = rep["winner"]
        L += ["", "🏆 Escolhida (só pelos 8 meses): " + w["desc"], "Conferência por alavancagem:"]
        for lev, r in w["by_leverage"].items():
            L.append(str(lev) + "x → US$ " + format(r["final"], ".2f") + " (" + format(r["ret"], "+.0f") + "%) · queda máx. " +
                     format(r["dd"], ".0f") + "%")
        L += ["", ("✅ PASSOU: deu lucro também nos 4 meses que não viu. Ainda assim, teste em simulação antes de "
                   "qualquer dinheiro real e use alavancagem baixa." if w["passed"] else
                   "❌ NÃO PASSOU: a melhor dos 8 meses não se confirmou nos 4 meses seguintes. Nenhuma vantagem "
                   "confiável encontrada. Não recomendo dinheiro real.")]
    else:
        L += ["", "❌ Nenhuma estratégia passou nos filtros dos 8 meses. Não há vantagem confiável nessas regras."]
    L.append("Pesquisa histórica, nenhuma ordem enviada. Não é promessa de lucro.")
    return "\n".join(L)


def save(rep, path):
    clean = {k: v for k, v in rep.items() if k != "winner"}
    if rep.get("winner"):
        clean["winner"] = {k: v for k, v in rep["winner"].items() if k != "by_leverage"}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, default=str)
