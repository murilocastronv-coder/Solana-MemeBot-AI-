"""GRADUATION backtest (owner: "do what gives the best result", Nov 16), 3 months, REAL on-chain trades (Helius).

Idea: buy a pump.fun coin right AFTER it completes the bonding curve and migrates to PumpSwap - it already proved
demand, and from then on it trades in a pool with real liquidity (lower slippage than on the curve).
How the past is rebuilt:
- launches: at POINTS moments over 3 months, up to 100 pump.fun launches each (pump.fun mint authority history);
- graduated? the 'complete' flag of each coin's bonding curve, 100 coins per cheap read (getMultipleAccounts);
- the graduation moment (first PumpSwap trade) is found by a time search (a few small reads);
- prices after graduation: the first trades (dense) + one price every 30 min for 4 h;
- entry DELAY after graduation (30 s / 2 min / 10 min), exits: stop / take profit / trailing stop / time, the stop is
  checked first (pessimistic); costs COST per side + network fee; US$ per trade, max open, daily loss.
The combination is chosen on the first 70% of the time and reported on the last 30% (out of sample).
Honest limits: only the sampled launches (graduations are ~1-3% of them); prices are trade prices; trades routed with
pre-wrapped SOL may be missed; failed transactions not modelled. Nothing is invented: no data = no trade.
"""
import base64
import logging
import threading
import time

import snipe_backtest as sbt
import snipe_bot as sb

PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
GRAD_WINDOW_H = 24
DELAYS = (30, 120, 600)
EXITS = (("sl20 tp50", 0.20, 0.50, 0, 0, 240), ("sl15 tp30 1h", 0.15, 0.30, 0, 0, 60), ("sl25 trail+50/20%", 0.25, 0, 0.50, 0.20, 240),
         ("sl30 trail+100/25%", 0.30, 0, 1.00, 0.25, 240), ("sl10 tp20 30min", 0.10, 0.20, 0, 0, 30))
COST = sbt.sg._f("GRAD_BT_COST_PER_SIDE", 0.02)
USD, MAX_OPEN, DAILY_LOSS, CAPITAL = 10.0, 2, 20.0, 500.0
MAX_S = 180
DAYS = 90
POINTS = int(sbt.sg._f("GRAD_BT_POINTS", 180))
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def curve_of(mint):
    from solders.pubkey import Pubkey
    return str(Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))],
                                           Pubkey.from_string(PUMP_PROGRAM))[0])


def completed(rpc, mints):
    """{mint: True/False} - pump.fun bonding curve 'complete' flag (byte 48), 100 per read."""
    out = {}
    for i in range(0, len(mints), 100):
        batch = mints[i:i + 100]
        res = rpc("getMultipleAccounts", [[curve_of(m) for m in batch], {"encoding": "base64"}]) or {}
        for m, acc in zip(batch, (res.get("value") if isinstance(res, dict) else res) or []):
            try:
                raw = base64.b64decode((acc or {}).get("data", [""])[0])
                out[m] = len(raw) > 48 and raw[48] == 1
            except Exception:
                out[m] = False
    return out
_RUNNING = threading.Lock()


def _is_swap(tx):
    return PUMPSWAP in sbt._keys(tx)


def _price(rpc, mint, t, sol_usd, window=900):
    txs = sbt.history_call(rpc, mint, gte=int(t), lte=int(t) + window, order="asc", limit=5)
    px = sorted(tr[1] for tr in (sbt.trade_of(tx, mint, sol_usd) for tx in txs) if tr)
    return px[len(px) // 2] if px else None


def graduation(rpc, mint, created, sol_usd):
    """Graduation time (first PumpSwap trade) or None."""
    end = created + GRAD_WINDOW_H * 3600
    txs = sbt.history_call(rpc, mint, gte=int(created), lte=int(end), order="desc", limit=5)
    if not any(_is_swap(tx) for tx in txs):
        return None
    lo, hi = created, max(int(tx.get("blockTime") or 0) for tx in txs if _is_swap(tx))
    for _ in range(8):                       # time search for the first PumpSwap trade
        if hi - lo < 60:
            break
        mid = (lo + hi) // 2
        page = sbt.history_call(rpc, mint, gte=int(lo), lte=int(mid), order="desc", limit=3)
        if any(_is_swap(tx) for tx in page):
            hi = min(int(tx.get("blockTime") or hi) for tx in page if _is_swap(tx))
        else:
            lo = mid
    return hi


def fetch(rpc, mint, created, sol_usd):
    g = graduation(rpc, mint, created, sol_usd)
    if not g:
        return {"grad": None}
    txs = sbt.history_call(rpc, mint, gte=int(g), lte=int(g) + 1200, order="asc", limit=100)
    dense = sorted([[tr[0], tr[1]] for tr in (sbt.trade_of(tx, mint, sol_usd) for tx in txs) if tr])
    sparse = []
    for k in range(1, 9):                          # one price every 30 min for 4 h
        px = _price(rpc, mint, g + k * 1800, sol_usd)
        if px:
            sparse.append([int(g + k * 1800), px])
    return {"grad": g, "path": dense + [p for p in sparse if not dense or p[0] > dense[-1][0]]}


def trade(e, delay, ex):
    name, sl, tp, arm, drop, hold = ex
    path = [p for p in e["path"] if p[0] >= e["grad"] + delay]
    if not path:
        return None
    t0, entry = path[0]
    peak = 1.0
    for t, px in path[1:]:
        r = px / entry
        peak = max(peak, r)
        if r <= 1 - sl:
            return r * (1 - COST) ** 2 - 1, t, "stop"
        if tp and r >= 1 + tp:
            return (1 + tp) * (1 - COST) ** 2 - 1, t, "alvo"
        if arm and peak >= 1 + arm and r <= peak * (1 - drop):
            return r * (1 - COST) ** 2 - 1, t, "stop móvel"
        if t - t0 >= hold * 60:
            return r * (1 - COST) ** 2 - 1, t, "tempo"
    return path[-1][1] / entry * (1 - COST) ** 2 - 1, path[-1][0], "fim dos dados"


def simulate(rows, start, end, delay, ex):
    open_until, day_pnl, pnl, exits = [], {}, [], {}
    for g, e in sorted(((e["grad"], e) for e in rows if start <= e["grad"] < end), key=lambda x: x[0]):
        r = trade(e, delay, ex)
        if r is None:
            continue
        open_until = [x for x in open_until if x > g]
        day = time.strftime("%Y-%m-%d", time.gmtime(g))
        if len(open_until) >= MAX_OPEN or day_pnl.get(day, 0) <= -DAILY_LOSS:
            continue
        x = USD * r[0] - 0.04
        open_until.append(r[1])
        day_pnl[day] = day_pnl.get(day, 0) + x
        pnl.append(x)
        exits[r[2]] = exits.get(r[2], 0) + 1
    gw, gl = sum(v for v in pnl if v > 0), -sum(v for v in pnl if v < 0)
    eq, peak, dd = CAPITAL, CAPITAL, 0.0
    for v in pnl:
        eq += v
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak)
    return {"n": len(pnl), "pnl": sum(pnl), "win": sum(1 for v in pnl if v > 0) / len(pnl) if pnl else 0.0,
            "pf": gw / gl if gl > 0 else None, "dd": dd, "exits": exits}


def _fmt(s):
    return (str(s["n"]) + " ops · acerto " + format(s["win"] * 100, ".0f") + "% · " + format(s["pnl"], "+.2f") + " USD · PF " +
            ("—" if s["pf"] is None else format(s["pf"], ".2f")) + " · queda máx " + format(s["dd"] * 100, ".0f") + "%")


def report(g, now, total_points=POINTS):
    rows = [dict(e, mint=m) for m, e in g["coins"].items() if e.get("grad") and e.get("path")]
    grads = sum(1 for ok in g["complete"].values() if ok)
    if not rows:
        start = end = now
    else:
        start, end = min(e["grad"] for e in rows), max(e["grad"] for e in rows) + 1
    cut = start + (end - start) * 0.7
    L = ["🎓 BACKTEST: COMPRAR MOEDA QUE GRADUOU NA PUMP.FUN (dados REAIS, 3 meses)",
         "Lançamentos da amostra: " + str(len(g["launch"])) + " em " + str(len(g["points"])) + " de " + str(total_points) +
         " momentos · verificados: " + str(len(g["complete"])) + " · graduaram: " + str(grads) + " (com preço depois: " +
         str(len(rows)) + ") · US$ " + format(USD, "g") + " por compra · máx. " + str(MAX_OPEN) + " abertas · custos " +
         format(COST * 100, ".0f") + "% por lado"]
    best = None
    for d in DELAYS:
        for ex in EXITS:
            tr = simulate(rows, start, cut, d, ex)
            if tr["n"] >= 10 and (best is None or tr["pnl"] > best[0]["pnl"]):
                best = (tr, d, ex)
    for d in DELAYS:
        full = [simulate(rows, start, end, d, ex) for ex in EXITS]
        top = max(zip(full, EXITS), key=lambda z: z[0]["pnl"]) if full else None
        if top and top[0]["n"]:
            L.append("• Entrando " + (str(d) + " s" if d < 120 else str(d // 60) + " min") + " depois da graduação — melhor saída (" +
                     top[1][0] + ") no período todo: " + _fmt(top[0]))
    ok = False
    if best:
        te = simulate(rows, cut, end, best[1], best[2])
        L += ["🏆 Escolhido no TREINO: entrar " + str(best[1]) + " s depois, saída " + best[2][0] + " → treino " + _fmt(best[0]),
              "  • TESTE (últimos 30%, fora da amostra): " + _fmt(te)]
        ok = te["n"] >= 30 and te["pnl"] > 0 and (te["pf"] is None or te["pf"] >= 1.3)
    else:
        L.append("🏆 Ainda sem combinação com 10+ operações no treino.")
    left = (total_points - len(g["points"])) + (len(g["launch"]) - len(g["complete"])) + (grads - len(g["coins"]))
    if left > 0:
        L.append("⏳ PARCIAL: ainda falta baixar parte da amostra — toque de novo (continua de onde parou).")
    L.append("✅ Deu lucro confiável fora da amostra." if ok else
             "❌ Ainda sem lucro confiável fora da amostra (precisa ≥ 30 ops no teste, lucro e PF ≥ 1,3).")
    L.append("⚠️ Só as moedas da amostra (graduação é rara: ~1–3% dos lançamentos); preço lido a cada 30 min "
             "depois dos primeiros minutos; falhas de transação não simuladas. Passado não garante futuro.")
    return "\n".join(L), ok


def start(rpc, sol_usd, send=None, chat=None):
    if rpc is None:
        return "precisa do SOLANA_RPC_URL (Helius) no Render"
    if not _RUNNING.acquire(blocking=False):
        return "o teste já está rodando"

    def work():
        try:
            now = time.time()
            g = sbt._get("grad_bt_cache", {"points": {}, "launch": {}, "complete": {}, "coins": {}})
            began = time.time()
            sbt.DEADLINE[0] = began + MAX_S
            end = now - 2 * 86400
            step = DAYS * 86400 / POINTS
            pts = [int(end - DAYS * 86400 + step * (i + 0.5)) // 3600 * 3600 for i in range(POINTS)]
            try:
                # 1) launches (1 read per moment, up to 100 launches)
                for p in pts:
                    if str(p) in g["points"] or time.time() - began > MAX_S * 0.4:
                        continue
                    txs = sbt.history_call(rpc, sb.PUMP_MINT_AUTHORITY, gte=p, lte=p + 3600, order="asc", limit=100)
                    ms = []
                    for tx in txs:
                        m = sbt.created_mint(tx)
                        if m:
                            g["launch"][m] = int(tx.get("blockTime") or p)
                            ms.append(m)
                    g["points"][str(p)] = len(ms)
                # 2) which ones graduated (100 per cheap read)
                todo = [m for m in g["launch"] if m not in g["complete"]]
                for i in range(0, len(todo), 100):
                    if time.time() - began > MAX_S * 0.6:
                        break
                    g["complete"].update(completed(rpc, todo[i:i + 100]))
                sbt._put("grad_bt_cache", g)
                # 3) graduation moment + prices after it, only for the graduated coins
                for m in [m for m, ok in g["complete"].items() if ok and m not in g["coins"]]:
                    if time.time() - began > MAX_S:
                        break
                    try:
                        g["coins"][m] = fetch(rpc, m, g["launch"][m], sol_usd)
                    except TimeoutError:
                        break
                    except Exception as exc:
                        logging.warning("grad bt %s: %s: %s", m[:8], type(exc).__name__, str(exc)[:120])
            except TimeoutError:
                pass
            sbt.DEADLINE[0] = 0.0
            sbt._put("grad_bt_cache", g)
            txt, ok = report(g, now, len(pts))
            if send and chat:
                send(chat, txt)
        except Exception as exc:
            logging.exception("grad backtest failed")
            if send and chat:
                send(chat, "⛔ Backtest de graduação falhou: " + type(exc).__name__ + ": " + str(exc)[:150])
        finally:
            sbt.DEADLINE[0] = 0.0
            _RUNNING.release()

    threading.Thread(target=work, daemon=True).start()
    return ""
