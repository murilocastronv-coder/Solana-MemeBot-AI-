"""Follower backtest (owner, Nov 15): test the DEV robot the way IT would really have traded.

The old backtest used the dev's own result, but the dev buys first, sometimes with free creator tokens,
and the copier never gets that price. Here every dev trade is replayed on the coin's real price history
(GeckoTerminal candles):
- the bot enters FOLLOW_DELAY_S after the dev's buy, at the price of that moment;
- it skips the trade when that price is already MAX_RUNUP_DEV above the dev's price (same rule as the live robot);
- exits: "mirror" (sells when and as much as the dev sold) and a grid of stop loss / take profit, where the
  stop is checked first inside a candle (pessimistic), and whatever was not hit leaves with the dev;
- costs: 2 x COST per trade + network fee.
Dev selection uses only trades the dev had CLOSED before each entry. Chronological split 70/30: the
combination (dev rule, exit) is chosen on the first 70% of the time and reported on the last 30% only.
Nothing is invented: trades without price history are counted as "no data" and left out.
"""
import logging
import time

import dev_bot as db
import signals as sg

FOLLOW_DELAY_S = db.E("DEV_FOLLOW_DELAY_SECONDS", 60)
EXITS = (("mirror", 0, 0),) + tuple(("sl" + format(s * 100, ".0f") + "tp" + format(t * 100, ".0f"), s, t)
                                    for s in (0.3, 0.5) for t in (0.5, 1.0, 2.0)) + \
    tuple(("sl" + format(s * 100, ".0f"), s, 0) for s in (0.3, 0.5)) + \
    tuple(("tp" + format(t * 100, ".0f"), 0, t) for t in (0.5, 1.0, 2.0))
RULES = (("todos os DEVs", "all"), ("acerto do DEV ≥ 70%", "dev70"), ("acerto do DEV ≥ 80%", "dev80"),
         ("cópias anteriores no lucro (média > 0, 5+)", "f_mean"),
         ("cópias anteriores: acerto ≥ 60% e média > 0", "f_win60"))
MIN_PRIOR_F = int(db.E("DEV_FOLLOW_MIN_PRIOR", 5))
FRAMES = ((1, "minute", 1), (5, "minute", 5), (15, "minute", 15), (60, "hour", 1), (240, "hour", 4), (1440, "day", 1))


def exit_label(name):
    if name == "mirror":
        return "vende junto com o DEV"
    s = t = ""
    if "sl" in name:
        s = "stop −" + name.split("sl")[1].split("tp")[0] + "%"
    if "tp" in name:
        t = "alvo +" + name.split("tp")[1] + "%"
    return " + ".join(x for x in (s, t) if x) + " (o resto sai com o DEV)"


def _candles(mint, start, end, pools, fetch):
    pool = pools.get(mint)
    if pool is None:
        pool = sg.gecko_pool(mint, fetch) or ""
        pools[mint] = pool
    if not pool:
        return None, 0
    span_min = (end - start) / 60 + 10
    for mins, frame, agg in FRAMES:
        if span_min <= 990 * mins:
            break
    url = (sg.GECKO + "/networks/solana/pools/" + pool + "/ohlcv/" + frame + "?aggregate=" + str(agg) +
           "&before_timestamp=" + str(int(end + mins * 60)) + "&limit=1000&currency=usd")
    data = (fetch or sg._get)(url)
    rows = ((data or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
    return sorted((tuple(float(x) for x in r[:5]) for r in rows), key=lambda r: r[0]), mins * 60


def _price_at(candles, t, step):
    """Close of the candle that contains t (or the last one before it); None if the coin had no candle yet."""
    best = None
    for c in candles:
        if c[0] <= t:
            best = c
        else:
            break
    if best is None or t - best[0] > max(step, 3600):
        return None
    return best[4]


def simulate(h, candles, step):
    """{exit_name: [return, exit_ts]} for one dev trade, or None when there is no price at the entry."""
    t0 = float(h["opened"]) + FOLLOW_DELAY_S
    end = float(h["closed"])
    entry = _price_at(candles, t0, step)
    if not entry or entry <= 0:
        return None
    dev_px = h.get("entry_usd")
    if dev_px and entry > float(dev_px) * (1 + db.MAX_RUNUP_DEV):
        return {"skip": "runup"}
    sells = h.get("sells") or [[end, 1.0]]
    path = [c for c in candles if t0 < c[0] + step and c[0] <= end]
    out = {}
    for name, sl, tp in EXITS:
        left, got, last_t = 1.0, 0.0, end
        si = 0
        for c in path:
            while si < len(sells) and sells[si][0] < c[0]:
                px = _price_at(candles, sells[si][0], step) or entry
                part = left * min(float(sells[si][1]), 1.0)
                got += part * px / entry
                left -= part
                si += 1
            if left <= 1e-9:
                break
            if sl and c[3] <= entry * (1 - sl):
                got += left * (1 - sl)
                left, last_t = 0.0, c[0]
                break
            if tp and c[2] >= entry * (1 + tp):
                got += left * (1 + tp)
                left, last_t = 0.0, c[0]
                break
        while left > 1e-9 and si < len(sells):
            px = _price_at(candles, sells[si][0], step) or entry
            part = left * min(float(sells[si][1]), 1.0) if si < len(sells) - 1 else left
            got += part * px / entry
            left -= part
            si += 1
        if left > 1e-9:
            got += left * ((_price_at(candles, end, step) or entry) / entry)
        out[name] = [round(got - 1 - 2 * db.COST, 4), int(last_t)]
    return out


def fill(max_s=600, fetch=None, now=None):
    """Replay dev trades that have no follower result yet (pump.fun only, plausible ones). Returns how many."""
    started = time.time()
    d = db._load()
    pools = d.setdefault("pools", {})
    todo = [(dev, i) for dev, v in sorted(d["devs"].items(), key=lambda kv: -len(kv[1].get("hist") or []))
            for i, h in enumerate(v.get("hist") or [])
            if "f" not in h and h.get("closed") and h.get("opened") and h.get("mint") and db.pump_trade(h) and db.bt_ok(h)]
    done, results = 0, {}
    for dev, i in todo:
        if time.time() - started > max_s:
            break
        h = d["devs"][dev]["hist"][i]
        try:
            candles, step = _candles(h["mint"], float(h["opened"]), float(h["closed"]), pools, fetch)
            res = simulate(h, candles, step) if candles else None
        except sg.GeckoBusy:
            time.sleep(30)
            continue
        except Exception as exc:
            logging.info("devbot follow: %s %s", h["mint"][:8], type(exc).__name__)
            res = None
        results[(dev, i)] = res
        done += 1
        if len(results) >= 25:
            _store(results, pools)
            results = {}
    _store(results, pools)
    return done


def _store(results, pools):
    if not results and not pools:
        return
    with db._LOCK:
        d = db._load()
        d.setdefault("pools", {}).update(pools)
        for (dev, i), res in results.items():
            hist = (d["devs"].get(dev) or {}).get("hist") or []
            if i < len(hist):
                hist[i]["f"] = res
        _save_follow_stats(d)
        db._save(d)


def _save_follow_stats(d, exit_name=None):
    """Per dev: what a copier got so far (used by the live rule when the owner applies a follower rule)."""
    exit_name = exit_name or ((d.get("follow_rule") or {}).get("exit") or "mirror")
    for v in d["devs"].values():
        rs = [h["f"][exit_name][0] for h in v.get("hist") or [] if isinstance(h.get("f"), dict) and exit_name in h["f"]]
        v["follow"] = {"n": len(rs), "win": round(sum(1 for r in rs if r > 0) / len(rs), 3) if rs else 0.0,
                       "mean": round(sum(rs) / len(rs), 4) if rs else 0.0}


def _dev_ok(rule, prior_dev, prior_f):
    if rule == "all":
        return True
    if rule in ("dev70", "dev80"):
        need = 0.7 if rule == "dev70" else 0.8
        return len(prior_dev) >= 20 and sum(1 for q in prior_dev if float(q.get("pnl_usd") or 0) > 0) / len(prior_dev) >= need
    if len(prior_f) < MIN_PRIOR_F:
        return False
    mean = sum(prior_f) / len(prior_f)
    if rule == "f_mean":
        return mean > 0
    return mean > 0 and sum(1 for r in prior_f if r > 0) / len(prior_f) >= 0.6


def follow_ok(info, rule):
    """Live check for a follower rule (prior copies of this dev measured by fill)."""
    f = (info or {}).get("follow") or {}
    if f.get("n", 0) < MIN_PRIOR_F or f.get("mean", 0) <= 0:
        return False
    return rule == "f_mean" or f.get("win", 0) >= 0.6


def run(d=None, usd=None, capital=None, max_open=None, train_share=0.7):
    d = d or db._load()
    usd = float(usd or db.buy_usd())
    capital = float(capital or db.BT_PERIOD_CAPITAL)
    max_open = int(max_open or db.MAX_OPEN)
    trades = sorted(((float(h["opened"]), dev, h) for dev, v in d["devs"].items() for h in v.get("hist") or []
                     if isinstance(h.get("f"), dict) and "mirror" in h["f"]), key=lambda t: t[0])
    info = {"replayed": len(trades),
            "no_data": sum(1 for v in d["devs"].values() for h in v.get("hist") or [] if "f" in h and h["f"] is None),
            "runup": sum(1 for v in d["devs"].values() for h in v.get("hist") or []
                         if isinstance(h.get("f"), dict) and h["f"].get("skip")),
            "todo": sum(1 for v in d["devs"].values() for h in v.get("hist") or []
                        if "f" not in h and h.get("closed") and db.pump_trade(h) and db.bt_ok(h))}
    if len(trades) < 10:
        return info, None
    cut = trades[int(len(trades) * train_share)][0]
    res = {}
    for rname, rule in RULES:
        for ename, _, _ in EXITS:
            row = {}
            for part, lo, hi in (("treino", -1, cut), ("teste", cut, float("inf"))):
                pnl, peak, eq, dd, n, wins, gw, gl, open_until = 0.0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0.0, []
                for t, dev, h in trades:
                    if not lo <= t < hi:
                        continue
                    hist = d["devs"][dev]["hist"]
                    prior_dev = [q for q in hist if q.get("closed") and q["closed"] < t]
                    prior_f = [q["f"][ename][0] for q in prior_dev if isinstance(q.get("f"), dict) and ename in q["f"]]
                    if not _dev_ok(rule, prior_dev, prior_f):
                        continue
                    open_until = [x for x in open_until if x > t]
                    if len(open_until) >= max_open:
                        continue
                    r, exit_t = h["f"][ename]
                    open_until.append(exit_t)
                    x = usd * r - 2 * db.NET_FEE_USD
                    pnl += x
                    n += 1
                    wins += x > 0
                    gw += max(x, 0)
                    gl += min(x, 0)
                    eq += x
                    peak = max(peak, eq)
                    dd = max(dd, (peak - eq) / (capital + peak))
                row[part] = {"pnl": round(pnl, 2), "trades": n, "win": wins / n if n else 0.0,
                             "pf": round(gw / abs(gl), 2) if gl < 0 else None, "dd": dd}
            res[(rname, rule, ename)] = row
    return info, res


def best(res, min_trades=10):
    ok = [(k, v) for k, v in res.items() if v["treino"]["trades"] >= min_trades]
    return max(ok, key=lambda kv: kv[1]["treino"]["pnl"]) if ok else None


def winner_ok(row):
    t = row["teste"]
    return t["trades"] >= 10 and t["pnl"] > 0 and (t["pf"] is None or t["pf"] >= 1.2)


def text(info, res, usd=None, capital=None):
    usd = float(usd or db.buy_usd())
    capital = float(capital or db.BT_PERIOD_CAPITAL)
    lines = ["🎯 BACKTEST DE QUEM COPIA (preço real da moeda " + format(FOLLOW_DELAY_S, ".0f") +
             " s depois do DEV · não compra se já subiu +" + format(db.MAX_RUNUP_DEV * 100, ".0f") +
             "% · US$ " + format(usd, "g") + " por compra · máx. " + str(db.MAX_OPEN) + " abertas · custos 2×" +
             format(db.COST * 100, ".0f") + "%)",
             "📊 Operações refeitas com preço real: " + str(info["replayed"]) + " · sem histórico de preço: " +
             str(info["no_data"]) + " · puladas (preço já tinha subido): " + str(info["runup"]) + " · na fila: " +
             str(info["todo"])]
    if not res:
        lines.append("⏳ Ainda poucas operações refeitas (precisa de 10+). Ele continua baixando os preços sozinho; "
                     "toque de novo mais tarde.")
        return "\n".join(lines)
    b = best(res)
    lines.append("Escolha feita só nos primeiros 70% do tempo; resultado abaixo é dos últimos 30% (fora da amostra):")
    mirror = sorted(((k, v) for k, v in res.items() if k[2] == "mirror"), key=lambda kv: -kv[1]["teste"]["pnl"])
    for (rname, _, _), v in mirror:
        t = v["teste"]
        lines.append("• " + rname + " · vende com o DEV: " + format(t["pnl"], "+.2f") + " USD · " + str(t["trades"]) +
                     " ops · acerto " + format(t["win"] * 100, ".0f") + "%")
    if not b:
        lines.append("🏆 Nenhuma combinação teve 10+ operações no treino ainda.")
        return "\n".join(lines)
    (rname, rule, ename), v = b
    t, tr = v["teste"], v["treino"]
    lines.append("🏆 Melhor no treino: " + rname + " + " + exit_label(ename) + " → treino " + format(tr["pnl"], "+.2f") +
                 " USD (" + str(tr["trades"]) + " ops) · TESTE " + format(t["pnl"], "+.2f") + " USD (" + str(t["trades"]) +
                 " ops, acerto " + format(t["win"] * 100, ".0f") + "%, PF " + ("—" if t["pf"] is None else format(t["pf"], ".2f")) +
                 ", queda máx " + format(t["dd"] * 100, ".0f") + "% de US$ " + format(capital, "g") + ")")
    if winner_ok(v):
        lines.append("✅ Deu lucro fora da amostra. Toque em ✅ Usar estratégia vencedora para o robô real passar a usar.")
    else:
        lines.append("❌ Nenhuma combinação deu lucro confiável fora da amostra (precisa 10+ operações, lucro e PF ≥ 1.2). "
                     "O robô real continua como está — sem vantagem comprovada, não comprar é o melhor.")
    lines.append("⚠️ Teste no passado não garante o futuro; velas de 1 min+ não mostram cada segundo.")
    return "\n".join(lines)


def report(now=None):
    d = db._load()
    info, res = run(d)
    out = text(info, res)
    b = best(res) if res else None
    with db._LOCK:
        dd = db._load()
        dd["follow_winner"] = ({"rule": b[0][1], "rule_name": b[0][0], "exit": b[0][2], "ok": winner_ok(b[1]),
                                "test": b[1]["teste"]} if b else None)
        db._save(dd)
    return out


def apply_winner():
    """Owner's button: the live robot uses the winning (dev rule, exit) - only if it won out of sample."""
    with db._LOCK:
        d = db._load()
        w = d.get("follow_winner")
        if not w or not w.get("ok"):
            return None
        sl = tp = 0.0
        for name, s, t in EXITS:
            if name == w["exit"]:
                sl, tp = s, t
        d["follow_rule"] = {"rule": w["rule"], "rule_name": w["rule_name"], "exit": w["exit"], "sl": sl, "tp": tp}
        _save_follow_stats(d, w["exit"])
        db._save(d)
        return d["follow_rule"]


def clear_rule():
    with db._LOCK:
        d = db._load()
        d.pop("follow_rule", None)
        db._save(d)
