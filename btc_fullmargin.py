"""BTC FULL MARGIN (owner, Nov 19): the MT5 "Full Margin" robot ported to BTC on Jupiter Perpetuals.

Two separate accounts (owner, Nov 19): a DEMO account (fake money, real prices, fees included) and a REAL account
(the bot wallet on Jupiter Perps). Each one trades a capital of US$ 100 per day:
- every day (UTC) the account starts again with CAPITAL_USD (100); "full margin" = the whole account balance is the
  collateral of each short, so a win makes the next short bigger;
- DAILY TARGET: when the account reaches TARGET_USD (250) it closes and STOPS until the next day;
  DAILY LOSS LIMIT: at 50% of the capital (US$ 50) it also closes and stops until the next day;
- SHORT ONLY, leverage chosen by the owner (2x-15x, default 5x), one position per account, day and night;
- reads the BTC 5-minute chart (score -9..+9: trend M15 + H1, last 10 candles, highs/lows, EMA20, candle patterns,
  strong candle); opens a short right away at the start of the day/when switched on, then shorts at score <= -4;
- stop behind the last 5 candles (0.8-2.5 ATR, far from liquidation); target 1.5x the risk; stop to the entry at
  +0.6 risk and trailing stop from +1 risk; 2 losses in a row = 15 min pause.
Both start OFF. The REAL account needs the SOL in the bot wallet (swapped to USDC collateral for each short).
"""
import json
import logging
import os
import tempfile
import threading
import time

import requests

MIN_SCORE = int(os.getenv("BTCFM_MIN_SCORE", "4"))
CAPITAL_USD = float(os.getenv("BTCFM_CAPITAL_USD", "100"))
TARGET_USD = float(os.getenv("BTCFM_TARGET_USD", "250"))
MAX_LOSS = 0.5
LEVERAGES = (2, 3, 5, 10, 15)
DEFAULT_LEVERAGE = 5
RR = 1.5
BE_R, TRAIL_START_R, TRAIL_DIST_R = 0.6, 1.0, 0.5
PAUSE_LOSSES, PAUSE_S = 2, 15 * 60
FEE_RATE = 0.0006               # per side, on the position size
BORROW_RATE_H = 0.0001          # Jupiter borrow fee estimate: ~0.01% of the size per hour held
PHOTO = None                    # telegram_bot sets this to its send_photo (charts in the results)
CHECK_EVERY_S = 20
TF = 300
MODES = ("demo", "real")
NAME = {"demo": "🧪 CONTA DEMO", "real": "💰 CONTA REAL"}

_LOCK = threading.RLock()
_http = requests.Session()
LAST = {"at": 0, "why": "", "price": None, "score": None, "reading": ""}


# ------------------------------------------------------------------ state (one file per account)
def _path(mode="demo"):
    import live_trade_state
    return live_trade_state._path().with_name("btc_fullmargin_" + mode + ".json")


def today(now=None):
    return time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))


def _load(mode):
    d = {"mode": mode, "on": False, "leverage": DEFAULT_LEVERAGE, "pos": None, "capital": CAPITAL_USD, "day": "",
         "done_why": "", "wins": 0, "losses": 0, "trades": 0, "consec": 0, "pause_until": 0, "last_bar": 0,
         "hist": [], "days": []}
    p = _path(mode)
    if p.exists():
        try:
            d.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    d["mode"] = mode
    return d


def _save(d):
    p = _path(d["mode"])
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".btcfm.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def new_day(d, now=None):
    """Starts the day: capital back to US$ 100, counters reset, target/limit unlocked."""
    if d["day"]:
        d["days"] = (d.get("days") or [])[-29:] + [{"day": d["day"], "end": round(d["capital"], 2),
                                                     "result": round(d["capital"] - CAPITAL_USD, 2),
                                                     "why": d.get("done_why") or ""}]
    d.update(day=today(now), capital=CAPITAL_USD, done_why="", wins=0, losses=0, trades=0, consec=0,
             pause_until=0, last_bar=0)


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


def breakdown(pos, price, now=None):
    """Short result in USD: (gross, fees, net). Fees = open + close (0.06% each) + borrow fee per hour held."""
    gross = pos["size_usd"] * (pos["entry"] - price) / pos["entry"]
    hours = max(0.0, ((now or time.time()) - pos["opened"]) / 3600) if pos.get("opened") else 0.0
    fees = pos["size_usd"] * (FEE_RATE * 2 + BORROW_RATE_H * hours)
    return gross, fees, gross - fees


def pnl_usd(pos, price):
    """Short P/L in USD after all the fees (what is left in the account)."""
    return breakdown(pos, price)[2]


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
def sol_usd():
    import copy_trader as ct
    return float(ct._sol_usd())


def equity(d, price=None):
    """Account value: the day's capital (100 + realized P/L) + the open short's P/L."""
    v = d["capital"]
    if d.get("pos") and price:
        v += pnl_usd(d["pos"], price)
    return v


def _open(d, price, dist, why):
    lev = d["leverage"]
    col = d["capital"]                    # full margin: the whole account balance
    if col < 5:
        raise RuntimeError("saldo da conta abaixo de US$ 5")
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
    """Closes the short. Result after the fees goes into the account; lifetime totals and the curve are updated."""
    pos = d["pos"]
    sig = None
    gross, fees, net = breakdown(pos, price)
    if d["mode"] == "real":
        import jupiter_perps as jp
        from solders.pubkey import Pubkey
        from bot_wallet_service import get_address
        owner = Pubkey.from_string(get_address())
        before = jp.usdc_balance(owner)
        out = jp.close_position("short", price)
        sig = out.get("signature")
        try:                                  # the real result: USDC that came back minus the collateral put in
            received = jp.usdc_balance(owner) - before
            if received > 0:
                net = received - pos["collateral_usd"]
                fees = gross - net
        except Exception as exc:
            logging.warning("btc full margin: real result read failed: %s", type(exc).__name__)
        try:
            jp.usdc_to_sol()
        except Exception as exc:
            logging.warning("btc full margin: USDC -> SOL failed: %s", type(exc).__name__)
    d["capital"] = max(0.0, d["capital"] + net)
    if net >= 0:
        d["wins"] += 1
        d["consec"] = 0
    else:
        d["losses"] += 1
        d["consec"] += 1
        if d["consec"] >= PAUSE_LOSSES:
            d["consec"] = 0
            d["pause_until"] = int(time.time()) + PAUSE_S
    tot = d.setdefault("total", {"trades": 0, "wins": 0, "losses": 0, "gross": 0.0, "fees": 0.0, "net": 0.0,
                                 "best": 0.0, "worst": 0.0, "since": int(time.time())})
    tot["trades"] += 1
    tot["wins" if net >= 0 else "losses"] += 1
    tot["gross"] = round(tot["gross"] + gross, 4)
    tot["fees"] = round(tot["fees"] + fees, 4)
    tot["net"] = round(tot["net"] + net, 4)
    tot["best"] = max(tot["best"], net)
    tot["worst"] = min(tot["worst"], net)
    d["curve"] = (d.get("curve") or [])[-499:] + [[int(time.time()), round(tot["net"], 2)]]
    d["last_close"] = {"gross": round(gross, 2), "fees": round(fees, 2), "net": round(net, 2)}
    d["hist"] = (d.get("hist") or [])[-49:] + [{"entry": pos["entry"], "exit": price, "pnl": round(net, 2),
                                                 "gross": round(gross, 2), "fees": round(fees, 2),
                                                 "why": why, "t": int(time.time())}]
    d["pos"] = None
    return net, sig


def close_text(d, why, price, entry):
    """Notification of a closed short: gross, fees, net, account balance and the total so far."""
    lc, tot = d["last_close"], d["total"]
    head = ("✅ LUCRO " if lc["net"] >= 0 else "❌ PREJUÍZO ") + format(lc["net"], "+.2f") + " USD (depois das taxas)"
    return ("₿ BTC FULL MARGIN " + NAME[d["mode"]] + " — " + why + "\n" + head +
            "\nShort: entrada $" + format(entry, ",.0f") + " → saída $" + format(price, ",.0f") +
            "\n💵 Bruto " + format(lc["gross"], "+.2f") + " · taxas −" + format(lc["fees"], ".2f") +
            " · líquido " + format(lc["net"], "+.2f") +
            "\n💼 Saldo da conta: US$ " + format(d["capital"], ".2f") + " (hoje " + format(d["capital"] - CAPITAL_USD, "+.2f") + ")" +
            "\n📈 Total desde o início: " + format(tot["net"], "+.2f") + " USD em " + str(tot["trades"]) + " operações (" +
            format(tot["wins"] / tot["trades"] * 100, ".0f") + "% no lucro)")


def _tx(sig):
    return "\n🔗 https://solscan.io/tx/" + str(sig) if sig else ""


# ------------------------------------------------------------------ robot
def cycle(mode, send, chat, now=None, klines=None):
    """One check of one account: daily reset, daily target/limit, manage the short, look for a new one."""
    now = now or time.time()
    with _LOCK:
        d = _load(mode)
        if not d["on"] and not d.get("pos"):
            return "desligado"
        cs = (klines or klines_5m)()
        if len(cs) < 700:
            return "poucos candles"
        price = cs[-1]["c"]
        closed = cs[:-1]
        LAST.update(at=int(now), price=price)
        tag = NAME[mode]
        pos = d.get("pos")
        if d["day"] != today(now) and not pos:
            new_day(d, now)
            if d["on"] and d["days"]:
                send(chat, "🌅 BTC FULL MARGIN " + tag + ": novo dia, capital de volta a US$ " + format(CAPITAL_USD, ".0f") +
                     " · meta US$ " + format(TARGET_USD, ".0f") + ". Ontem: " + format(d["days"][-1]["result"], "+.2f") + " USD.")
        # daily target / loss limit
        eq = equity(d, price)
        hit = eq >= TARGET_USD
        if d["on"] and not d["done_why"] and (hit or eq <= CAPITAL_USD * (1 - MAX_LOSS)):
            if pos:
                _close(d, price, "🎯 META DO DIA" if hit else "🛑 LIMITE DO DIA")
                send(chat, close_text(d, "🎯 META DO DIA" if hit else "🛑 LIMITE DO DIA", price, pos["entry"]))
            d["done_why"] = "META DO DIA BATIDA" if hit else "LIMITE DE PERDA DO DIA"
            _save(d)
            send(chat, ("🎯 BTC FULL MARGIN " + tag + ": META DO DIA BATIDA! " if hit else
                        "🛑 BTC FULL MARGIN " + tag + ": LIMITE DE PERDA DO DIA. ") +
                 "US$ " + format(CAPITAL_USD, ".0f") + " → US$ " + format(d["capital"], ".2f") +
                 ". Parou de operar hoje; volta sozinho amanhã (00:00 UTC) com US$ " + format(CAPITAL_USD, ".0f") + ".")
            return d["done_why"]
        if pos:
            why, new_stop = manage(pos, price)
            pos["stop"] = new_stop
            if why:
                pnl, sig = _close(d, price, why)
                _save(d)
                send(chat, close_text(d, why, price, pos["entry"]) + _tx(sig) +
                     ("\n⏸️ 2 perdas seguidas: pausa de 15 min." if d["pause_until"] > now else ""))
                return "fechou"
            _save(d)
            return "posição aberta"
        if not d["on"]:
            _save(d)
            return "desligado"
        if d["done_why"]:
            _save(d)
            return "parado até amanhã"
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
        why = ("início do dia, " if first else "") + reading
        try:
            pos = _open(d, price, dist, why)
        except Exception as exc:
            _save(d)
            send(chat, "⛔ BTC FULL MARGIN " + tag + ": não abri o short (" + str(exc)[:160] + ").")
            return "erro"
        _save(d)
        send(chat, "₿ BTC FULL MARGIN " + tag + " — SHORT aberto\n" + why + "\nEntrada $" + format(pos["entry"], ",.0f") +
             " · garantia US$ " + format(pos["collateral_usd"], ".2f") + " · " + str(d["leverage"]) + "x (posição US$ " +
             format(pos["size_usd"], ",.0f") + ")\n🛑 Stop $" + format(pos["stop"], ",.0f") + " · 🎯 Alvo $" +
             format(pos["entry"] - RR * dist, ",.0f") + _tx(pos.get("signature")))
        return "abriu"


def cycle_all(send, chat, now=None):
    """Both accounts with the same candles (one download)."""
    active = [m for m in MODES if (lambda d: d["on"] or d.get("pos"))(_load(m))]
    if not active:
        return {}
    cs = klines_5m()
    return {m: cycle(m, send, chat, now=now, klines=lambda: cs) for m in active}


# ------------------------------------------------------------------ Telegram
def account_text(mode):
    d = _load(mode)
    price = LAST.get("price")
    eq = equity(d, price)
    state = ("🟢 ligada" if d["on"] else "🔴 desligada") + (" · ⛔ " + d["done_why"] + " (volta amanhã)" if d["on"] and d["done_why"] else "")
    L = [NAME[mode] + " — " + state + " · " + str(d["leverage"]) + "x",
         "💼 Saldo da conta: US$ " + format(eq, ".2f") + " (hoje " + format(eq - CAPITAL_USD, "+.2f") + " · começou com US$ " +
         format(CAPITAL_USD, ".0f") + " · 🎯 meta US$ " + format(TARGET_USD, ".0f") + ")"]
    prog = max(0.0, min(1.0, (eq - CAPITAL_USD) / (TARGET_USD - CAPITAL_USD)))
    L.append("▕" + "█" * int(prog * 10) + "░" * (10 - int(prog * 10)) + "▏ " + format(prog * 100, ".0f") + "% da meta")
    tot = d["wins"] + d["losses"]
    L.append("📊 Operações " + str(d["trades"]) + " · ganhos " + str(d["wins"]) + " · perdas " + str(d["losses"]) +
             (" · acerto " + format(d["wins"] / tot * 100, ".0f") + "%" if tot else ""))
    tt = d.get("total") or {}
    if tt.get("trades"):
        L.append("📈 Total desde o início: " + format(tt["net"], "+.2f") + " USD líquido (taxas pagas " + format(tt["fees"], ".2f") +
                 ") · " + str(tt["trades"]) + " operações · " + format(tt["wins"] / tt["trades"] * 100, ".0f") + "% no lucro · " +
                 ("✅ LUCRANDO" if tt["net"] > 0 else "❌ NO PREJUÍZO"))
    pos = d.get("pos")
    if pos:
        L.append("📉 Short aberto: entrada $" + format(pos["entry"], ",.0f") + " · stop $" + format(pos["stop"], ",.0f") +
                 " · alvo $" + format(pos["entry"] - RR * pos["risk"], ",.0f") +
                 (" · agora " + format(pnl_usd(pos, price), "+.2f") + " USD" if price else ""))
    if d["pause_until"] > time.time():
        L.append("⏸️ Pausa até " + time.strftime("%H:%M", time.gmtime(d["pause_until"])) + " UTC (perdas seguidas)")
    days = (d.get("days") or [])[-5:]
    if days:
        L.append("📅 Últimos dias: " + " · ".join(x["day"][5:] + " " + format(x["result"], "+.0f") for x in days))
    return "\n".join(L)


def results_text():
    """Full report of both accounts: is the robot making money after the fees?"""
    L = ["📊 RESULTADOS DO ROBÔ BTC FULL MARGIN (líquido = depois das taxas da Jupiter)"]
    for m in MODES:
        d = _load(m)
        tt = d.get("total") or {}
        L += ["", NAME[m] + " — saldo de hoje US$ " + format(d["capital"], ".2f")]
        if not tt.get("trades"):
            L.append("Ainda sem operações fechadas.")
            continue
        days = d.get("days") or []
        L += ["Desde " + time.strftime("%d/%m", time.gmtime(tt["since"])) + ": " + str(tt["trades"]) + " operações · ganhos " +
              str(tt["wins"]) + " · perdas " + str(tt["losses"]) + " · acerto " + format(tt["wins"] / tt["trades"] * 100, ".0f") + "%",
              "💵 Bruto " + format(tt["gross"], "+.2f") + " · taxas −" + format(tt["fees"], ".2f") + " · LÍQUIDO " +
              format(tt["net"], "+.2f") + " USD",
              "🏆 Melhor " + format(tt["best"], "+.2f") + " · pior " + format(tt["worst"], "+.2f"),
              "🎯 Dias com meta batida: " + str(sum(1 for x in days if "META" in (x.get("why") or ""))) + " de " + str(len(days)) +
              " dias fechados",
              ("✅ O robô está LUCRANDO nessa conta." if tt["net"] > 0 else "❌ O robô está NO PREJUÍZO nessa conta.")]
        for x in days[-7:]:
            L.append("  " + x["day"][5:] + ": " + format(x["result"], "+.2f") + (" 🎯" if "META" in (x.get("why") or "") else ""))
        last = (d.get("hist") or [])[-5:]
        if last:
            L.append("Últimas: " + " · ".join(format(h["pnl"], "+.2f") for h in last))
    return "\n".join(L)


def results_png():
    import demo_report
    series = []
    for m, color in (("demo", (90, 200, 255)), ("real", (255, 196, 0))):
        d = _load(m)
        curve = [[t, CAPITAL_USD + v] for t, v in (d.get("curve") or [])]
        if curve:
            curve.insert(0, [min(curve[0][0] - 60, (d.get("total") or {}).get("since") or curve[0][0] - 60), CAPITAL_USD])
            series.append((NAME[m].split(" ", 1)[1].title() + " (resultado acumulado)", CAPITAL_USD, curve, color))
    return demo_report.chart_png(series) if series else None


def status_text():
    L = ["₿ BTC FULL MARGIN — Jupiter Perps · só VENDA (short)",
         "Cada conta começa o dia com US$ " + format(CAPITAL_USD, ".0f") + ", usa tudo como garantia e PARA quando chega em US$ " +
         format(TARGET_USD, ".0f") + " (ou cai a US$ " + format(CAPITAL_USD * (1 - MAX_LOSS), ".0f") + "). Recomeça todo dia às 00:00 UTC.",
         "", account_text("demo"), "", account_text("real")]
    if LAST.get("price"):
        L += ["", "₿ BTC agora: $" + format(LAST["price"], ",.0f") + (" · " + LAST["reading"] if LAST.get("reading") else "")]
    L.append("⚠️ Com alavancagem, uma alta rápida do BTC pode levar a garantia toda. Não existe garantia de lucro.")
    return "\n".join(L)


def keyboard():
    acc = {m: _load(m) for m in MODES}
    rows = [[{"text": ("⏸️ Desligar " if acc[m]["on"] else ("🧪 Ligar " if m == "demo" else "💰 Ligar ")) + m.upper(),
              "callback_data": ("bfm_off_" if acc[m]["on"] else "bfm_on_") + m} for m in MODES]]
    closes = [{"text": "❌ Fechar short " + m.upper(), "callback_data": "bfm_close_" + m} for m in MODES if acc[m].get("pos")]
    if closes:
        rows.append(closes)
    lev = _load("demo")["leverage"]
    rows.append([{"text": ("✅ " if lev == x else "") + str(x) + "x", "callback_data": "bfm_lev" + str(x)} for x in LEVERAGES])
    rows.append([{"text": "📥 Depositar", "callback_data": "wallet_deposit"}, {"text": "💸 Sacar", "callback_data": "wallet_withdraw"},
                 {"text": "💰 Saldo", "callback_data": "wallet_balance"}])
    rows.append([{"text": "📊 Resultados", "callback_data": "bfm_res"}, {"text": "🔄 Atualizar", "callback_data": "bfm"}])
    return rows


ACTIONS = ({"bfm", "/btc", "bfm_real_ok", "bfm_res", "/resultados_btc"} | {"bfm_lev" + str(x) for x in LEVERAGES} |
           {p + m for p in ("bfm_on_", "bfm_off_", "bfm_close_") for m in MODES})


def _start(mode, send, chat, run_async):
    with _LOCK:
        d = _load(mode)
        if d["on"]:
            return
        d["on"] = True
        if d["day"] != today() or not d.get("trades"):
            new_day(d)
        _save(d)
    send(chat, "▶️ BTC FULL MARGIN " + NAME[mode] + " ligada: US$ " + format(d["capital"], ".2f") + " · meta do dia US$ " +
         format(TARGET_USD, ".0f") + " · " + str(d["leverage"]) + "x · só short. Abrindo o 1º short…")

    def go():
        try:
            send(chat, "₿ " + NAME[mode] + ": " + str(cycle(mode, send, chat)))
        except Exception as exc:
            send(chat, "⛔ BTC FULL MARGIN " + NAME[mode] + ": " + str(exc)[:200])
    run_async(go)


def handle(action, send, chat, run_async):
    if action in ("bfm", "/btc"):
        send(chat, status_text(), keyboard())
    elif action in ("bfm_res", "/resultados_btc"):
        def go():
            try:
                png = results_png()
                if png and PHOTO:
                    PHOTO(chat, png, "📈 Resultado acumulado das contas (linha fraca = começo, acima dela = lucro)")
            except Exception as exc:
                logging.warning("btc full margin chart failed: %s", type(exc).__name__)
            send(chat, results_text(), keyboard())
        run_async(go)
    elif action.startswith("bfm_lev"):
        lev = int(action[7:])
        with _LOCK:
            for m in MODES:
                d = _load(m)
                if lev in LEVERAGES and not d.get("pos"):
                    d["leverage"] = lev
                    _save(d)
        send(chat, "⚙️ Alavancagem: " + str(lev) + "x nas duas contas (vale para o próximo short).", keyboard())
    elif action == "bfm_on_demo":
        _start("demo", send, chat, run_async)
    elif action == "bfm_on_real":
        send(chat, "⚠️ CONTA REAL: usa US$ " + format(CAPITAL_USD, ".0f") + " da carteira do bot (em SOL, trocado por USDC) como "
             "garantia de um SHORT de BTC com " + str(_load("real")["leverage"]) + "x no Jupiter Perps, até a meta de US$ " +
             format(TARGET_USD, ".0f") + " por dia. Deixe pelo menos US$ 110 em SOL na carteira. Se o BTC subir rápido, "
             "você pode perder a garantia toda. Não existe garantia de lucro.",
             [[{"text": "✅ Entendi, ligar a conta REAL", "callback_data": "bfm_real_ok"}],
              [{"text": "↩️ Cancelar", "callback_data": "bfm"}]])
    elif action == "bfm_real_ok":
        _start("real", send, chat, run_async)
    elif action.startswith("bfm_off_"):
        mode = action[8:]
        with _LOCK:
            d = _load(mode)
            d["on"] = False
            _save(d)
        send(chat, "⏸️ " + NAME[mode] + " desligada: não abre shorts novos." +
             (" O short aberto continua com stop e alvo; toque em ❌ para fechar agora." if d.get("pos") else ""), keyboard())
    elif action.startswith("bfm_close_"):
        mode = action[10:]

        def go():
            with _LOCK:
                d = _load(mode)
                if not d.get("pos"):
                    send(chat, "Nenhum short aberto na " + NAME[mode] + ".", keyboard())
                    return
                try:
                    price = klines_5m(2)[-1]["c"]
                    pnl, sig = _close(d, price, "fechado por você")
                    _save(d)
                    send(chat, close_text(d, "fechado por você", price, d["hist"][-1]["entry"]) + _tx(sig), keyboard())
                except Exception as exc:
                    send(chat, "⛔ Não consegui fechar: " + str(exc)[:200], keyboard())
        run_async(go)
    return True
