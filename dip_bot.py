"""Dip Buying Bot: buys strong pump.fun coins right after a sharp drop, once they stop falling.

Real money in the owner's wallet (same wallet as the copy trade), separate from the signals bot.
- Coins: pump.fun (curve or PumpSwap), liquidity US$ 10k-300k, SOL/USDC pair, 10+ minutes old,
  strong (up 20%+ in 24 h or 10%+ in 6 h), 30+ buys in the last hour, US$ 10k+ volume.
- Dip: down 15-45% in the last hour, the last 5 minutes green with more buys than sells
  (the fall stopped), and the hour not dominated by sellers.
- Anti-scam checks of the signals bot (holders, freeze/mint authority, token program, RugCheck).
- US$ 10 per buy, at most 5 dip positions open, one try per coin every 6 hours.
- Exit (live_exit_monitor, quotes every 20 s): stop -20%; from +20% trailing stop 8% below the top;
  after 6 hours without reaching +20% it sells.
Data comes from DexScreener only (GeckoTerminal and pump.fun block the server).
"""
import json
import logging
import os
import tempfile
import threading
import time

import signals as sg

ORDER_USD = sg._f("DIP_ORDER_USD", 10)
MAX_OPEN = int(sg._f("DIP_MAX_OPEN", 5))
RESERVE_USD = sg._f("DIP_RESERVE_USD", 1)
MIN_LIQ = sg._f("DIP_MIN_LIQUIDITY_USD", 10_000)
MAX_LIQ = sg._f("DIP_MAX_LIQUIDITY_USD", 300_000)
MIN_AGE_S = sg._f("DIP_MIN_AGE_MINUTES", 10) * 60
MIN_UP_24H = sg._f("DIP_MIN_UP_24H", 20)
MIN_UP_6H = sg._f("DIP_MIN_UP_6H", 10)
MIN_BUYS_1H = int(sg._f("DIP_MIN_BUYS_1H", 30))
MIN_VOL_24H = sg._f("DIP_MIN_VOLUME_24H", 10_000)
DIP_MIN = sg._f("DIP_MIN_DROP_1H", 15)          # % down in the last hour
DIP_MAX = sg._f("DIP_MAX_DROP_1H", 45)
MAX_SELL_SHARE_1H = sg._f("DIP_MAX_SELL_SHARE_1H", 0.62)
COOLDOWN_S = sg._f("DIP_COOLDOWN_HOURS", 6) * 3600
SCAN_EVERY_S = sg._f("DIP_SCAN_SECONDS", 60)
STOP_LOSS = sg._f("DIP_STOP_LOSS", 0.20)
TRAIL_ARM = sg._f("DIP_TRAIL_ARM", 0.20)
TRAIL_DROP = sg._f("DIP_TRAIL_DROP", 0.08)
MAX_HOLD_S = sg._f("DIP_MAX_HOLD_HOURS", 6) * 3600
EXIT = {"stop_loss": str(STOP_LOSS), "arm": str(1 + TRAIL_ARM), "drop": str(TRAIL_DROP)}

_LOCK = threading.RLock()
LAST = {"at": 0, "coins": 0, "dips": 0, "bought": 0, "why": {}}


def _path():
    import live_trade_state
    return live_trade_state._path().with_name("dip_bot.json")


def _load():
    d = {"enabled": True, "tried": {}, "buys": 0}
    p = _path()
    if p.exists():
        try:
            d.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return d


def _save(d):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".dip.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def set_enabled(on):
    with _LOCK:
        d = _load()
        d["enabled"] = bool(on)
        _save(d)


def enabled():
    return bool(_load()["enabled"])


def dip_reason(p, now=None):
    """'' when the coin is a strong pump.fun coin in a sharp dip that stopped falling; else why not."""
    now = now or time.time()
    liq = float((p.get("liquidity") or {}).get("usd") or 0)
    if liq < MIN_LIQ:
        return "liquidez baixa"
    if liq > MAX_LIQ:
        return "liquidez alta"
    if ((p.get("quoteToken") or {}).get("address")) not in sg.QUOTES:
        return "par sem SOL/USDC"
    if not sg.is_pump(p):
        return "não é da pump.fun"
    created = p.get("pairCreatedAt")
    if not created or now - float(created) / 1000 < MIN_AGE_S:
        return "moeda nova demais"
    tx = p.get("txns") or {}
    h1, m5 = tx.get("h1") or {}, tx.get("m5") or {}
    b, s = int(h1.get("buys") or 0), int(h1.get("sells") or 0)
    if b < MIN_BUYS_1H:
        return "pouca gente comprando"
    if float((p.get("volume") or {}).get("h24") or 0) < MIN_VOL_24H:
        return "volume baixo"
    ch = p.get("priceChange") or {}
    c5, c1, c6, c24 = (float(ch.get(k) or 0) for k in ("m5", "h1", "h6", "h24"))
    if c24 < MIN_UP_24H and c6 < MIN_UP_6H:
        return "moeda fraca (não está em alta)"
    if c1 > -DIP_MIN:
        return "sem queda forte"
    if c1 < -DIP_MAX:
        return "queda demais (pode ser rug)"
    if s / max(b + s, 1) > MAX_SELL_SHARE_1H:
        return "vendedores dominando"
    if c5 <= 0 or int(m5.get("buys") or 0) <= int(m5.get("sells") or 0):
        return "ainda caindo"
    return ""


def _buy(p, send, chat):
    """Buy US$ ORDER_USD of the coin. '' when bought, else why not."""
    import copy_trader as ct
    import live_engine
    import live_trade_state as st
    mint = p["baseToken"]["address"]
    symbol = (p.get("baseToken") or {}).get("symbol") or mint[:6]
    if not live_engine.ready():
        return "robô real não está pronto: " + str(live_engine.why_stopped() or "")
    for _ in range(15):
        if not st.pending_orders():
            break
        time.sleep(2)
    allowed, why = st.can_open()
    if not allowed:
        return why
    positions = st._load()["positions"]
    if mint in positions:
        return "já tem a moeda"
    if st.positions_by_mode("dip") >= MAX_OPEN:
        return "já tem " + str(MAX_OPEN) + " compras do Dip Bot abertas"
    if len(positions) >= st.MAX_POSITIONS:
        return "limite de posições da carteira"
    free = ct.wallet_usd(live_engine.get_address(), max_age=0)
    if free is None:
        return "saldo indisponível"
    if free < ORDER_USD + RESERVE_USD:
        return "saldo livre US$ " + format(free, ".2f")
    ch = p.get("priceChange") or {}
    if send and chat:
        send(chat, "📉 DIP BOT: " + symbol + " caiu " + format(float(ch.get("h1") or 0), ".0f") + "% na última hora, "
             "mas está " + format(float(ch.get("h24") or 0), "+.0f") + "% em 24 h e voltou a subir nos últimos 5 min "
             "(" + format(float(ch.get("m5") or 0), "+.1f") + "%). Comprando US$ " + format(ORDER_USD, ".0f") + ".")
    settled = live_engine._execute_buy({"baseToken": {"address": mint, "symbol": symbol}}, mint, symbol, send, chat,
                                       live_engine.get_address(), None, "dip", usd=ORDER_USD)
    return "" if settled else "compra não confirmada"


def cycle(send=None, chat=None, pairs=None, now=None, safety=None, buyer=None):
    """One scan: find dips and buy them (at most one buy per scan)."""
    if not enabled():
        return []
    now = now or time.time()
    pairs = sg.candidate_pairs() if pairs is None else pairs
    why, dips = {}, []
    for p in pairs:
        r = dip_reason(p, now)
        if r:
            why[r] = why.get(r, 0) + 1
        else:
            dips.append(p)
    # deepest real dip with the most buyers first
    dips.sort(key=lambda p: int(((p.get("txns") or {}).get("h1") or {}).get("buys") or 0), reverse=True)
    bought = []
    d = _load()
    for p in dips:
        mint = p["baseToken"]["address"]
        if now - float(d["tried"].get(mint, 0)) < COOLDOWN_S:
            why["já tentou essa moeda"] = why.get("já tentou essa moeda", 0) + 1
            continue
        r = (safety or sg.safety_reason)(mint, curve=p.get("dexId") == "pumpfun")
        with _LOCK:
            dd = _load()
            if not (r and "indisponível" in r):
                dd["tried"][mint] = now
            dd["tried"] = {m: t for m, t in dd["tried"].items() if now - t < COOLDOWN_S}
            _save(dd)
        if r:
            key = "anti-golpe: " + r.split(" (")[0]
            why[key] = why.get(key, 0) + 1
            continue
        try:
            r = (buyer or _buy)(p, send, chat)
        except Exception as exc:
            r = type(exc).__name__
        if r:
            why["não comprou: " + r[:40]] = why.get("não comprou: " + r[:40], 0) + 1
            if send and chat and not r.startswith("já tem"):
                send(chat, "📉 Dip Bot não comprou " + str((p.get("baseToken") or {}).get("symbol") or mint[:6]) + ": " + r)
            continue
        bought.append(mint)
        with _LOCK:
            dd = _load()
            dd["buys"] = int(dd.get("buys", 0)) + 1
            _save(dd)
        break
    LAST.update(at=now, coins=len(pairs), dips=len(dips), bought=len(bought), why=why)
    top = ", ".join(k + "=" + str(v) for k, v in sorted(why.items(), key=lambda kv: -kv[1])[:6])
    logging.info("dip scan: %s coins, %s dips, %s bought | %s", len(pairs), len(dips), len(bought), top)
    return bought


def status_text():
    import live_trade_state as st
    d = _load()
    lines = ["📉 DIP BUYING BOT (dinheiro real) — " + ("LIGADO" if d["enabled"] else "DESLIGADO"),
             "Compra moeda forte da pump.fun (+" + format(MIN_UP_24H, ".0f") + "% em 24 h) que caiu " +
             format(DIP_MIN, ".0f") + "–" + format(DIP_MAX, ".0f") + "% na última hora e voltou a subir nos últimos 5 min "
             "(mais compra que venda). Liquidez US$ " + format(MIN_LIQ / 1000, ".0f") + "–" + format(MAX_LIQ / 1000, ".0f") +
             " mil, moeda com 10+ min, anti-golpe.",
             "💵 US$ " + format(ORDER_USD, ".0f") + " por compra · máx. " + str(MAX_OPEN) + " abertas · 🛑 stop -" +
             format(STOP_LOSS * 100, ".0f") + "% · 🚀 a partir de +" + format(TRAIL_ARM * 100, ".0f") +
             "% stop móvel (vende " + format(TRAIL_DROP * 100, ".0f") + "% abaixo do topo) · ⏱️ sai em " +
             format(MAX_HOLD_S / 3600, ".0f") + " h se não subir."]
    try:
        hist = [h for h in st.history() if h.get("mode") == "dip" and h.get("pnl_usd") is not None]
        open_n = st.positions_by_mode("dip")
    except Exception:
        hist, open_n = [], 0
    lines.append("📦 Abertas agora: " + str(open_n) + "/" + str(MAX_OPEN))
    if hist:
        won = sum(1 for h in hist if h["pnl_usd"] > 0)
        pnl = sum(h["pnl_usd"] for h in hist)
        lines.append("📊 Resultado: " + str(len(hist)) + " fechadas · " + str(won) + " no lucro (" +
                     format(won / len(hist) * 100, ".0f") + "%) · total " + format(pnl, "+.2f") + " USD")
    else:
        lines.append("📊 Ainda sem operações fechadas.")
    if LAST["at"]:
        top = ", ".join(k + " " + str(v) for k, v in sorted(LAST["why"].items(), key=lambda kv: -kv[1])[:4])
        lines.append("🔎 Última varredura: " + str(LAST["coins"]) + " moedas, " + str(LAST["dips"]) + " em dip" +
                     (" · " + top if top else ""))
    return "\n".join(lines)
