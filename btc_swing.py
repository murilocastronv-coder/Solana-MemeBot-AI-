"""BTC on longer timeframes (1-hour candles, 1 year): look for an edge that is bigger than the fees.

The 1-minute "buying flood" idea had no edge (2,304 settings, the best ones lost on unseen days).
Here three classic families are simulated on 1-hour candles, with the same honesty rules:
- breakout (trend following): LONG when the close breaks the highest high of the last N hours,
  SHORT when it breaks the lowest low (optionally only with the 200-hour trend);
- flood (the owner's idea, slower): an hour with a lot more volume than usual and most of it on one
  side; follow that side;
- reversion: RSI(14) very low -> LONG, very high -> SHORT, out when it gets back to 50.
Exits: stop = a x ATR, trailing stop = b x ATR from the best price, maximum hold in hours.
Costs: Jupiter Perps 0.06% open + 0.06% close of the position + borrow ~0.002% of the position per hour.
Entries at the next candle's open; stops checked on each candle's low/high with the stop first.
TRAIN = first 2/3 of the year (choose), TEST = last 1/3 (never seen by the choice).
"""
import itertools
import json
import logging
import time

import requests

FEE = 0.0006
BORROW_H = 0.00002
_http = requests.Session()
SOURCES = ("https://api.binance.com", "https://api.binance.us")


def hourly(days=365, now=None, get=None, symbol="BTCUSDT"):
    now = now or time.time()
    start, end, out = int((now - days * 86400) * 1000), int(now * 1000), []
    while start < end:
        rows = None
        if get:
            rows = get(start)
        else:
            for base in SOURCES:
                try:
                    r = _http.get(base + "/api/v3/klines", params={"symbol": symbol, "interval": "1h",
                                                                  "startTime": start, "limit": 1000}, timeout=15)
                    r.raise_for_status()
                    rows = r.json()
                    break
                except Exception:
                    continue
            if rows is None:
                raise RuntimeError("sem dados horários do BTC")
        if not rows:
            break
        out += [(int(x[0]) / 1000, float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), float(x[9]))
                for x in rows]
        start = int(rows[-1][0]) + 3_600_000
        if len(rows) < 1000:
            break
    return out


def indicators(cs):
    n = len(cs)
    c = [x[4] for x in cs]
    atr, rsi, ema200 = [0.0] * n, [50.0] * n, [0.0] * n
    a, e, k = 0.0, c[0], 2 / 201
    gain = loss = 0.0
    for i in range(n):
        tr = cs[i][2] - cs[i][3] if i == 0 else max(cs[i][2] - cs[i][3], abs(cs[i][2] - c[i - 1]), abs(cs[i][3] - c[i - 1]))
        a = tr if i == 0 else (a * 13 + tr) / 14
        atr[i] = a
        e = c[i] * k + e * (1 - k)
        ema200[i] = e
        if i:
            ch = c[i] - c[i - 1]
            gain = (gain * 13 + max(ch, 0)) / 14
            loss = (loss * 13 + max(-ch, 0)) / 14
            rsi[i] = 100 - 100 / (1 + gain / loss) if loss else 100.0
    vavg = [0.0] * n
    acc = 0.0
    for i in range(n):
        acc += cs[i][5]
        if i >= 48:
            acc -= cs[i - 48][5]
        vavg[i] = acc / min(i + 1, 48)
    from collections import deque
    hh, ll = {}, {}
    for N in (24, 48, 96, 168):
        h, l = [0.0] * n, [0.0] * n
        qh, ql = deque(), deque()
        for i in range(n):                    # max/min of the N candles BEFORE i (monotonic queues)
            if i:
                j = i - 1
                while qh and cs[qh[-1]][2] <= cs[j][2]:
                    qh.pop()
                qh.append(j)
                while ql and cs[ql[-1]][3] >= cs[j][3]:
                    ql.pop()
                ql.append(j)
                while qh[0] < i - N:
                    qh.popleft()
                while ql[0] < i - N:
                    ql.popleft()
                h[i], l[i] = cs[qh[0]][2], cs[ql[0]][3]
        hh[N], ll[N] = h, l
    return {"atr": atr, "rsi": rsi, "ema": ema200, "vavg": vavg, "hh": hh, "ll": ll}


def signal(cs, ind, i, p):
    """+1 LONG, -1 SHORT, 0 nothing, at the close of candle i."""
    x = cs[i]
    fam = p["family"]
    if fam == "breakout":
        hi, lo = ind["hh"][p["n"]][i], ind["ll"][p["n"]][i]
        d = 1 if x[4] > hi else (-1 if x[4] < lo else 0)
        if d and p["trend"] and (d > 0) != (x[4] > ind["ema"][i]):
            return 0
        return d
    if fam == "flood":
        if x[5] < p["n"] * ind["vavg"][i - 1]:
            return 0
        share = x[6] / x[5] if x[5] else 0.5
        d = 1 if share >= p["share"] else (-1 if share <= 1 - p["share"] else 0)
        if d and p["trend"] and (d > 0) != (x[4] > ind["ema"][i]):
            return 0
        return d
    r = ind["rsi"][i]
    return 1 if r < p["n"] else (-1 if r > 100 - p["n"] else 0)


def simulate(cs, ind, p, start, end, times=None):
    lev = p["lev"]
    trades, pos = [], None
    side_ok = {"both": (1, -1), "long": (1,), "short": (-1,)}[p["side"]]
    for i in range(max(start, 220), end):
        x = cs[i]
        if pos:
            d, e = pos["d"], pos["e"]
            if d > 0:
                pos["best"] = max(pos["best"], x[2])
                stop = max(pos["stop"], pos["best"] - p["trail"] * pos["atr"])
                hit = x[3] <= stop
                exit_px = stop if hit else None
            else:
                pos["best"] = min(pos["best"], x[3])
                stop = min(pos["stop"], pos["best"] + p["trail"] * pos["atr"])
                hit = x[2] >= stop
                exit_px = stop if hit else None
            held = i - pos["i"]
            if exit_px is None and p["family"] == "reversion" and ((d > 0 and ind["rsi"][i] >= 50) or (d < 0 and ind["rsi"][i] <= 50)):
                exit_px = x[4]
            if exit_px is None and held >= p["hold"]:
                exit_px = x[4]
            if exit_px is not None:
                move = (exit_px / e - 1) * d
                pnl = lev * move - 2 * FEE * lev - BORROW_H * lev * max(held, 1)
                trades.append(max(pnl, -0.9))                        # liquidation caps the loss
                if times is not None:
                    times.append(cs[i][0])
                pos = None
            continue
        d = signal(cs, ind, i, p)
        if d in side_ok and d != 0 and i + 1 < len(cs):
            e = cs[i + 1][1]
            a = ind["atr"][i]
            pos = {"d": d, "e": e, "i": i + 1, "atr": a, "best": e,
                   "stop": e - p["stop"] * a if d > 0 else e + p["stop"] * a}
    return trades


def score(trades):
    if not trades:
        return {"n": 0, "pnl": 0.0, "win": 0.0, "pf": 0.0, "dd": 0.0}
    gain = sum(t for t in trades if t > 0)
    loss = -sum(t for t in trades if t <= 0)
    eq = peak = dd = 0.0
    for t in trades:
        eq += t
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": len(trades), "pnl": sum(trades), "win": sum(1 for t in trades if t > 0) / len(trades),
            "pf": gain / loss if loss else 99.0, "dd": dd}


def grid():
    for side in ("both", "long", "short"):
        for lev in (2, 3, 5):
            for stop, trail in ((1.5, 2.0), (2.0, 3.0), (3.0, 4.0)):
                for hold in (24, 72, 168):
                    for n in (24, 48, 96, 168):
                        for trend in (False, True):
                            yield {"family": "breakout", "side": side, "lev": lev, "stop": stop, "trail": trail,
                                   "hold": hold, "n": n, "trend": trend}
                    for n in (2.0, 3.0):
                        for share in (0.55, 0.6):
                            for trend in (False, True):
                                yield {"family": "flood", "side": side, "lev": lev, "stop": stop, "trail": trail,
                                       "hold": hold, "n": n, "share": share, "trend": trend}
                    for n in (20, 25, 30):
                        yield {"family": "reversion", "side": side, "lev": lev, "stop": stop, "trail": trail,
                               "hold": hold, "n": n, "trend": False}


def search(cs, keep=8, min_train=15, min_test=6):
    t0 = time.time()
    ind = indicators(cs)
    split = int(len(cs) * 2 / 3)
    rows, total = [], 0
    for p in grid():
        total += 1
        tr = score(simulate(cs, ind, p, 0, split))
        if tr["n"] >= min_train and tr["pnl"] > 0 and tr["pf"] > 1.2:
            rows.append((tr, p))
    rows.sort(key=lambda r: -r[0]["pnl"])
    out = []
    for tr, p in rows[:80]:
        te = score(simulate(cs, ind, p, split, len(cs)))
        out.append({"p": p, "train": tr, "test": te,
                    "ok": te["n"] >= min_test and te["pnl"] > 0 and te["pf"] > 1.1})
    out.sort(key=lambda r: (r["ok"], min(r["train"]["pnl"] / 2, r["test"]["pnl"])), reverse=True)
    hold_pct = (cs[-1][4] / cs[split][4] - 1) * 100
    logging.info("btc swing research: %s settings in %.0fs, %s good in train, %s also good in test, BTC test %+.1f%%",
                 total, time.time() - t0, len(rows), sum(1 for r in out if r["ok"]), hold_pct)
    return out[:keep], hold_pct


def describe(p):
    fam = {"breakout": "rompimento de " + str(p["n"]) + " h", "flood": "hora com volume " + format(p["n"], "g") +
           "x e " + format(p.get("share", 0) * 100, ".0f") + "% de um lado", "reversion": "RSI " + str(p["n"]) + "/" +
           str(100 - p["n"])}[p["family"]]
    return (fam + (" + tendência 200 h" if p.get("trend") else "") + " · " +
            {"both": "LONG e SHORT", "long": "só LONG", "short": "só SHORT"}[p["side"]] + " · " + str(p["lev"]) +
            "x · stop " + format(p["stop"], "g") + " ATR · móvel " + format(p["trail"], "g") + " ATR · máx " +
            str(p["hold"]) + " h")


def report(rows, hold_pct, margin=20.0):
    good = [r for r in rows if r["ok"]]
    head = ("🔬 PESQUISA BTC (velas de 1 h, 1 ano; TREINO 8 meses para escolher, TESTE 4 meses que a escolha não viu; "
            "lucro em US$ por garantia de US$ " + format(margin, ".0f") + ", taxas incluídas; BTC no teste " +
            format(hold_pct, "+.1f") + "%)")
    if not good:
        head += "\n❌ Nenhuma configuração ganhou no TESTE. Não ligue o robô."
    lines = [head]
    for k, r in enumerate(rows, 1):
        tr, te = r["train"], r["test"]
        lines.append(str(k) + ") " + describe(r["p"]) + "\n   TREINO " + str(tr["n"]) + " op. " +
                     format(tr["pnl"] * margin, "+.2f") + " USD (" + format(tr["win"] * 100, ".0f") + "%) · TESTE " +
                     str(te["n"]) + " op. " + format(te["pnl"] * margin, "+.2f") + " USD (" + format(te["win"] * 100, ".0f") +
                     "%, pior sequência −" + format(te["dd"] * margin, ".2f") + ")" + (" ✅" if r["ok"] else " ❌"))
    return "\n".join(lines)


RESULT = {"running": False, "text": "", "rows": []}


def run(send=None, chat=None, cs=None):
    if RESULT["running"]:
        return
    RESULT["running"] = True
    try:
        cs = cs or hourly(365)
        rows, hold_pct = search(cs)
        RESULT.update(rows=rows, text=report(rows, hold_pct))
        for r in rows:
            logging.info("btc swing row: %s", json.dumps(r))
        if send and chat:
            send(chat, RESULT["text"])
    except Exception as exc:
        RESULT["text"] = "⛔ Pesquisa BTC (1 h) falhou: " + type(exc).__name__ + ": " + str(exc)[:150]
        logging.warning(RESULT["text"])
    finally:
        RESULT["running"] = False


# ------------------------------------------------------------------ weekly challenge: US$ 150 -> US$ 400 in a week
def week_challenge(cs, ind, p, frac, start_usd=150.0, target_usd=400.0, start=0, end=None):
    """Every week starts again with start_usd; each trade uses frac of the balance as collateral (compounding).
    The week is a WIN when the balance reaches target_usd (the robot stops: profit locked), a BUST when it
    falls under 10% of the start. Returns a list of per-week dicts."""
    end = end or len(cs)
    times = []
    trades = simulate(cs, ind, p, start, end, times)
    if not cs:
        return []
    weeks, t0 = [], cs[max(start, 220)][0]
    last = cs[end - 1][0]
    k = 0
    while t0 + 7 * 86400 <= last:
        bal, done = start_usd, ""
        while k < len(times) and times[k] < t0:
            k += 1
        j = k
        while j < len(times) and times[j] < t0 + 7 * 86400:
            if not done:
                bal += bal * frac * trades[j]
                if bal >= target_usd:
                    done = "win"
                elif bal <= start_usd * 0.1:
                    done = "bust"
            j += 1
        weeks.append({"end": done or "", "bal": round(bal, 2)})
        t0 += 7 * 86400
    return weeks


def challenge_search(cs, start_usd=150.0, target_usd=400.0):
    """Best settings for the weekly target on TRAIN weeks, checked on TEST weeks (never seen)."""
    ind = indicators(cs)
    split = int(len(cs) * 2 / 3)
    rows = []
    for base in grid():
        if base["lev"] != 5:
            continue                                      # leverage is varied below
        for lev in (5, 10, 20):
            for frac in (0.5, 1.0):
                p = dict(base, lev=lev)
                tr = week_challenge(cs, ind, p, frac, start_usd, target_usd, 0, split)
                if not tr:
                    continue
                win = sum(1 for w in tr if w["end"] == "win") / len(tr)
                bust = sum(1 for w in tr if w["end"] == "bust") / len(tr)
                rows.append((win - bust, p, frac, tr))
    rows.sort(key=lambda r: -r[0])
    out = []
    for _, p, frac, tr in rows[:10]:
        te = week_challenge(cs, ind, p, frac, start_usd, target_usd, split, len(cs))
        last8 = week_challenge(cs, ind, p, frac, start_usd, target_usd, max(0, len(cs) - 61 * 24), len(cs))
        out.append({"p": p, "frac": frac, "train": tr, "test": te, "last2m": last8})
    return out


def _wk(weeks):
    if not weeks:
        return "sem semanas"
    win = sum(1 for w in weeks if w["end"] == "win")
    bust = sum(1 for w in weeks if w["end"] == "bust")
    avg = sum(w["bal"] for w in weeks) / len(weeks)
    return (str(win) + "/" + str(len(weeks)) + " semanas chegaram a US$ 400 · " + str(bust) + " quebraram · saldo médio no fim US$ " +
            format(avg, ".0f"))


def challenge_report(rows):
    lines = ["🎯 DESAFIO US$ 150 → US$ 400 EM 1 SEMANA (BTC, velas de 1 h, 1 ano; cada semana recomeça com US$ 150; "
             "chegou em 400 = para e guarda o lucro; caiu abaixo de US$ 15 = quebrou)"]
    if not rows:
        return lines[0] + "\nNenhuma configuração."
    for k, r in enumerate(rows[:6], 1):
        lines.append(str(k) + ") " + describe(r["p"]) + " · usa " + format(r["frac"] * 100, ".0f") + "% do saldo por operação"
                     "\n   TREINO: " + _wk(r["train"]) + "\n   TESTE: " + _wk(r["test"]) + "\n   ÚLTIMOS 2 MESES: " + _wk(r["last2m"]))
    return "\n".join(lines)


CHALLENGE = {"running": False, "text": ""}


def run_challenge(send=None, chat=None, cs=None):
    if CHALLENGE["running"]:
        return
    CHALLENGE["running"] = True
    try:
        cs = cs or hourly(365)
        t0 = time.time()
        rows = challenge_search(cs)
        CHALLENGE["text"] = challenge_report(rows)
        logging.info("btc challenge (%.0fs): %s", time.time() - t0, CHALLENGE["text"].replace("\n", " | "))
        if send and chat:
            send(chat, CHALLENGE["text"])
    except Exception as exc:
        CHALLENGE["text"] = "⛔ Desafio falhou: " + type(exc).__name__ + ": " + str(exc)[:150]
        logging.warning(CHALLENGE["text"])
    finally:
        CHALLENGE["running"] = False


# ------------------------------------------------------------------ monthly challenge on the Jupiter Perps coins
# Jupiter Perpetuals only lists SOL, ETH and BTC: those are the coins the robot could really trade.
PERP_COINS = (("SOL", "SOLUSDT"), ("ETH", "ETHUSDT"), ("BTC", "BTCUSDT"))


def month_challenge(cs, ind, p, frac, start_usd=150.0, target_usd=400.0, start=0, end=None, days=30, step_days=7,
                    safe_frac=0.25):
    """Rolling 30-day windows (a new one every 7 days), each starting with start_usd and compounding.
    Reaching target_usd -> CONSERVATIVE mode for the rest of the window: only safe_frac of the normal size
    (and never below the target minus 10%: then it stops). Under 10% of the start = bust."""
    end = end or len(cs)
    times = []
    trades = simulate(cs, ind, p, start, end, times)
    out, t0, last = [], cs[max(start, 220)][0], cs[end - 1][0]
    while t0 + days * 86400 <= last:
        bal, hit, done = start_usd, False, ""
        for t, r in zip(times, trades):
            if t < t0 or t >= t0 + days * 86400 or done:
                continue
            bal += bal * (safe_frac if hit else 1.0) * frac * r
            if not hit and bal >= target_usd:
                hit = True
            if hit and bal < target_usd * 0.9:
                done = "locked"                         # gave back 10%: stop and keep the rest
            if not hit and bal <= start_usd * 0.1:
                done = "bust"
        out.append({"hit": hit, "bust": done == "bust", "bal": round(bal, 2)})
        t0 += step_days * 86400
    return out


def _mo(ws):
    if not ws:
        return "sem janelas"
    hit = sum(1 for w in ws if w["hit"])
    bust = sum(1 for w in ws if w["bust"])
    avg = sum(w["bal"] for w in ws) / len(ws)
    return (str(hit) + "/" + str(len(ws)) + " meses chegaram a US$ 400 · " + str(bust) + " quebraram · saldo médio no fim US$ " +
            format(avg, ".0f"))


def month_search(cs, keep=3):
    ind = indicators(cs)
    split = int(len(cs) * 2 / 3)
    rows = []
    for base in grid():
        if base["lev"] != 5:
            continue
        for lev in (3, 5, 10, 20):
            for frac in (0.5, 1.0):
                p = dict(base, lev=lev)
                tr = month_challenge(cs, ind, p, frac, start=0, end=split)
                if not tr:
                    continue
                hit = sum(1 for w in tr if w["hit"]) / len(tr)
                bust = sum(1 for w in tr if w["bust"]) / len(tr)
                rows.append((hit - 2 * bust, p, frac, tr))
    rows.sort(key=lambda r: -r[0])
    out = []
    for _, p, frac, tr in rows[:keep]:
        te = month_challenge(cs, ind, p, frac, start=split, end=len(cs))
        out.append({"p": p, "frac": frac, "train": tr, "test": te,
                    "ok": bool(te) and sum(1 for w in te if w["hit"]) / len(te) >= 0.5 and not any(w["bust"] for w in te)})
    return out


MONTH = {"running": False, "text": ""}


def run_month(send=None, chat=None, data=None):
    if MONTH["running"]:
        return
    MONTH["running"] = True
    try:
        lines = ["🎯 DESAFIO US$ 150 → US$ 400 EM 1 MÊS (moedas da Jupiter Perps, velas de 1 h, 1 ano; janelas de 30 dias; "
                 "chegou em 400 = modo conservador: 25% do tamanho, e para se devolver 10%)"]
        any_ok = False
        for name, symbol in PERP_COINS:
            cs = (data or {}).get(name) or hourly(365, symbol=symbol)
            rows = month_search(cs)
            lines.append("\n— " + name + " (no período: " + format((cs[-1][4] / cs[0][4] - 1) * 100, "+.0f") + "%)")
            for k, r in enumerate(rows, 1):
                any_ok = any_ok or r["ok"]
                lines.append(str(k) + ") " + describe(r["p"]) + " · usa " + format(r["frac"] * 100, ".0f") + "% do saldo"
                             "\n   TREINO: " + _mo(r["train"]) + "\n   TESTE: " + _mo(r["test"]) + (" ✅" if r["ok"] else " ❌"))
        lines.append("\n" + ("✅ Pelo menos uma configuração passou no TESTE (metade ou mais dos meses chegaram a 400, nenhum quebrou)."
                             if any_ok else "❌ Nenhuma configuração passou no TESTE em nenhuma moeda."))
        MONTH["text"] = "\n".join(lines)
        logging.info("month challenge: %s", MONTH["text"].replace("\n", " | "))
        if send and chat:
            send(chat, MONTH["text"])
    except Exception as exc:
        MONTH["text"] = "⛔ Desafio do mês falhou: " + type(exc).__name__ + ": " + str(exc)[:150]
        logging.warning(MONTH["text"])
    finally:
        MONTH["running"] = False
