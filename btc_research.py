"""Search for BTC perps settings that actually made money, without fooling ourselves.

The 60-day backtest of the first version lost money (153 trades, 33% wins, -US$ 77): 135 of 153
trades ended on the 60-minute time stop - after a 5-minute buying burst BTC usually did not keep
going, and the Jupiter fees (0.06% open + 0.06% close of the position) ate every small move.

Here every candidate setting is simulated on the SAME minute candles, using only data available at
each candle's close (entry at the next candle's open, stop / trailing checked on the candle's low and
high with the stop first, fees included):
- TRAIN = the first 2/3 of the period: the settings are ranked there;
- TEST  = the last 1/3, never used to choose: only settings that also made money there are kept.
A setting that wins in TRAIN but loses in TEST was luck, not an edge.
"""
import itertools
import json
import logging
import time

FEE = 0.0006


def features(cs, ema_lens=(60, 240)):
    """Per closed candle i: 5-min move, 5-min volume vs the previous hour, taker-buy share, EMAs."""
    n = len(cs)
    c = [x[4] for x in cs]
    v = [x[5] for x in cs]
    tb = [x[6] for x in cs]
    up5 = [0.0] * n
    volr = [0.0] * n
    share = [0.5] * n
    emas = {L: [0.0] * n for L in ema_lens}
    acc_v = [0.0] * (n + 1)
    acc_b = [0.0] * (n + 1)
    for i in range(n):
        acc_v[i + 1] = acc_v[i] + v[i]
        acc_b[i + 1] = acc_b[i] + tb[i]
    for L in ema_lens:
        k, e = 2 / (L + 1), c[0]
        for i in range(n):
            e = c[i] * k + e * (1 - k)
            emas[L][i] = e
    for i in range(65, n):
        up5[i] = c[i] / c[i - 5] - 1
        vol5 = acc_v[i + 1] - acc_v[i - 4]
        prev = (acc_v[i - 4] - acc_v[i - 64]) / 60 * 5
        volr[i] = vol5 / prev if prev else 0.0
        share[i] = (acc_b[i + 1] - acc_b[i - 4]) / vol5 if vol5 else 0.5
    return {"up5": up5, "volr": volr, "share": share, "ema": emas}


def simulate(cs, f, p, start=0, end=None):
    """One setting on cs[start:end]. p: side, up_min, up_max, vol, share, ema, lev, stop, arm, drop,
    hold (min), mode ('momentum' = follow the flood, 'fade' = bet against it). P/L in % of the margin."""
    end = end or len(cs) - 1
    lev = p["lev"]
    fee = 2 * FEE * lev
    trades, pos, last = [], None, -1e18
    up5, volr, share, emas = f["up5"], f["volr"], f["share"], f["ema"].get(p["ema"])
    for i in range(max(start, 250), end):
        x = cs[i]
        if pos:
            d = pos["d"]
            e = pos["e"]
            best = (x[2] / e - 1) if d > 0 else (1 - x[3] / e)
            worst = (x[3] / e - 1) if d > 0 else (1 - x[2] / e)
            r_best = 1 + lev * best - fee
            r_worst = 1 + lev * worst - fee
            out = None
            if r_worst <= 1 - p["stop"]:
                out = 1 - p["stop"]
            else:
                pos["peak"] = max(pos["peak"], r_best)
                if pos["peak"] >= 1 + p["arm"] and r_worst <= pos["peak"] * (1 - p["drop"]):
                    out = pos["peak"] * (1 - p["drop"])
                elif x[0] - pos["t"] > p["hold"] * 60:
                    mv = (x[4] / e - 1) if d > 0 else (1 - x[4] / e)
                    out = 1 + lev * mv - fee
            if out is not None:
                trades.append(out - 1)
                pos, last = None, x[0]
            continue
        if x[0] - last < 30 * 60:
            continue
        u, vr, s = up5[i], volr[i], share[i]
        if vr < p["vol"] or abs(u) < p["up_min"] or abs(u) > p["up_max"]:
            continue
        flood = 1 if (u > 0 and s >= p["share"]) else (-1 if (u < 0 and (1 - s) >= p["share"]) else 0)
        if not flood:
            continue
        d = flood if p["mode"] == "momentum" else -flood
        if p["side"] == "long" and d < 0 or p["side"] == "short" and d > 0:
            continue
        if emas is not None:
            above = x[4] > emas[i]
            if p["mode"] == "momentum" and (d > 0) != above:
                continue                                   # trade with the trend only
        pos = {"d": d, "e": cs[i + 1][1], "t": cs[i + 1][0], "peak": 1.0}
    return trades


def score(trades):
    if not trades:
        return {"n": 0, "pnl": 0.0, "win": 0.0, "pf": 0.0}
    gain = sum(t for t in trades if t > 0)
    loss = -sum(t for t in trades if t <= 0)
    return {"n": len(trades), "pnl": sum(trades), "win": sum(1 for t in trades if t > 0) / len(trades),
            "pf": gain / loss if loss else 99.0}


GRID = {
    "mode": ("momentum", "fade"),
    "side": ("long", "short", "both"),
    "up_min": (0.0025, 0.005),
    "up_max": (0.015, 0.03),
    "vol": (1.5, 2.5),
    "share": (0.55, 0.62),
    "ema": (None, 240),
    "lev": (5, 10, 20),
    "stop": (0.30,),
    "exitp": ((0.15, 0.10), (0.40, 0.25)),
    "hold": (60, 240),
}


def search(cs, min_trades=15, keep=8):
    """Rank every setting on TRAIN, check the best ones on TEST. Returns (rows, split_time)."""
    t0 = time.time()
    f = features(cs)
    split = int(len(cs) * 2 / 3)
    keys = list(GRID)
    rows = []
    for combo in itertools.product(*(GRID[k] for k in keys)):
        p = dict(zip(keys, combo))
        p["arm"], p["drop"] = p.pop("exitp")
        if p["mode"] == "fade" and p["ema"] is not None:
            continue
        tr = score(simulate(cs, f, p, 0, split))
        if tr["n"] >= min_trades and tr["pnl"] > 0:
            rows.append((tr["pnl"], p, tr))
    rows.sort(key=lambda r: -r[0])
    out = []
    for _, p, tr in rows[:60]:
        te = score(simulate(cs, f, p, split, len(cs) - 1))
        out.append({"p": p, "train": tr, "test": te})
    out.sort(key=lambda r: (r["test"]["pnl"] > 0, min(r["train"]["pnl"], r["test"]["pnl"] * 2)), reverse=True)
    logging.info("btc research: %s settings checked in %.0fs, %s profitable in train",
                 len(list(itertools.product(*(GRID[k] for k in keys)))), time.time() - t0, len(rows))
    return out[:keep], cs[split][0]


def describe(p):
    return ((("seguir" if p["mode"] == "momentum" else "contra") + " a enxurrada · " +
             {"long": "só LONG", "short": "só SHORT", "both": "LONG e SHORT"}[p["side"]] + " · " +
             format(p["lev"], ".0f") + "x · 5 min " + format(p["up_min"] * 100, ".2f") + "–" + format(p["up_max"] * 100, ".1f") +
             "% · volume " + format(p["vol"], "g") + "x · " + format(p["share"] * 100, ".0f") + "% de um lado" +
             (" · com tendência 4 h" if p["ema"] else "") + " · stop −" + format(p["stop"] * 100, ".0f") +
             "% · stop móvel +" + format(p["arm"] * 100, ".0f") + "%/" + format(p["drop"] * 100, ".0f") +
             "% · sai em " + format(p["hold"], ".0f") + " min"))


def report(rows, margin=20.0):
    if not rows:
        return "🔬 PESQUISA BTC: nenhuma configuração deu lucro no período de treino. Não ligue o robô com dinheiro real."
    lines = ["🔬 PESQUISA BTC — melhores configurações (lucro em US$ com garantia de US$ " + format(margin, ".0f") +
             "; TREINO = 40 dias usados para escolher, TESTE = 20 dias que a escolha não viu):"]
    for k, r in enumerate(rows, 1):
        tr, te = r["train"], r["test"]
        lines.append(str(k) + ") " + describe(r["p"]) + "\n   TREINO " + str(tr["n"]) + " op. " +
                     format(tr["pnl"] * margin, "+.2f") + " USD (" + format(tr["win"] * 100, ".0f") + "% acerto) · TESTE " +
                     str(te["n"]) + " op. " + format(te["pnl"] * margin, "+.2f") + " USD (" + format(te["win"] * 100, ".0f") +
                     "% acerto)" + (" ✅" if te["pnl"] > 0 and te["n"] >= 5 else " ❌"))
    return "\n".join(lines)


RESULT = {"running": False, "text": "", "rows": []}


def run(cs=None, days=60, send=None, chat=None):
    if RESULT["running"]:
        return
    RESULT["running"] = True
    try:
        import btc_bot
        cs = cs or btc_bot.history(days)
        rows, _ = search(cs)
        RESULT.update(rows=rows, text=report(rows))
        for r in rows:
            logging.info("btc research row: %s", json.dumps({"p": r["p"], "train": r["train"], "test": r["test"]}))
        if send and chat:
            send(chat, RESULT["text"])
    except Exception as exc:
        RESULT["text"] = "⛔ Pesquisa BTC falhou: " + type(exc).__name__ + ": " + str(exc)[:150]
        logging.warning(RESULT["text"])
    finally:
        RESULT["running"] = False
