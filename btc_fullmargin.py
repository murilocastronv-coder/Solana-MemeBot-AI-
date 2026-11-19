"""BTC FULL MARGIN (owner, Nov 19): the MT5 "Full Margin" robot ported to BTC on Jupiter Perpetuals.

- SHORT ONLY (owner's choice), collateral from the bot wallet's SOL ("full margin": all the SOL except a small
  reserve for fees is turned into USDC collateral); leverage chosen by the owner (2x-10x, default 5x).
- Reads the BTC 5-minute chart and gives a score from -9 to +9 (trend M15 + H1, last 10 candles, highs/lows,
  EMA20, candle patterns, strong candle). On switching on it opens a short right away; afterwards it shorts
  when the score is <= -MIN_SCORE (one position at a time, day and night).
- Stop behind the last 5 candles (0.8-2.5 ATR, always far from liquidation); target 1.5x the risk; stop to the
  entry at +0.6 risk and trailing stop from +1 risk. 2 losses in a row = 15 min pause.
- Session: the value when it was switched on; stops itself at the TARGET (x3) or at the LOSS LIMIT (-50%).
- Starts OFF. "Simulação" uses fake money with real prices (fees included); "REAL" sends real orders.
Fees (Jupiter Perps ~0.06% to open and to close + borrow fee) eat a lot of a scalp: that is why it reads M5.
"""
import json
import logging
import os
import tempfile
import threading
import time

import requests

MIN_SCORE = int(os.getenv("BTCFM_MIN_SCORE", "4"))
LEVERAGES = (2, 3, 5, 10)
DEFAULT_LEVERAGE = 5
TARGET_MULT = 3.0
MAX_LOSS = 0.5
RR = 1.5
BE_R, TRAIL_START_R, TRAIL_DIST_R = 0.6, 1.0, 0.5
PAUSE_LOSSES, PAUSE_S = 2, 15 * 60
FEE_RATE = 0.0006               # per side, on the position size
SOL_RESERVE = 0.03              # SOL kept for network fees
DEMO_START = 50.0
CHECK_EVERY_S = 20
TF = 300

_LOCK = threading.RLock()
_http = requests.Session()
LAST = {"at": 0, "why": "", "price": None, "score": None, "reading": ""}


# ------------------------------------------------------------------ state
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("btc_fullmargin.json")


def _load():
    d = {"on": False, "mode": "demo", "leverage": DEFAULT_LEVERAGE, "pos": None, "start_usd": 0.0,
         "demo_usd": DEMO_START, "wins": 0, "losses": 0, "trades": 0, "consec": 0, "pause_until": 0,
         "last_bar": 0, "stopped_why": "", "hist": []}
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
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".btcfm.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


# ------------------------------------------------------------------ market data
def _get(url, params=None):
    r = _http.get(url, params=params, timeout=12, headers={"User-Agent": "BTCFullMargin/1.0"})
    r.raise_for_status()
    return r.json()


def klines_5m(limit=1000):
    """5-minute BTC candles, oldest first (the last one is still forming)."""
    for base in ("https://api.binance.com", "https://api.binance.us"):
        try:
            rows = _get(base + "/api/v3/klines", {"symbol": "BTCUSDT", "interval": "5m", "limit": limit})
            return [{"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
                    for r in rows]
        except Exception:
            continue
    now = int(time.time()) // TF * TF + TF
    rows = {}
    for k in range(4):                      # Coinbase: 300 candles per call
        end, start = now - k * 300 * TF, now - (k + 1) * 300 * TF
        for r in _get("https://api.exchange.coinbase.com/products/BTC-USD/candles",
                      {"granularity": TF, "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
                       "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end))}):
            rows[int(r[0])] = {"t": int(r[0]), "l": float(r[1]), "h": float(r[2]), "o": float(r[3]), "c": float(r[4])}
    return [rows[t] for t in sorted(rows)]


def ema(values, n):
    if not values:
        return []
    k = 2.0 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def atr(cs, n=14):
    trs = [cs[i]["h"] - cs[i]["l"] if i == 0 else
           max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - cs[i - 1]["c"]), abs(cs[i]["l"] - cs[i - 1]["c"]))
           for i in range(len(cs))]
    return ema(trs, n)[-1] if trs else 0.0


def group(cs, secs):
    g = {}
    for c in cs:
        g.setdefault(c["t"] // secs * secs, []).append(c)
    return [{"t": t, "o": v[0]["o"], "h": max(x["h"] for x in v), "l": min(x["l"] for x in v), "c": v[-1]["c"]}
            for t, v in sorted(g.items())]


def pattern(cs, a):
    """Strong pattern on the last closed candle: (1 up / -1 down / 0, name)."""
    c1, c2 = cs[-1], cs[-2]
    rng = c1["h"] - c1["l"]
    if rng <= 0 or rng < 0.5 * a:
        return 0, ""
    body, body2 = abs(c1["c"] - c1["o"]), abs(c2["c"] - c2["o"])
    up_w, dn_w = c1["h"] - max(c1["o"], c1["c"]), min(c1["o"], c1["c"]) - c1["l"]
    if c2["c"] < c2["o"] and c1["c"] > c1["o"] and c1["c"] > c2["o"] and c1["o"] <= c2["c"] and body >= 1.2 * body2 and body >= 0.3 * a:
        return 1, "engolfo de alta"
    if c2["c"] > c2["o"] and c1["c"] < c1["o"] and c1["c"] < c2["o"] and c1["o"] >= c2["c"] and body >= 1.2 * body2 and body >= 0.3 * a:
        return -1, "engolfo de baixa"
    if dn_w >= 2 * body and dn_w >= 0.6 * rng and c1["c"] >= c1["o"]:
        return 1, "martelo"
    if up_w >= 2 * body and up_w >= 0.6 * rng and c1["c"] <= c1["o"]:
        return -1, "estrela cadente"
    return 0, ""


def read_chart(closed):
    """Score -9..+9 of the closed 5-minute candles (oldest first). Returns (score, atr, text)."""
    a = atr(closed[-200:])
    price = closed[-1]["c"]
    e15 = ema([c["c"] for c in group(closed, 900)], 50)[-1]
    e60 = ema([c["c"] for c in group(closed, 3600)], 50)[-1]
    t15, t60 = (1 if price > e15 else -1), (1 if price > e60 else -1)
    sc = 2 * t15 if t15 == t60 else t15
    last10 = closed[-10:]
    bulls = sum(1 for c in last10 if c["c"] > c["o"])
    bears = sum(1 for c in last10 if c["c"] < c["o"])
    sc += 1 if bulls - bears >= 3 else (-1 if bears - bulls >= 3 else 0)
    hh = ll = 0
    for i in range(1, 5):
        c, p = closed[-i], closed[-i - 1]
        hh += c["h"] > p["h"] and c["l"] > p["l"]
        ll += c["h"] < p["h"] and c["l"] < p["l"]
    sc += 1 if hh >= 2 and hh > ll else (-1 if ll >= 2 and ll > hh else 0)
    e20 = ema([c["c"] for c in closed[-200:]], 20)[-1]
    sc += 1 if price > e20 else -1
    pat, name = pattern(closed, a)
    sc += 2 * pat
    c1 = closed[-1]
    if abs(c1["c"] - c1["o"]) >= 0.6 * a:
        sc += 1 if c1["c"] > c1["o"] else -1
    return sc, a, "leitura " + format(sc, "+d") + (" " + name if name else "")


def stop_distance(closed, a, price, lev):
    """Short: stop above the high of the last 5 candles (+0.2 ATR), 0.8-2.5 ATR, at most 60% of the way to liquidation."""
    ref = max(c["h"] for c in closed[-5:]) + 0.2 * a - price
    dist = min(max(ref, 0.8 * a), 2.5 * a)
    return min(dist, price * 0.6 / lev)


def pnl_usd(pos, price):
    """Short P/L in USD after the open and close fees."""
    gross = pos["size_usd"] * (pos["entry"] - price) / pos["entry"]
    return gross - pos["size_usd"] * FEE_RATE * 2


def manage(pos, price):
    """Moves the stop (breakeven / trailing) and says whether to close: returns (why or '', new_stop)."""
    R = pos["risk"]
    prof = pos["entry"] - price
    stop = pos["stop"]
    if prof >= BE_R * R:
        stop = min(stop, pos["entry"] * (1 - FEE_RATE * 2))      # covers the fees
    if prof >= TRAIL_START_R * R:
        stop = min(stop, price + TRAIL_DIST_R * R)
    if price <= pos["entry"] - RR * R:
        return "🎯 ALVO", stop
    if price >= stop:
        return ("🛡️ STOP NO LUCRO" if stop < pos["entry"] else "🛑 STOP"), stop
    return "", stop


# ------------------------------------------------------------------ money
def sol_balance():
    from bot_wallet_service import get_address
    from live_swap_executor import rpc
    return int((rpc("getBalance", [get_address(), {"commitment": "confirmed"}]) or {}).get("value", 0)) / 1e9


def sol_usd():
    import copy_trader as ct
    return float(ct._sol_usd())


def equity(d, price=None):
    """Session value in USD: demo balance, or wallet SOL + USDC + open position value."""
    if d["mode"] == "demo":
        v = d["demo_usd"]
        if d.get("pos") and price:
            v += pnl_usd(d["pos"], price)      # demo collateral stays inside demo_usd
        return v
    import jupiter_perps as jp
    from solders.pubkey import Pubkey
    from bot_wallet_service import get_address
    v = sol_balance() * sol_usd() + jp.usdc_balance(Pubkey.from_string(get_address()))
    if d.get("pos") and price:
        v += d["pos"]["collateral_usd"] + pnl_usd(d["pos"], price)
    return v


def _collateral(d):
    """Full margin: everything available minus the SOL reserve and a 3% cushion for the swap/fees."""
    if d["mode"] == "demo":
        return d["demo_usd"]
    return max(0.0, (sol_balance() - SOL_RESERVE) * sol_usd() * 0.97)


def _open(d, price, dist, why):
    lev = d["leverage"]
    col = _collateral(d)
    if col < 5:
        raise RuntimeError("saldo insuficiente para abrir (precisa de pelo menos US$ 5 em SOL)")
    pos = {"entry": price, "size_usd": col * lev, "collateral_usd": col, "risk": dist, "stop": price + dist,
           "opened": int(time.time()), "why": why}
    if d["mode"] == "real":
        import jupiter_perps as jp
        jp.ensure_usdc(col, sol_usd())
        out = jp.open_position("short", col, lev, price)
        pos["entry"] = float(out.get("price") or price)
        pos["stop"] = pos["entry"] + dist
        pos["signature"] = out.get("signature")
    d["pos"] = pos
    d["trades"] += 1
    return pos


def _close(d, price, why):
    pos = d["pos"]
    sig = None
    if d["mode"] == "real":
        import jupiter_perps as jp
        out = jp.close_position("short", price)
        sig = out.get("signature")
        try:
            jp.usdc_to_sol()
        except Exception as exc:
            logging.warning("btc full margin: USDC -> SOL failed: %s", type(exc).__name__)
    pnl = pnl_usd(pos, price)
    if d["mode"] == "demo":
        d["demo_usd"] = max(0.0, d["demo_usd"] + pnl)
    if pnl >= 0:
        d["wins"] += 1
        d["consec"] = 0
    else:
        d["losses"] += 1
        d["consec"] += 1
        if d["consec"] >= PAUSE_LOSSES:
            d["consec"] = 0
            d["pause_until"] = int(time.time()) + PAUSE_S
    d["hist"] = (d.get("hist") or [])[-49:] + [{"entry": pos["entry"], "exit": price, "pnl": round(pnl, 2),
                                                 "why": why, "t": int(time.time())}]
    d["pos"] = None
    return pnl, sig


# ------------------------------------------------------------------ robot
def cycle(send, chat, now=None, klines=None):
    """One check (every CHECK_EVERY_S): manages the open short and looks for a new one on each closed candle."""
    now = now or time.time()
    d = _load()
    if not d["on"]:
        return "desligado"
    cs = (klines or klines_5m)()
    if len(cs) < 700:
        return "poucos candles"
    price = cs[-1]["c"]
    closed = cs[:-1]
    LAST.update(at=int(now), price=price)
    tag = "🧪 SIMULAÇÃO" if d["mode"] == "demo" else "💰 REAL"
    with _LOCK:
        # session target / loss limit
        eq = equity(d, price)
        if d["start_usd"] > 0 and (eq >= d["start_usd"] * TARGET_MULT or eq <= d["start_usd"] * (1 - MAX_LOSS)):
            hit = eq >= d["start_usd"] * TARGET_MULT
            if d.get("pos"):
                _close(d, price, "🎯 META" if hit else "🛑 LIMITE")
            d["on"] = False
            d["stopped_why"] = "META BATIDA" if hit else "LIMITE DE PERDA"
            _save(d)
            send(chat, ("🎯 BTC FULL MARGIN: META BATIDA! " if hit else "🛑 BTC FULL MARGIN: LIMITE DE PERDA. ") + tag +
                 "\nComeçou com US$ " + format(d["start_usd"], ".2f") + " → agora US$ " + format(eq, ".2f") +
                 ". Robô desligado; ligue de novo para uma nova sessão.")
            return d["stopped_why"]
        pos = d.get("pos")
        if pos:
            why, new_stop = manage(pos, price)
            pos["stop"] = new_stop
            if why:
                pnl, sig = _close(d, price, why)
                _save(d)
                send(chat, "₿ BTC FULL MARGIN " + tag + " — " + why + "\nShort fechado em $" + format(price, ",.0f") +
                     " (entrada $" + format(pos["entry"], ",.0f") + ") → " + format(pnl, "+.2f") + " USD" +
                     ("\n🔗 https://solscan.io/tx/" + str(sig) if sig else "") +
                     ("\n⏸️ 2 perdas seguidas: pausa de 15 min." if d["pause_until"] > now else ""))
                return "fechou"
            _save(d)
            return "posição aberta"
        bar = closed[-1]["t"]
        first = d["trades"] == 0
        if bar == d["last_bar"] and not first:
            return "esperando candle"
        d["last_bar"] = bar
        if d["pause_until"] > now and not first:
            _save(d)
            return "pausa"
        score, a, reading = read_chart(closed)
        LAST.update(score=score, reading=reading)
        if not first and score > -MIN_SCORE:
            _save(d)
            return "sem sinal (" + reading + ")"
        dist = stop_distance(closed, a, price, d["leverage"])
        why = ("ao ligar, " if first else "") + reading
        try:
            pos = _open(d, price, dist, why)
        except Exception as exc:
            _save(d)
            send(chat, "⛔ BTC FULL MARGIN: não abri o short (" + str(exc)[:160] + ").")
            return "erro"
        _save(d)
        send(chat, "₿ BTC FULL MARGIN " + tag + " — SHORT aberto\n" + why + "\nEntrada $" + format(pos["entry"], ",.0f") +
             " · garantia US$ " + format(pos["collateral_usd"], ".2f") + " · " + str(d["leverage"]) + "x (posição US$ " +
             format(pos["size_usd"], ",.0f") + ")\n🛑 Stop $" + format(pos["stop"], ",.0f") + " · 🎯 Alvo $" +
             format(pos["entry"] - RR * dist, ",.0f") + (("\n🔗 https://solscan.io/tx/" + str(pos["signature"]))
                                                      if pos.get("signature") else ""))
        return "abriu"


# ------------------------------------------------------------------ Telegram
def status_text():
    d = _load()
    price = LAST.get("price")
    tag = "🧪 SIMULAÇÃO (dinheiro de mentira, preço real)" if d["mode"] == "demo" else "💰 REAL (carteira do bot, Jupiter Perps)"
    L = ["₿ BTC FULL MARGIN — só VENDA (short) · " + str(d["leverage"]) + "x",
         ("🟢 LIGADO" if d["on"] else "🔴 DESLIGADO" + (" (" + d["stopped_why"] + ")" if d.get("stopped_why") else "")) + " · " + tag]
    try:
        eq = equity(d, price)
        if d["start_usd"] > 0:
            L.append("💰 Começou com US$ " + format(d["start_usd"], ".2f") + " → agora US$ " + format(eq, ".2f") + " (" +
                     format((eq / d["start_usd"] - 1) * 100, "+.1f") + "%)")
            L.append("🎯 Meta US$ " + format(d["start_usd"] * TARGET_MULT, ".2f") + " · 🛑 Limite US$ " +
                     format(d["start_usd"] * (1 - MAX_LOSS), ".2f"))
        else:
            L.append("💰 Saldo: US$ " + format(eq, ".2f"))
    except Exception as exc:
        L.append("💰 Saldo indisponível agora (" + type(exc).__name__ + ")")
    tot = d["wins"] + d["losses"]
    L.append("📊 Operações " + str(d["trades"]) + " · ganhos " + str(d["wins"]) + " · perdas " + str(d["losses"]) +
             (" · acerto " + format(d["wins"] / tot * 100, ".0f") + "%" if tot else ""))
    pos = d.get("pos")
    if pos:
        L.append("📉 Short aberto: entrada $" + format(pos["entry"], ",.0f") + " · stop $" + format(pos["stop"], ",.0f") +
                 " · alvo $" + format(pos["entry"] - RR * pos["risk"], ",.0f") +
                 (" · agora " + format(pnl_usd(pos, price), "+.2f") + " USD" if price else ""))
    elif d["on"]:
        L.append("⏳ Esperando leitura ≤ −" + str(MIN_SCORE) + " nos candles de 5 min" +
                 (" · " + LAST["reading"] if LAST.get("reading") else ""))
    if d["pause_until"] > time.time():
        L.append("⏸️ Pausa até " + time.strftime("%H:%M", time.gmtime(d["pause_until"])) + " UTC (perdas seguidas)")
    if price:
        L.append("₿ BTC agora: $" + format(price, ",.0f"))
    L.append("⚠️ Alavancagem: se o BTC subir ~" + format(100 / d["leverage"], ".0f") +
             "% contra, a garantia é liquidada. O stop do robô fica antes disso, mas não é garantia.")
    return "\n".join(L)


def keyboard():
    d = _load()
    rows = []
    if d["on"]:
        rows.append([{"text": "⏸️ Desligar", "callback_data": "bfm_off"}, {"text": "🔄 Atualizar", "callback_data": "bfm"}])
        if d.get("pos"):
            rows.append([{"text": "❌ Fechar o short agora", "callback_data": "bfm_close"}])
    else:
        rows.append([{"text": "🧪 Ligar simulação", "callback_data": "bfm_demo"},
                     {"text": "💰 Ligar REAL", "callback_data": "bfm_real"}])
        rows.append([{"text": ("✅ " if d["leverage"] == x else "") + str(x) + "x", "callback_data": "bfm_lev" + str(x)}
                     for x in LEVERAGES])
        rows.append([{"text": "🔄 Atualizar", "callback_data": "bfm"}])
    rows.append([{"text": "💱 Voltar ao forex", "callback_data": "fx"}])
    return rows


ACTIONS = {"bfm", "/btc", "bfm_demo", "bfm_real", "bfm_real_ok", "bfm_off", "bfm_close"} | {"bfm_lev" + str(x) for x in LEVERAGES}


def _start(mode, send, chat, run_async):
    with _LOCK:
        d = _load()
        if d["on"]:
            return
        d.update(on=True, mode=mode, start_usd=0.0, wins=0, losses=0, trades=0, consec=0, pause_until=0,
                 last_bar=0, stopped_why="", pos=None)
        if mode == "demo":
            d["demo_usd"] = DEMO_START
        _save(d)

    def go():
        try:
            with _LOCK:
                d = _load()
                d["start_usd"] = round(equity(d), 2)
                _save(d)
            send(chat, "▶️ BTC FULL MARGIN ligado (" + ("🧪 simulação" if mode == "demo" else "💰 REAL") + ", " +
                 str(d["leverage"]) + "x, só short). Sessão começa com US$ " + format(d["start_usd"], ".2f") +
                 " · meta US$ " + format(d["start_usd"] * TARGET_MULT, ".2f") + ". Abrindo o 1º short…")
            send(chat, "₿ " + str(cycle(send, chat)))
        except Exception as exc:
            send(chat, "⛔ BTC FULL MARGIN: " + str(exc)[:200])
    run_async(go)


def handle(action, send, chat, run_async):
    if action in ("bfm", "/btc"):
        send(chat, status_text(), keyboard())
    elif action.startswith("bfm_lev"):
        lev = int(action[7:])
        with _LOCK:
            d = _load()
            if not d["on"] and lev in LEVERAGES:
                d["leverage"] = lev
                _save(d)
        send(chat, "⚙️ Alavancagem: " + str(lev) + "x (vale para a próxima sessão).", keyboard())
    elif action == "bfm_demo":
        _start("demo", send, chat, run_async)
    elif action == "bfm_real":
        d = _load()
        send(chat, "⚠️ MODO REAL: o robô usa TODO o SOL da carteira do bot (menos " + format(SOL_RESERVE, "g") +
             " SOL para taxas) como garantia de um SHORT de BTC com " + str(d["leverage"]) + "x no Jupiter Perps. "
             "Se o BTC subir rápido, você pode perder a garantia toda. Não existe garantia de lucro.",
             [[{"text": "✅ Entendi, ligar REAL", "callback_data": "bfm_real_ok"}],
              [{"text": "↩️ Cancelar", "callback_data": "bfm"}]])
    elif action == "bfm_real_ok":
        _start("real", send, chat, run_async)
    elif action == "bfm_off":
        with _LOCK:
            d = _load()
            d["on"] = False
            d["stopped_why"] = "desligado por você"
            _save(d)
        send(chat, "⏸️ BTC FULL MARGIN desligado: não abre shorts novos." +
             (" O short aberto continua; toque em ❌ Fechar para fechar agora." if d.get("pos") else ""), keyboard())
    elif action == "bfm_close":
        def go():
            with _LOCK:
                d = _load()
                if not d.get("pos"):
                    send(chat, "Nenhum short aberto.", keyboard())
                    return
                try:
                    price = klines_5m(2)[-1]["c"]
                    pnl, sig = _close(d, price, "fechado por você")
                    _save(d)
                    send(chat, "❌ Short fechado em $" + format(price, ",.0f") + " → " + format(pnl, "+.2f") + " USD" +
                         ("\n🔗 https://solscan.io/tx/" + str(sig) if sig else ""), keyboard())
                except Exception as exc:
                    send(chat, "⛔ Não consegui fechar: " + str(exc)[:200], keyboard())
        run_async(go)
    return True


def manage_open(send, chat):
    """Keeps managing an open short even while switched off (stop / target)."""
    with _LOCK:
        d = _load()
        if d["on"] or not d.get("pos"):
            return
        price = klines_5m(2)[-1]["c"]
        pos = d["pos"]
        why, pos["stop"] = manage(pos, price)
        if why:
            pnl, sig = _close(d, price, why)
            send(chat, "₿ BTC FULL MARGIN — " + why + ": short fechado → " + format(pnl, "+.2f") + " USD" +
                 ("\n🔗 https://solscan.io/tx/" + str(sig) if sig else ""))
        _save(d)
