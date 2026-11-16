"""SNIPER backtest, 8 months (owner, Nov 15), with REAL on-chain trades read through Helius.

How it works (honest approximation, written in the report):
1. Sample of launches: at SAMPLE_POINTS moments spread over the period, read the pump.fun program's
   transactions at that moment (Helius getTransactionsForAddress, filtered by time) and keep the coins
   created there ("Instruction: Create" in the logs).
2. For each coin, at the decision moments (age 5, 10, 15, 20, 30, 45 min) read the last trades of the 5 minutes
   before: price, 5-min volume, buys / sells, 5-min move, market cap (pump.fun supply = 1 billion).
   Same entry rule as the live bot (snipe_bot.metrics_reason).
3. If it enters: read the trades after the entry (+SNIPE_BT_DELAY_S) and walk them one by one with the same exit
   rule as the live bot (snipe_bot.exit_signal): stop, trailing stop, time stop. Costs: swap + slippage per side
   and network fee. A coin with no trade after the entry = total loss (rug / no liquidity).
4. Portfolio: US$ per trade, max open, daily loss limit; chronological; first 70% of the time = training
   (a small grid of entry / exit settings is chosen there), last 30% = test (out of sample).
Limits: a SAMPLE of launches, not all; SOL price taken as constant (current) for US$ thresholds; failed
transactions are not modelled. Every downloaded coin is cached (PostgreSQL), so the next run continues.
"""
import json
import logging
import os
import threading
import time

import signals as sg
import snipe_bot as sb

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
DAYS = sg._f("SNIPE_BT_DAYS", 60)          # owner (Nov 16): 2 months
SAMPLE_POINTS = int(sg._f("SNIPE_BT_SAMPLE_POINTS", 120))      # 2 per day over 2 months
DECISION_MIN = (5, 10, 15, 20, 30, 45)
DELAY_S = sg._f("SNIPE_BT_DELAY_S", 20)
COST = sg._f("SNIPE_BT_COST_PER_SIDE", 0.03)                  # swap fee + slippage on young coins
NET_FEE = sg._f("SNIPE_BT_NET_FEE_USD", 0.02)
CAPITAL = sg._f("SNIPE_BT_CAPITAL", 500)
CALLS_PER_HOUR = int(sg._f("SNIPE_BT_CALLS_PER_HOUR", 1500))   # Helius budget (~100 credits per call)
JOB_MAX_S = sg._f("SNIPE_BT_JOB_MINUTES", 3) * 60     # owner (Nov 16): answer in ~3 min; the next tap continues
SUPPLY = 1_000_000_000
MIN_TRADE_USD = sg._f("SNIPE_BT_MIN_TRADE_USD", 2)
GRID_ENTRY = ((3000, 1.5, 5, 40), (5000, 2.0, 5, 30), (2000, 1.3, 10, 60))
GRID_EXIT = ((0.20, 0.30, 0.12, 30), (0.15, 0.20, 0.10, 20), (0.30, 0.50, 0.20, 45))
_STOP = threading.Event()
_RUNNING = threading.Lock()
_CALLS = []


# ---------------------------------------------------------------- storage
def _path(name):
    import live_trade_state
    return live_trade_state._path().with_name(name + ".json")


def _get(key, default):
    import pg_store
    if pg_store.enabled():
        try:
            got = pg_store.get(key)
            if got is not None:
                return got
        except Exception:
            pass
    p = _path(key)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except (OSError, ValueError):
        return default


def _put(key, value):
    import pg_store
    if pg_store.enabled():
        try:
            pg_store.put(key, value)
        except Exception as exc:
            logging.warning("snipe backtest: postgres save failed (%s)", type(exc).__name__)
    p = _path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value), encoding="utf-8")


def job():
    return _get("snipe_bt_job", {})


def _set_job(**kw):
    j = job()
    j.update(kw)
    _put("snipe_bt_job", j)
    return j


def runs():
    return _get("snipe_bt_runs", [])


def cache():
    return _get("snipe_bt_cache", {"points": {}, "coins": {}})


# ---------------------------------------------------------------- Helius
def _budget_ok():
    now = time.time()
    _CALLS[:] = [t for t in _CALLS if now - t < 3600]
    return len(_CALLS) < CALLS_PER_HOUR


def history_call(rpc, address, gte=None, lte=None, order="asc", limit=100):
    """Helius getTransactionsForAddress (full transactions, time filter). Returns a list of transactions."""
    while not _budget_ok():
        if _STOP.is_set():
            return []
        time.sleep(20)
    _CALLS.append(time.time())
    flt = {"status": "succeeded"}
    bt = {}
    if gte is not None:
        bt["gte"] = int(gte)
    if lte is not None:
        bt["lte"] = int(lte)
    if bt:
        flt["blockTime"] = bt
    res = rpc("getTransactionsForAddress", [address, {"transactionDetails": "full", "sortOrder": order, "limit": limit,
                                                      "encoding": "json", "maxSupportedTransactionVersion": int(os.getenv("SOLANA_MAX_TX_VERSION", "1")),
                                                      "filters": flt}])
    if isinstance(res, dict):
        return res.get("data") or []
    return res or []


def _keys(tx):
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return keys + list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])


def created_mint(tx):
    """The coin created by a pump.fun create transaction, else None."""
    meta = tx.get("meta") or {}
    logs = " ".join(meta.get("logMessages") or [])
    if "Instruction: Create" not in logs:
        return None
    for b in meta.get("postTokenBalances") or []:
        m = b.get("mint")
        if m and m.endswith("pump"):
            return m
    return None


def trade_of(tx, mint, sol_usd):
    """(time, price_usd, usd_value, side) of the fee payer's swap of `mint`, else None."""
    meta = tx.get("meta") or {}
    keys = _keys(tx)
    if not keys:
        return None
    payer = keys[0]
    pre = sum(int((b.get("uiTokenAmount") or {}).get("amount") or 0) for b in meta.get("preTokenBalances") or []
              if b.get("owner") == payer and b.get("mint") == mint)
    post = sum(int((b.get("uiTokenAmount") or {}).get("amount") or 0) for b in meta.get("postTokenBalances") or []
               if b.get("owner") == payer and b.get("mint") == mint)
    dec = 6
    for b in (meta.get("postTokenBalances") or []) + (meta.get("preTokenBalances") or []):
        if b.get("mint") == mint:
            dec = int((b.get("uiTokenAmount") or {}).get("decimals") or 6)
            break
    tok = (post - pre) / 10 ** dec
    pb, qb = meta.get("preBalances") or [], meta.get("postBalances") or []
    if not tok or not pb or not qb:
        return None
    sol = (qb[0] - pb[0] + int(meta.get("fee") or 0)) / 1e9
    if abs(sol) * sol_usd < MIN_TRADE_USD:
        return None                                   # dust: a tiny amount + account rent gives absurd prices
    if tok > 0 and sol < 0:
        usd = -sol * sol_usd
        return int(tx.get("blockTime") or 0), usd / tok, usd, "buy"
    if tok < 0 and sol > 0:
        usd = sol * sol_usd
        return int(tx.get("blockTime") or 0), usd / -tok, usd, "sell"
    return None


def snapshot(trades, t_end, minutes=5):
    """5-min metrics before t_end from trades [(t, price, usd, side)]."""
    win = [x for x in trades if t_end - minutes * 60 <= x[0] <= t_end]
    if not win:
        return None
    win.sort(key=lambda x: x[0])
    price = win[-1][1]
    first = win[0][1]
    return {"price": price, "mcap": price * SUPPLY, "vol5": sum(x[2] for x in win),
            "buys": sum(1 for x in win if x[3] == "buy"), "sells": sum(1 for x in win if x[3] == "sell"),
            "up5": (price / first - 1) * 100 if first else 0.0}


def download_coin(rpc, mint, created, sol_usd):
    """Snapshots at each decision age + the trades after each possible entry (compact, cached)."""
    out = {"created": created, "snaps": {}, "after": {}}
    for age in DECISION_MIN:
        if _STOP.is_set():
            break
        t = created + age * 60
        txs = history_call(rpc, mint, gte=t - 300, lte=t, order="desc", limit=100)
        trades = [x for x in (trade_of(tx, mint, sol_usd) for tx in txs) if x]
        snap = snapshot(trades, t)
        out["snaps"][str(age)] = snap
        if snap is None:
            break                                    # no trades any more: dead coin, no later entries
        if not any(not sb.metrics_reason(snap["mcap"], snap["vol5"], snap["buys"], snap["sells"], snap["up5"],
                                         mv, mr, mu, mx) for mv, mr, mu, mx in GRID_ENTRY):
            continue
        # some grid setting would enter here: read the path after the entry (up to ~3 pages)
        path, start = [], t + DELAY_S
        for _ in range(3):
            txs = history_call(rpc, mint, gte=start, lte=t + 60 * 60, order="asc", limit=100)
            got = [x for x in (trade_of(tx, mint, sol_usd) for tx in txs) if x]
            path += [[x[0], round(x[1], 12)] for x in got]
            if len(txs) < 100:
                break
            start = int(txs[-1].get("blockTime") or start) + 1
        out["after"][str(age)] = path
    return out


def sample_points(start, end):
    """Moments on a FIXED grid (multiples of the step), so a later run reuses what earlier runs downloaded."""
    step = max(3600, int(DAYS * 86400 // SAMPLE_POINTS))
    first = (int(start) // step + 1) * step
    return list(range(first, int(end), step))


def download(rpc, sol_usd, now=None, start=None, end=None, should_stop=None, on_progress=None, max_s=None):
    """Fill the cache: launches at the sample points, then each coin. Checkpoint after every point / coin."""
    now = now or time.time()
    end = end or now - 2 * 3600
    start = start or end - DAYS * 86400
    max_s = JOB_MAX_S if max_s is None else max_s
    began = time.time()
    c = cache()
    pts = sample_points(start, end)
    todo_pts = [p for p in pts if str(p) not in c["points"]]
    total = len(pts)
    for i, p in enumerate(todo_pts):
        if (should_stop and should_stop()) or time.time() - began > max_s:
            break
        try:
            txs = history_call(rpc, PUMP, gte=p, lte=p + 600, order="asc", limit=100)
        except Exception as exc:
            logging.warning("snipe bt point %s: %s: %s", p, type(exc).__name__, str(exc)[:200])
            continue
        mints = []
        for tx in txs:
            m = created_mint(tx)
            if m:
                mints.append([m, int(tx.get("blockTime") or p)])
        c["points"][str(p)] = mints[:3]
        for m, created in mints[:3]:
            if m in c["coins"] or (should_stop and should_stop()):
                continue
            try:
                c["coins"][m] = download_coin(rpc, m, created, sol_usd)
            except Exception as exc:
                logging.warning("snipe bt coin %s: %s: %s", m[:8], type(exc).__name__, str(exc)[:200])
        _put("snipe_bt_cache", c)
        if on_progress:
            on_progress(total - len(todo_pts) + i + 1, total, len(c["coins"]))
    return c


# ---------------------------------------------------------------- simulation
def simulate(c, start, end, entry, exit_, usd=None, max_open=None, daily_loss=None):
    usd = float(usd or sb.USD)
    max_open = int(max_open or sb.MAX_OPEN)
    daily_loss = sb.DAILY_LOSS if daily_loss is None else daily_loss
    mv, mr, mu, mx = entry
    stop, arm, drop, hold = exit_
    cands = []
    for m, coin in c["coins"].items():
        if not start <= coin["created"] < end:
            continue
        for age in DECISION_MIN:
            snap = coin["snaps"].get(str(age))
            if snap is None:
                break
            if sb.metrics_reason(snap["mcap"], snap["vol5"], snap["buys"], snap["sells"], snap["up5"], mv, mr, mu, mx):
                continue
            t0 = coin["created"] + age * 60
            path = coin["after"].get(str(age)) or []
            if not path:
                res, t_exit, why = -1.0, t0 + DELAY_S, "sem negociação depois (rug / sem liquidez)"
            else:
                entry_px = path[0][1]
                peak, res, t_exit, why = 1.0, None, path[-1][0], "fim dos dados (vendeu no último preço)"
                for t, px in path:
                    ratio = px / entry_px
                    peak = max(peak, ratio)
                    act, reason = sb.exit_signal(ratio, peak, t - path[0][0], stop, arm, drop, hold)
                    if act == "sell":
                        res, t_exit, why = ratio - 1, t, reason
                        break
                if res is None:
                    res = path[-1][1] / entry_px - 1
            cands.append((t0, t_exit, m, res, why))
            break                                    # one entry per coin
    cands.sort()
    pnl_day, open_until, trades, rejected = {}, [], [], {}
    for t0, t1, m, res, why in cands:
        open_until = [x for x in open_until if x > t0]
        day = time.strftime("%Y-%m-%d", time.gmtime(t0))
        if len(open_until) >= max_open:
            rejected["já tinha " + str(max_open) + " abertas"] = rejected.get("já tinha " + str(max_open) + " abertas", 0) + 1
            continue
        if daily_loss and pnl_day.get(day, 0) <= -daily_loss:
            rejected["limite de perda do dia"] = rejected.get("limite de perda do dia", 0) + 1
            continue
        pnl = usd * ((1 + res) * (1 - COST) * (1 - COST) - 1) - 2 * NET_FEE
        pnl_day[day] = pnl_day.get(day, 0) + pnl
        open_until.append(t1)
        trades.append({"mint": m, "opened": t0, "closed": t1, "result_pct": round(res * 100, 2), "pnl_usd": round(pnl, 4),
                       "exit": why})
    return _summary(trades, start, end), trades, rejected


def _summary(trades, start, end):
    eq, peak, dd = CAPITAL, CAPITAL, 0.0
    for tr in sorted(trades, key=lambda x: x["closed"]):
        eq += tr["pnl_usd"]
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak > 0 else 0)
    pnl = [x["pnl_usd"] for x in trades]
    gw, gl = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x < 0)
    return {"final": round(CAPITAL + sum(pnl), 2), "pnl": round(sum(pnl), 2), "trades": len(trades),
            "per_day": round(len(trades) / max(1.0, (end - start) / 86400), 2),
            "win": round(sum(1 for x in pnl if x > 0) / len(pnl), 3) if pnl else 0.0,
            "pf": round(gw / gl, 2) if gl > 0 else None, "max_dd": round(dd, 3), "worst": round(min(pnl, default=0), 2)}


def report(c, start, end):
    cut = start + (end - start) * 0.7
    grid = []
    for en in GRID_ENTRY:
        for ex in GRID_EXIT:
            tr, _, _ = simulate(c, start, cut, en, ex)
            grid.append((tr, en, ex))
    ok = [g for g in grid if g[0]["trades"] >= 10]
    best = max(ok, key=lambda g: g[0]["pnl"]) if ok else None
    live = ((sb.MIN_VOL_5M, sb.MIN_BUY_RATIO, sb.MIN_UP_5M, sb.MAX_UP_5M), (sb.STOP, sb.ARM, sb.DROP, sb.MAX_HOLD_MIN))
    out = {"start": start, "end": end, "cut": cut, "coins": sum(1 for x in c["coins"].values() if start <= x["created"] < end),
           "points": sum(1 for p in c["points"] if start <= int(p) < end), "total_points": len(sample_points(start, end)),
           "best": None, "live": {}}
    lt = simulate(c, cut, end, *live)
    lf = simulate(c, start, end, *live)
    out["live"] = {"test": lt[0], "full": lf[0], "rejected": lf[2], "exits": _exits(lf[1])}
    if best:
        bt = simulate(c, cut, end, best[1], best[2])
        out["best"] = {"entry": best[1], "exit": best[2], "train": best[0], "test": bt[0]}
    out["trades"] = lf[1]
    return out


def _exits(trades):
    r = {}
    for t in trades:
        r[t["exit"]] = r.get(t["exit"], 0) + 1
    return r


def _fmt(s):
    return ("US$ " + format(CAPITAL, "g") + " → " + format(s["final"], ",.2f") + " (" + format(s["pnl"], "+.2f") + ") · " +
            str(s["trades"]) + " ops (" + format(s["per_day"], ".1f") + "/dia) · acerto " + format(s["win"] * 100, ".0f") +
            "% · PF " + ("—" if s["pf"] is None else format(s["pf"], ".2f")) + " · DD " + format(s["max_dd"] * 100, ".0f") +
            "% · pior " + format(s["worst"], "+.2f"))


def text(rep):
    L = ["🎯 BACKTEST SNIPER — " + time.strftime("%d/%m/%Y", time.gmtime(rep["start"])) + " a " +
         time.strftime("%d/%m/%Y", time.gmtime(rep["end"])),
         "Amostra REAL da blockchain: " + str(rep["coins"]) + " lançamentos da pump.fun em " + str(rep["points"]) + " de " +
         str(rep.get("total_points", SAMPLE_POINTS)) + " momentos · US$ " + format(sb.USD, "g") + " por compra · máx. " + str(sb.MAX_OPEN) +
         " abertas · custos " + format(COST * 100, ".0f") + "% por lado + taxa · entrada " + format(DELAY_S, ".0f") + " s depois",
         "▶ Regra que está no robô real:",
         "  • TESTE (últimos 30%, fora da amostra): " + _fmt(rep["live"]["test"]),
         "  • Período todo: " + _fmt(rep["live"]["full"])]
    if rep["live"]["exits"]:
        L.append("  • Saídas: " + " · ".join(k + ": " + str(v) for k, v in rep["live"]["exits"].items()))
    if rep["live"]["rejected"]:
        L.append("  • Rejeitadas: " + " · ".join(k + ": " + str(v) for k, v in rep["live"]["rejected"].items()))
    b = rep.get("best")
    if b:
        en, ex = b["entry"], b["exit"]
        L += ["🏆 Melhor ajuste no TREINO: volume 5 min ≥ US$ " + format(en[0], ",.0f") + ", compras/vendas ≥ " + format(en[1], "g") +
              ", subiu " + format(en[2], "g") + "–" + format(en[3], "g") + "% · stop −" + format(ex[0] * 100, ".0f") + "%, stop móvel de +" +
              format(ex[1] * 100, ".0f") + "% (" + format(ex[2] * 100, ".0f") + "% abaixo do topo), " + format(ex[3], "g") + " min",
              "  • treino: " + _fmt(b["train"]), "  • TESTE: " + _fmt(b["test"])]
    else:
        L.append("🏆 Nenhum ajuste teve 10+ operações no treino: amostra pequena — rode de novo para baixar mais lançamentos.")
    left = rep.get("total_points", SAMPLE_POINTS) - rep["points"]
    if left > 0:
        L.append("⏳ Resultado PARCIAL: faltam " + str(left) + " momentos da amostra. Toque de novo: cada toque baixa mais "
                 "~3 min e o resultado fica mais confiável.")
    L.append("⚠️ Amostra de lançamentos (não todos); preço do SOL constante; falhas de transação não simuladas. "
             "Moeda sem negociação depois da entrada conta como perda total. Passado não garante futuro.")
    return "\n".join(L)


def cancel():
    _STOP.set()
    return True


def mark_interrupted():
    if job().get("status") == "rodando":
        _set_job(status="interrompido", stage="o servidor reiniciou — toque de novo para continuar de onde parou")


def start(rpc, sol_usd, send=None, chat=None, now=None):
    if not _RUNNING.acquire(blocking=False):
        return "já tem um backtest do sniper rodando"
    if rpc is None:
        _RUNNING.release()
        return "precisa do SOLANA_RPC_URL (Helius) no Render"
    _STOP.clear()
    now = now or time.time()
    end = now - 2 * 3600
    begin = end - DAYS * 86400
    _set_job(status="rodando", progress=0, stage="baixando lançamentos", started=now)

    def work():
        try:
            def prog(done, total, coins):
                _set_job(progress=int(95 * done / max(total, 1)),
                         stage="momentos " + str(done) + "/" + str(total) + " · " + str(coins) + " moedas baixadas")
            c = download(rpc, sol_usd, now, begin, end, _STOP.is_set, prog)
            rep = report(c, begin, end)
            txt = text(rep)
            _put("snipe_bt_runs", runs()[-4:] + [{"id": str(int(now)), "created": time.time(), "text": txt}])
            _set_job(status="cancelado" if _STOP.is_set() else "pronto", progress=100 if not _STOP.is_set() else job().get("progress"),
                     stage="pronto", finished=time.time())
            sb.note("backtest: " + txt.split("\n")[3][:150])
            if send and chat:
                send(chat, txt)
        except Exception as exc:
            logging.exception("snipe backtest failed")
            _set_job(status="erro", stage=type(exc).__name__ + ": " + str(exc)[:120])
            if send and chat:
                send(chat, "⛔ Backtest do sniper falhou: " + type(exc).__name__ + ": " + str(exc)[:120])
        finally:
            _RUNNING.release()

    threading.Thread(target=work, daemon=True).start()
    return ""
