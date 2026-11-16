"""Launch-copy test (owner, Nov 16): would buying a good dev's NEW coin right after the launch make money?

For the devs with a good record (hit rate >= 80%, >= 20 trades, approved or dropped only for an empty wallet),
take their own launches that the bot already knows (launch mints + the dev's real buy / sell times), and replay
them with REAL on-chain trades (Helius getTransactionsForAddress):
- entry = price of the first trade at least DELAY seconds after the dev's buy, for DELAYS (2 s, 10 s, 30 s);
- exit = the same share the dev sold, at the price of the first trade after each dev sale ("vende junto com o DEV");
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
MAX_LAUNCHES = int(db.E("LAUNCH_TEST_MAX", 200))
MAX_S = db.E("LAUNCH_TEST_MINUTES", 4) * 60
_RUNNING = threading.Lock()


def candidates(d=None):
    """[(dev, mint, dev_buy_time, sells[[t, fraction]])] of good devs' own launches, newest first."""
    d = d or db._load()
    out = []
    for dev, v in d["devs"].items():
        if v.get("win", 0) < 0.8 or v.get("trades", 0) < 20 or v.get("linked_from"):
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


def _first_price(rpc, mint, t, sol_usd, window=900):
    txs = sbt.history_call(rpc, mint, gte=t, lte=t + window, order="asc", limit=20)
    for tx in txs:
        tr = sbt.trade_of(tx, mint, sol_usd)
        if tr and tr[0] >= t:
            return tr[1], tr[0]
    return None, None


def replay(rpc, mint, opened, sells, sol_usd):
    """{delay: return} for one launch (None when there is no price at the entry)."""
    txs = sbt.history_call(rpc, mint, gte=int(opened) - 2, lte=int(opened) + 120, order="asc", limit=100)
    trades = sorted((x for x in (sbt.trade_of(tx, mint, sol_usd) for tx in txs) if x), key=lambda x: x[0])
    exits = []
    for t, frac in sells[:4]:
        px, _ = _first_price(rpc, mint, int(t), sol_usd)
        exits.append((float(frac), px))
    out = {}
    for delay in DELAYS:
        entry = next((x[1] for x in trades if x[0] >= opened + delay), None)
        if not entry:
            out[delay] = None
            continue
        left, got = 1.0, 0.0
        for i, (frac, px) in enumerate(exits):
            part = left if i == len(exits) - 1 else left * min(frac, 1.0)
            got += part * ((px or 0.0) / entry)        # no trade after the dev's sale = nothing back (rug)
            left -= part
        out[delay] = (1 + (got - 1)) * (1 - sbt.COST) ** 2 - 1
    return out


def run(rpc, sol_usd, should_stop=None):
    began = time.time()
    rows = []
    for dev, mint, opened, sells in candidates():
        if time.time() - began > MAX_S or (should_stop and should_stop()):
            break
        try:
            rows.append((dev, mint, replay(rpc, mint, opened, sells, sol_usd)))
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
