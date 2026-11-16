"""SNIPER BACKTEST PRO 180 DAYS (owner, Nov 16).

Data: the REAL on-chain sample downloaded by snipe_backtest (Helius history of the Pump.fun mint authority, one
sample per day, dead coins included). For each coin it has, at ages 10/15/20/30/45/55 min, the 5-minute snapshot built
only from trades BEFORE that moment (price, volume, buys, sells, 5-min move, market cap), and the real price path
starting SNIPE_BT_DELAY_S seconds AFTER the decision (all trades of the mint, so Pump.fun AND the pool after the
migration). Nothing after the decision moment is used to decide (look-ahead test in test_sniper_pro.py).

Not in the historical data (reported, never invented): number/concentration of holders, liquidity depth, creator
wallet activity, token permissions at that time, 1- and 15-minute momentum, Birdeye (needs an API key).
Those filters stay only in the live sniper (anti-rug check before every buy).

Simulator: US$ 60 capital, cash can never go negative, reserve kept, max open, daily loss limit, size US$ 3/5/10,
swap fee + slippage per side, network fee per transaction, entry delay, exits at the REAL next traded price (never at
the exact TP/SL level), partial sale option, sell failures (the sale happens at a later trade).
Optimisation: random search (equivalent to Optuna's random sampler, fixed seed) on days 1-90, the best 25 re-ranked on
days 91-120 (validation), then reported untouched on days 121-150 (test) and 151-180 (independent test).
Selection: validation profit minus half the drawdown, PF >= 1 and enough trades - never only the biggest profit.
Robustness: costs x1.5 and x2, one more trade of delay, 20% of sales failing, month by month, worst losing streak.
"""
import hashlib
import json
import logging
import random
import threading
import time

import signals as sg
import snipe_backtest as sbt
import snipe_bot as sb

E = sg._f
CAPITAL = E("PRO_CAPITAL", 60)
COST = E("PRO_COST_PER_SIDE", 0.03)          # Pump.fun/PumpSwap fee ~1% + slippage on young coins
NET_FEE = E("PRO_NET_FEE_USD", 0.02)         # base + priority fee per transaction
SEARCH_N = int(E("PRO_SEARCH_N", 600))       # combinations per profile (the whole run fits in ~3 min)
MIN_OPS = int(E("PRO_MIN_OPS", 200))         # owner: at least 200 operations over the 180 days
DL_S = E("PRO_DOWNLOAD_SECONDS", 90)         # each tap downloads this long, then analyses everything downloaded
ANALYSIS_S = E("PRO_ANALYSIS_SECONDS", 100)
TOP_K = 25
BUCKETS = (("10–15 min", (10,)), ("15–20 min", (15,)), ("20–30 min", (20,)), ("30–40 min", (30,)),
           ("40–50 min", (45,)), ("50–60 min", (55,)), ("10–60 min", (10, 15, 20, 30, 45, 55)))
TPS = (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.75, 1.00)
SLS = (0.05, 0.08, 0.10, 0.12, 0.15, 0.20)
PROFILES = {
    "A": {"label": "Conservador", "size": 3, "max_open": 1, "reserve": 40, "daily_loss": 3, "min_score": (60, 75),
          "buckets": range(7)},
    "B": {"label": "Equilibrado", "size": 5, "max_open": 2, "reserve": 30, "daily_loss": 5, "min_score": (0, 45, 60),
          "buckets": range(7)},
    "C": {"label": "Agressivo", "size": 10, "max_open": 3, "reserve": 20, "daily_loss": 10, "min_score": (0, 45),
          "buckets": (0, 1, 2, 3, 6)},
}
_RUN = threading.Lock()
_STOP = threading.Event()


# ---------------------------------------------------------------- one trade on the real price path
def _u(key):
    return int(hashlib.sha1(key.encode()).hexdigest()[:8], 16) / 0xffffffff


def walk(path, p, fail=0.0, delay_steps=0, key=""):
    """(gross ratio of the money put in, exit reason, close time, sells) on the real path [[t, price], ...]."""
    if len(path) <= delay_steps:
        return 0.0, "sem negociação depois (rug / sem liquidez)", (path[-1][0] if path else 0), 1
    pts = path[delay_steps:]
    entry, t_in = pts[0][1], pts[0][0]
    peak, sold, got, sells = 1.0, 0.0, 0.0, 0
    pending, why = None, ""
    for i, (t, px) in enumerate(pts):
        r = px / entry
        peak = max(peak, r)
        if pending is not None:                         # a sale that failed goes through on this later trade
            frac, why = pending
            got += frac * r
            sold += frac
            sells += 1
            pending = None
            if sold >= 0.999:
                return got, why, t, sells
            continue
        act, frac = None, 0.0
        if r <= 1 - p["sl"] or (sold > 0 and r <= 1.0):
            act, frac = ("stop_loss" if sold == 0 else "resto no empate"), 1 - sold
        elif p["trail"] and peak >= 1 + p["tp"] and r <= peak * (1 - p["trail"]):
            act, frac = "stop móvel", 1 - sold
        elif not p["trail"] and r >= 1 + p["tp"] and sold == 0:
            act, frac = "take_profit", (0.5 if p.get("partial") else 1.0)
        elif t - t_in >= p["hold"] * 60:
            act, frac = "tempo máximo", 1 - sold
        if act:
            if fail and _u(key + str(i)) < fail:
                pending = (frac, act + " (venda atrasou)")
                continue
            got += frac * r
            sold += frac
            sells += 1
            why = act
            if sold >= 0.999:
                return got, why, t, sells
    last = pts[-1]
    return got + (1 - sold) * last[1] / entry, "fim dos dados", last[0], sells + 1


def entry_ok(snap, p):
    if not snap or not sb.MIN_MCAP <= snap["mcap"] <= sb.MAX_MCAP or snap["vol5"] < max(2000, p["min_vol"]):
        return False
    if snap["buys"] / max(snap["sells"], 1) < p["min_ratio"] or not p["min_up"] <= snap["up5"] <= p["max_up"]:
        return False
    return not p["min_score"] or sb.score(snap["mcap"], snap["vol5"], snap["buys"], snap["sells"], snap["up5"]) >= p["min_score"]


def candidates(c, start, end, p, fail=0.0, delay_steps=0):
    out = []
    for m, coin in c["coins"].items():
        if not start <= coin["created"] < end:
            continue
        for age in p["ages"]:
            snap = coin["snaps"].get(str(age))
            if snap is None:
                break
            if not entry_ok(snap, p):
                continue
            path = coin["after"].get(str(age))
            if path is None:
                continue                                   # not downloaded (cannot be judged): left out, not invented
            t0 = coin["created"] + age * 60
            g, why, t1, sells = walk(path, p, fail, delay_steps, m + str(age))
            out.append((t0, max(t1, t0), m, age, g, why, sells))
            break                                          # one entry per coin
    out.sort()
    return out


def simulate(c, start, end, p, cost_mult=1.0, fail=0.0, delay_steps=0):
    """Portfolio run with real cash: US$ capital, reserve, max open, daily loss limit. -> summary dict."""
    cost, fee = COST * cost_mult, NET_FEE * cost_mult
    cash, open_, trades, fees = CAPITAL, [], [], 0.0
    day_pnl, skipped = {}, {}
    for t0, t1, m, age, g, why, sells in candidates(c, start, end, p, fail, delay_steps):
        for x in [x for x in open_ if x[0] <= t0]:
            cash += x[1]
            open_.remove(x)
        day = time.strftime("%Y-%m-%d", time.gmtime(t0))
        if len(open_) >= p["max_open"]:
            skipped["posições cheias"] = skipped.get("posições cheias", 0) + 1
            continue
        if p["daily_loss"] and day_pnl.get(day, 0) <= -p["daily_loss"]:
            skipped["limite de perda do dia"] = skipped.get("limite de perda do dia", 0) + 1
            continue
        size = p["size"]
        if cash - size - fee < p["reserve"]:
            skipped["sem caixa acima da reserva"] = skipped.get("sem caixa acima da reserva", 0) + 1
            continue
        back = size * g * (1 - cost) * (1 - cost)          # bought 'cost' worse, sold 'cost' worse
        tx_fees = fee * (1 + sells)
        pnl = back - size - tx_fees
        fees += size * cost + size * g * (1 - cost) * cost + tx_fees
        cash -= size + fee
        open_.append((t1, back - fee * sells))
        day_pnl[day] = day_pnl.get(day, 0) + pnl
        trades.append({"mint": m, "age": age, "opened": t0, "closed": t1, "pnl": round(pnl, 4), "pct": round((g - 1) * 100, 2), "exit": why})
    for x in open_:
        cash += x[1]
    return summary(trades, fees, cash, skipped)


def summary(trades, fees, cash, skipped=None):
    eq, peak, dd, curve, streak, worst_streak = CAPITAL, CAPITAL, 0.0, [CAPITAL], 0, 0
    for tr in sorted(trades, key=lambda x: x["closed"]):
        eq += tr["pnl"]
        curve.append(round(eq, 2))
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = streak + 1 if tr["pnl"] <= 0 else 0
        worst_streak = max(worst_streak, streak)
    pnl = [t["pnl"] for t in trades]
    gw, gl = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x < 0)
    months, ages = {}, {}
    for t in trades:
        mo = time.strftime("%Y-%m", time.gmtime(t["opened"]))
        months[mo] = round(months.get(mo, 0) + t["pnl"], 2)
        a = ages.setdefault(str(t["age"]), [0, 0.0])
        a[0] += 1
        a[1] = round(a[1] + t["pnl"], 2)
    return {"start_cap": CAPITAL, "final": round(cash, 2), "pnl": round(sum(pnl), 2), "ret": round(sum(pnl) / CAPITAL * 100, 1),
            "trades": len(trades), "win": round(sum(1 for x in pnl if x > 0) / len(pnl), 3) if pnl else 0.0,
            "pf": round(gw / gl, 2) if gl > 0 else None, "dd": round(dd, 2), "avg": round(sum(pnl) / len(pnl), 3) if pnl else 0.0,
            "best": round(max(pnl, default=0), 2), "worst": round(min(pnl, default=0), 2), "fees": round(fees, 2),
            "streak": worst_streak, "months": months, "ages": ages, "curve": curve[-120:], "skipped": skipped or {},
            "trade_list": trades[-200:]}


# ---------------------------------------------------------------- optimisation
def windows(end):
    d = 86400
    s = end - 180 * d
    return {"dev": (s, s + 90 * d), "val": (s + 90 * d, s + 120 * d), "test": (s + 120 * d, s + 150 * d), "final": (s + 150 * d, end)}


def sample_params(rnd, prof, bucket=None):
    pr = PROFILES[prof]
    b = bucket if bucket is not None else rnd.choice(list(pr["buckets"]))
    up = rnd.choice(((-100, 10000), (0, 60), (5, 40), (-20, 200), (10, 100)))
    return {"bucket": b, "ages": list(BUCKETS[b][1]), "min_vol": rnd.choice((2000, 3000, 5000, 8000)),
            "min_ratio": rnd.choice((0, 1.0, 1.3, 1.5, 2.0)), "min_up": up[0], "max_up": up[1],
            "min_score": rnd.choice(pr["min_score"]), "tp": rnd.choice(TPS), "sl": rnd.choice(SLS),
            "trail": rnd.choice((0, 0, 0.10, 0.20)), "hold": rnd.choice((10, 20, 30, 60)), "partial": rnd.random() < 0.3,
            "size": pr["size"], "max_open": pr["max_open"], "reserve": pr["reserve"], "daily_loss": pr["daily_loss"]}


def score_of(s, min_trades):
    if s["trades"] < min_trades or (s["pf"] is not None and s["pf"] < 1.0):
        return -1e9 + s["pnl"]
    return s["pnl"] - 0.5 * s["dd"]


def optimise(c, end, prof, n=None, rnd=None, bucket=None, should_stop=None):
    rnd = rnd or random.Random(7)
    w = windows(end)
    tried = []
    for _ in range(n or SEARCH_N):
        if should_stop and should_stop():
            break
        p = sample_params(rnd, prof, bucket)
        tried.append((score_of(simulate(c, *w["dev"], p), MIN_OPS // 2), p))
    tried.sort(key=lambda x: -x[0])
    best, best_val = None, None
    for _, p in tried[:TOP_K]:
        v = simulate(c, *w["val"], p)
        if best is None or score_of(v, 5) > score_of(best_val, 5):
            best, best_val = p, v
    if best is None:
        return None
    return {"params": best, "dev": simulate(c, *w["dev"], best), "val": best_val, "test": simulate(c, *w["test"], best),
            "final": simulate(c, *w["final"], best), "tried": len(tried)}


def robustness(c, end, p):
    w = windows(end)
    s, e = w["test"][0], end                    # the 60 days never used to choose
    base = simulate(c, s, e, p)
    r = {"base": base["pnl"], "cost_x1.5": simulate(c, s, e, p, cost_mult=1.5)["pnl"], "cost_x2": simulate(c, s, e, p, cost_mult=2.0)["pnl"],
         "delay": simulate(c, s, e, p, delay_steps=1)["pnl"], "sell_fail_20%": simulate(c, s, e, p, fail=0.2)["pnl"]}
    full = simulate(c, w["dev"][0], end, p)
    r["months"] = full["months"]
    r["streak"] = full["streak"]
    r["months_negative"] = sum(1 for v in full["months"].values() if v < 0)
    r["ok"] = r["base"] > 0 and r["cost_x1.5"] > 0 and r["delay"] > 0 and r["sell_fail_20%"] > 0
    return r


def total_ops(o):
    return sum(o[k]["trades"] for k in ("dev", "val", "test", "final"))


def validated(o, rob):
    t, f = o["test"], o["final"]
    return bool(rob["ok"] and t["pnl"] > 0 and f["pnl"] > 0 and total_ops(o) >= MIN_OPS and t["trades"] + f["trades"] >= 20 and
                (t["pf"] is None or t["pf"] >= 1.2))


# ---------------------------------------------------------------- jobs
def state():
    return sbt._get("sniper_pro_job", {})


def _job(**kw):
    j = state()
    j.update(kw)
    sbt._put("sniper_pro_job", j)


def report():
    return sbt._get("sniper_pro_report", {})


def data_check(c, end):
    w = windows(end)
    coins = [x for x in c["coins"].values() if w["dev"][0] <= x["created"] < end]
    pts = sbt.sample_points(w["dev"][0], end)
    have = sum(1 for p in pts if str(p) in c["points"])
    return {"coins": len(coins), "alive": sum(1 for x in coins if not x.get("dead")), "days": have, "days_total": len(pts),
            "per_window": {k: sum(1 for x in coins if a <= x["created"] < b) for k, (a, b) in w.items()},
            "missing": ["holders (quantidade e concentração) na hora da decisão", "profundidade de liquidez", "atividade da carteira do criador",
                        "permissões do token na época", "momentum de 1 e 15 min (só há janelas de 5 min)", "Birdeye (precisa de chave de API)"]}


def run(kind, send=None, chat=None, now=None):
    """kind: 'compare' (ages + profiles), 'optimise' (all profiles, full search), 'full' (= optimise + robustness)."""
    now = now or time.time()
    end = now - 2 * 3600
    c = sbt.cache()
    dc = data_check(c, end)
    _job(status="rodando", kind=kind, stage="dados: " + str(dc["coins"]) + " moedas, " + str(dc["days"]) + "/" + str(dc["days_total"]) + " dias",
         progress=5, started=time.time())
    rep = {"at": time.time(), "end": end, "data": dc, "kind": kind, "profiles": {}, "ages": {}}
    n = SEARCH_N if kind != "compare" else max(60, SEARCH_N // 5)
    deadline = time.time() + ANALYSIS_S
    late = lambda: _STOP.is_set() or time.time() > deadline
    steps = len(PROFILES) + len(BUCKETS)
    k = 0
    for prof in PROFILES:
        if _STOP.is_set():
            break
        o = optimise(c, end, prof, n, should_stop=late)
        if o:
            o["robust"] = robustness(c, end, o["params"])
            o["ok"] = validated(o, o["robust"])
        rep["profiles"][prof] = o
        k += 1
        _job(progress=5 + int(90 * k / steps), stage="perfil " + prof + " pronto")
    for i, (label, ages) in enumerate(BUCKETS):
        if _STOP.is_set():
            break
        o = optimise(c, end, "B", max(40, n // 4), random.Random(11 + i), bucket=i, should_stop=late)
        rep["ages"][label] = {"test": o["test"], "final": o["final"]} if o else None
        k += 1
        _job(progress=5 + int(90 * k / steps), stage="idade " + label + " pronta")
    good = [(p, o) for p, o in rep["profiles"].items() if o and o.get("ok")]
    rep["winner"] = max(good, key=lambda po: po[1]["test"]["pnl"] + po[1]["final"]["pnl"])[0] if good else None
    slim = json.loads(json.dumps(rep))
    for o in slim["profiles"].values():
        if o:
            for k2 in ("dev", "val"):
                o[k2].pop("trade_list", None)
    sbt._put("sniper_pro_report", slim)
    if rep["winner"]:
        sbt._put("sniper_pro_best", {"ok": True, "profile": rep["winner"], "params": rep["profiles"][rep["winner"]]["params"]})
    else:
        sbt._put("sniper_pro_best", {"ok": False})
    _job(status="cancelado" if _STOP.is_set() else "pronto", progress=100, stage="pronto", finished=time.time())
    txt = text(slim)
    if send and chat:
        send(chat, txt)
    return slim


def start(kind, rpc=None, sol_usd=None, send=None, chat=None):
    """'download' = the 180-day download (snipe_backtest, resumable) and then the full analysis."""
    if not _RUN.acquire(blocking=False):
        return "já tem um backtest PRO rodando"
    _STOP.clear()

    def go():
        try:
            if kind == "download":
                if rpc is None:
                    raise ValueError("precisa do SOLANA_RPC_URL (Helius) no Render")
                _job(status="rodando", kind=kind, stage="baixando lançamentos reais (" + format(DL_S, "g") + " s por toque)", progress=0,
                     started=time.time())
                end = time.time() - 2 * 3600
                begin = end - 180 * 86400

                def prog(done, total, coins):
                    _job(progress=int(40 * done / max(total, 1)), stage="baixando: dias " + str(done) + "/" + str(total) +
                         " · " + str(coins) + " moedas")
                sbt.DEADLINE[0] = time.time() + DL_S
                try:
                    sbt.download(rpc, sol_usd, None, begin, end, _STOP.is_set, prog, max_s=DL_S)
                except TimeoutError:
                    pass
                finally:
                    sbt.DEADLINE[0] = 0.0
                run("full", send, chat)
            else:
                run(kind, send, chat)
        except Exception as exc:
            logging.exception("sniper pro failed")
            _job(status="erro", stage=type(exc).__name__ + ": " + str(exc)[:150])
            if send and chat:
                send(chat, "⛔ Backtest PRO falhou: " + type(exc).__name__ + ": " + str(exc)[:150])
        finally:
            _RUN.release()

    threading.Thread(target=go, daemon=True).start()
    return ""


def cancel():
    _STOP.set()
    sbt.cancel()


def apply_to_simulation():
    """Best VALIDATED setting -> the sniper's SIMULATION mode (never the real account)."""
    b = sbt._get("sniper_pro_best", {})
    if not b.get("ok"):
        return None
    p = b["params"]
    cfg = {"entry": [p["min_vol"], p["min_ratio"], p["min_up"], p["max_up"], p["min_score"]],
           "exit": [p["sl"], p["tp"] if p["trail"] else 9.0, p["trail"] or 0.0, p["hold"], 0.0 if p["trail"] else p["tp"]],
           "window": [min(p["ages"]), max(p["ages"]) + 5]}
    sb._use_cfg(cfg)
    sb.start_sim()
    with sb._LOCK:
        d = sb._load()
        d["cfg"] = cfg                                     # used by the simulation after restarts too
        sb._save(d)
    return b


# ---------------------------------------------------------------- report text
def _fmt(s):
    if not s:
        return "sem dados"
    return ("US$ " + format(s["start_cap"], "g") + " → " + format(s["final"], ".2f") + " (" + format(s["pnl"], "+.2f") + ", " +
            format(s["ret"], "+.1f") + "%) · " + str(s["trades"]) + " ops · acerto " + format(s["win"] * 100, ".0f") + "% · PF " +
            ("—" if s["pf"] is None else format(s["pf"], ".2f")) + " · DD US$ " + format(s["dd"], ".2f") + " · taxas US$ " +
            format(s["fees"], ".2f"))


def _pdesc(p):
    return (BUCKETS[p["bucket"]][0] + " · vol ≥ US$ " + format(p["min_vol"], ",.0f") + (" · compras/vendas ≥ " + format(p["min_ratio"], "g") if p["min_ratio"] else "") +
            (" · subiu " + format(p["min_up"], "g") + "–" + format(p["max_up"], "g") + "%" if p["max_up"] < 10000 else "") +
            (" · nota ≥ " + format(p["min_score"], "g") if p["min_score"] else "") + " · " +
            ("stop móvel " + format(p["trail"] * 100, ".0f") + "% depois de +" if p["trail"] else "alvo +") + format(p["tp"] * 100, ".0f") + "%" +
            (" (vende metade)" if p.get("partial") and not p["trail"] else "") + " · stop −" + format(p["sl"] * 100, ".0f") + "% · máx. " +
            format(p["hold"], "g") + " min · US$ " + format(p["size"], "g") + " · máx. " + str(p["max_open"]))


def text(rep):
    if not rep or not rep.get("data"):
        return "🔬 Backtest PRO: ainda não rodou."
    dc = rep["data"]
    L = ["🔬 SNIPER BACKTEST PRO — 180 dias (dados REAIS da blockchain, histórico, não é operação real)",
         "Dados: " + str(dc["coins"]) + " lançamentos (" + str(dc["alive"]) + " com volume), " + str(dc["days"]) + " de " + str(dc["days_total"]) +
         " dias da amostra · por período: " + ", ".join(k + " " + str(v) for k, v in dc["per_window"].items()),
         "Períodos: otimiza nos dias 1–90, valida 91–120, TESTE 121–150 e TESTE INDEPENDENTE 151–180 (nunca usados para escolher)."]
    for prof, o in (rep.get("profiles") or {}).items():
        L.append(("🏆 " if rep.get("winner") == prof else "▫️ ") + "Perfil " + prof + " — " + PROFILES[prof]["label"])
        if not o:
            L.append("  • sem operações suficientes")
            continue
        L += ["  • ajuste: " + _pdesc(o["params"]), "  • operações nos 180 dias: " + str(total_ops(o)) + (" ✅" if total_ops(o) >= MIN_OPS else
              " (mínimo " + str(MIN_OPS) + ": faltam dados ou o filtro é rígido)"), "  • validação: " + _fmt(o["val"]), "  • TESTE: " + _fmt(o["test"]),
              "  • TESTE INDEPENDENTE: " + _fmt(o["final"])]
        r = o.get("robust") or {}
        if r:
            L.append("  • robustez (60 dias finais): normal " + format(r["base"], "+.2f") + " · taxas ×1,5 " + format(r["cost_x1.5"], "+.2f") +
                     " · ×2 " + format(r["cost_x2"], "+.2f") + " · atraso " + format(r["delay"], "+.2f") + " · 20% vendas falhando " +
                     format(r["sell_fail_20%"], "+.2f") + " · pior sequência " + str(r["streak"]) + " perdas · meses negativos " +
                     str(r["months_negative"]) + "/" + str(len(r["months"])))
    if rep.get("ages"):
        L.append("📅 Por idade (perfil B, resultado nos 60 dias de teste):")
        for label, o in rep["ages"].items():
            if o:
                L.append("  • " + label + ": " + format(o["test"]["pnl"] + o["final"]["pnl"], "+.2f") + " USD em " +
                         str(o["test"]["trades"] + o["final"]["trades"]) + " ops")
            else:
                L.append("  • " + label + ": sem operações suficientes")
    if rep.get("winner"):
        L.append("✅ Perfil " + rep["winner"] + " passou: ≥ " + str(MIN_OPS) + " operações, lucro nos dois testes fora da amostra, PF ≥ 1,2 e ainda positivo com taxas ×1,5, "
                 "atraso e 20% de vendas falhando. Próximo passo: TESTAR MELHOR ESTRATÉGIA EM SIMULAÇÃO.")
    else:
        L.append("❌ Nenhum perfil passou nos testes fora da amostra e de robustez. Não recomendo dinheiro real com esse sniper.")
    if dc["days"] < dc["days_total"]:
        L.append("⏳ PARCIAL: " + str(dc["days"]) + " de " + str(dc["days_total"]) + " dias da amostra nova baixados (mais os lançamentos dos "
                 "testes antigos). Toque de novo: cada toque baixa mais ~" + format(DL_S, "g") + " s e refaz a análise.")
    L.append("⚠️ Faltam nos dados históricos: " + "; ".join(dc["missing"]) + ". Esses filtros só existem no robô ao vivo. "
             "Amostra de lançamentos (não todos); preço do SOL constante. Passado não garante futuro.")
    return "\n".join(L)
