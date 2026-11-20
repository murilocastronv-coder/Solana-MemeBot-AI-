"""BTC FULL MARGIN (owner, Nov 20): the same strategy as the owner's MT5 robot FullMargin_7, on BTC (Jupiter Perps).

Two separate accounts: DEMO (fake money, real prices, fees included) and REAL (the bot wallet on Jupiter Perps).
Each one starts every day (00:00 UTC) with US$ 100 (minimum capital), day and night:
- BUY (long) AND SELL (short). Reads the BTC 5-minute chart (score -9..+9);
- STRONG MOVE: BTC moved >= 1.5 ATR of the 1 h chart in the last 6 h and is on that side of the 1 h EMA50 ->
  trades MORE in that direction (score 3, full lot); otherwise trades MUCH LESS (score 7, at most 1 entry per
  hour, half lot). The first order of the day opens right away to the side of the reading;
- AVERAGE PRICE: if the price goes 0.5% against the last order, it opens one more on the same side (up to 5) and
  CLOSES ALL when the orders together are 0.4% above the average price (BTC version of "20 pips / +10 pips";
  0.4% leaves profit after the ~0.12% Jupiter fees);
- MARGIN: 100% of the balance split across the 5 orders while the balance is under US$ 150, then 40%;
- the day stops at the TARGET (US$ 300, like the MT5 robot) or at the LOSS LIMIT (50% = US$ 50);
  a protection closes everything before the liquidation price (60% of the way).
Both start OFF. The REAL account needs SOL in the bot wallet (swapped to USDC collateral for each order).
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
TARGET_USD = float(os.getenv("BTCFM_TARGET_USD", "300"))   # owner, Nov 20: same as the MT5 robot (target 300)
MAX_LOSS = 0.5
GRID_MAX = 5                    # up to 5 orders on the same side (average price)
GRID_STEP_PCT = 0.005           # next order when the price goes 0.5% against the last one
BASKET_TP_PCT = 0.004           # close ALL at +0.4% over the average price
STRONG_ATR = 1.5                # strong move: >= 1.5 ATR(1 h) in 6 h, on that side of the 1 h EMA50
SCORE_STRONG, SCORE_WEAK = 3, 7
WEAK_EVERY_S = 3600             # no strong move: at most 1 entry per hour
WEAK_LOT = 0.5                  # ... with half the lot
TOTAL_MARGIN, SAFE_FROM, SAFE_MARGIN = 1.0, 150.0, 0.4
LIQ_GUARD = 0.6                 # closes everything at 60% of the way to the liquidation price


def total_margin(capital):
    """Share of the balance used by the 5 orders together: 100% under US$ 150, 40% from US$ 150."""
    return TOTAL_MARGIN if capital < SAFE_FROM else SAFE_MARGIN


def order_collateral(capital, factor=1.0):
    """Collateral of ONE order: the total margin split across the GRID_MAX orders."""
    return round(capital * total_margin(capital) / GRID_MAX * factor, 2)


LEVERAGES = (2, 3, 5, 10, 15)
DEFAULT_LEVERAGE = 5
PAUSE_LOSSES, PAUSE_S = 2, 15 * 60
FEE_RATE = 0.0006               # per side, on the position size
BORROW_RATE_H = 0.0001          # Jupiter borrow fee estimate: ~0.01% of the size per hour held
PHOTO = None                    # telegram_bot sets this to its send_photo (charts in the results)
EDIT = None                     # telegram_bot sets this to edit a message in place (live view)
LIVE_EVERY_S = 5
LIVE_FOR_S = 15 * 60
LIVE = {}                       # chat -> {"mode", "until", "hi", "lo"}
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
             pause_until=0, last_bar=0, last_entry=0)


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


def price_now():
    """Current BTC price (light call for the live view)."""
    for url in ("https://api.binance.com/api/v3/ticker/price", "https://api.binance.us/api/v3/ticker/price"):
        try:
            return float(_get(url, {"symbol": "BTCUSDT"})["price"])
        except Exception:
            continue
    return float(_get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")["price"])


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


def strong_move(closed):
    """(direction, size): +1 strong rise / -1 strong fall / 0, and the 6 h move in ATRs of the 1 h chart."""
    hourly = group(closed, 3600)
    if len(hourly) < 60:
        return 0, 0.0
    a = atr(hourly[-100:])
    if a <= 0:
        return 0, 0.0
    price = closed[-1]["c"]
    move = (price - hourly[-7]["c"]) / a
    e50 = ema([c["c"] for c in hourly], 50)[-1]
    if move <= -STRONG_ATR and price < e50:
        return -1, move
    if move >= STRONG_ATR and price > e50:
        return 1, move
    return 0, move


def _sgn(pos):
    return 1 if pos.get("side", "short") == "long" else -1


def breakdown(pos, price, now=None):
    """Result in USD of the open orders: (gross, fees, net). Fees = open + close (0.06% each) + borrow per hour."""
    gross = pos["size_usd"] * (price - pos["entry"]) / pos["entry"] * _sgn(pos)
    hours = max(0.0, ((now or time.time()) - pos["opened"]) / 3600) if pos.get("opened") else 0.0
    fees = pos["size_usd"] * (FEE_RATE * 2 + BORROW_RATE_H * hours)
    return gross, fees, gross - fees


def pnl_usd(pos, price, now=None):
    """P/L in USD after all the fees (what is left in the account)."""
    return breakdown(pos, price, now)[2]


def basket_pct(pos, price):
    """How far the price is from the average price, in the favour of the orders (0.004 = +0.4%)."""
    return (price - pos["entry"]) / pos["entry"] * _sgn(pos)


def tp_price(pos):
    return pos["entry"] * (1 + BASKET_TP_PCT * _sgn(pos))


def next_grid_price(pos):
    return pos.get("last", pos["entry"]) * (1 - GRID_STEP_PCT * _sgn(pos))


def side_name(pos_or_side):
    side = pos_or_side if isinstance(pos_or_side, str) else pos_or_side.get("side", "short")
    return "COMPRA (long)" if side == "long" else "VENDA (short)"


# ------------------------------------------------------------------ money
def sol_usd():
    import copy_trader as ct
    return float(ct._sol_usd())


def equity(d, price=None, now=None):
    """Account value: the day's capital (100 + realized P/L) + the open short's P/L."""
    v = d["capital"]
    if d.get("pos") and price:
        v += pnl_usd(d["pos"], price, now)
    return v


def _open(d, price, side, why, factor=1.0, now=None):
    """Opens the first order or ADDS one to the average price (same side). Returns the position."""
    lev = d["leverage"]
    col = order_collateral(d["capital"], factor)
    if col < 2:
        raise RuntimeError("saldo da conta baixo demais para mais uma ordem")
    pos = d.get("pos")
    sig = None
    onchain_avg = None
    if d["mode"] == "real":
        import jupiter_perps as jp
        jp.ensure_usdc(col, sol_usd())
        out = jp.open_position(side, col, lev, price, min_size=(pos or {}).get("size_usd", 0.0))
        onchain_avg = float(out.get("price") or 0) or None
        sig = out.get("signature")
    add = col * lev
    t = int(now or time.time())
    if pos:
        size = pos["size_usd"] + add
        entry = size / (pos["size_usd"] / pos["entry"] + add / price)        # average price of the USD notional
        pos.update(size_usd=size, entry=onchain_avg or entry, collateral_usd=pos["collateral_usd"] + col,
                   n=pos.get("n", 1) + 1, last=price)
    else:
        pos = {"side": side, "entry": onchain_avg or price, "size_usd": add, "collateral_usd": col, "n": 1,
               "last": price, "opened": t, "why": why, "factor": factor}
        d["pos"] = pos
    if sig:
        pos["signature"] = sig
    d["trades"] += 1
    d["last_entry"] = t
    return pos


def _close(d, price, why, now=None):
    """Closes the short. Result after the fees goes into the account; lifetime totals and the curve are updated."""
    pos = d["pos"]
    sig = None
    gross, fees, net = breakdown(pos, price, now)
    if d["mode"] == "real":
        import jupiter_perps as jp
        from solders.pubkey import Pubkey
        from bot_wallet_service import get_address
        owner = Pubkey.from_string(get_address())
        before = jp.usdc_balance(owner)
        out = jp.close_position(pos.get("side", "short"), price)
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
    d["hist"] = (d.get("hist") or [])[-49:] + [{"side": pos.get("side", "short"), "n": pos.get("n", 1),
                                                 "entry": pos["entry"], "exit": price, "pnl": round(net, 2),
                                                 "gross": round(gross, 2), "fees": round(fees, 2),
                                                 "why": why, "t": int(time.time())}]
    d["pos"] = None
    return net, sig


def close_text(d, why, price, entry, pos=None):
    """Notification of closed orders: gross, fees, net, account balance and the total so far."""
    pos = pos or (d.get("hist") or [{}])[-1]
    lc, tot = d["last_close"], d["total"]
    head = ("✅ LUCRO " if lc["net"] >= 0 else "❌ PREJUÍZO ") + format(lc["net"], "+.2f") + " USD (depois das taxas)"
    return ("₿ BTC FULL MARGIN " + NAME[d["mode"]] + " — " + why + "\n" + head +
            "\n" + side_name(pos.get("side", "short")) + " · " + str(pos.get("n", 1)) + " ordem(ns) · preço médio $" +
            format(entry, ",.0f") + " → saída $" + format(price, ",.0f") +
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
        eq = equity(d, price, now)
        hit = eq >= TARGET_USD
        if d["on"] and not d["done_why"] and (hit or eq <= CAPITAL_USD * (1 - MAX_LOSS)):
            if pos:
                _close(d, price, "🎯 META DO DIA" if hit else "🛑 LIMITE DO DIA", now)
                send(chat, close_text(d, "🎯 META DO DIA" if hit else "🛑 LIMITE DO DIA", price, pos["entry"], pos))
            d["done_why"] = "META DO DIA BATIDA" if hit else "LIMITE DE PERDA DO DIA"
            _save(d)
            send(chat, ("🎯 BTC FULL MARGIN " + tag + ": META DO DIA BATIDA! " if hit else
                        "🛑 BTC FULL MARGIN " + tag + ": LIMITE DE PERDA DO DIA. ") +
                 "US$ " + format(CAPITAL_USD, ".0f") + " → US$ " + format(d["capital"], ".2f") +
                 ". Parou de operar hoje; volta sozinho amanhã (00:00 UTC) com US$ " + format(CAPITAL_USD, ".0f") + ".")
            return d["done_why"]
        if pos:
            bp = basket_pct(pos, price)
            why = ""
            if bp >= BASKET_TP_PCT:
                why = "🎯 +" + format(bp * 100, ".2f") + "% SOBRE O PREÇO MÉDIO"
            elif bp <= -LIQ_GUARD / d["leverage"]:
                why = "🛑 PROTEÇÃO (perto da liquidação)"
            if why:
                pnl, sig = _close(d, price, why, now)
                _save(d)
                send(chat, close_text(d, why, price, pos["entry"], pos) + _tx(sig))
                return "fechou"
            # average price: the price went 0.5% against the last order -> one more on the same side (up to 5)
            if d["on"] and not d["done_why"] and pos.get("n", 1) < GRID_MAX and \
                    (price - next_grid_price(pos)) * _sgn(pos) <= 0:
                try:
                    _open(d, price, pos["side"], "preço médio", pos.get("factor", 1.0), now)
                except Exception as exc:
                    _save(d)
                    send(chat, "⛔ BTC FULL MARGIN " + tag + ": não abri a ordem do preço médio (" + str(exc)[:160] + ").")
                    return "erro"
                _save(d)
                send(chat, "➕ BTC FULL MARGIN " + tag + " — ordem " + str(pos["n"]) + "/" + str(GRID_MAX) + " (" +
                     side_name(pos) + ") em $" + format(price, ",.0f") + "\nPreço médio agora $" + format(pos["entry"], ",.0f") +
                     " · fecha tudo em $" + format(tp_price(pos), ",.0f") + " (+" + format(BASKET_TP_PCT * 100, ".1f") +
                     "%)" + _tx(pos.get("signature")))
                return "preço médio"
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
        score, a, reading = read_chart(closed)
        sdir, move = strong_move(closed)
        LAST.update(score=score, reading=reading, strong=sdir, move=round(move, 2))
        if first:
            side_dir, factor = (1 if score >= 0 else -1), 1.0
        else:
            if d["pause_until"] > now and not sdir:
                _save(d)
                return "pausa"
            if not sdir and now - d.get("last_entry", 0) < WEAK_EVERY_S:
                _save(d)
                return "sem movimento forte: 1 entrada por hora"
            need_buy = SCORE_STRONG if sdir == 1 else SCORE_WEAK
            need_sell = SCORE_STRONG if sdir == -1 else SCORE_WEAK
            side_dir = 1 if score >= need_buy else (-1 if score <= -need_sell else 0)
            if not side_dir:
                _save(d)
                return "sem sinal (" + reading + ")"
            factor = 1.0 if side_dir == sdir else WEAK_LOT
        side = "long" if side_dir == 1 else "short"
        label = {1: " [ALTA FORTE]", -1: " [QUEDA FORTE]"}.get(sdir, "")
        why = ("início do dia, " if first else "") + reading + label
        try:
            pos = _open(d, price, side, why, factor, now)
        except Exception as exc:
            _save(d)
            send(chat, "⛔ BTC FULL MARGIN " + tag + ": não abri a ordem (" + str(exc)[:160] + ").")
            return "erro"
        _save(d)
        send(chat, "₿ BTC FULL MARGIN " + tag + " — " + side_name(side) + " aberta (ordem 1/" + str(GRID_MAX) + ")\n" + why +
             "\nEntrada $" + format(pos["entry"], ",.0f") + " · garantia US$ " + format(pos["collateral_usd"], ".2f") + " · " +
             str(d["leverage"]) + "x (posição US$ " + format(pos["size_usd"], ",.0f") + ")" +
             "\n🎯 Fecha tudo em $" + format(tp_price(pos), ",.0f") + " (+" + format(BASKET_TP_PCT * 100, ".1f") +
             "%) · ➕ próxima ordem se for a $" + format(next_grid_price(pos), ",.0f") + _tx(pos.get("signature")))
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
    L.append("🛡️ Margem: " + format(total_margin(d["capital"]) * 100, ".0f") + "% do saldo em até " + str(GRID_MAX) +
             " ordens (US$ " + format(order_collateral(d["capital"]), ".2f") + " cada) — 40% a partir de US$ " + format(SAFE_FROM, ".0f"))
    L.append("📊 Ordens " + str(d["trades"]) + " · fechamentos no lucro " + str(d["wins"]) + " · no prejuízo " + str(d["losses"]) +
             (" · acerto " + format(d["wins"] / tot * 100, ".0f") + "%" if tot else ""))
    tt = d.get("total") or {}
    if tt.get("trades"):
        L.append("📈 Total desde o início: " + format(tt["net"], "+.2f") + " USD líquido (taxas pagas " + format(tt["fees"], ".2f") +
                 ") · " + str(tt["trades"]) + " operações · " + format(tt["wins"] / tt["trades"] * 100, ".0f") + "% no lucro · " +
                 ("✅ LUCRANDO" if tt["net"] > 0 else "❌ NO PREJUÍZO"))
    pos = d.get("pos")
    if pos:
        L.append(("📈 " if pos.get("side") == "long" else "📉 ") + side_name(pos) + ": " + str(pos.get("n", 1)) + "/" + str(GRID_MAX) +
                 " ordens · preço médio $" + format(pos["entry"], ",.0f") + " · fecha tudo em $" + format(tp_price(pos), ",.0f") +
                 (" · agora " + format(pnl_usd(pos, price), "+.2f") + " USD" if price else ""))
    if d["pause_until"] > time.time():
        L.append("⏸️ Pausa até " + time.strftime("%H:%M", time.gmtime(d["pause_until"])) + " UTC (perdas seguidas)")
    days = (d.get("days") or [])[-5:]
    if days:
        L.append("📅 Últimos dias: " + " · ".join(x["day"][5:] + " " + format(x["result"], "+.0f") for x in days))
    return "\n".join(L)


def live_text(mode, price, now=None, track=None):
    """Live view of an account: floating result of the open short (after the fees), stop/target distance."""
    now = now or time.time()
    d = _load(mode)
    pos = d.get("pos")
    L = ["📺 AO VIVO — " + NAME[mode] + " (atualiza a cada " + str(LIVE_EVERY_S) + " s)",
         "₿ BTC agora: $" + format(price, ",.2f")]
    if not pos:
        L.append("⏳ Nenhuma ordem aberta agora " + ("— esperando sinal nos candles de 5 min." if d["on"] else "— conta desligada."))
        L.append("💼 Saldo da conta: US$ " + format(d["capital"], ".2f") + " (hoje " + format(d["capital"] - CAPITAL_USD, "+.2f") + ")")
    else:
        gross, fees, net = breakdown(pos, price, now)
        if track is not None:
            track["hi"] = max(track.get("hi", net), net)
            track["lo"] = min(track.get("lo", net), net)
        col = pos["collateral_usd"]
        move = (price / pos["entry"] - 1) * 100
        tp, nxt = tp_price(pos), next_grid_price(pos)
        L += [("📈 " if pos.get("side") == "long" else "📉 ") + side_name(pos) + " · " + str(pos.get("n", 1)) + "/" + str(GRID_MAX) +
              " ordens · preço médio $" + format(pos["entry"], ",.2f") + " · garantia US$ " + format(col, ".2f") +
              " (posição US$ " + format(pos["size_usd"], ",.0f") + ")",
              ("🟢 " if net >= 0 else "🔴 ") + "FLUTUAÇÃO: " + format(net, "+.2f") + " USD (" + format(net / col * 100, "+.1f") +
              "% da garantia) — já com as taxas",
              "💵 Bruto " + format(gross, "+.2f") + " · taxas −" + format(fees, ".2f") + " · BTC " + format(move, "+.2f") +
              "% desde o preço médio (" + ("a favor" if move * _sgn(pos) > 0 else "contra") + ")",
              "🎯 Fecha tudo em $" + format(tp, ",.0f") + " (falta " + format((tp / price - 1) * 100, "+.2f") + "%)" +
              (" · ➕ próxima ordem em $" + format(nxt, ",.0f") if pos.get("n", 1) < GRID_MAX else " · grade completa")]
        lo, hi = (nxt, tp) if pos.get("side") == "long" else (tp, nxt)
        if hi > lo:
            k = int(round(max(0.0, min(1.0, (price - lo) / (hi - lo))) * 14))
            bar = "─" * k + "●" + "─" * (14 - k)
            L.append(("➕ " + bar + " 🎯") if pos.get("side") == "long" else ("🎯 " + bar + " ➕"))
        if track is not None:
            L.append("📊 Nesta tela: máx " + format(track["hi"], "+.2f") + " · mín " + format(track["lo"], "+.2f") + " USD")
        L.append("💼 Saldo da conta agora: US$ " + format(d["capital"] + net, ".2f") + " (hoje " +
                 format(d["capital"] + net - CAPITAL_USD, "+.2f") + ") · 🎯 meta US$ " + format(TARGET_USD, ".0f"))
        L.append("⏱️ Aberta há " + str(int((now - pos.get("opened", now)) // 60)) + " min")
    L.append("🕒 " + time.strftime("%H:%M:%S", time.gmtime(now)) + " UTC")
    return "\n".join(L)


def live_keyboard():
    return [[{"text": "⏹️ Parar ao vivo", "callback_data": "bfm_live_stop"}, {"text": "⬅️ Robô BTC", "callback_data": "bfm"}]]


def start_live(mode, send, chat):
    """Sends the live message and edits it every LIVE_EVERY_S for LIVE_FOR_S (or until ⏹️)."""
    track = {}
    LIVE[str(chat)] = {"mode": mode, "until": time.time() + LIVE_FOR_S, "id": None}
    me = LIVE[str(chat)]
    price = price_now()
    r = send(chat, live_text(mode, price, track=track), live_keyboard()) or {}
    msg_id = (r.get("result") or {}).get("message_id")
    if not msg_id or not EDIT:
        return
    me["id"] = msg_id
    last = ""
    while LIVE.get(str(chat)) is me and time.time() < me["until"]:
        time.sleep(LIVE_EVERY_S)
        if LIVE.get(str(chat)) is not me:
            break
        try:
            text = live_text(mode, price_now(), track=track)
            if text != last:
                EDIT(chat, msg_id, text, live_keyboard())
                last = text
        except Exception as exc:
            logging.info("btc live view: %s", type(exc).__name__)
    if LIVE.get(str(chat)) is me:
        LIVE.pop(str(chat), None)
        try:
            EDIT(chat, msg_id, live_text(mode, price_now()) + "\n⏹️ Ao vivo encerrado (15 min). Toque em 📺 para ver de novo.",
                 [[{"text": "📺 Ao vivo de novo", "callback_data": "bfm_live_" + mode}, {"text": "⬅️ Robô BTC", "callback_data": "bfm"}]])
        except Exception:
            pass


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
    L = ["₿ BTC FULL MARGIN — Jupiter Perps · COMPRA e VENDA · preço médio",
         "Cada conta começa o dia com US$ " + format(CAPITAL_USD, ".0f") + ". Movimento forte: opera mais para esse lado; "
         "sem movimento forte: bem menos. Se o preço vai " + format(GRID_STEP_PCT * 100, ".1f") + "% contra, abre mais uma ordem "
         "(até " + str(GRID_MAX) + ") e fecha tudo com +" + format(BASKET_TP_PCT * 100, ".1f") + "% sobre o preço médio. "
         "Para no dia em US$ " + format(TARGET_USD, ".0f") + " (ou se cair a US$ " + format(CAPITAL_USD * (1 - MAX_LOSS), ".0f") +
         "). Recomeça todo dia às 00:00 UTC.",
         "", account_text("demo"), "", account_text("real")]
    if LAST.get("price"):
        L += ["", "₿ BTC agora: $" + format(LAST["price"], ",.0f") + (" · " + LAST["reading"] if LAST.get("reading") else "")]
    L.append("⚠️ Com alavancagem, uma alta rápida do BTC pode levar a garantia toda. Não existe garantia de lucro.")
    return "\n".join(L)


def keyboard():
    acc = {m: _load(m) for m in MODES}
    rows = [[{"text": ("⏸️ Desligar " if acc[m]["on"] else ("🧪 Ligar " if m == "demo" else "💰 Ligar ")) + m.upper(),
              "callback_data": ("bfm_off_" if acc[m]["on"] else "bfm_on_") + m} for m in MODES]]
    closes = [{"text": "❌ Fechar ordens " + m.upper(), "callback_data": "bfm_close_" + m} for m in MODES if acc[m].get("pos")]
    if closes:
        rows.append(closes)
    lev = _load("demo")["leverage"]
    rows.append([{"text": ("✅ " if lev == x else "") + str(x) + "x", "callback_data": "bfm_lev" + str(x)} for x in LEVERAGES])
    rows.append([{"text": "📥 Depositar", "callback_data": "wallet_deposit"}, {"text": "💸 Sacar", "callback_data": "wallet_withdraw"},
                 {"text": "💰 Saldo", "callback_data": "wallet_balance"}])
    rows.append([{"text": "📺 Ao vivo DEMO", "callback_data": "bfm_live_demo"}, {"text": "📺 Ao vivo REAL", "callback_data": "bfm_live_real"}])
    rows.append([{"text": "📊 Resultados", "callback_data": "bfm_res"}, {"text": "🔄 Atualizar", "callback_data": "bfm"}])
    return rows


ACTIONS = ({"bfm", "/btc", "bfm_real_ok", "bfm_res", "/resultados_btc", "bfm_live_demo", "bfm_live_real", "bfm_live_stop",
            "/aovivo"} | {"bfm_lev" + str(x) for x in LEVERAGES} |
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
         format(TARGET_USD, ".0f") + " · " + str(d["leverage"]) + "x · compra e venda. Abrindo a 1ª ordem…")

    def go():
        try:
            send(chat, "₿ " + NAME[mode] + ": " + str(cycle(mode, send, chat)))
        except Exception as exc:
            send(chat, "⛔ BTC FULL MARGIN " + NAME[mode] + ": " + str(exc)[:200])
    run_async(go)


def handle(action, send, chat, run_async):
    if action in ("bfm", "/btc"):
        send(chat, status_text(), keyboard())
    elif action in ("bfm_live_demo", "bfm_live_real", "/aovivo"):
        mode = "real" if action == "bfm_live_real" else "demo"
        run_async(lambda: start_live(mode, send, chat))
    elif action == "bfm_live_stop":
        LIVE.pop(str(chat), None)
        send(chat, "⏹️ Ao vivo parado.", keyboard())
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
        send(chat, "⚙️ Alavancagem: " + str(lev) + "x nas duas contas (vale para a próxima ordem).", keyboard())
    elif action == "bfm_on_demo":
        _start("demo", send, chat, run_async)
    elif action == "bfm_on_real":
        send(chat, "⚠️ CONTA REAL: usa US$ " + format(CAPITAL_USD, ".0f") + " da carteira do bot (em SOL, trocado por USDC) como "
             "garantia de ordens de COMPRA e VENDA de BTC (até " + str(GRID_MAX) + ", preço médio) com " + str(_load("real")["leverage"]) + "x no Jupiter Perps, até a meta de US$ " +
             format(TARGET_USD, ".0f") + " por dia. Deixe pelo menos US$ 110 em SOL na carteira. Se o BTC andar rápido contra as "
             "ordens, você pode perder a garantia toda. Não existe garantia de lucro.",
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
        send(chat, "⏸️ " + NAME[mode] + " desligada: não abre ordens novas." +
             (" As ordens abertas continuam até o +" + format(BASKET_TP_PCT * 100, ".1f") + "% do preço médio; toque em ❌ para fechar agora." if d.get("pos") else ""), keyboard())
    elif action.startswith("bfm_close_"):
        mode = action[10:]

        def go():
            with _LOCK:
                d = _load(mode)
                if not d.get("pos"):
                    send(chat, "Nenhuma ordem aberta na " + NAME[mode] + ".", keyboard())
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
