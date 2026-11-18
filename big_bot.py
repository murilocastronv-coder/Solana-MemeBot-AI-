"""Robô das moedas grandes (owner, Nov 18): the strategy that worked on the big coins - buy when a big Solana coin
(not a memecoin) is DROP (5%) below its 7-day top; stop loss SL (35%); trailing stop armed at +ARM (10%) that sells
TRAIL (10%) below the top. Every account can switch it on for its OWN wallet (users pay the 2% trading fee).

Coins: official mints written here (never found by name, so a fake copy with the same symbol is never bought), plus
coins found as VERIFIED on Jupiter by symbol (ZEC). Prices: DexScreener; 7-day top: Binance hourly candles when the
coin trades there, the robot's own saved prices and DexScreener's 1 h / 6 h / 24 h changes."""
import json
import logging
import os
import threading
import time

import requests

DROP = float(os.getenv("BIG_DROP", "0.05"))
SL = float(os.getenv("BIG_SL", "0.35"))
ARM = 0.10                         # trade_panel.TRAIL_ARM / user_trade.exit_reason arm
TRAIL = float(os.getenv("BIG_TRAIL", "0.10"))
LOOKBACK_H = 168
MAX_OPEN = int(os.getenv("BIG_MAX_OPEN", "2"))          # robot positions per account
COOLDOWN_S = 24 * 3600                                    # no rebuy of the same coin for 24 h
CYCLE_S = int(os.getenv("BIG_CYCLE_S", "900"))
KEY = "big_bot"
MAJORS = {"JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "JTO": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
          "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R", "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
          "ORCA": "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE", "W": "85VBFQZC9TZkfaptBWjvUw7YbZjy52A6mjtPGjstQAmQ",
          "RENDER": "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof", "HNT": "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux",
          "WETH": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs", "CBBTC": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij"}
BY_JUPITER = tuple(s.strip().upper() for s in os.getenv("BIG_EXTRA", "ZEC").split(",") if s.strip())
BINANCE = {"JUP": "JUPUSDT", "JTO": "JTOUSDT", "RAY": "RAYUSDT", "PYTH": "PYTHUSDT", "ORCA": "ORCAUSDT", "W": "WUSDT",
           "RENDER": "RENDERUSDT", "HNT": "HNTUSDT", "WETH": "ETHUSDT", "CBBTC": "BTCUSDT", "ZEC": "ZECUSDT"}
_LOCK = threading.RLock()
_MEM = {}
_CHG = {}


def _load():
    with _LOCK:
        if not _MEM:
            d = {"px": {}, "btop": {}, "coins": {}, "coins_at": 0}
            try:
                import pg_store
                if pg_store.enabled():
                    d.update(pg_store.get(KEY) or {})
            except Exception:
                pass
            _MEM.update(d)
        return _MEM


def _save():
    try:
        import pg_store
        if pg_store.enabled():
            pg_store.put(KEY, json.loads(json.dumps(_MEM)))
    except Exception as exc:
        logging.warning("big bot save: %s", type(exc).__name__)


# ---------------------------------------------------------------- coins
def jupiter_verified(symbol, get=None):
    """Mint of the VERIFIED Jupiter token with exactly this symbol (the most liquid one), else None."""
    import jupiter_rate_limiter
    try:
        if get:
            rows = get("https://lite-api.jup.ag/tokens/v2/search?query=" + symbol)
        else:
            r = jupiter_rate_limiter.get("https://lite-api.jup.ag/tokens/v2/search", params={"query": symbol}, timeout=15)
            r.raise_for_status()
            rows = r.json()
    except Exception as exc:
        logging.info("big bot jupiter %s: %s", symbol, type(exc).__name__)
        return None
    ok = [t for t in (rows or []) if isinstance(t, dict) and str(t.get("symbol") or "").upper() == symbol and t.get("isVerified")]
    best = max(ok, key=lambda t: float(t.get("liquidity") or 0), default=None)
    return best.get("id") if best else None


def coins(get=None, now=None):
    """{symbol: mint} of the robot's coins (Jupiter lookups refreshed once a day)."""
    now = now or time.time()
    d = _load()
    if now - float(d.get("coins_at") or 0) > 86400 or not d.get("coins"):
        extra = {}
        for sym in BY_JUPITER:
            m = jupiter_verified(sym, get)
            if m:
                extra[sym] = m
        with _LOCK:
            d["coins"], d["coins_at"] = extra, now
            _save()
    out = dict(MAJORS)
    out.update(d.get("coins") or {})
    return out


# ---------------------------------------------------------------- prices and the 7-day top
def dex_prices(mints, get=None):
    import signals as sg
    get = get or sg._get
    out = {}
    mints = list(mints)
    for i in range(0, len(mints), 30):
        for p in get(sg.DEX + "/tokens/v1/solana/" + ",".join(mints[i:i + 30])) or []:
            m = (p.get("baseToken") or {}).get("address")
            try:
                px = float(p.get("priceUsd") or 0)
            except (TypeError, ValueError):
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if m and px > 0 and liq >= out.get(m, (0, -1))[1]:
                out[m] = (px, liq)
                past = []
                for k in ("h1", "h6", "h24"):
                    try:
                        c = float((p.get("priceChange") or {}).get(k))
                        if c > -100:
                            past.append(px / (1 + c / 100))
                    except (TypeError, ValueError):
                        pass
                _CHG[m] = past
    return {m: v[0] for m, v in out.items()}


def binance_top(pair, now=None, get=None):
    """Highest hourly high of the last 7 days on Binance (two hosts), 0 when unavailable."""
    for host in ("https://api.binance.com", "https://data-api.binance.vision"):
        try:
            url = host + "/api/v3/klines?symbol=" + pair + "&interval=1h&limit=" + str(LOOKBACK_H)
            rows = get(url) if get else requests.get(url, timeout=10).json()
            highs = [float(r[2]) for r in rows or []]
            if len(highs) >= LOOKBACK_H // 2:
                return max(highs)
        except Exception:
            continue
    return 0.0


def record(prices, now=None):
    now = now or time.time()
    with _LOCK:
        hist = _load().setdefault("px", {})
        for m, px in prices.items():
            hist[m] = ([x for x in hist.get(m, []) if now - x[0] < LOOKBACK_H * 3600] + [[int(now), px]])[-800:]
        _save()


def top_7d(sym, mint, now=None, get=None):
    """Highest of: Binance 7-day top (read once an hour), the robot's own prices of 7 days, DexScreener's past prices.
    A wrapped coin follows the original's price, so the Binance top of the original is used."""
    now = now or time.time()
    d = _load()
    cands = []
    if sym in BINANCE:
        b = (d.get("btop") or {}).get(sym)
        if not b or now - b[0] >= 3600:
            top = binance_top(BINANCE[sym], now, get)
            if top:
                b = [now, top]
                with _LOCK:
                    d.setdefault("btop", {})[sym] = b
                    _save()
        if b and now - b[0] < 6 * 3600:
            cands.append(b[1])
    cands += [px for t, px in (d.get("px") or {}).get(mint, []) if now - t < LOOKBACK_H * 3600]
    cands += _CHG.get(mint, [])
    return max(cands) if cands else 0.0


def signals_now(get=None, now=None):
    """[(symbol, mint, price, top, buy?)] for every coin, cheapest vs its top first."""
    now = now or time.time()
    cs = coins(get, now)
    prices = dex_prices(list(cs.values()), get)
    record(prices, now)
    out = []
    for sym, m in cs.items():
        px = prices.get(m, 0.0)
        top = top_7d(sym, m, now, get) if px else 0.0
        out.append((sym, m, px, top, bool(px and top and px <= top * (1 - DROP))))
    return sorted(out, key=lambda r: (r[2] / r[3]) if r[3] else 9)


# ---------------------------------------------------------------- per-account robot
def state(chat):
    import trade_panel as tp
    r = dict(tp.settings(chat).get("robot") or {})
    r.setdefault("on", False)
    r.setdefault("sol", float(tp.settings(chat)["buy"][0]))
    r.setdefault("bought", {})
    return r


def set_state(chat, r):
    import trade_panel as tp
    tp.set_setting(chat, "robot", r)


def open_positions(chat):
    import trade_panel as tp
    mints = set(coins().values())
    return [m for m, p in tp.positions(chat) if m in mints and (p.get("robot") or p.get("trade_robot"))]


def apply_rules(chat, mint):
    """The robot's exits on the position it just bought: stop −SL, trailing TRAIL from +ARM, no target."""
    import trade_panel as tp
    if tp.is_owner(chat):
        import live_trade_state as st
        st.set_position_fields(mint, trade_tp=0.0, trade_sl=SL, trade_trail=TRAIL, trade_robot=True)
    else:
        import user_trade as ut
        ut.update(chat, lambda d: d["positions"].get(mint, {}).update(tp=0.0, sl=SL, trail=TRAIL, robot=True))


def run_for(chat, sigs, send, now=None, buy=None):
    """One robot cycle for one account: buys the coins in the dip it does not hold (up to MAX_OPEN)."""
    import trade_panel as tp
    now = now or time.time()
    r = state(chat)
    if not r["on"]:
        return []
    held = {m for m, _ in tp.positions(chat)}
    n_open = len(open_positions(chat))
    done = []
    for sym, m, px, top, buy_now in sigs:
        if not buy_now or m in held or n_open >= MAX_OPEN or (m in r["bought"] and now - float(r["bought"][m]) < COOLDOWN_S):
            continue
        send(chat, "🤖 ROBÔ MOEDAS GRANDES: " + sym + " está " + format((1 - px / top) * 100, ".1f") + "% abaixo do topo de 7 dias. "
             "Comprando " + format(r["sol"], "g") + " SOL…")
        (buy or tp.buy)(chat, m, r["sol"], send, sym)
        if m in {x for x, _ in tp.positions(chat)}:
            apply_rules(chat, m)
            r["bought"][m] = now
            n_open += 1
            done.append(sym)
            send(chat, "🤖 " + sym + ": stop loss −" + format(SL * 100, "g") + "% · stop móvel liga em +" + format(ARM * 100, "g") +
                 "% e vende " + format(TRAIL * 100, "g") + "% abaixo do topo.")
    r["bought"] = {k: v for k, v in r["bought"].items() if now - v < COOLDOWN_S}
    set_state(chat, r)
    return done


def cycle(send, owner_chat, get=None):
    import user_trade as ut
    chats = ([owner_chat] if owner_chat else []) + ut.users()
    if not any(state(c)["on"] for c in chats):
        return
    sigs = signals_now(get)
    for c in chats:
        try:
            run_for(c, sigs, send)
        except Exception as exc:
            logging.info("big bot %s: %s", c, type(exc).__name__)


def status_text(chat, sigs=None):
    r = state(chat)
    L = ["🤖 ROBÔ MOEDAS GRANDES — " + ("🟢 LIGADO" if r["on"] else "🔴 DESLIGADO"),
         "Compra " + format(r["sol"], "g") + " SOL quando uma moeda grande da Solana (não é memecoin) cai " + format(DROP * 100, "g") +
         "% do topo de 7 dias · 🛑 stop loss −" + format(SL * 100, "g") + "% · 🚀 stop móvel liga em +" + format(ARM * 100, "g") +
         "% e vende " + format(TRAIL * 100, "g") + "% abaixo do topo · máx. " + str(MAX_OPEN) + " moedas abertas · confere a cada " +
         str(CYCLE_S // 60) + " min",
         "Robô aberto agora: " + str(len(open_positions(chat))) + "/" + str(MAX_OPEN)]
    if sigs:
        L.append("Moedas (preço vs topo de 7 dias):")
        for sym, m, px, top, buy_now in sigs:
            L.append(("🟢 " if buy_now else "• ") + sym + (": " + format((px / top - 1) * 100, "+.1f") + "% do topo" if top else ": sem preço agora"))
    L.append("⚠️ Resultado passado não garante lucro. O robô usa a SUA carteira e precisa de SOL nela.")
    return "\n".join(L)


def keyboard(chat):
    r = state(chat)
    return [[{"text": "🔴 Desligar robô" if r["on"] else "🟢 Ligar robô", "callback_data": "trb_off" if r["on"] else "trb_on"},
             {"text": "💵 Valor: " + format(r["sol"], "g") + " SOL", "callback_data": "trb_sol"}],
            [{"text": "🔄 Atualizar", "callback_data": "trob"}]]
