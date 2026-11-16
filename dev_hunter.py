"""DEV Hunter (owner's spec, Nov 15): mass discovery of memecoin creators.

'BUSCAR DEVS LUCRATIVOS' job: reads pump.fun launches spread over the last 7 / 30 / 90 days (Helius history of the
pump.fun program, time-filtered), takes the creator of each new coin (fee payer of the create transaction),
removes duplicates and wallets already known, and puts the new ones in the robot's analysis queue (dev_bot
devs, tier 'NA FILA'). The 24/7 audit then rebuilds each one's real trades and classifies it with the same
rules as always (it is the Helius credit budget that sets the pace, not the number of threads).
Progress, cancel and the results (found / analysed / approved / rejected + reasons) are saved in PostgreSQL.
"""
import logging
import threading
import time

import dev_bot as db

_STOP = threading.Event()
_RUNNING = threading.Lock()
POINT_WINDOW_S = 600


def job():
    return db._load().get("hunt_job") or {}


def _set_job(**kw):
    with db._LOCK:
        d = db._load()
        j = d.get("hunt_job") or {}
        j.update(kw)
        d["hunt_job"] = j
        db._save(d)
        return j


def set_cfg(min_win=None, min_trades=None):
    with db._LOCK:
        d = db._load()
        cfg = d.get("hunt_cfg") or {}
        if min_win is not None:
            cfg["min_win"] = max(0.7, min(1.0, float(min_win)))        # never below the owner's 70%
        if min_trades is not None:
            cfg["min_trades"] = max(20, int(min_trades))               # never below 20 operations
        d["hunt_cfg"] = cfg
        db._save(d)
        return cfg


def results(d=None):
    """Counts of the hunted wallets: found, analysed, approved, rejected and the rejection reasons."""
    d = d or db._load()
    hunted = {k: v for k, v in d["devs"].items() if v.get("found_by") == "hunter"}
    analysed = {k: v for k, v in hunted.items() if v.get("tier") not in ("NA FILA", None)}
    ok = [k for k, v in analysed.items() if v.get("tier") in db.TIER_OK]
    reasons = {}
    for v in analysed.values():
        if v.get("tier") in db.TIER_OK:
            continue
        why = str(v.get("why") or v.get("tier"))
        if why.startswith("carteira sem fundo"):
            why = "carteira sem fundo"
        key = why.split("(")[0].split(":")[0].strip()[:40]
        reasons[key] = reasons.get(key, 0) + 1
    return {"found": len(hunted), "analysed": len(analysed), "approved": len(ok), "rejected": len(analysed) - len(ok),
            "queue": len(hunted) - len(analysed), "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:8])}


def cancel():
    _STOP.set()
    return True


def mark_interrupted():
    if job().get("status") == "rodando":
        _set_job(status="interrompido", stage="o servidor reiniciou — toque de novo (o que já achou fica salvo)")


def hunt(rpc, days, max_cand, now=None, history=None, should_stop=None, on_progress=None, balance=None):
    """Find up to max_cand new creator wallets in the last `days`. Returns how many new ones were queued."""
    import snipe_backtest as sbt
    history = history or sbt.history_call
    now = now or time.time()
    points = max(10, min(400, int(max_cand)))                # ~1 Helius call per point
    found = 0
    for i in range(points):
        if (should_stop and should_stop()) or found >= max_cand:
            break
        t = int(now - days * 86400 + (days * 86400 - 3600) * (i + 0.5) / points)
        try:
            txs = history(rpc, sbt.PUMP, gte=t, lte=t + POINT_WINDOW_S, order="asc", limit=100)
        except Exception as exc:
            logging.warning("devhunter point: %s: %s", type(exc).__name__, str(exc)[:200])
            if isinstance(exc, ValueError) and i == 0:
                raise                                  # the API refused the request: stop and show why
            continue
        new = []
        for tx in txs:
            mint = sbt.created_mint(tx)
            keys = sbt._keys(tx)
            if mint and keys:
                new.append((keys[0], mint))
        if new:
            known = set(db._load()["devs"])
            fresh = []
            for dev, mint in dict(new).items():
                if dev in known:
                    continue                             # duplicates / already known: no second analysis
                bal = (balance or db.balance_sol)(rpc, dev)   # owner: only wallets with funds (1 cheap call)
                fresh.append((dev, mint, bal))
            with db._LOCK:
                d = db._load()
                for dev, mint, bal in fresh:
                    if dev in d["devs"] or found >= max_cand:
                        continue
                    if bal is not None and bal < db.MIN_BALANCE_SOL:
                        d["devs"][dev] = {"tokens_seen": [mint], "tier": "MONITORADO", "rated": now, "found_by": "hunter",
                                          "found_at": now, "balance_sol": bal,
                                          "why": "carteira sem fundo: " + format(bal, ".2f") + " SOL"}
                        continue
                    d["devs"][dev] = {"tokens_seen": [mint], "tier": "NA FILA", "rated": 0, "found_by": "hunter",
                                      "found_at": now, "balance_sol": bal}
                    found += 1
                db._save(d)
        if on_progress:
            on_progress(i + 1, points, found)
    return found


def start(rpc, days=30, max_cand=300, min_win=None, min_trades=None, send=None, chat=None):
    if rpc is None:
        return "precisa do SOLANA_RPC_URL (Helius) no Render"
    if not _RUNNING.acquire(blocking=False):
        return "já tem uma busca rodando"
    set_cfg(min_win, min_trades)
    _STOP.clear()
    _set_job(status="rodando", progress=0, days=days, max_cand=max_cand, started=time.time(),
             stage="lendo lançamentos dos últimos " + str(days) + " dias")

    def work():
        try:
            def prog(done, total, found):
                _set_job(progress=int(100 * done / max(total, 1)),
                         stage="momentos lidos " + str(done) + "/" + str(total) + " · " + str(found) + " carteiras novas na fila")
            n = hunt(rpc, days, max_cand, should_stop=_STOP.is_set, on_progress=prog)
            r = results()
            _set_job(status="cancelado" if _STOP.is_set() else "pronto", finished=time.time(), new=n,
                     stage=str(n) + " carteiras novas na fila de análise")
            db.note("DEV Hunter: " + str(n) + " carteiras novas de criadores (últimos " + str(days) + " dias)")
            if send and chat:
                send(chat, "🕵️ DEV HUNTER: achei " + str(n) + " carteiras novas de criadores (últimos " + str(days) +
                     " dias). Elas entram na fila e são analisadas uma a uma (limite de créditos da Helius).\n"
                     "Total da busca: " + str(r["found"]) + " achadas · " + str(r["analysed"]) + " analisadas · " +
                     str(r["approved"]) + " aprovadas · " + str(r["queue"]) + " na fila.")
        except Exception as exc:
            logging.exception("devhunter failed")
            _set_job(status="erro", stage=type(exc).__name__ + ": " + str(exc)[:120])
            if send and chat:
                send(chat, "⛔ DEV Hunter falhou: " + type(exc).__name__ + ": " + str(exc)[:160])
        finally:
            _RUNNING.release()

    threading.Thread(target=work, daemon=True).start()
    return ""


def table(order="lucro", limit=100, d=None):
    """Rows for the DEV panel: approved and analysed devs, sorted by profit, hit rate or recent activity."""
    d = d or db._load()
    rows = []
    for k, v in d["devs"].items():
        if "win" not in v:
            continue
        last = max([float(h.get("closed") or 0) for h in v.get("hist") or []] + [0.0])
        rows.append({"dev": k, "tier": v.get("tier"), "win": v.get("win", 0), "ci": v.get("win_ci"),
                     "trades": v.get("trades", 0), "launches": v.get("launches"), "tokens": v.get("tokens"),
                     "pnl": v.get("pnl", 0), "pf": v.get("pf", 0), "dd": v.get("max_dd", 0), "last": last,
                     "win_own": v.get("win_own"), "n_own": v.get("n_own"), "win_other": v.get("win_other"),
                     "n_other": v.get("n_other"), "watch": v.get("tier") in db.TIER_OK, "why": v.get("why")})
    key = {"acerto": lambda r: (r["win"], r["trades"]), "atividade": lambda r: r["last"]}.get(order, lambda r: r["pnl"])
    rows.sort(key=lambda r: (r["watch"], key(r)), reverse=True)
    return rows[:limit]
