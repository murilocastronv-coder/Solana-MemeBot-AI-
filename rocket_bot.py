"""Rocket Sniper: buys new pump.fun coins (10-60 min old) the moment they explode in buys, lets winners run.

Real money in the owner's wallet, separate from the signals bot and the Dip Bot (mode "rocket").
- Coins: pump.fun, liquidity US$ 10k-150k, SOL/USDC pair, 10-60 minutes old.
- Explosion: up 5-60% in the last 5 minutes and green in the hour (not more than +400%), 30+ buys in
  5 min with 2x more buys than sells, 150+ buys in the hour (60%+ buyers), US$ 15k+ volume in the hour.
- Anti-scam checks of the signals bot (holders, freeze/mint authority, token program, RugCheck).
- US$ 10 per buy, at most 5 open, one try per coin every 6 hours.
- Exit (live_exit_monitor, quotes every 20 s): stop -40%; from +30% trailing stop 15% below the top
  (lets a 2x-10x run); after 90 minutes without reaching +30% it sells.
- Pauses itself after 5 losses in a row (no daily loss limit).
Most snipes lose; the result comes from the few that run. Nothing here guarantees profit.
Data comes from DexScreener only (GeckoTerminal and pump.fun block the server).
"""
import json
import logging
import os
import tempfile
import threading
import time

import signals as sg

ORDER_USD = sg._f("ROCKET_ORDER_USD", 10)
MAX_OPEN = int(sg._f("ROCKET_MAX_OPEN", 5))
RESERVE_USD = sg._f("ROCKET_RESERVE_USD", 1)
MIN_LIQ = sg._f("ROCKET_MIN_LIQUIDITY_USD", 10_000)
MAX_LIQ = sg._f("ROCKET_MAX_LIQUIDITY_USD", 150_000)
MIN_AGE_S = sg._f("ROCKET_MIN_AGE_MINUTES", 10) * 60
MAX_AGE_S = sg._f("ROCKET_MAX_AGE_MINUTES", 60) * 60
MIN_UP_5M = sg._f("ROCKET_MIN_UP_5M", 5)
MAX_UP_5M = sg._f("ROCKET_MAX_UP_5M", 60)
MAX_UP_1H = sg._f("ROCKET_MAX_UP_1H", 400)
MIN_BUYS_5M = int(sg._f("ROCKET_MIN_BUYS_5M", 30))
MIN_BUY_RATIO_5M = sg._f("ROCKET_MIN_BUY_RATIO_5M", 2.0)
MIN_BUYS_1H = int(sg._f("ROCKET_MIN_BUYS_1H", 150))
MIN_BUY_SHARE_1H = sg._f("ROCKET_MIN_BUY_SHARE_1H", 0.60)
MIN_VOL_1H = sg._f("ROCKET_MIN_VOLUME_1H", 15_000)
MAX_LOSS_STREAK = int(sg._f("ROCKET_MAX_LOSS_STREAK", 5))
COOLDOWN_S = sg._f("ROCKET_COOLDOWN_HOURS", 6) * 3600
SCAN_EVERY_S = sg._f("ROCKET_SCAN_SECONDS", 45)
STOP_LOSS = sg._f("ROCKET_STOP_LOSS", 0.40)
TRAIL_ARM = sg._f("ROCKET_TRAIL_ARM", 0.30)
TRAIL_DROP = sg._f("ROCKET_TRAIL_DROP", 0.15)
MAX_HOLD_S = sg._f("ROCKET_MAX_HOLD_MINUTES", 90) * 60
EXIT = {"stop_loss": str(STOP_LOSS), "arm": str(1 + TRAIL_ARM), "drop": str(TRAIL_DROP)}
RULES = 1

_LOCK = threading.RLock()
LAST = {"at": 0, "coins": 0, "rockets": 0, "bought": 0, "why": {}}


def _path():
    import live_trade_state
    return live_trade_state._path().with_name("rocket_bot.json")


def _load():
    d = {"enabled": True, "tried": {}, "buys": 0}
    p = _path()
    if p.exists():
        try:
            d.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    if d.get("rules") != RULES:
        d.update(rules=RULES, since=time.time(), enabled=True, paused_why="")
    return d


def _save(d):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".rocket.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def set_enabled(on):
    with _LOCK:
        d = _load()
        d["enabled"] = bool(on)
        if on:
            d.update(since=time.time(), paused_why="")
        _save(d)


def enabled():
    return bool(_load()["enabled"])


def closed_trades(since=None):
    import live_trade_state as st
    since = _load().get("since", 0) if since is None else since
    return [h for h in st.history() if h.get("mode") == "rocket" and h.get("pnl_usd") is not None
            and float(h.get("closed_at") or 0) >= since]


def loss_streak(trades):
    n = 0
    for h in reversed(trades):
        if h["pnl_usd"] > 0:
            break
        n += 1
    return n


def rocket_reason(p, now=None):
    """'' when a new pump.fun coin is exploding in buys right now; else why not."""
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
    age = now - float(created) / 1000 if created else None
    if age is None or age < MIN_AGE_S:
        return "moeda nova demais (< 10 min)"
    if age > MAX_AGE_S:
        return "moeda velha (> 60 min)"
    tx = p.get("txns") or {}
    h1, m5 = tx.get("h1") or {}, tx.get("m5") or {}
    b1, s1 = int(h1.get("buys") or 0), int(h1.get("sells") or 0)
    b5, s5 = int(m5.get("buys") or 0), int(m5.get("sells") or 0)
    if b1 < MIN_BUYS_1H:
        return "pouca gente comprando (1 h)"
    if b1 / max(b1 + s1, 1) < MIN_BUY_SHARE_1H:
        return "vendedores fortes (1 h)"
    if float((p.get("volume") or {}).get("h1") or 0) < MIN_VOL_1H:
        return "volume baixo (1 h)"
    ch = p.get("priceChange") or {}
    c5, c1 = float(ch.get("m5") or 0), float(ch.get("h1") or 0)
    if c1 <= 0:
        return "vermelho na hora"
    if c1 > MAX_UP_1H:
        return "já subiu demais"
    if c5 < MIN_UP_5M:
        return "sem explosão agora (5 min)"
    if c5 > MAX_UP_5M:
        return "pico vertical (topo)"
    if b5 < MIN_BUYS_5M or b5 < MIN_BUY_RATIO_5M * s5:
        return "pouca compra nos 5 min"
    return ""


def _buy(p, send, chat):
    import copy_trader as ct
    import live_engine
    import live_trade_state as st
    mint = p["baseToken"]["address"]
    symbol = (p.get("baseToken") or {}).get("symbol") or mint[:6]
    if not live_engine.ready():
        return "robô real não está pronto: " + str(live_engine.why_stopped() or "")
    for _ in range(10):
        if not st.pending_orders():
            break
        time.sleep(2)
    allowed, why = st.can_open()
    if not allowed:
        return why
    positions = st._load()["positions"]
    if mint in positions:
        return "já tem a moeda"
    if st.positions_by_mode("rocket") >= MAX_OPEN:
        return "já tem " + str(MAX_OPEN) + " compras do Sniper abertas"
    if len(positions) >= st.MAX_POSITIONS:
        return "limite de posições da carteira"
    free = ct.wallet_usd(live_engine.get_address(), max_age=0)
    if free is None:
        return "saldo indisponível"
    if free < ORDER_USD + RESERVE_USD:
        return "saldo livre US$ " + format(free, ".2f")
    ch = p.get("priceChange") or {}
    m5 = (p.get("txns") or {}).get("m5") or {}
    if send and chat:
        send(chat, "🚀 SNIPER: " + symbol + " explodindo — " + format(float(ch.get("m5") or 0), "+.0f") + "% em 5 min, " +
             str(int(m5.get("buys") or 0)) + " compras x " + str(int(m5.get("sells") or 0)) + " vendas. Comprando US$ " +
             format(ORDER_USD, ".0f") + ".")
    settled = live_engine._execute_buy({"baseToken": {"address": mint, "symbol": symbol}}, mint, symbol, send, chat,
                                       live_engine.get_address(), None, "rocket", usd=ORDER_USD)
    return "" if settled else "compra não confirmada"


def cycle(send=None, chat=None, pairs=None, now=None, safety=None, buyer=None):
    """One scan: find exploding new coins and buy them (at most one buy per scan)."""
    if not enabled():
        return []
    try:
        streak = loss_streak(closed_trades())
    except Exception:
        streak = 0
    if streak >= MAX_LOSS_STREAK:
        with _LOCK:
            dd = _load()
            dd.update(enabled=False, paused_why=str(streak) + " perdas seguidas")
            _save(dd)
        if send and chat:
            send(chat, "⏸️ SNIPER PAUSADO SOZINHO: " + str(streak) + " perdas seguidas. Ligue de novo no botão "
                 "🚀 Ligar Sniper quando quiser.")
        return []
    now = now or time.time()
    pairs = sg.candidate_pairs() if pairs is None else pairs
    why, rockets = {}, []
    for p in pairs:
        r = rocket_reason(p, now)
        if r:
            why[r] = why.get(r, 0) + 1
        else:
            rockets.append(p)
    # strongest buying pressure first
    rockets.sort(key=lambda p: int(((p.get("txns") or {}).get("m5") or {}).get("buys") or 0), reverse=True)
    bought = []
    d = _load()
    for p in rockets:
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
                send(chat, "🚀 Sniper não comprou " + str((p.get("baseToken") or {}).get("symbol") or mint[:6]) + ": " + r)
            continue
        bought.append(mint)
        with _LOCK:
            dd = _load()
            dd["buys"] = int(dd.get("buys", 0)) + 1
            _save(dd)
        break
    LAST.update(at=now, coins=len(pairs), rockets=len(rockets), bought=len(bought), why=why)
    top = ", ".join(k + "=" + str(v) for k, v in sorted(why.items(), key=lambda kv: -kv[1])[:6])
    logging.info("rocket scan: %s coins, %s rockets, %s bought | %s", len(pairs), len(rockets), len(bought), top)
    return bought


def status_text():
    import live_trade_state as st
    d = _load()
    lines = ["🚀 SNIPER DE MOEDA NOVA (dinheiro real) — " + ("LIGADO" if d["enabled"] else "DESLIGADO") +
             (" (" + d["paused_why"] + ")" if not d["enabled"] and d.get("paused_why") else ""),
             "Compra moeda da pump.fun com 10–60 min de vida no momento em que explode: +" + format(MIN_UP_5M, ".0f") +
             "% a +" + format(MAX_UP_5M, ".0f") + "% em 5 min, " + str(MIN_BUYS_5M) + "+ compras em 5 min (" +
             format(MIN_BUY_RATIO_5M, "g") + "x mais que vendas), " + str(MIN_BUYS_1H) + "+ compras na hora. Liquidez US$ " +
             format(MIN_LIQ / 1000, ".0f") + "–" + format(MAX_LIQ / 1000, ".0f") + " mil, anti-golpe.",
             "💵 US$ " + format(ORDER_USD, ".0f") + " por compra · máx. " + str(MAX_OPEN) + " abertas · 🛑 stop -" +
             format(STOP_LOSS * 100, ".0f") + "% · 🚀 a partir de +" + format(TRAIL_ARM * 100, ".0f") +
             "% deixa correr (vende " + format(TRAIL_DROP * 100, ".0f") + "% abaixo do topo) · ⏱️ sai em " +
             format(MAX_HOLD_S / 60, ".0f") + " min se não decolar · ⏸️ pausa com " + str(MAX_LOSS_STREAK) +
             " perdas seguidas · sem limite diário."]
    try:
        hist = [h for h in st.history() if h.get("mode") == "rocket" and h.get("pnl_usd") is not None]
        open_n = st.positions_by_mode("rocket")
    except Exception:
        hist, open_n = [], 0
    lines.append("📦 Abertas agora: " + str(open_n) + "/" + str(MAX_OPEN))
    if hist:
        won = sum(1 for h in hist if h["pnl_usd"] > 0)
        pnl = sum(h["pnl_usd"] for h in hist)
        best = max(h["pnl_usd"] for h in hist)
        lines.append("📊 Resultado: " + str(len(hist)) + " fechadas · " + str(won) + " no lucro (" +
                     format(won / len(hist) * 100, ".0f") + "%) · melhor " + format(best, "+.2f") + " · total " +
                     format(pnl, "+.2f") + " USD")
    else:
        lines.append("📊 Ainda sem operações fechadas.")
    if LAST["at"]:
        top = ", ".join(k + " " + str(v) for k, v in sorted(LAST["why"].items(), key=lambda kv: -kv[1])[:4])
        lines.append("🔎 Última varredura: " + str(LAST["coins"]) + " moedas, " + str(LAST["rockets"]) + " explodindo" +
                     (" · " + top if top else ""))
    lines.append("⚠️ A maioria dos snipes perde; o lucro vem das poucas que disparam. Não há garantia de lucro.")
    return "\n".join(lines)
