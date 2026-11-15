"""Disciplined search for a leveraged BTC strategy (research only: places no orders).

Tests ~430 fixed variants from 5 different families on 1 year of real 15-minute BTC candles:
- breakout:   close beyond the high/low of the last N candles (optional EMA200 trend filter)
- ema_cross:  fast EMA crosses the slow EMA
- rsi_rev:    RSI oversold/overbought, only in the direction of the EMA200 trend (optional)
- bb_rev:     close outside the Bollinger band, betting on the return (optional trend filter)
- squeeze:    Bollinger bands unusually narrow, then a close outside the band
each with exits: stop 1 or 2 ATR, target 1/2/3x the stop or none, time limit 6 h or 24 h,
and exit on the opposite signal.

Honest selection (testing many variants on one history always finds lucky ones):
1. development = first 8 months; out of sample = last 4 months, never used to choose;
2. a variant only qualifies with >= 60 trades, profit factor >= 1.15, profit in BOTH halves of
   the development period and max drawdown < 40%;
3. the winner is picked on development only; the 4 out-of-sample months then decide.
Costs: Jupiter-like 0.06% per side + borrow; 20% of the account as collateral; 2x for the test.
"""
import json
import os
import time

import requests

TF = 900
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

_http = requests.Session()


# ------------------------------------------------------------------ data
def _get(url):
    r = _http.get(url, timeout=15, headers={"User-Agent": "BTCResearch/1.0"})
    r.raise_for_status()
    return r.json()


def fetch_year(days=365, warm=400, now=None):
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


# ------------------------------------------------------------------ indicators (index i uses data <= i)
def ema(v, n):
    out, k = [None] * len(v), 2 / (n + 1)
    if len(v) < n:
        return out
    p = sum(v[:n]) / n
    out[n - 1] = p
    for i in range(n, len(v)):
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


def atr(cs, n=14):
    out = [None] * len(cs)
    tr = [cs[0]["h"] - cs[0]["l"]] + [max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"]))
                                      for p, c in zip(cs, cs[1:])]
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


# ------------------------------------------------------------------ entry signals per family
def entry_configs():
    out = []
    for n in (20, 48, 96):
        for f in (False, True):
            out.append({"family": "breakout", "n": n, "trend": f})
    for fs in ((9, 21), (20, 50), (50, 200)):
        out.append({"family": "ema_cross", "fast": fs[0], "slow": fs[1]})
    for per, lv in ((2, (10, 90)), (2, (5, 95)), (2, (20, 80)), (14, (30, 70)), (14, (25, 75)), (14, (20, 80))):
        for f in (False, True):
            out.append({"family": "rsi_rev", "period": per, "low": lv[0], "high": lv[1], "trend": f})
    for k in (2.0, 2.5):
        for f in (False, True):
            out.append({"family": "bb_rev", "k": k, "trend": f})
    for pct in (0.10, 0.20):
        out.append({"family": "squeeze", "pct": pct})
    return out


def exit_configs():
    return [{"sl": sl, "rr": rr, "max_bars": mb} for sl in (1.0, 2.0) for rr in (1.0, 2.0, 3.0, None) for mb in (24, 96)]


class Data:
    def __init__(self, cs):
        self.cs = cs
        self.c = [x["c"] for x in cs]
        self.atr = atr(cs)
        self._ema, self._rsi, self._bb = {}, {}, {}

    def ema(self, n):
        if n not in self._ema:
            self._ema[n] = ema(self.c, n)
        return self._ema[n]

    def rsi(self, n):
        if n not in self._rsi:
            self._rsi[n] = rsi(self.c, n)
        return self._rsi[n]

    def bb(self, k):
        if k not in self._bb:
            self._bb[k] = bollinger(self.c, 20, k)
        return self._bb[k]


def signals(data, e):
    """'long' / 'short' / None for each closed candle (no look-ahead)."""
    cs, c, n_all = data.cs, data.c, len(data.c)
    out = [None] * n_all
    trend = data.ema(200)
    fam = e["family"]
    if fam == "breakout":
        n = e["n"]
        for i in range(max(n, 200), n_all):
            hi = max(x["h"] for x in cs[i - n:i])
            lo = min(x["l"] for x in cs[i - n:i])
            if c[i] > hi and (not e["trend"] or c[i] > trend[i]):
                out[i] = "long"
            elif c[i] < lo and (not e["trend"] or c[i] < trend[i]):
                out[i] = "short"
    elif fam == "ema_cross":
        f, s = data.ema(e["fast"]), data.ema(e["slow"])
        for i in range(e["slow"] + 1, n_all):
            if None in (f[i], s[i], f[i - 1], s[i - 1]):
                continue
            if f[i - 1] <= s[i - 1] and f[i] > s[i]:
                out[i] = "long"
            elif f[i - 1] >= s[i - 1] and f[i] < s[i]:
                out[i] = "short"
    elif fam == "rsi_rev":
        r = data.rsi(e["period"])
        for i in range(200, n_all):
            if r[i] is None:
                continue
            if r[i] < e["low"] and (not e["trend"] or c[i] > trend[i]):
                out[i] = "long"
            elif r[i] > e["high"] and (not e["trend"] or c[i] < trend[i]):
                out[i] = "short"
    elif fam == "bb_rev":
        _, up, lo, _ = data.bb(e["k"])
        for i in range(200, n_all):
            if c[i] < lo[i] and (not e["trend"] or c[i] > trend[i]):
                out[i] = "long"
            elif c[i] > up[i] and (not e["trend"] or c[i] < trend[i]):
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
    return out


# ------------------------------------------------------------------ engine
def backtest(data, sig, x, lev, first, last, mid=None, start=START):
    """Entry at the next open; stop first if stop and target fall in the same candle; exit on the
    opposite signal or the time limit. Returns stats (+ equity at `mid` for the two-halves check)."""
    cs, a = data.cs, data.atr
    eq = peak = start
    dd = 0.0
    pos = pend = None
    trades = wins = 0
    gw = gl = 0.0
    eq_mid = None
    liq_move = LIQ_LOSS / lev

    def close(price, i, liq=False):
        nonlocal eq, pos, trades, wins, gw, gl
        hours = (i - pos["i"] + 1) * TF / 3600
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


def describe(e, x):
    fam = e["family"]
    if fam == "breakout":
        s = "rompimento de " + str(e["n"]) + " velas" + (" a favor da EMA200" if e["trend"] else "")
    elif fam == "ema_cross":
        s = "cruzamento EMA " + str(e["fast"]) + "/" + str(e["slow"])
    elif fam == "rsi_rev":
        s = "RSI" + str(e["period"]) + " " + str(e["low"]) + "/" + str(e["high"]) + (" a favor da EMA200" if e["trend"] else "")
    elif fam == "bb_rev":
        s = "volta à média (Bollinger " + format(e["k"], "g") + ")" + (" a favor da EMA200" if e["trend"] else "")
    else:
        s = "squeeze de Bollinger (" + format(e["pct"] * 100, ".0f") + "% mais estreitas)"
    return (s + " · stop " + format(x["sl"], "g") + " ATR · alvo " + ("sem alvo" if x["rr"] is None else
            format(x["rr"], "g") + "× o stop") + " · máx. " + format(x["max_bars"] * TF / 3600, "g") + " h")


# ------------------------------------------------------------------ search
def search(candles=None, progress=None):
    cs = candles or fetch_year()
    data = Data(cs)
    first = max(len(cs) - 365 * 96, 400)
    split = first + int((len(cs) - first) * 2 / 3)
    mid = first + (split - first) // 2
    entries, exits = entry_configs(), exit_configs()
    rows, qualified = [], []
    for k, e in enumerate(entries):
        sig = signals(data, e)
        for x in exits:
            r = backtest(data, sig, x, TEST_LEVERAGE, first, split, mid)
            h1 = r["eq_mid"] > START
            h2 = r["final"] > r["eq_mid"]
            ok = r["trades"] >= MIN_TRADES and r["pf"] >= MIN_PF and h1 and h2 and r["dd"] < MAX_DD * 100
            rows.append((e, x, r, ok, sig))
            if ok:
                qualified.append(rows[-1])
        if progress:
            progress(k + 1, len(entries))
    qualified.sort(key=lambda row: row[2]["final"], reverse=True)
    report = {"tested": len(rows), "qualified": len(qualified), "first": cs[first]["t"], "split": cs[split]["t"],
              "last": cs[-1]["t"], "top": [], "winner": None}
    # how much of the "edge" is luck: share of qualified variants that also make money out of sample
    oos_hits = 0
    for e, x, r, ok, sig in qualified:
        o = backtest(data, sig, x, TEST_LEVERAGE, split, len(cs))
        oos_hits += o["final"] > START
        if len(report["top"]) < 5:
            report["top"].append({"desc": describe(e, x), "dev": r, "oos": o})
    report["oos_hit_rate"] = oos_hits / len(qualified) if qualified else 0.0
    if qualified:
        e, x, r, _, sig = qualified[0]
        o = report["top"][0]["oos"]
        passed = o["final"] > START and o["pf"] >= 1.1 and o["dd"] < 35
        report["winner"] = {"desc": describe(e, x), "entry": e, "exit": x, "dev": r, "oos": o, "passed": passed,
                            "by_leverage": {lev: backtest(data, sig, x, lev, split, len(cs)) for lev in LEVERAGES}}
    return report


def report_text(rep):
    day = lambda t: time.strftime("%d/%m/%Y", time.gmtime(t))
    pf = lambda v: "∞" if v >= 99 else format(v, ".2f")
    line = lambda r: (format(r["ret"], "+.0f") + "% · queda máx. " + format(r["dd"], ".0f") + "% · " + str(r["trades"]) +
                      " ops · acerto " + format(r["win"] * 100, ".0f") + "% · fator " + pf(r["pf"]))
    L = ["🔬 PESQUISA BTC ALAVANCADO — " + str(rep["tested"]) + " estratégias de 5 tipos",
         "Escolha: " + day(rep["first"]) + " → " + day(rep["split"]) + " (8 meses) · Conferência: " + day(rep["split"]) +
         " → " + day(rep["last"]) + " (4 meses que a escolha não viu) · teste em " + str(TEST_LEVERAGE) + "x, " +
         format(FRACTION * 100, ".0f") + "% da conta, com taxas",
         "Passaram nos filtros dos 8 meses (≥ " + str(MIN_TRADES) + " ops, fator ≥ " + format(MIN_PF, "g") +
         ", lucro nas duas metades, queda < " + format(MAX_DD * 100, ".0f") + "%): " + str(rep["qualified"]) + " de " +
         str(rep["tested"])]
    if rep["qualified"]:
        L.append("Dessas, " + format(rep["oos_hit_rate"] * 100, ".0f") + "% também deram lucro nos 4 meses seguintes "
                 "(perto de 50% = é sorte, não vantagem).")
        L += ["", "Top 5 nos 8 meses → resultado nos 4 meses de conferência:"]
        for t in rep["top"]:
            L.append("• " + t["desc"] + "\n  escolha: " + line(t["dev"]) + "\n  conferência: " + line(t["oos"]))
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
