"""Signal quality (owner's request Nov 14): only the best Sniper signals, aiming at 70%+ hits.

Nobody can promise that a memecoin goes up, so this MEASURES it instead of guessing:
- every candidate gets a score 0-100 (buy pressure, healthy move, pool size vs market cap, volume);
- every signal sent is followed with the live DexScreener price: HIT when it reaches +HIT_UP (TP1)
  before falling -MISS_DOWN (stop), MISS otherwise (or after 24 h without reaching TP1);
- after MIN_RESOLVED measured signals, the minimum score is chosen automatically: the lowest score whose
  signals hit >= TARGET (70%) with at least MIN_BAND samples. If no score reaches 70% yet, it uses the
  best-measured one and says so in the signal (honest number, never an invented one).
Until enough data exists, only signals with score >= START_SCORE go out.
"""
import json
import os
import tempfile
import threading
import time

import signals as sg

HIT_UP = sg._f("SIGNAL_Q_HIT_UP", 0.15)
MISS_DOWN = sg._f("SIGNAL_Q_MISS_DOWN", 0.30)
WINDOW_S = sg._f("SIGNAL_Q_WINDOW_HOURS", 24) * 3600
TARGET = sg._f("SIGNAL_Q_TARGET", 0.70)
START_SCORE = sg._f("SIGNAL_Q_START_SCORE", 60)
MIN_RESOLVED = int(sg._f("SIGNAL_Q_MIN_RESOLVED", 30))
MIN_BAND = int(sg._f("SIGNAL_Q_MIN_BAND", 12))
STEPS = tuple(range(40, 96, 5))

_LOCK = threading.RLock()


def _path():
    import live_trade_state
    return live_trade_state._path().with_name("signal_quality.json")


def _load():
    d = {"open": {}, "done": []}
    p = _path()
    if p.exists():
        try:
            d.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return d


def _save(d):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".sq.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def _n(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def score(p):
    """0-100: how good the setup looks right now (calibrated later by the measured hits)."""
    tx = p.get("txns") or {}
    m5, h1 = tx.get("m5") or {}, tx.get("h1") or {}
    ch = p.get("priceChange") or {}
    b5, s5 = _n(m5.get("buys")), _n(m5.get("sells"))
    b1, s1 = _n(h1.get("buys")), _n(h1.get("sells"))
    liq = _n((p.get("liquidity") or {}).get("usd"))
    mcap = _n(p.get("marketCap") or p.get("fdv"))
    vol1 = _n((p.get("volume") or {}).get("h1"))
    up5, up1 = _n(ch.get("m5")), _n(ch.get("h1"))
    pts = 0
    ratio5 = b5 / max(s5, 1)
    pts += 25 if ratio5 >= 3 else 18 if ratio5 >= 2 else 10 if ratio5 >= 1.5 else 0
    share1 = b1 / max(b1 + s1, 1)
    pts += 15 if share1 >= 0.65 else 10 if share1 >= 0.58 else 3 if share1 >= 0.52 else 0
    pts += 20 if 5 <= up5 <= 30 else 10 if 2 <= up5 < 5 or 30 < up5 <= 50 else 0
    pts += 15 if 0 <= up1 <= 100 else 8 if 100 < up1 <= 200 else 0
    pool = liq / mcap if mcap else 0
    pts += 15 if pool >= 0.15 else 8 if pool >= 0.08 else 0
    turn = vol1 / liq if liq else 0
    pts += 10 if turn >= 1 else 5 if turn >= 0.5 else 0
    return int(pts)


def record(p, sc, now=None):
    now = now or time.time()
    mint = p["baseToken"]["address"]
    price = _n(p.get("priceUsd"))
    if price <= 0:
        return
    with _LOCK:
        d = _load()
        if mint in d["open"]:
            return
        d["open"][mint] = {"symbol": (p.get("baseToken") or {}).get("symbol") or mint[:6], "entry": price,
                           "at": now, "score": sc, "peak": 1.0, "low": 1.0}
        _save(d)


def open_mints():
    return list(_load()["open"])


def update(prices, now=None):
    """Follow every open signal with the live prices {mint: price} (checked every scan, so the first
    level touched wins: +HIT_UP = hit, -MISS_DOWN = miss); returns the ones resolved now."""
    now = now or time.time()
    out = []
    with _LOCK:
        d = _load()
        for mint, s in list(d["open"].items()):
            price = prices.get(mint)
            hit = None
            if price and price > 0:
                r = price / s["entry"]
                s["peak"], s["low"] = max(s["peak"], r), min(s["low"], r)
                if r <= 1 - MISS_DOWN:
                    hit = False
                elif r >= 1 + HIT_UP:
                    hit = True
            if hit is None and now - s["at"] > WINDOW_S:
                hit = False
            if hit is not None:
                res = dict(s, mint=mint, hit=hit, closed=now)
                d["done"] = d["done"][-1000:] + [res]
                d["open"].pop(mint)
                out.append(res)
        _save(d)
    return out


def band(done, t):
    xs = [x for x in done if x["score"] >= t]
    return len(xs), (sum(1 for x in xs if x["hit"]) / len(xs) if xs else 0.0)


def threshold(done=None):
    """(minimum score, measured hit rate of that band, samples, reaches_target)."""
    done = _load()["done"] if done is None else done
    if len(done) < MIN_RESOLVED:
        n, rate = band(done, START_SCORE)
        return START_SCORE, rate, n, False
    best = None
    for t in STEPS:
        n, rate = band(done, t)
        if n < MIN_BAND:
            continue
        if rate >= TARGET:
            return t, rate, n, True
        if best is None or rate > best[1]:
            best = (t, rate, n, False)
    if best:
        return best
    n, rate = band(done, START_SCORE)
    return START_SCORE, rate, n, False


def line(sc):
    """Text for the signal: its score and the honest measured hit rate."""
    done = _load()["done"]
    t, rate, n, ok = threshold(done)
    if len(done) < MIN_RESOLVED:
        return ("⭐ Nota " + str(sc) + "/100 · medindo o acerto: " + str(len(done)) + "/" + str(MIN_RESOLVED) +
                " sinais conferidos (acerto = subir +" + format(HIT_UP * 100, ".0f") + "% antes de cair −" +
                format(MISS_DOWN * 100, ".0f") + "%)")
    return ("⭐ Nota " + str(sc) + "/100 · acerto medido dos sinais com nota " + str(t) + "+: " +
            format(rate * 100, ".0f") + "% (" + str(n) + " sinais)" + ("" if ok else " — ainda abaixo da meta de " +
                                                                       format(TARGET * 100, ".0f") + "%"))


def status_text():
    d = _load()
    done = d["done"]
    t, rate, n, ok = threshold(done)
    hits = sum(1 for x in done if x["hit"])
    return ("🎯 Qualidade dos sinais: " + str(len(done)) + " conferidos, " + str(hits) + " acertos (" +
            (format(hits / len(done) * 100, ".0f") + "%" if done else "—") + ") · só manda nota " + str(t) + "+ · " +
            ("acerto dessa faixa " + format(rate * 100, ".0f") + "% em " + str(n) + " sinais" if n else "faixa ainda sem dados") +
            (" ✅ meta de " + format(TARGET * 100, ".0f") + "% atingida" if ok else "") + " · " + str(len(d["open"])) +
            " sinais sendo acompanhados")
