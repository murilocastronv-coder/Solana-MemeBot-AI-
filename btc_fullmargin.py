"""FULL MARGIN (owner, Nov 20): the owner's MT5 FullMargin strategy on Jupiter Perps - trading only SOL (BTCFM_ASSET).

Two separate accounts: DEMO (fake money, real prices, fees included) and REAL (the bot wallet on Jupiter Perps).
Each one starts every day (00:00 UTC) with US$ 100 (minimum capital), day and night:
- BUY (long) AND SELL (short). Reads the BTC 5-minute chart (score -9..+9);
- STRONG MOVE: BTC moved >= 1.5 ATR of the 1 h chart in the last 6 h and is on that side of the 1 h EMA50 ->
  trades MORE in that direction (score 3, full lot); otherwise trades MUCH LESS (score 7, at most 1 entry per
  hour, half lot). The first order of the day opens right away to the side of the reading;
- MT5 FullMargin_7_2 orders (owner, Nov 20), in % of the BTC price: 1st order with 30% of the account; "it will win"
  (+0.10% and the reading in favour) -> +1 order with 50% of the free capital; "it will come back" (-0.25%, the reading
  in favour and the last 5-minute candle turning) -> up to 4 orders more with the rest of the capital, each one 0.5%,
  0.75%, 1.0%, 1.25% beyond the previous; CLOSES ALL at +0.25% over the average price (2x the ~0.12% Jupiter fees, so
  there is profit after the fees); from US$ 500 the risk is cut by 50%;
- the day stops at the TARGET (US$ 300, like the MT5 robot) or at the LOSS LIMIT (50% = US$ 50);
  a protection closes everything before the liquidation price (60% of the way).
- ALWAYS IN (owner, Nov 20, BTCFM_ALWAYS_IN=true): as soon as the orders close it opens the next one (full lot) to the
  side of the strong move or of the chart reading, so it trades all the time until the daily target/limit.
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
# owner, Nov 21: the account has US$ 300 - it trades with US$ 100 and keeps US$ 200 in RESERVE; when the open orders
# are eating the margin (losing TOPUP_AT of the way to the liquidation) it ADDS margin (collateral) from the reserve
RESERVE_USD = float(os.getenv("BTCFM_RESERVE_USD", "200"))
START_TOTAL = CAPITAL_USD + RESERVE_USD
TARGET_PROFIT = TARGET_USD - CAPITAL_USD          # day target: +200 (the old "100 -> 300")
TOPUP_USD = 50.0                # margin added each time
TOPUP_AT = 0.35                 # ... when the orders lost 35% of the way to the liquidation (the guard closes at 60%)
BANK_EVERY = 300.0              # owner, Nov 21 (latest): every US$ 300 of profit in the day...
BANK_PUT = 200.0                # ... US$ 200 leave the trading money and go to the margin reserve (not traded)
# owner, Nov 20: the MT5 FullMargin_7_2 strategy in % of the BTC price (no pips on BTC). The Jupiter fees are ~0.12%
# of the position (open + close), so every exit is sized to leave profit AFTER the fees.
FIRST_PCT = 0.30                # 1st order: 30% of the account as collateral
WIN_ADD_PCT = 0.50              # "it will win" (profit + reading in favour): +1 order with 50% of the free capital
WIN_TRIGGER = 0.001             # ... once the orders are +0.10% (= the "+2 pips" of the MT5 robot)
LOSS_ADDS = 4                   # "it will come back": up to 4 orders more to kill the losing ones
LOSS_TRIGGER = 0.0025           # ... once the orders are -0.25% (= the "-5 pips")
LOSS_STEP = 0.005               # ... each one at least 0.5% beyond the previous order (= "10 pips")
LOSS_STEP_GROW = 0.5            # ... growing 50% each time: 0.5%, 0.75%, 1.0%, 1.25%
# owner, Nov 21 (latest): the 1st order alone closes at +1% over its price; once there are more orders (recovery /
# "it will win"), everything closes together at +5% over the average price
TP_ONE = 0.01
TP_MANY = 0.05
BASKET_TP_PCT = TP_ONE
SAFE_FROM, SAFE_CUT = 500.0, 0.5   # from US$ 500 the risk is cut by 50%
GRID_MAX = 1 + 1 + LOSS_ADDS    # at most 6 orders together
STRONG_ATR = 1.5                # strong move: >= 1.5 ATR(1 h) in 6 h, on that side of the 1 h EMA50
SCORE_STRONG, SCORE_WEAK = 3, 7
WEAK_EVERY_S = 3600             # (selective mode) no strong move: at most 1 entry per hour
WEAK_LOT = 0.5
ADD_SCORE = 4                   # the reading must point to the side of the orders with at least this strength
BT_ASSETS = {"BTC": ("BTCUSDT", "BTC-USD"), "SOL": ("SOLUSDT", "SOL-USD"), "ETH": ("ETHUSDT", "ETH-USD")}
ASSET = os.getenv("BTCFM_ASSET", "SOL").strip().upper()      # owner, Nov 20: demo and real trade only SOL
LABEL = {"BTC": "₿ BTC", "SOL": "◎ SOL", "ETH": "Ξ ETH"}.get(ASSET, ASSET)
ALWAYS_IN = os.getenv("BTCFM_ALWAYS_IN", "true").strip().lower() != "false"   # owner, Nov 20: always with orders open
LIQ_GUARD = 0.6                 # closes everything at 60% of the way to the liquidation price


# owner, Nov 21 (latest): backtest of the SAME strategy the SOL accounts run, but every order against ("vai voltar")
# only when SOL went 6% against the previous order - all of them 6% apart
GRID6_CFG = {"step": 0.06}


def risk_factor(capital, cf):
    """MT5 v4.33: the risk halves every `risk_step` of balance (200 -> 50%, 400 -> 25%...), never below 10%."""
    step = (cf or {}).get("risk_step")
    if not step:
        return 1.0
    return max(0.1, 0.5 ** int(capital // step))


def risk_cut(capital):
    """1.0 below US$ 500, 0.5 from US$ 500 (the risk is cut by half)."""
    return SAFE_CUT if capital >= SAFE_FROM else 1.0


def free_capital(d):
    """Account capital not yet used as collateral by the open orders."""
    return max(0.0, d["capital"] - (d.get("pos") or {}).get("collateral_usd", 0.0))


def first_collateral(capital):
    return round(capital * FIRST_PCT * risk_cut(capital), 2)


def loss_step(n_adds):
    """Distance (in % of the price) from the previous order for the next recovery order."""
    return LOSS_STEP * (1 + LOSS_STEP_GROW * n_adds)


LEVERAGES = (2, 3, 5, 10, 15, 20, 30, 40, 50)   # owner, Nov 20: max 50x
DEFAULT_LEVERAGE = 40            # owner, Nov 20: demo and real use 40x
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
NAME = {"demo": "🧪 CONTA DEMO", "real": "💰 CONTA REAL", "bt": "🧪 BACKTEST"}
CURVE_MAX = 500
BT_DAYS = 30
BT = {"running": False}

_LOCK = threading.RLock()
_http = requests.Session()
LAST = {"at": 0, "why": "", "price": None, "score": None, "reading": ""}


# ------------------------------------------------------------------ state (one file per account)
def _path(mode="demo"):
    import live_trade_state
    return live_trade_state._path().with_name("btc_fullmargin_" + mode + ".json")


def today(now=None):
    return time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))


BT_MEM = {}                     # backtest account kept in memory (no disk write per candle)


def _load(mode):
    if mode == "bt" and BT_MEM.get("d") is not None:
        return BT_MEM["d"]
    d = {"mode": mode, "on": False, "leverage": DEFAULT_LEVERAGE, "pos": None, "capital": CAPITAL_USD, "day": "",
         "reserve": RESERVE_USD, "injected": 0.0,
         "done_why": "", "wins": 0, "losses": 0, "trades": 0, "consec": 0, "pause_until": 0, "last_bar": 0,
         "hist": [], "days": []}
    p = _path(mode)
    if p.exists():
        try:
            d.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    d["mode"] = mode
    if mode in MODES and d.get("asset_v") != ASSET:
        # owner, Nov 20: the robot now trades only ASSET (SOL) - both accounts start again from US$ 100, clean results.
        # An order still open on the old asset is kept aside: the real one is closed on-chain by the next cycle.
        legacy = d.get("pos") if d.get("pos") and d["pos"].get("asset", "BTC") != ASSET else None
        d.update(asset_v=ASSET, pos=None, capital=CAPITAL_USD, day="", done_why="", wins=0, losses=0, trades=0,
                 consec=0, pause_until=0, last_bar=0, hist=[], days=[], curve=[])
        d.pop("total", None); d.pop("last_close", None)
        if legacy and mode == "real":
            d["legacy_pos"] = legacy
    if mode in MODES and d.get("tp_v") != 3:
        # owner, Nov 21: targets +1% (1 order) / +5% (more orders) over the average price; no optimizer variant
        d.update(tp_v=3, cfg=None, cfg_name=None)
        if d.get("pos"):
            d["pos"].pop("tp", None)
            d["pos"].pop("half", None)
    if d.get("lev_v") != 40:                 # owner, Nov 20: both accounts switch to 40x once (the buttons still work)
        d["leverage"], d["lev_v"] = DEFAULT_LEVERAGE, 40
    return d


def _save(d):
    if d.get("mode") == "bt" and BT_MEM.get("on"):
        BT_MEM["d"] = d
        return
    p = _path(d["mode"])
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".btcfm.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def new_day(d, now=None):
    """Starts the day: capital back to US$ 100, counters reset, target/limit unlocked."""
    if d["day"]:
        d["days"] = (d.get("days") or [])[-(399 if d["mode"] == "bt" else 29):] + [{"day": d["day"], "end": round(d["capital"], 2),
                                                     "result": round(d["capital"] + d.get("reserve", 0.0) - START_TOTAL, 2),
                                                     "banked": round(d.get("banked", 0.0), 2),
                                                     "why": d.get("done_why") or ""}]
    d.update(day=today(now), capital=CAPITAL_USD, reserve=RESERVE_USD, injected=0.0, banked=0.0, last_bank=0.0, done_why="", wins=0, losses=0, trades=0, consec=0,
             pause_until=0, last_bar=0, last_entry=0)


# ------------------------------------------------------------------ market data
def _get(url, params=None):
    r = _http.get(url, params=params, timeout=12, headers={"User-Agent": "BTCFullMargin/1.0"})
    r.raise_for_status()
    return r.json()


def klines_5m(limit=1000):
    """5-minute candles of the traded asset (ASSET), oldest first (the last one is still forming)."""
    binance_sym, coinbase_sym = BT_ASSETS[ASSET]
    for base in ("https://api.binance.com", "https://api.binance.us"):
        try:
            rows = _get(base + "/api/v3/klines", {"symbol": binance_sym, "interval": "5m", "limit": limit})
            return [{"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
                    for r in rows]
        except Exception:
            continue
    now = int(time.time()) // TF * TF + TF
    rows = {}
    for k in range(4):                      # Coinbase: 300 candles per call
        end, start = now - k * 300 * TF, now - (k + 1) * 300 * TF
        for r in _get("https://api.exchange.coinbase.com/products/" + coinbase_sym + "/candles",
                      {"granularity": TF, "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
                       "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end))}):
            rows[int(r[0])] = {"t": int(r[0]), "l": float(r[1]), "h": float(r[2]), "o": float(r[3]), "c": float(r[4])}
    return [rows[t] for t in sorted(rows)]


def price_now(asset=None):
    """Current price of the traded asset (light call for the live view)."""
    binance_sym, coinbase_sym = BT_ASSETS[asset or ASSET]
    for url in ("https://api.binance.com/api/v3/ticker/price", "https://api.binance.us/api/v3/ticker/price"):
        try:
            return float(_get(url, {"symbol": binance_sym})["price"])
        except Exception:
            continue
    return float(_get("https://api.exchange.coinbase.com/products/" + coinbase_sym + "/ticker")["price"])


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


def effective_lev(pos):
    """Real leverage of the open orders together (Jupiter merges them into one position): size / collateral."""
    col = pos.get("collateral_usd") or 0
    return max(1.0, pos["size_usd"] / col) if col > 0 else float(pos.get("lev") or DEFAULT_LEVERAGE)


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


def px(p):
    """Price with cents below $1,000 (SOL), without above (BTC)."""
    return format(p, ",.2f") if p < 1000 else format(p, ",.0f")


def basket_tp(pos):
    """+1% with the 1st order alone, +5% over the average price once there are more orders (a backtest strategy
    variant can set its own target in pos["tp"])."""
    if pos.get("tp"):
        return pos["tp"]
    return TP_ONE if pos.get("n", 1) <= 1 else TP_MANY


def tp_text(pos):
    return ("fecha tudo em $" + px(tp_price(pos)) + " (+" + format(basket_tp(pos) * 100, ".0f") + "% sobre " +
            ("a entrada" if pos.get("n", 1) <= 1 else "o preço médio das " + str(pos.get("n", 1)) + " ordens") + ")")


def tp_price(pos):
    return pos["entry"] * (1 + basket_tp(pos) * _sgn(pos))


def next_grid_price(pos):
    """Price of the next recovery order (the price must be that far against the last order)."""
    step = pos.get("step") or loss_step(pos.get("loss_adds", 0))
    return pos.get("last", pos["entry"]) * (1 - step * _sgn(pos))


def side_name(pos_or_side):
    side = pos_or_side if isinstance(pos_or_side, str) else pos_or_side.get("side", "short")
    return "COMPRA (long)" if side == "long" else "VENDA (short)"


# ------------------------------------------------------------------ money
def sol_usd():
    import copy_trader as ct
    return float(ct._sol_usd())


def total_value(d, price=None, now=None):
    """Whole account: the trading part (with the open orders) + the reserve not used yet."""
    return equity(d, price, now) + d.get("reserve", 0.0)


def day_result(d, price=None, now=None):
    return total_value(d, price, now) - START_TOTAL


def bank_profit(d):
    """Every US$ 300 of profit in the day, US$ 200 leave the trading money and go to the reserve, which is only used as
    margin when the orders need it. Returns the amount moved now."""
    moved = 0.0
    while (d["capital"] + d.get("reserve", 0.0) - START_TOTAL >= (round(d.get("banked", 0.0) / BANK_PUT) + 1) * BANK_EVERY and
           d["capital"] - BANK_PUT >= CAPITAL_USD * 0.5):
        d["capital"] -= BANK_PUT
        d["reserve"] = round(d.get("reserve", 0.0) + BANK_PUT, 2)
        d["banked"] = round(d.get("banked", 0.0) + BANK_PUT, 2)
        moved += BANK_PUT
    return moved


def topup(d, price, now=None):
    """Adds margin (collateral, no size) to the open orders from the reserve. Returns the amount added."""
    pos = d.get("pos")
    amt = round(min(TOPUP_USD, d.get("reserve", 0.0)), 2)
    if not pos or amt < 2:
        return 0.0
    sig = None
    if d["mode"] == "real":
        import jupiter_perps as jp
        jp.ensure_usdc(amt, sol_usd())
        out = jp.add_collateral(pos.get("side", "short"), amt, price, asset=pos.get("asset", "BTC"))
        sig = out.get("signature")
    d["reserve"] = round(d.get("reserve", 0.0) - amt, 2)
    d["capital"] += amt
    d["injected"] = round(d.get("injected", 0.0) + amt, 2)
    pos["collateral_usd"] += amt
    pos["topups"] = pos.get("topups", 0) + 1
    if sig:
        pos["signature"] = sig
    return amt


def equity(d, price=None, now=None):
    """Account value: the day's capital (100 + realized P/L) + the open short's P/L."""
    v = d["capital"]
    if d.get("pos") and price:
        v += pnl_usd(d["pos"], price, now)
    return v


def _open(d, price, side, why, col=None, now=None, lev=None):
    """Opens the first order or ADDS one (same side) with `col` US$ of collateral. Returns the position."""
    lev = lev or d["leverage"]
    col = round(first_collateral(d["capital"]) if col is None else col, 2)
    if col < 2:
        raise RuntimeError("saldo da conta baixo demais para mais uma ordem")
    pos = d.get("pos")
    sig = None
    onchain_avg = None
    if d["mode"] == "real":
        import jupiter_perps as jp
        jp.ensure_usdc(col, sol_usd())
        out = jp.open_position(side, col, lev, price, min_size=(pos or {}).get("size_usd", 0.0), asset=ASSET)
        onchain_avg = float(out.get("price") or 0) or None
        sig = out.get("signature")
    add = col * lev
    t = int(now or time.time())
    if pos:
        size = pos["size_usd"] + add
        entry = size / (pos["size_usd"] / pos["entry"] + add / price)        # average price of the USD notional
        pos.update(size_usd=size, entry=onchain_avg or entry, collateral_usd=pos["collateral_usd"] + col,
                   n=pos.get("n", 1) + 1, last=price, lev=max(pos.get("lev", lev), lev))
    else:
        pos = {"side": side, "entry": onchain_avg or price, "size_usd": add, "collateral_usd": col, "n": 1,
               "last": price, "opened": t, "why": why, "win_added": False, "loss_adds": 0, "lev": lev, "asset": ASSET,
               "tp": (CFG.get(d["mode"]) or {}).get("tp"), "step": (CFG.get(d["mode"]) or {}).get("step")}
        d["pos"] = pos
    if sig:
        pos["signature"] = sig
    d["trades"] += 1
    d["last_entry"] = t
    return pos


def _close(d, price, why, now=None, frac=1.0, half=True):
    """Closes the orders (frac < 1: only that part, e.g. half at the 1st target). The result after the fees goes into
    the account; lifetime totals and the curve are updated."""
    full = d["pos"]
    d["last_bank"] = 0.0
    part = frac < 1.0
    pos = dict(full, size_usd=full["size_usd"] * frac, collateral_usd=full["collateral_usd"] * frac) if part else full
    sig = None
    tnow = int(now or time.time())
    gross, fees, net = breakdown(pos, price, now)
    if d["mode"] == "real":
        import jupiter_perps as jp
        from solders.pubkey import Pubkey
        from bot_wallet_service import get_address
        owner = Pubkey.from_string(get_address())
        before = jp.usdc_balance(owner)
        if part:
            out = jp.reduce_position(pos.get("side", "short"), pos["size_usd"], pos["collateral_usd"], price,
                                     asset=pos.get("asset", "BTC"))
        else:
            out = jp.close_position(pos.get("side", "short"), price, asset=pos.get("asset", "BTC"))
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
            d["pause_until"] = tnow + PAUSE_S
    tot = d.setdefault("total", {"trades": 0, "wins": 0, "losses": 0, "gross": 0.0, "fees": 0.0, "net": 0.0,
                                 "best": 0.0, "worst": 0.0, "since": tnow})
    tot["trades"] += 1
    tot["wins" if net >= 0 else "losses"] += 1
    tot["gross"] = round(tot["gross"] + gross, 4)
    tot["fees"] = round(tot["fees"] + fees, 4)
    tot["net"] = round(tot["net"] + net, 4)
    tot["best"] = max(tot["best"], net)
    tot["worst"] = min(tot["worst"], net)
    d["curve"] = (d.get("curve") or [])[-(CURVE_MAX - 1):] + [[tnow, round(tot["net"], 2)]]
    d["last_close"] = {"gross": round(gross, 2), "fees": round(fees, 2), "net": round(net, 2)}
    d["hist"] = (d.get("hist") or [])[-(199 if d["mode"] == "bt" else 49):] + [{"side": pos.get("side", "short"), "n": pos.get("n", 1),
                                                 "entry": pos["entry"], "exit": price, "pnl": round(net, 2),
                                                 "gross": round(gross, 2), "fees": round(fees, 2),
                                                 "why": why, "t": tnow,
                                                 "held_h": round((tnow - pos.get("opened", tnow)) / 3600, 1)}]
    if part:
        full.update(size_usd=full["size_usd"] - pos["size_usd"], collateral_usd=full["collateral_usd"] - pos["collateral_usd"])
        if half:
            full["half"] = True
    else:
        d["pos"] = None
        if not (CFG.get(d["mode"]) or {}).get("no_reserve"):
            d["last_bank"] = bank_profit(d)
    return net, sig


def close_text(d, why, price, entry, pos=None):
    """Notification of closed orders: gross, fees, net, account balance and the total so far."""
    pos = pos or (d.get("hist") or [{}])[-1]
    lc, tot = d["last_close"], d["total"]
    head = ("✅ LUCRO " if lc["net"] >= 0 else "❌ PREJUÍZO ") + format(lc["net"], "+.2f") + " USD (depois das taxas)"
    return (LABEL + " FULL MARGIN " + NAME[d["mode"]] + " — " + why + "\n" + head +
            "\n" + side_name(pos.get("side", "short")) + " · " + str(pos.get("n", 1)) + " ordem(ns) · preço médio $" +
            px(entry) + " → saída $" + px(price) +
            "\n💵 Bruto " + format(lc["gross"], "+.2f") + " · taxas −" + format(lc["fees"], ".2f") +
            " · líquido " + format(lc["net"], "+.2f") +
            "\n💼 Saldo total: US$ " + format(d["capital"] + d.get("reserve", 0.0), ".2f") + " (operando " +
            format(d["capital"], ".2f") + " + reserva " + format(d.get("reserve", 0.0), ".2f") + " · hoje " +
            format(d["capital"] + d.get("reserve", 0.0) - START_TOTAL, "+.2f") + ")" +
            ("\n🏦 Guardei US$ " + format(d["last_bank"], ".0f") + " do lucro na reserva (vira margem) · reserva US$ " +
             format(d.get("reserve", 0.0), ".2f") if d.get("last_bank") else "") +
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
        if mode in MODES:
            CFG[mode] = d.get("cfg") or None             # strategy variant chosen with the optimizer (None = default)
        if mode == "real" and d.get("legacy_pos"):        # an order still open on the old asset: close it on-chain
            lp = d["legacy_pos"]
            try:
                import jupiter_perps as jp
                old = lp.get("asset", "BTC")
                jp.close_position(lp.get("side", "short"), price_now(old), asset=old)
                try:
                    jp.usdc_to_sol()
                except Exception:
                    pass
                d.pop("legacy_pos", None)
                _save(d)
                send(chat, "🔁 Conta REAL: fechei a ordem antiga de " + old + " (o robô agora opera só " + ASSET + ").")
            except Exception as exc:
                logging.warning("full margin: legacy close failed: %s", type(exc).__name__)
                return "fechando ordem antiga"
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
                send(chat, "🌅 " + ASSET + " FULL MARGIN " + tag + ": novo dia, de volta a US$ " + format(CAPITAL_USD, ".0f") +
                     " operando + US$ " + format(RESERVE_USD, ".0f") + " de reserva · meta +" + format(TARGET_PROFIT, ".0f") + ". Ontem: " + format(d["days"][-1]["result"], "+.2f") + " USD.")
        # daily target / loss limit
        res = day_result(d, price, now)
        hit = res >= TARGET_PROFIT
        if d["on"] and not d["done_why"] and (hit or res <= -START_TOTAL * MAX_LOSS):
            if pos:
                _close(d, price, "🎯 META DO DIA" if hit else "🛑 LIMITE DO DIA", now)
                send(chat, close_text(d, "🎯 META DO DIA" if hit else "🛑 LIMITE DO DIA", price, pos["entry"], pos))
            d["done_why"] = "META DO DIA BATIDA" if hit else "LIMITE DE PERDA DO DIA"
            _save(d)
            send(chat, ("🎯 " + ASSET + " FULL MARGIN " + tag + ": META DO DIA BATIDA! " if hit else
                        "🛑 " + ASSET + " FULL MARGIN " + tag + ": LIMITE DE PERDA DO DIA. ") +
                 "US$ " + format(START_TOTAL, ".0f") + " → US$ " + format(d["capital"] + d.get("reserve", 0.0), ".2f") +
                 ". Parou de operar hoje; volta sozinho amanhã (00:00 UTC) com US$ " + format(CAPITAL_USD, ".0f") +
                 " + US$ " + format(RESERVE_USD, ".0f") + " de reserva.")
            return d["done_why"]
        if pos:
            bp = basket_pct(pos, price)
            cf = CFG.get(mode) or {}
            if d["on"] and not cf.get("no_reserve") and d.get("reserve", 0.0) >= 2 and bp <= -TOPUP_AT / effective_lev(pos):
                try:
                    amt = topup(d, price, now)
                except Exception as exc:
                    amt = 0.0
                    send(chat, "⛔ " + ASSET + " FULL MARGIN " + tag + ": não consegui adicionar margem (" + str(exc)[:160] + ").")
                if amt:
                    _save(d)
                    send(chat, "🛟 " + ASSET + " FULL MARGIN " + tag + " — a margem estava acabando (" + format(bp * 100, "+.2f") +
                         "% do preço médio): ADICIONEI US$ " + format(amt, ".2f") + " da reserva · garantia agora US$ " +
                         format(pos["collateral_usd"], ".2f") + " (" + format(effective_lev(pos), ".1f") + "x) · reserva US$ " +
                         format(d["reserve"], ".2f") + _tx(pos.get("signature")))
            why = ""
            cf = CFG.get(mode) or {}
            if pos.get("tp") and bp >= pos["tp"]:
                why = "🎯 +" + format(bp * 100, ".2f") + "% SOBRE O PREÇO MÉDIO (alvo +" + format(pos["tp"] * 100, ".1f") + "%)"
            elif bp >= basket_tp(pos):
                why = ("🎯 +" + format(bp * 100, ".2f") + "% SOBRE A ENTRADA (1 ordem: alvo +" + format(TP_ONE * 100, ".0f") + "%)"
                       if pos.get("n", 1) <= 1 else
                       "🎯 +" + format(bp * 100, ".2f") + "% SOBRE O PREÇO MÉDIO (" + str(pos.get("n", 1)) + " ordens: alvo +" +
                       format(TP_MANY * 100, ".0f") + "%)")
            elif bp <= -LIQ_GUARD / effective_lev(pos):
                why = "🛑 PROTEÇÃO (perto da liquidação)"
            elif cf.get("stop") and bp <= -cf["stop"]:
                why = "🛑 STOP DO CONJUNTO (-" + format(cf["stop"] * 100, ".1f") + "%)"
            elif cf.get("hold_h") and now - pos.get("opened", now) >= cf["hold_h"] * 3600:
                why = "⏱️ TEMPO MÁXIMO (" + str(cf["hold_h"]) + " h)"
            if why:
                pnl, sig = _close(d, price, why, now)
                _save(d)
                send(chat, close_text(d, why, price, pos["entry"], pos) + _tx(sig))
                return "fechou"
            if cf.get("cut_pct"):                        # MT5 v4.33: losing -> cut part of the size at each bounce
                pos["worst"] = min(pos.get("worst", bp), bp)
                if (pos.get("cuts", 0) < cf["cut_max"] and pos["worst"] <= -cf["cut_from"] and bp < 0 and
                        bp >= pos["worst"] + cf["cut_retrace"]):
                    pos["cuts"] = pos.get("cuts", 0) + 1
                    why = "✂️ CORTE DE PERDA " + str(pos["cuts"]) + "/" + str(cf["cut_max"]) + " no repique (" + \
                          format(bp * 100, "+.2f") + "%; pior " + format(pos["worst"] * 100, "+.2f") + "%)"
                    pos["worst"] = bp
                    pnl, sig = _close(d, price, why, now, frac=cf["cut_pct"], half=False)
                    _save(d)
                    send(chat, close_text(d, why, price, pos["entry"], pos) + _tx(sig))
                    return "corte"
            # MT5 FullMargin_7_2 adds, once per closed 5-minute candle, with the chart reading in favour
            if d["on"] and not d["done_why"] and not pos.get("half") and closed[-1]["t"] != d.get("last_bar"):
                d["last_bar"] = closed[-1]["t"]
                score, _a, reading = read_chart(closed)
                sg = _sgn(pos)
                in_favor = score * sg >= ADD_SCORE
                turning = (closed[-1]["c"] - closed[-1]["o"]) * sg > 0
                add, label, plan = 0.0, "", LEV_PLAN.get(mode)
                add_lev = None
                rf = risk_factor(d["capital"], cf)
                room = d["capital"] * cf.get("win_max", 0) * rf - pos["collateral_usd"]
                win_add = min(d["capital"] * cf.get("win_pct", 0) * rf, room, free_capital(d))
                if cf.get("win_max"):                    # MT5 v4.33: one more order per candle while winning, up to 65%
                    if pos.get("loss_adds", 0) == 0 and bp >= cf["win_trigger"] and in_favor and win_add >= 2:
                        add, label = win_add, "VAI GANHAR " + str(pos.get("win_adds", 0) + 1)
                        pos["win_adds"] = pos.get("win_adds", 0) + 1
                elif not pos.get("win_added") and bp >= WIN_TRIGGER and in_favor:
                    add_lev = plan["win"] if plan else None
                    add, label = free_capital(d) * WIN_ADD_PCT * risk_cut(d["capital"]), "VAI GANHAR (+50%)"
                    pos["win_added"] = True
                elif (CFG.get(mode) or {}).get("adds", True) and \
                        pos.get("loss_adds", 0) < LOSS_ADDS and bp <= -LOSS_TRIGGER and in_favor and turning and \
                        (price - next_grid_price(pos)) * sg <= 0:
                    left = LOSS_ADDS - pos.get("loss_adds", 0)
                    add_lev = plan["loss"][min(pos.get("loss_adds", 0), len(plan["loss"]) - 1)] if plan else None
                    add = free_capital(d) / left * risk_cut(d["capital"])
                    pos["loss_adds"] = pos.get("loss_adds", 0) + 1
                    label = "VAI VOLTAR " + str(pos["loss_adds"]) + "/" + str(LOSS_ADDS)
                if label:
                    try:
                        if add_lev:
                            label += " " + str(add_lev) + "x"
                        _open(d, price, pos["side"], label + ", " + reading, add, now, add_lev)
                    except Exception as exc:
                        _save(d)
                        send(chat, "⛔ " + ASSET + " FULL MARGIN " + tag + ": não abri a ordem " + label + " (" + str(exc)[:160] + ").")
                        return "erro"
                    _save(d)
                    send(chat, "➕ " + ASSET + " FULL MARGIN " + tag + " — " + label + ": ordem " + str(pos["n"]) + " (" + side_name(pos) +
                         ") em $" + px(price) + " · garantia US$ " + format(add, ".2f") +
                         "\nPreço médio agora $" + px(pos["entry"]) + " · 🎯 " + tp_text(pos) + _tx(pos.get("signature")))
                    return "reforço"
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
        if bar == d["last_bar"] and not first and not ALWAYS_IN:
            return "esperando candle"
        d["last_bar"] = bar
        score, a, reading = read_chart(closed)
        sdir, move = strong_move(closed)
        LAST.update(score=score, reading=reading, strong=sdir, move=round(move, 2))
        cf = CFG.get(mode) or {}
        if cf.get("entry") == "score":                 # MT5 v4.33: reading of the chart >= 4 to one side
            if abs(score) < ADD_SCORE:
                _save(d)
                return "sem sinal (" + reading + ")"
            side_dir, factor = (1 if score > 0 else -1), risk_factor(d["capital"], cf)
        elif cf.get("entry") == "trend":                 # optimizer variant: only with a strong move, to its side
            if not sdir:
                _save(d)
                return "sem tendência forte"
            side_dir, factor = sdir, 1.0
        elif ALWAYS_IN:
            # owner, Nov 20: ALWAYS with orders open (maximum trades): right after closing it opens the next one,
            # to the side of the strong move, or else to the side of the chart reading; always the full lot
            side_dir, factor = (sdir or (1 if score >= 0 else -1)), 1.0
        elif first:
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
        if ONLY_SIDE.get(mode) == "short":            # backtest "só venda": never buys
            side_dir = -1
        side = "long" if side_dir == 1 else "short"
        label = {1: " [ALTA FORTE]", -1: " [QUEDA FORTE]"}.get(sdir, "")
        why = ("início do dia, " if first else "") + reading + label
        try:
            pos = _open(d, price, side, why, first_collateral(d["capital"]) * factor, now,
                        (LEV_PLAN.get(mode) or {}).get("first"))
        except Exception as exc:
            _save(d)
            send(chat, "⛔ " + ASSET + " FULL MARGIN " + tag + ": não abri a ordem (" + str(exc)[:160] + ").")
            return "erro"
        _save(d)
        send(chat, LABEL + " FULL MARGIN " + tag + " — " + side_name(side) + " aberta (1ª ordem, 30% da conta)\n" + why +
             "\nEntrada $" + px(pos["entry"]) + " · garantia US$ " + format(pos["collateral_usd"], ".2f") + " · " +
             str(d["leverage"]) + "x (posição US$ " + format(pos["size_usd"], ",.0f") + ")" +
             "\n🎯 " + tp_text(pos) + " · ➕ recuperação se for a $" + px(next_grid_price(pos)) + " e o gráfico virar" + _tx(pos.get("signature")))
        return "abriu"


def cycle_all(send, chat, now=None):
    """Both accounts with the same candles (one download)."""
    active = [m for m in MODES if (lambda d: d["on"] or d.get("pos"))(_load(m))]
    if not active:
        return {}
    cs = klines_5m()
    return {m: cycle(m, send, chat, now=now, klines=lambda: cs) for m in active}


# ------------------------------------------------------------------ backtest (owner, Nov 20)
ONLY_SIDE = {}                  # mode -> "short" (backtest that only sells)
BT_CACHE = {}                   # chart readings of the candles (shared by the optimizer's runs over the same data)
LEV_PLAN = {}                   # mode -> dynamic leverage plan (backtest)
CFG = {}                        # mode -> strategy variant (optimizer): tp, stop, hold_h, entry ("always"/"trend"), adds
DYN_PLAN = {"first": 15, "loss": [20, 25, 30, 35], "win": 30}   # owner, Nov 20: 15x, more when losing, more when winning


def klines_history(days=BT_DAYS, asset="BTC"):
    """5-minute candles (BTC, SOL or ETH) of the last `days` days (+ 3 days of warm-up), oldest first."""
    binance_sym, coinbase_sym = BT_ASSETS[asset]
    end = int(time.time()) // TF * TF
    start = end - (days + 3) * 86400
    rows = {}
    for base in ("https://api.binance.com", "https://api.binance.us"):
        try:
            t = start
            while t < end:
                part = _get(base + "/api/v3/klines", {"symbol": binance_sym, "interval": "5m", "startTime": t * 1000, "limit": 1000})
                if not part:
                    break
                for r in part:
                    rows[int(r[0]) // 1000] = {"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                                               "c": float(r[4])}
                t = int(part[-1][0]) // 1000 + TF
            if len(rows) > (days + 2) * 288:
                return [rows[k] for k in sorted(rows)]
        except Exception:
            rows = {}
            continue
    t = start
    while t < end:                                   # Coinbase: 300 candles per call
        e = min(end, t + 300 * TF)
        for r in _get("https://api.exchange.coinbase.com/products/" + coinbase_sym + "/candles",
                      {"granularity": TF, "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
                       "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e))}):
            rows[int(r[0])] = {"t": int(r[0]), "l": float(r[1]), "h": float(r[2]), "o": float(r[3]), "c": float(r[4])}
        t = e
    return [rows[k] for k in sorted(rows)]


def backtest(cs, leverage=DEFAULT_LEVERAGE, progress=None, only=None, plan=None, cfg=None):  # noqa: C901
    """Runs the robot's own cycle() over the candles, as a demo account that started 3 days into the data.
    Every 5-minute candle: it closes at the candle's close; the +0.25% close is also checked with the candle's
    high/low (the order touched the price). Fees are the same as the demo (open/close + borrow per hour)."""
    global CURVE_MAX
    WARM = 800
    if len(cs) < WARM + 300:
        raise RuntimeError("poucos candles para o backtest")
    BT_MEM.clear()
    BT_MEM["on"] = True                               # the backtest account lives in memory
    d = _load("bt")
    d.update(on=True, leverage=leverage)
    ONLY_SIDE["bt"] = only
    LEV_PLAN["bt"] = plan
    CFG["bt"] = cfg
    _save(d)
    old_curve, CURVE_MAX = CURVE_MAX, 100000
    msgs = []
    global read_chart, strong_move
    real_rc, real_sm = read_chart, strong_move
    rc_cache, sm_cache = BT_CACHE.setdefault("rc", {}), BT_CACHE.setdefault("sm", {})

    def cached_rc(closed):
        k = (closed[-1]["t"], closed[-1]["c"])
        if k not in rc_cache:
            rc_cache[k] = real_rc(closed)
        return rc_cache[k]

    def cached_sm(closed):
        k = (closed[-1]["t"], closed[-1]["c"])
        if k not in sm_cache:
            sm_cache[k] = real_sm(closed)
        return sm_cache[k]
    read_chart, strong_move = cached_rc, cached_sm
    try:
        for i in range(WARM, len(cs)):
            now = cs[i]["t"] + TF
            d = _load("bt")
            pos = d.get("pos")
            if pos:                                   # the +0.25% close touched inside the candle
                tp = tp_price(pos)
                touched = cs[i]["l"] <= tp if pos.get("side") == "short" else cs[i]["h"] >= tp
                if touched:
                    win = cs[max(0, i - WARM + 1):i] + [dict(cs[i], c=tp)]
                    cycle("bt", lambda c, t, k=None: msgs.append(t), "bt", now=now,
                          klines=lambda w=win: w + [dict(w[-1], t=w[-1]["t"] + TF)])
                    continue
            win = cs[max(0, i - WARM + 1):i + 1]
            cycle("bt", lambda c, t, k=None: msgs.append(t), "bt", now=now,
                  klines=lambda w=win: w + [dict(w[-1], t=w[-1]["t"] + TF, o=w[-1]["c"], h=w[-1]["c"], l=w[-1]["c"])])
            if progress and i % 2000 == 0:
                progress(i - WARM, len(cs) - WARM)
        d = _load("bt")
        if d.get("pos"):                              # still open at the end: closed at the last price
            _close(d, cs[-1]["c"], "fim do backtest", cs[-1]["t"] + TF)
        new_day(d, cs[-1]["t"] + 86400 * 2)          # records the last day
        _save(d)
    finally:
        CURVE_MAX = old_curve
        read_chart, strong_move = real_rc, real_sm
        BT_MEM.clear()
    return d


def backtest_text(d, leverage, span_days=BT_DAYS, asset="BTC"):
    days = d.get("days") or []
    tt = d.get("total") or {}
    n = tt.get("trades", 0)
    total = sum(x["result"] for x in days)
    hit = sum(1 for x in days if "META" in (x.get("why") or ""))
    lim = sum(1 for x in days if "LIMITE" in (x.get("why") or ""))
    pos_days = sum(1 for x in days if x["result"] > 0)
    hist = d.get("hist") or []
    held = max((h.get("held_h", 0) for h in hist), default=0)
    L = ["🧪 BACKTEST " + str(span_days) + " DIAS — " + asset + " (estratégia atual do Full Margin, " + str(leverage) + "x)",
         "Cada dia: US$ " + format(START_TOTAL, ".0f") + " = opera com US$ " + format(CAPITAL_USD, ".0f") + " + US$ " +
         format(RESERVE_USD, ".0f") + " de reserva (vira margem quando a margem acaba; a cada US$ " + format(BANK_EVERY, ".0f") +
         " de lucro, US$ " + format(BANK_PUT, ".0f") + " vão para a reserva) · meta +" + format(TARGET_PROFIT, ".0f") +
         " · limite −" + format(START_TOTAL * MAX_LOSS, ".0f") + ". Candles reais de 5 min, taxas da Jupiter incluídas.",
         "",
         ("✅ LUCROU " if total > 0 else "❌ PERDEU ") + format(total, "+.2f") + " USD em " + str(span_days) + " dias (soma dos dias)",
         "📅 Dias no lucro " + str(pos_days) + " de " + str(len(days)) + " · 🎯 meta batida " + str(hit) + " · 🛑 limite " + str(lim),
         "📊 Fechamentos " + str(n) + (" · no lucro " + format(tt.get("wins", 0) / n * 100, ".0f") + "%" if n else "") +
         " · taxas pagas " + format(tt.get("fees", 0), ".2f") + " USD",
         "🏆 Melhor fechamento " + format(tt.get("best", 0), "+.2f") + " · pior " + format(tt.get("worst", 0), "+.2f"),
         "⏱️ Maior tempo com ordens abertas: " + format(held, ".0f") + " h" +
         (" (sem stop por ordem: ficou esperando o preço voltar e o dia não virou nesse tempo)" if held > 24 else "")]
    if days:
        best = max(days, key=lambda x: x["result"])
        worst = min(days, key=lambda x: x["result"])
        L.append("📈 Melhor dia " + best["day"][5:] + " " + format(best["result"], "+.2f") + " · 📉 pior dia " +
                 worst["day"][5:] + " " + format(worst["result"], "+.2f"))
        L.append("")
        L.append("Contas fechadas (" + str(len(days)) + ")" + (", últimas 40" if len(days) > 40 else "") + ": " +
                 " · ".join(x["day"][5:] + " " + format(x["result"], "+.0f") for x in days[-40:]))
        months = {}
        for x in days:
            months[x["day"][:7]] = months.get(x["day"][:7], 0.0) + x["result"]
        if len(months) > 1:
            L.append("Por mês: " + " · ".join(k[5:] + "/" + k[2:4] + " " + format(v, "+.2f") for k, v in sorted(months.items())))
    L.append("")
    L.append("⚠️ O passado não garante o futuro: é a mesma regra do robô, mas o mês que vem pode ser bem diferente.")
    return "\n".join(L)


def backtest_png(d, days=BT_DAYS, asset="BTC"):
    import demo_report
    curve = [[t, CAPITAL_USD + v] for t, v in (d.get("curve") or [])]
    if not curve:
        return None
    curve.insert(0, [curve[0][0] - 60, CAPITAL_USD])
    color = {"BTC": (255, 196, 0), "SOL": (170, 110, 255), "ETH": (120, 170, 255)}.get(asset, (255, 196, 0))
    return demo_report.chart_png([("Backtest " + asset + " " + str(days) + " dias (resultado acumulado)", CAPITAL_USD, curve, color)])


def run_backtest(send, chat, leverage, days=BT_DAYS, asset="BTC", only=None, plan=None, cfg=None):
    if BT["running"]:
        send(chat, "⏳ O backtest já está rodando, espera o resultado.")
        return
    BT["running"] = True
    try:
        send(chat, "🧪 Backtest de " + str(days) + " dias do " + asset + " começou: baixando os candles de 5 min e rodando a estratégia "
             "atual (" + str(leverage) + "x). Leva uns " + ("2–5" if days <= 30 else "5–15" if days <= 120 else "15–30") + " minutos. Nenhuma ordem real.")
        cs = klines_history(days, asset)
        keep = dict(LAST)                             # the robot screen keeps showing its own price
        try:
            d = backtest(cs, leverage, only=only, plan=plan, cfg=cfg)
        finally:
            LAST.clear()
            LAST.update(keep)
        try:
            png = backtest_png(d, days, asset)
            if png and PHOTO:
                PHOTO(chat, png, "📈 Backtest " + asset + " " + str(days) + " dias: resultado acumulado (acima da linha fraca = lucro)")
        except Exception as exc:
            logging.warning("btc backtest chart: %s", type(exc).__name__)
        span = max(1, round((cs[-1]["t"] - cs[800]["t"]) / 86400))
        tag = asset + (" SÓ VENDA" if only == "short" else "") + (" · ORDENS CONTRA A CADA " + format(cfg["step"] * 100, ".0f") +
                                                                   "%" if cfg and cfg.get("step") else "")
        if plan:
            tag += " · ALAVANCAGEM DINÂMICA " + str(plan["first"]) + "x → " + "/".join(str(x) for x in plan["loss"]) + \
                   "x perdendo, " + str(plan["win"]) + "x ganhando"
        send(chat, backtest_text(d, plan["first"] if plan else leverage, span, tag), keyboard())
    except Exception as exc:
        send(chat, "⛔ Backtest: " + str(exc)[:200], keyboard())
    finally:
        BT["running"] = False
        BT_CACHE.clear()                              # 1 year = ~105k candles: free the chart readings (512 MB server)


# ------------------------------------------------------------------ optimizer (owner, Nov 20)
VARIANTS = [   # (name, leverage, config)
    ("Atual (sem stop)", 15, {}),
    ("Atual 5x", 5, {}),
    ("Stop 2% · 24h · alvo 0,5%", 10, {"stop": 0.02, "hold_h": 24, "tp": 0.005}),
    ("Stop 2% · 24h · alvo 0,5% · 5x", 5, {"stop": 0.02, "hold_h": 24, "tp": 0.005}),
    ("Tendência · stop 2% · 24h · alvo 1%", 10, {"entry": "trend", "stop": 0.02, "hold_h": 24, "tp": 0.01}),
    ("Tendência · stop 2% · 24h · alvo 1% · 5x", 5, {"entry": "trend", "stop": 0.02, "hold_h": 24, "tp": 0.01}),
    ("Tendência · stop 1,5% · 12h · alvo 0,75%", 10, {"entry": "trend", "stop": 0.015, "hold_h": 12, "tp": 0.0075}),
    ("Tendência · stop 3% · 48h · alvo 1,5%", 5, {"entry": "trend", "stop": 0.03, "hold_h": 48, "tp": 0.015}),
    ("Tendência · sem recuperação · stop 1% · alvo 2%", 10, {"entry": "trend", "stop": 0.01, "hold_h": 24, "tp": 0.02, "adds": False}),
    ("Tendência · sem recuperação · stop 1,5% · alvo 3%", 5, {"entry": "trend", "stop": 0.015, "hold_h": 48, "tp": 0.03, "adds": False}),
    ("Sempre · sem recuperação · stop 1% · alvo 1%", 10, {"stop": 0.01, "hold_h": 12, "tp": 0.01, "adds": False}),
    ("Sempre · stop 3% · 12h · alvo 0,5%", 5, {"stop": 0.03, "hold_h": 12, "tp": 0.005}),
]


def summary(d):
    days = d.get("days") or []
    tt = d.get("total") or {}
    months = {}
    for x in days:
        months[x["day"][:7]] = months.get(x["day"][:7], 0.0) + x["result"]
    return {"total": round(sum(x["result"] for x in days), 2), "months": months, "trades": tt.get("trades", 0),
            "win": (tt.get("wins", 0) / tt["trades"] * 100) if tt.get("trades") else 0, "fees": tt.get("fees", 0),
            "limit": sum(1 for x in days if "LIMITE" in (x.get("why") or "")),
            "held": max((h.get("held_h", 0) for h in d.get("hist") or []), default=0)}


def optimize(cs, only=None):
    """Runs every variant over the same candles. Best = most months in profit, then the biggest total."""
    BT_CACHE.clear()
    out = []
    for name, lev, cfg in VARIANTS:
        d = backtest(cs, lev, only=only, cfg=cfg)
        out.append((name, lev, cfg, summary(d)))
    BT_CACHE.clear()
    out.sort(key=lambda r: (sum(1 for v in r[3]["months"].values() if v > 0), r[3]["total"]), reverse=True)
    return out


def optimize_text(out, asset, days):
    L = ["🔬 OTIMIZADOR — " + asset + " · " + str(days) + " dias de candles reais de 5 min · taxas incluídas",
         "Cada versão começa todo dia com US$ " + format(CAPITAL_USD, ".0f") + ". Melhor = mais meses no lucro, depois o maior total.", ""]
    for k, (name, lev, cfg, sm) in enumerate(out):
        pos_m = sum(1 for v in sm["months"].values() if v > 0)
        L.append(("🥇 " if k == 0 else ("🥈 " if k == 1 else ("🥉 " if k == 2 else str(k + 1) + ". "))) + name + " · " + str(lev) + "x")
        L.append("   " + ("✅ " if sm["total"] > 0 else "❌ ") + format(sm["total"], "+.2f") + " USD · meses no lucro " + str(pos_m) + "/" +
                 str(len(sm["months"])) + " · " + str(sm["trades"]) + " fechamentos (" + format(sm["win"], ".0f") + "% lucro) · taxas " +
                 format(sm["fees"], ".0f") + " · limite " + str(sm["limit"]) + "x · máx " + format(sm["held"], ".0f") + " h aberta")
        L.append("   por mês: " + " · ".join(m[5:] + " " + format(v, "+.0f") for m, v in sorted(sm["months"].items())))
    best = out[0]
    L += ["", ("✅ A melhor deu lucro: " if best[3]["total"] > 0 else "❌ Nenhuma versão deu lucro no período. A melhor foi: ") +
          best[0] + " (" + str(best[1]) + "x).",
          "Toque em ✅ Usar a melhor na DEMO para o robô passar a operar assim na conta demo.",
          "⚠️ A melhor no passado pode não ser a melhor no futuro: confira na DEMO antes da conta real."]
    return "\n".join(L)


def run_optimize(send, chat, asset="SOL", days=90, only=None):
    if BT["running"]:
        send(chat, "⏳ Já tem um backtest rodando, espera o resultado.")
        return
    BT["running"] = True
    try:
        send(chat, "🔬 Otimizador " + asset + " começou: " + str(len(VARIANTS)) + " versões da estratégia em " + str(days) +
             " dias de candles reais (stop no conjunto, tempo máximo, alvo maior, alavancagem menor, só tendência…). "
             "Leva uns 5–15 minutos. Nenhuma ordem real.")
        cs = klines_history(days, asset)
        keep = dict(LAST)
        try:
            out = optimize(cs, only)
        finally:
            LAST.clear()
            LAST.update(keep)
        BT["best"] = {"asset": asset, "name": out[0][0], "lev": out[0][1], "cfg": out[0][2]}
        txt = optimize_text(out, asset, days)
        for k in range(0, len(txt), 3800):
            send(chat, txt[k:k + 3800], keyboard() if k + 3800 >= len(txt) else None)
    except Exception as exc:
        send(chat, "⛔ Otimizador: " + str(exc)[:200], keyboard())
    finally:
        BT["running"] = False


def apply_best(mode):
    """Puts the optimizer's best variant (strategy + leverage) on an account."""
    best = BT.get("best")
    if not best:
        return None
    with _LOCK:
        d = _load(mode)
        d["cfg"], d["leverage"], d["cfg_name"] = best["cfg"], best["lev"], best["name"]
        _save(d)
    return best


# ------------------------------------------------------------------ Telegram
def account_text(mode):
    d = _load(mode)
    price = LAST.get("price")
    eq = total_value(d, price)
    state = ("🟢 ligada" if d["on"] else "🔴 desligada") + (" · ⛔ " + d["done_why"] + " (volta amanhã)" if d["on"] and d["done_why"] else "")
    L = [NAME[mode] + " — " + state + " · " + str(d["leverage"]) + "x" + (" · versão: " + d["cfg_name"] if d.get("cfg_name") else ""),
         "💼 Saldo total: US$ " + format(eq, ".2f") + " (hoje " + format(eq - START_TOTAL, "+.2f") + " · começou com US$ " +
         format(START_TOTAL, ".0f") + " · 🎯 meta +" + format(TARGET_PROFIT, ".0f") + ")",
         "🛟 Reserva US$ " + format(d.get("reserve", 0.0), ".2f") + " de " + format(RESERVE_USD, ".0f") +
         (" · já adicionou US$ " + format(d.get("injected", 0.0), ".2f") + " de margem hoje" if d.get("injected") else
          " · entra como margem quando a margem das ordens está acabando") +
         (" · 🏦 guardou US$ " + format(d.get("banked", 0.0), ".0f") + " do lucro" if d.get("banked") else
          " · a cada US$ " + format(BANK_EVERY, ".0f") + " de lucro guarda US$ " + format(BANK_PUT, ".0f") + " aqui")]
    prog = max(0.0, min(1.0, (eq - START_TOTAL) / TARGET_PROFIT))
    L.append("▕" + "█" * int(prog * 10) + "░" * (10 - int(prog * 10)) + "▏ " + format(prog * 100, ".0f") + "% da meta")
    tot = d["wins"] + d["losses"]
    L.append("🛡️ 1ª ordem 30% da conta (US$ " + format(first_collateral(d["capital"]), ".2f") + ") · vai ganhar +50% · vai voltar até " +
             str(LOSS_ADDS) + " ordens com o resto" + (" · RISCO −50% (saldo ≥ US$ " + format(SAFE_FROM, ".0f") + ")"
                                                      if risk_cut(d["capital"]) < 1 else ""))
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
                 " ordens · preço médio $" + px(pos["entry"]) + " · fecha tudo em $" + px(tp_price(pos)) +
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
         LABEL + " agora: $" + format(price, ",.2f")]
    if not pos:
        L.append("⏳ Nenhuma ordem aberta agora " + ("— esperando sinal nos candles de 5 min." if d["on"] else "— conta desligada."))
        L.append("💼 Saldo total: US$ " + format(d["capital"] + d.get("reserve", 0.0), ".2f") + " (hoje " +
                 format(d["capital"] + d.get("reserve", 0.0) - START_TOTAL, "+.2f") + ")")
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
              "💵 Bruto " + format(gross, "+.2f") + " · taxas −" + format(fees, ".2f") + " · " + ASSET + " " + format(move, "+.2f") +
              "% desde o preço médio (" + ("a favor" if move * _sgn(pos) > 0 else "contra") + ")",
              "🎯 " + tp_text(pos) + " (falta " + format((tp / price - 1) * 100, "+.2f") + "%)" +
              (" · ➕ próxima recuperação em $" + px(nxt) if pos.get("loss_adds", 0) < LOSS_ADDS else " · recuperação completa")]
        lo, hi = (nxt, tp) if pos.get("side") == "long" else (tp, nxt)
        if hi > lo:
            k = int(round(max(0.0, min(1.0, (price - lo) / (hi - lo))) * 14))
            bar = "─" * k + "●" + "─" * (14 - k)
            L.append(("➕ " + bar + " 🎯") if pos.get("side") == "long" else ("🎯 " + bar + " ➕"))
        if track is not None:
            L.append("📊 Nesta tela: máx " + format(track["hi"], "+.2f") + " · mín " + format(track["lo"], "+.2f") + " USD")
        tv = d["capital"] + d.get("reserve", 0.0) + net
        L.append("💼 Saldo total agora: US$ " + format(tv, ".2f") + " (hoje " + format(tv - START_TOTAL, "+.2f") +
                 ") · 🛟 reserva US$ " + format(d.get("reserve", 0.0), ".2f") + " · 🎯 meta +" + format(TARGET_PROFIT, ".0f"))
        L.append("⏱️ Aberta há " + str(int((now - pos.get("opened", now)) // 60)) + " min")
    L.append("🕒 " + time.strftime("%H:%M:%S", time.gmtime(now)) + " UTC")
    return "\n".join(L)


def live_keyboard():
    return [[{"text": "⏹️ Parar ao vivo", "callback_data": "bfm_live_stop"}, {"text": "⬅️ Robô " + ASSET, "callback_data": "bfm"}]]


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
                 [[{"text": "📺 Ao vivo de novo", "callback_data": "bfm_live_" + mode}, {"text": "⬅️ Robô " + ASSET, "callback_data": "bfm"}]])
        except Exception:
            pass


def results_text():
    """Full report of both accounts: is the robot making money after the fees?"""
    L = ["📊 RESULTADOS DO ROBÔ " + ASSET + " FULL MARGIN (líquido = depois das taxas da Jupiter)"]
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
    L = [LABEL + " FULL MARGIN — Jupiter Perps · opera só " + ASSET + " · COMPRA e VENDA · preço médio",
         "Cada conta tem US$ " + format(START_TOTAL, ".0f") + " por dia: opera com US$ " + format(CAPITAL_USD, ".0f") + " e guarda US$ " +
         format(RESERVE_USD, ".0f") + " de reserva, que entra como MARGEM (US$ " + format(TOPUP_USD, ".0f") + " por vez) quando a margem "
         "das ordens está acabando; a cada US$ " + format(BANK_EVERY, ".0f") + " de lucro no dia, guarda US$ " + format(BANK_PUT, ".0f") +
         " na reserva. Opera O TEMPO TODO: fechou, já abre a próxima ordem "
         "(para o lado do movimento forte ou da leitura do gráfico), com 30% da conta. Vai ganhar: +1 ordem com 50%. "
         "Vai voltar (perdendo e o gráfico virou): até " + str(LOSS_ADDS) + " ordens a mais, cada vez mais distantes. "
         "A 1ª ordem sozinha fecha com +" + format(TP_ONE * 100, ".0f") + "%; com mais ordens abertas, fecha tudo junto com +" +
         format(TP_MANY * 100, ".0f") + "% sobre o preço médio. "
         "Para no dia com +" + format(TARGET_PROFIT, ".0f") + " (ou com −" + format(START_TOTAL * MAX_LOSS, ".0f") +
         "). Recomeça todo dia às 00:00 UTC.",
         "", account_text("demo"), "", account_text("real")]
    if LAST.get("price"):
        L += ["", LABEL + " agora: $" + format(LAST["price"], ",.2f") + (" · " + LAST["reading"] if LAST.get("reading") else "")]
    L.append("⚠️ Com alavancagem, um movimento rápido do " + ASSET + " pode levar a garantia toda. Não existe garantia de lucro.")
    return "\n".join(L)


def keyboard():
    acc = {m: _load(m) for m in MODES}
    rows = [[{"text": ("⏸️ Desligar " if acc[m]["on"] else ("🧪 Ligar " if m == "demo" else "💰 Ligar ")) + m.upper(),
              "callback_data": ("bfm_off_" if acc[m]["on"] else "bfm_on_") + m} for m in MODES]]
    closes = [{"text": "❌ Fechar ordens " + m.upper(), "callback_data": "bfm_close_" + m} for m in MODES if acc[m].get("pos")]
    if closes:
        rows.append(closes)
    lev = _load("demo")["leverage"]
    levs = [{"text": ("✅ " if lev == x else "") + str(x) + "x", "callback_data": "bfm_lev" + str(x)} for x in LEVERAGES]
    rows += [levs[i:i + 4] for i in range(0, len(levs), 4)]
    rows.append([{"text": "📥 Depositar", "callback_data": "wallet_deposit"}, {"text": "💸 Sacar", "callback_data": "wallet_withdraw"},
                 {"text": "💰 Saldo", "callback_data": "wallet_balance"}])
    rows.append([{"text": "📺 Ao vivo DEMO", "callback_data": "bfm_live_demo"}, {"text": "📺 Ao vivo REAL", "callback_data": "bfm_live_real"}])
    # owner, Nov 23: no backtest buttons - only the DEMO / REAL robot and its results
    rows.append([{"text": "📊 Resultados", "callback_data": "bfm_res"}])
    rows.append([{"text": "🔄 Atualizar", "callback_data": "bfm"}])
    return rows


ACTIONS = ({"bfm", "/btc", "bfm_real_ok", "bfm_res", "/resultados_btc", "bfm_live_demo", "bfm_live_real", "bfm_live_stop",
            "/aovivo", "bfm_bt", "bfm_bt60", "bfm_bt120", "bfm_bt365", "bfm_btg_sol",
            "bfm_btg90_sol", "bfm_btg365_sol", "/backtest_btc", "bfm_bt90", "/backtest_btc_3m", "bfm_bt_sol", "bfm_bt90_sol",
            "bfm_bt_eth", "bfm_bt90_eth", "/backtest_sol", "/backtest_eth", "bfm_bts_sol", "bfm_bts90_sol",
            "bfm_bts_eth", "bfm_bts90_eth", "bfm_btd_sol", "bfm_btd90_sol", "bfm_opt_sol", "bfm_opt_btc", "bfm_opt_eth",
            "bfm_use_demo", "bfm_use_real"} | {"bfm_lev" + str(x) for x in LEVERAGES} |
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
    send(chat, "▶️ " + ASSET + " FULL MARGIN " + NAME[mode] + " ligada: US$ " + format(d["capital"], ".2f") + " · meta do dia US$ " +
         format(TARGET_USD, ".0f") + " · " + str(d["leverage"]) + "x · compra e venda. Abrindo a 1ª ordem…")

    def go():
        try:
            send(chat, LABEL + " " + NAME[mode] + ": " + str(cycle(mode, send, chat)))
        except Exception as exc:
            send(chat, "⛔ " + ASSET + " FULL MARGIN " + NAME[mode] + ": " + str(exc)[:200])
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
    elif action.startswith("bfm_opt_"):
        asset = action[8:].upper()
        run_async(lambda: run_optimize(send, chat, asset, 90))
    elif action in ("bfm_use_demo", "bfm_use_real"):
        best = apply_best(action[8:])
        send(chat, ("✅ " + NAME[action[8:]] + " agora opera com: " + best["name"] + " · " + str(best["lev"]) + "x (a partir da próxima "
                    "ordem). Obs.: o robô opera " + ASSET + "; a versão veio do otimizador de " + best["asset"] + ".")
             if best else "Rode primeiro um 🔬 Otimizar.", keyboard())
    elif action.startswith("bfm_bt") or action.startswith("/backtest_"):
        lev = _load("demo")["leverage"]
        days = (365 if "365" in action else 120 if "120" in action else 90 if ("90" in action or action.endswith("_3m")) else
                60 if "60" in action else BT_DAYS)
        asset = "SOL" if "sol" in action else ("ETH" if "eth" in action else ASSET)
        only = "short" if action.startswith("bfm_bts") else None
        plan = DYN_PLAN if action.startswith("bfm_btd") else None
        cfg = GRID6_CFG if action.startswith("bfm_btg") else None
        run_async(lambda: run_backtest(send, chat, lev, days, asset, only, plan, cfg))
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
                if lev in LEVERAGES:                      # also with orders open: it is used from the next order on
                    d["leverage"] = lev
                    _save(d)
        send(chat, "✅ Alavancagem trocada para " + str(lev) + "x nas duas contas (DEMO e REAL). Vale a partir da próxima ordem; "
             "as ordens já abertas continuam com a alavancagem delas.", keyboard())
    elif action == "bfm_on_demo":
        _start("demo", send, chat, run_async)
    elif action == "bfm_on_real":
        send(chat, "⚠️ CONTA REAL: usa até US$ " + format(START_TOTAL, ".0f") + " da carteira do bot (US$ " + format(CAPITAL_USD, ".0f") +
             " operando + US$ " + format(RESERVE_USD, ".0f") + " de reserva para margem; em SOL, trocado por USDC) como "
             "garantia de ordens de COMPRA e VENDA de " + ASSET + " (até " + str(GRID_MAX) + ", preço médio) com " + str(_load("real")["leverage"]) + "x no Jupiter Perps, até a meta de US$ " +
             format(TARGET_PROFIT, ".0f") + " de lucro por dia. Deixe pelo menos US$ " + format(START_TOTAL + 10, ".0f") + " em SOL na carteira. Se o " + ASSET + " andar rápido contra as "
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
             (" As ordens abertas continuam até o alvo (+" + format(TP_ONE * 100, ".0f") + "% com 1 ordem / +" + format(TP_MANY * 100, ".0f") + "% com mais); toque em ❌ para fechar agora." if d.get("pos") else ""), keyboard())
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
