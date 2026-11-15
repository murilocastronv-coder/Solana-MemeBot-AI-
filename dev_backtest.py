"""DEV backtest job (owner's spec Nov 15: 'EXECUTAR BACKTEST — 5 MESES').

Runs on the server in its own thread (the panel and the robot keep working): downloads the devs' real
on-chain trade history (checkpoint per dev, continues after a restart or a cancel), then simulates the
copier over the chosen period and stores the report + CSV in PostgreSQL (dev_bot state).

Model (honest approximation, written in every report):
- each copied trade = the dev's REAL closed trade on a pump.fun coin, minus entry delay, swap costs per side
  and network fees; dev trades with near-zero cost (free creator tokens) or > +1000% are left out;
- a dev is copied on a date only if its trades CLOSED BEFORE that date pass the rule (no future data):
  >= 20 trades, hit rate >= the strategy's, profit factor >= 2 and net profit > 0;
- max open positions and cash are respected; the last 30% of the period is out of sample (the best strategy
  is chosen on the first 70% only).
Not modelled (no data in the downloaded history): the price path inside a trade, other wallets' buys
(smart money), momentum, failed transactions. Strategies D/E/F are reported as NOT CALCULATED.
"""
import csv
import io
import logging
import threading
import time

import dev_bot as db

STRATS = (("A: DEV com acerto ≥ 70%", 0.70), ("B: DEV com acerto ≥ 80%", 0.80), ("C: DEV com acerto ≥ 90%", 0.90))
MISSING = (("D: DEV 80% + Smart Money", "precisa das compras de outras carteiras em cada moeda"),
           ("E: DEV 80% + Momentum", "precisa do caminho do preço de cada moeda (velas) — a fonte gratuita bloqueia o servidor"),
           ("F: DEV 80% + Smart Money + Momentum", "precisa dos dois dados acima"))
DELAY_COST = db.E("DEV_BT_DELAY_COST", 0.03)
CAPITAL = db.E("DEV_BT_PERIOD_CAPITAL", 500)
MIN_TRADES, MIN_PF = 20, 2.0
JOB_MAX_S = db.E("DEV_BT_JOB_MINUTES", 20) * 60
_STOP = threading.Event()
_RUNNING = threading.Lock()


# ---------------------------------------------------------------- simulation
def _qualifies(prior, need):
    if len(prior) < MIN_TRADES:
        return "DEV com menos de 20 operações antes da data"
    pnl = [float(q.get("pnl_usd") or 0) for q in prior]
    win = sum(1 for x in pnl if x > 0) / len(pnl)
    if win < need:
        return "DEV com acerto abaixo de " + format(need * 100, ".0f") + "% antes da data"
    gw, gl = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x < 0)
    if gl > 0 and gw / gl < MIN_PF:
        return "DEV com profit factor abaixo de 2 antes da data"
    if sum(pnl) <= 0:
        return "DEV sem lucro líquido antes da data"
    return ""


def simulate(devs, start, end, need, capital=None, usd=None, max_open=None):
    """Copier simulation over [start, end). Returns (summary, trades, rejected{reason: n})."""
    capital = float(capital or CAPITAL)
    usd = float(usd or db.buy_usd())
    max_open = int(max_open or db.MAX_OPEN)
    events = sorted(((float(h["opened"]), dev, h) for dev, hs in devs.items() for h in hs
                     if h.get("closed") and h.get("opened") and start <= float(h["opened"]) < end),
                    key=lambda t: t[0])
    cash, open_until, trades, rejected = capital, [], [], {}
    for t, dev, h in events:
        still = []                                   # settle the positions closed before this entry
        for c, back in open_until:
            if c <= t:
                cash += back
            else:
                still.append((c, back))
        open_until = still
        prior = [q for q in devs[dev] if q.get("closed") and float(q["closed"]) < t]
        why = _qualifies(prior, need)
        if not why:
            if len(open_until) >= max_open:
                why = "já tinha " + str(max_open) + " posições abertas"
            elif cash < usd + 2 * db.NET_FEE_USD:
                why = "sem saldo"
        if why:
            rejected[why] = rejected.get(why, 0) + 1
            continue
        ret = float(h.get("result_pct") or 0) - DELAY_COST - 2 * db.COST
        pnl = usd * ret - 2 * db.NET_FEE_USD
        fee = usd * 2 * db.COST + 2 * db.NET_FEE_USD
        cash -= usd
        open_until.append((float(h["closed"]), usd + pnl))
        trades.append({"dev": dev, "mint": h.get("mint"), "opened": t, "closed": float(h["closed"]),
                       "dev_result_pct": round(float(h.get("result_pct") or 0) * 100, 2), "pnl_usd": round(pnl, 4),
                       "fee_usd": round(fee, 4)})
    for c, back in open_until:
        cash += back
    return _summary(trades, capital, start, end), trades, rejected


def _summary(trades, capital, start, end):
    eq, peak, dd, days = capital, capital, 0.0, {}
    for tr in sorted(trades, key=lambda x: x["closed"]):
        eq += tr["pnl_usd"]
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak > 0 else 0)
        k = time.strftime("%Y-%m-%d", time.gmtime(tr["closed"]))
        days[k] = days.get(k, 0.0) + tr["pnl_usd"]
    pnl = [x["pnl_usd"] for x in trades]
    gw, gl = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x < 0)
    span = max(1.0, (end - start) / 86400)
    return {"capital": capital, "final": round(capital + sum(pnl), 2), "pnl": round(sum(pnl), 2),
            "pct": round(sum(pnl) / capital * 100, 1) if capital else 0.0, "trades": len(trades),
            "per_day": round(len(trades) / span, 2), "win": round(sum(1 for x in pnl if x > 0) / len(pnl), 3) if pnl else 0.0,
            "pf": round(gw / gl, 2) if gl > 0 else None, "max_dd": round(dd, 3), "worst": round(min(pnl, default=0.0), 2),
            "fees": round(sum(x["fee_usd"] for x in trades), 2),
            "best_day": max(days.items(), key=lambda kv: kv[1]) if days else None,
            "worst_day": min(days.items(), key=lambda kv: kv[1]) if days else None,
            "curve": _curve(trades, capital, start, end)}


def _curve(trades, capital, start, end, points=30):
    if end <= start:
        return []
    step = (end - start) / points
    out, eq, i = [], capital, 0
    srt = sorted(trades, key=lambda x: x["closed"])
    for k in range(1, points + 1):
        edge = start + k * step
        while i < len(srt) and srt[i]["closed"] <= edge:
            eq += srt[i]["pnl_usd"]
            i += 1
        out.append(round(eq, 2))
    return out


def coverage(devs, start, end):
    """Days of the period that have dev trades in the downloaded history, and the missing stretches."""
    have = sorted({int(float(h["opened"]) // 86400) for hs in devs.values() for h in hs
                   if h.get("opened") and start <= float(h["opened"]) < end})
    first, last = int(start // 86400), int((end - 1) // 86400)
    gaps, cur = [], None
    have_set = set(have)
    for day in range(first, last + 1):
        if day not in have_set:
            cur = [day, day] if cur is None else [cur[0], day]
        elif cur is not None:
            gaps.append(cur)
            cur = None
    if cur is not None:
        gaps.append(cur)
    big = [g for g in gaps if g[1] - g[0] >= 1]
    return len(have), max(1, round((end - start) / 86400)), big


def _day(d):
    return time.strftime("%d/%m/%Y", time.gmtime(d * 86400))


def report(start, end, label="", d=None):
    d = d or db._load()
    devs = {k: [h for h in v.get("hist") or [] if (not db.PUMP_ONLY or db.pump_trade(h)) and db.bt_ok(h)]
            for k, v in d["devs"].items() if v.get("hist")}
    devs = {k: v for k, v in devs.items() if v}
    cut = start + (end - start) * 0.7
    rows, res = [], {}
    for name, need in STRATS:
        tr_sum, _, _ = simulate(devs, start, cut, need)
        te_sum, te_trades, te_rej = simulate(devs, cut, end, need)
        full, trades, rej = simulate(devs, start, end, need)
        res[name] = {"train": tr_sum, "test": te_sum, "full": full, "rejected": rej, "trades": trades}
        for t in trades:
            rows.append(dict(t, strategy=name, part="teste" if t["opened"] >= cut else "treino"))
    ok = [(n, r) for n, r in res.items() if r["train"]["trades"] >= 10]
    best = max(ok, key=lambda kv: kv[1]["train"]["pnl"] - kv[1]["train"]["capital"] * kv[1]["train"]["max_dd"])[0] if ok else None
    have, total, gaps = coverage(devs, start, end)
    return {"label": label, "start": start, "end": end, "cut": cut, "devs": len(devs), "results": res, "best": best,
            "days_with_data": have, "days": total, "gaps": gaps, "rows": rows}


def text(rep):
    usd = db.buy_usd()
    L = ["🧪 BACKTEST DEV — " + rep["label"] + " (" + time.strftime("%d/%m/%Y", time.gmtime(rep["start"])) + " a " +
         time.strftime("%d/%m/%Y", time.gmtime(rep["end"])) + ")",
         "Capital US$ " + format(CAPITAL, "g") + " · US$ " + format(usd, "g") + " por compra · máx. " + str(db.MAX_OPEN) +
         " abertas · atraso " + format(DELAY_COST * 100, ".0f") + "% + custos 2×" + format(db.COST * 100, ".0f") + "% + taxa de rede · " +
         str(rep["devs"]) + " DEVs com histórico",
         "📅 Dados: " + str(rep["days_with_data"]) + " de " + str(rep["days"]) + " dias têm operações de DEVs no histórico baixado."]
    if rep["gaps"]:
        g = rep["gaps"]
        L.append("⚠️ Períodos SEM dados: " + "; ".join(_day(a) + "–" + _day(b) for a, b in g[:4]) +
                 (" e mais " + str(len(g) - 4) if len(g) > 4 else "") + ". Toque de novo para baixar mais DEVs.")
    L.append("Escolha só nos primeiros 70% do tempo; TESTE = últimos 30% (fora da amostra):")
    for name, r in rep["results"].items():
        t, f = r["test"], r["full"]
        L.append("• " + name + " — TESTE: US$ " + format(t["capital"], "g") + " → " + format(t["final"], ",.2f") + " (" +
                 format(t["pct"], "+.1f") + "%) · " + str(t["trades"]) + " ops · acerto " + format(t["win"] * 100, ".0f") +
                 "% · PF " + ("—" if t["pf"] is None else format(t["pf"], ".2f")) + " · DD " + format(t["max_dd"] * 100, ".0f") +
                 "% | período todo: " + format(f["pnl"], "+.2f") + " USD em " + str(f["trades"]) + " ops")
    for name, why in MISSING:
        L.append("• " + name + ": NÃO CALCULADO — " + why + ".")
    if rep["best"]:
        r = rep["results"][rep["best"]]["full"]
        t = rep["results"][rep["best"]]["test"]
        L += ["🏆 Melhor no treino: " + rep["best"] + " → no TESTE " + format(t["pnl"], "+.2f") + " USD",
              "📊 Período todo (" + rep["best"] + "): US$ " + format(r["capital"], "g") + " → US$ " + format(r["final"], ",.2f") +
              " (" + format(r["pnl"], "+.2f") + " · " + format(r["pct"], "+.1f") + "%) · " + str(r["trades"]) + " ops (" +
              format(r["per_day"], ".1f") + "/dia) · acerto " + format(r["win"] * 100, ".0f") + "% · PF " +
              ("—" if r["pf"] is None else format(r["pf"], ".2f")) + " · DD " + format(r["max_dd"] * 100, ".0f") +
              "% · maior perda " + format(r["worst"], "+.2f") + " · taxas US$ " + format(r["fees"], ".2f")]
        if r["best_day"]:
            L.append("📈 Melhor dia " + r["best_day"][0] + " " + format(r["best_day"][1], "+.2f") + " · 📉 pior dia " +
                     r["worst_day"][0] + " " + format(r["worst_day"][1], "+.2f"))
        by_dev, by_tok = {}, {}
        for tr in rep["results"][rep["best"]]["trades"]:
            by_dev[tr["dev"]] = by_dev.get(tr["dev"], 0.0) + tr["pnl_usd"]
            by_tok[tr["mint"]] = by_tok.get(tr["mint"], 0.0) + tr["pnl_usd"]
        L.append("👤 Por DEV: " + " · ".join(k[:6] + "… " + format(v, "+.2f") for k, v in
                                            sorted(by_dev.items(), key=lambda kv: -kv[1])[:5]))
        L.append("🪙 Melhores moedas: " + " · ".join(str(k)[:6] + "… " + format(v, "+.2f") for k, v in
                                                  sorted(by_tok.items(), key=lambda kv: -kv[1])[:3]) +
                 " | piores: " + " · ".join(str(k)[:6] + "… " + format(v, "+.2f") for k, v in
                                           sorted(by_tok.items(), key=lambda kv: kv[1])[:3]))
        rej = rep["results"][rep["best"]]["rejected"]
        if rej:
            L.append("🚫 Rejeitadas: " + " · ".join(k + ": " + str(v) for k, v in sorted(rej.items(), key=lambda kv: -kv[1])))
    else:
        L.append("🏆 Nenhuma estratégia teve 10+ operações no treino — dados insuficientes neste período.")
    L.append("⚠️ Aproximação: usa o resultado REAL de cada operação do DEV menos atraso e custos; não tem o caminho do "
             "preço, falhas de transação nem compras de outras carteiras; posições que o DEV nunca fechou ficam de fora. "
             "Passado não garante futuro.")
    return "\n".join(L)


def to_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["estrategia", "parte", "dev", "moeda", "abriu_utc", "fechou_utc", "resultado_dev_pct", "lucro_bot_usd", "taxas_usd"])
    for r in rows[:3000]:
        w.writerow([r["strategy"], r["part"], r["dev"], r["mint"], time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["opened"])),
                    time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["closed"])), r["dev_result_pct"], r["pnl_usd"], r["fee_usd"]])
    return buf.getvalue()


# ---------------------------------------------------------------- job
def job():
    return db._load().get("bt_job") or {}


def _runs_path():
    return db._path().with_name("dev_bt_runs.json")


def runs():
    """Saved reports (PostgreSQL key dev_bt_runs, disk copy as backup) - kept apart from the robot state,
    which is read every few seconds, because the CSVs are big."""
    import json
    import pg_store
    if pg_store.enabled():
        try:
            got = pg_store.get("dev_bt_runs")
            if got is not None:
                return got
        except Exception:
            pass
    p = _runs_path()
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except (OSError, ValueError):
        return []


def _save_runs(rs):
    import json
    import pg_store
    if pg_store.enabled():
        try:
            pg_store.put("dev_bt_runs", rs)
        except Exception as exc:
            logging.warning("devbot backtest runs: postgres save failed (%s)", type(exc).__name__)
    p = _runs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rs), encoding="utf-8")


def _set_job(**kw):
    with db._LOCK:
        d = db._load()
        j = d.get("bt_job") or {}
        j.update(kw)
        d["bt_job"] = j
        db._save(d)
        return j


def mark_interrupted():
    """At start-up: a job that was running when the server restarted is marked so (the next run continues)."""
    j = job()
    if j.get("status") == "rodando":
        _set_job(status="interrompido", stage="o servidor reiniciou — toque de novo para continuar de onde parou")


def cancel():
    _STOP.set()
    if job().get("status") == "rodando":
        _set_job(stage="cancelando…")
    return True


def start(days=None, start_ts=None, end_ts=None, label=None, rpc=None, sol_usd=None, send=None, chat=None, now=None):
    """Start the backtest job in the background. Returns '' when started, else why not."""
    if not _RUNNING.acquire(blocking=False):
        return "já tem um backtest rodando"
    now = now or time.time()
    end_ts = float(end_ts or now)
    start_ts = float(start_ts or end_ts - float(days or 150) * 86400)
    if end_ts <= start_ts:
        _RUNNING.release()
        return "data final antes da inicial"
    label = label or (format((end_ts - start_ts) / 86400 / 30, ".0f") + " meses")
    _STOP.clear()
    jid = str(int(now))
    _set_job(id=jid, status="rodando", progress=0, stage="baixando histórico dos DEVs", label=label,
             start=start_ts, end=end_ts, started=now, finished=None, devs_done=0)

    def work():
        try:
            if rpc is not None:
                def prog(done, total):
                    _set_job(progress=int(80 * done / max(total, 1)), devs_done=done,
                             stage="baixando histórico dos DEVs: " + str(done) + "/" + str(total))
                db.download_histories(rpc, sol_usd, now=now, max_s=JOB_MAX_S, should_stop=_STOP.is_set, on_progress=prog)
            if _STOP.is_set():
                _set_job(status="cancelado", stage="cancelado pelo dono (o que já baixou fica salvo)", finished=time.time())
                if send and chat:
                    send(chat, "⏹️ Backtest DEV cancelado. O histórico já baixado fica salvo.")
                return
            _set_job(progress=90, stage="simulando as estratégias")
            rep = report(start_ts, end_ts, label)
            txt = text(rep)
            summary = {n: {"test": r["test"], "full": r["full"]} for n, r in rep["results"].items()}
            _save_runs(runs()[-4:] + [{
                "id": jid, "label": label, "start": start_ts, "end": end_ts, "created": time.time(), "text": txt,
                "best": rep["best"], "summary": summary, "csv": to_csv(rep["rows"]),
                "days_with_data": rep["days_with_data"], "days": rep["days"]}])
            with db._LOCK:
                d = db._load()
                d["backtest"] = txt
                j = d.get("bt_job") or {}
                j.update(status="pronto", progress=100, stage="pronto", finished=time.time())
                d["bt_job"] = j
                db._save(d)
            db.note("backtest " + label + ": " + (rep["best"] or "sem estratégia com dados suficientes"))
            if send and chat:
                send(chat, txt)
        except Exception as exc:
            logging.exception("devbot backtest job failed")
            _set_job(status="erro", stage=type(exc).__name__ + ": " + str(exc)[:120], finished=time.time())
            if send and chat:
                send(chat, "⛔ Backtest DEV falhou: " + type(exc).__name__)
        finally:
            _RUNNING.release()

    threading.Thread(target=work, daemon=True).start()
    return ""
