"""Launch-copy test (owner, Nov 16): would buying a good dev's NEW coin right after the launch make money?

For the devs with a good record (hit rate >= 70%, >= 20 trades, approved or dropped only for an empty wallet),
take their own launches that the bot already knows (launch mints + the dev's real buy / sell times), and replay
them with REAL on-chain trades (Helius getTransactionsForAddress):
- entry = price of the first trade at least DELAY seconds after the dev's buy, for DELAYS (2 s, 10 s, 30 s);
- exit = the same share the dev sold, at the first trade by SOMEONE ELSE at least DELAY seconds after each dev sale
  (the bot reacts with the same delay; it never gets the dev's own price);
  a coin with no trade after the dev's sale = total loss;
- costs: SNIPE_BT_COST per side + network fee; US$ per trade = the sniper's.
Honest limits: a 2-second entry assumes the bot sees the launch in ~1 s (WebSocket); today the bot checks every
30 s. Prices are trade prices (the curve can move inside the same second).
"""
import logging
import threading
import time

import dev_bot as db
import snipe_backtest as sbt

DELAYS = (2, 10, 30)
MAX_LAUNCHES = int(db.E("LAUNCH_TEST_MAX", 150))
MAX_S = db.E("LAUNCH_TEST_MINUTES", 8) * 60
_RUNNING = threading.Lock()


def candidates(d=None):
    """[(dev, mint, dev_buy_time, sells[[t, fraction]])] of good devs' own launches, newest first."""
    d = d or db._load()
    out = []
    for dev, v in d["devs"].items():
        if v.get("win", 0) < 0.7 or v.get("trades", 0) < 20 or v.get("linked_from"):
            continue
        if v.get("tier") not in db.TIER_OK and not str(v.get("why") or "").startswith("carteira sem fundo"):
            continue
        own = set(v.get("launch_mints") or []) | set(v.get("tokens_seen") or [])
        for h in v.get("hist") or []:
            if h.get("mint") in own and h.get("opened") and h.get("closed"):
                sells = h.get("sells") or [[h["closed"], 1.0]]
                out.append((dev, h["mint"], float(h["opened"]), sells))
    out.sort(key=lambda x: -x[2])
    return out[:MAX_LAUNCHES]


def _trades(rpc, mint, gte, until, sol_usd, pages=5):
    """Trades [(t, price, payer)] from gte, paging until `until` (or `pages` pages)."""
    out, start = [], int(gte)
    for _ in range(pages):
        txs = sbt.history_call(rpc, mint, gte=start, lte=int(until) + 3600, order="asc", limit=100)
        for tx in txs:
            tr = sbt.trade_of(tx, mint, sol_usd)
            keys = sbt._keys(tx)
            if tr:
                out.append((tr[0], tr[1], keys[0] if keys else ""))
        if len(txs) < 100 or not out or out[-1][0] > until:
            break
        start = out[-1][0]
    return sorted(set(out), key=lambda x: x[0])


def replay(rpc, mint, opened, sells, sol_usd, dev=""):
    """{delay: return} for one launch (None when there is no price at the entry). The bot reacts with the SAME delay
    to the dev's sales and sells at the first trade by someone else after that (never at the dev's own price)."""
    first = _trades(rpc, mint, int(opened) - 2, opened + max(DELAYS) + 5, sol_usd)
    sale_trades = [_trades(rpc, mint, int(t), int(t) + max(DELAYS) + 5, sol_usd, pages=2) for t, _ in sells[:4]]
    out = {}
    for delay in DELAYS:
        entry = next((px for t, px, who in first if t >= opened + delay and who != dev), None)
        if not entry:
            out[delay] = None
            continue
        left, got = 1.0, 0.0
        for i, ((t_sale, frac), trades) in enumerate(zip(sells[:4], sale_trades)):
            px = next((p for t, p, who in trades if t >= t_sale + delay and who != dev), None)
            part = left if i == len(sale_trades) - 1 else left * min(float(frac), 1.0)
            got += part * ((px or 0.0) / entry)        # no trade after the dev's sale = nothing back (rug)
            left -= part
        out[delay] = got * (1 - sbt.COST) ** 2 - 1
    return out


def run(rpc, sol_usd, should_stop=None):
    began = time.time()
    rows = []
    for dev, mint, opened, sells in candidates():
        if time.time() - began > MAX_S or (should_stop and should_stop()):
            break
        try:
            rows.append((dev, mint, replay(rpc, mint, opened, sells, sol_usd, dev)))
        except Exception as exc:
            logging.warning("launch test %s: %s: %s", mint[:8], type(exc).__name__, str(exc)[:120])
    return rows


def text(rows, total):
    import snipe_bot as sb
    usd = sb.USD
    L = ["🚀 TESTE: COMPRAR NO LANÇAMENTO DOS DEVs BONS (dados reais da blockchain)",
         "Lançamentos testados: " + str(len(rows)) + " de " + str(total) + " conhecidos · US$ " + format(usd, "g") +
         " por compra · vende junto com o DEV · custos " + format(sbt.COST * 100, ".0f") + "% por lado"]
    best = None
    for delay in DELAYS:
        rs = [r[2][delay] for r in rows if r[2].get(delay) is not None]
        if not rs:
            L.append("• Entrando " + str(delay) + " s depois: sem dados")
            continue
        pnl = [usd * r - 2 * sbt.NET_FEE for r in rs]
        gw, gl = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x < 0)
        wins = sum(1 for x in pnl if x > 0)
        L.append("• Entrando " + str(delay) + " s depois: " + str(len(rs)) + " ops · acerto " + format(wins / len(rs) * 100, ".0f") +
                 "% · resultado " + format(sum(pnl), "+.2f") + " USD · média " + format(sum(rs) / len(rs) * 100, "+.1f") +
                 "% por operação · PF " + ("—" if gl == 0 else format(gw / gl, ".2f")) + " · pior " + format(min(pnl), "+.2f"))
        if best is None or sum(pnl) > best[1]:
            best = (delay, sum(pnl), len(rs))
    if best and best[1] > 0 and best[2] >= 20:
        L.append("✅ Deu lucro entrando " + str(best[0]) + " s depois em " + str(best[2]) + " lançamentos. Para entrar tão rápido "
                 "o robô precisa de conexão em tempo real (WebSocket) — dá para fazer.")
    elif best and best[1] > 0:
        L.append("⚠️ Positivo, mas com menos de 20 lançamentos: amostra pequena, não prova vantagem.")
    else:
        L.append("❌ Não deu lucro para quem compra depois do DEV: quem ganha é o DEV (e quem chega no mesmo bloco).")
    L.append("⚠️ Hoje o robô confere os DEVs a cada 30 s; os números de 2 s e 10 s só valem com conexão em tempo real.")
    return "\n".join(L)


def start(rpc, sol_usd, send=None, chat=None):
    if rpc is None:
        return "precisa do SOLANA_RPC_URL (Helius) no Render"
    if not _RUNNING.acquire(blocking=False):
        return "o teste já está rodando"
    total = len(candidates())
    if not total:
        _RUNNING.release()
        return "ainda não há lançamentos conhecidos de DEVs bons (o robô precisa analisar mais DEVs)"

    def work():
        try:
            rows = run(rpc, sol_usd)
            txt = text(rows, total)
            db.note("teste de lançamento: " + txt.split("\n")[2][:150] if len(txt.split("\n")) > 2 else txt[:150])
            if send and chat:
                send(chat, txt)
        except Exception as exc:
            logging.exception("launch test failed")
            if send and chat:
                send(chat, "⛔ Teste de lançamento falhou: " + type(exc).__name__ + ": " + str(exc)[:150])
        finally:
            _RUNNING.release()

    threading.Thread(target=work, daemon=True).start()
    return ""
