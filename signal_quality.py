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
X2 = sg._f("SIGNAL_Q_X2", 2.0)                      # also measured: how many signals made 2x
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
    """Follow every open signal with the live prices {mint: price} (checked every scan, so the first level
    touched wins). Hit: +HIT_UP before -MISS_DOWN. After a hit the signal stays followed to see if it makes 2x
    (until 2x, -MISS_DOWN from the entry, or WINDOW_S). Returns the ones resolved now."""
    now = now or time.time()
    out = []
    with _LOCK:
        d = _load()
        for mint, s in list(d["open"].items()):
            price = prices.get(mint)
            done = False
            if price and price > 0:
                r = price / s["entry"]
                s["peak"], s["low"] = max(s["peak"], r), min(s["low"], r)
                if "hit" not in s:
                    if r <= 1 - MISS_DOWN:
                        s["hit"], done = False, True
                    elif r >= 1 + HIT_UP:
                        s["hit"] = True
                if s.get("hit") and (r >= X2 or r <= 1 - MISS_DOWN):
                    done = True
            if not done and now - s["at"] > WINDOW_S:
                s.setdefault("hit", False)
                done = True
            if done:
                res = dict(s, mint=mint, hit=bool(s.get("hit")), x2=s["peak"] >= X2, closed=now)
                d["done"] = d["done"][-1000:] + [res]
                d["open"].pop(mint)
                out.append(res)
        _save(d)
    return out


def measured(d=None):
    """Signals whose hit / miss is already known: closed ones + open ones that already hit +15%
    (still followed for 2x) - so the hit rate is not biased toward misses, which close sooner."""
    d = _load() if d is None else d
    return d["done"] + [dict(s, hit=True) for s in d["open"].values() if "hit" in s]


def x2_rate(done=None):
    done = _load()["done"] if done is None else done
    return len(done), (sum(1 for x in done if x.get("x2")) / len(done) if done else 0.0)


def band(done, t):
    xs = [x for x in done if x["score"] >= t]
    return len(xs), (sum(1 for x in xs if x["hit"]) / len(xs) if xs else 0.0)


def threshold(done=None):
    """(minimum score, measured hit rate of that band, samples, reaches_target)."""
    done = measured() if done is None else done
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
    done = measured()
    t, rate, n, ok = threshold(done)
    if len(done) < MIN_RESOLVED:
        return ("⭐ Nota " + str(sc) + "/100 · medindo o acerto: " + str(len(done)) + "/" + str(MIN_RESOLVED) +
                " sinais conferidos (acerto = subir +" + format(HIT_UP * 100, ".0f") + "% antes de cair −" +
                format(MISS_DOWN * 100, ".0f") + "%)")
    n2, r2 = x2_rate(_load()["done"])
    return ("⭐ Nota " + str(sc) + "/100 · fizeram 2x: " + format(r2 * 100, ".0f") + "% dos " + str(n2) +
            " sinais conferidos · acerto medido dos sinais com nota " + str(t) + "+: " +
            format(rate * 100, ".0f") + "% (" + str(n) + " sinais)" + ("" if ok else " — ainda abaixo da meta de " +
                                                                       format(TARGET * 100, ".0f") + "%"))


def status_text():
    d = _load()
    done = measured(d)
    t, rate, n, ok = threshold(done)
    hits = sum(1 for x in done if x["hit"])
    return ("🎯 Qualidade dos sinais: " + str(len(done)) + " conferidos, " + str(hits) + " acertos (" +
            (format(hits / len(done) * 100, ".0f") + "%" if done else "—") + ") · só manda nota " + str(t) + "+ · " +
            ("acerto dessa faixa " + format(rate * 100, ".0f") + "% em " + str(n) + " sinais" if n else "faixa ainda sem dados") +
            (" ✅ meta de " + format(TARGET * 100, ".0f") + "% atingida" if ok else "") + " · " + str(len(d["open"])) +
            " sinais sendo acompanhados · 🚀 fizeram 2x: " + str(sum(1 for x in done if x.get("x2"))) + " de " + str(len(done)))
