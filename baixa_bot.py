"""ROBÔ BAIXA, REAL MONEY (owner, Nov 17): the rule of the memecoin backtest the owner chose, on the coins it traded.

Owner's decision, after being shown that this rule LOST money in the backtest's out-of-sample test (-US$ 10.18, no
target hit) and that the coin list was not memecoins: run it with real money anyway, only this robot.
Coins: the ones the backtest really bought (BP, ZEC, SPCX, HYPE, NVDAX); USD1 (stablecoin, never +25%) and ONYC
(yield token) left out. Mints are found on GeckoTerminal (Solana pool with the most liquidity, >= US$ 250k).
Rule (same as the backtest winner): every CYCLE_S, hourly candles of each coin; when the price is DROP below the
highest price of the last LOOKBACK_H hours and the bot holds none of that coin, buy US$ USD (Jupiter, live engine,
mode "baixa"); the exit monitor sells at +TP or at -SL (live_exit_monitor). Max one position per coin, always
leaves KEEP_SOL_USD in SOL, stops buying for the day after DAILY_LOSS. Off until the owner presses LIGAR.
"""
import json
import logging
import threading
import time

import signals as sg

E = sg._f
COINS = ("BP", "ZEC", "SPCX", "HYPE", "NVDAX")
DROP = E("BAIXA_DROP", 0.05)
LOOKBACK_H = int(E("BAIXA_LOOKBACK_H", 168))
TP = E("BAIXA_TP", 0.25)
SL = E("BAIXA_SL", 0.40)
USD = E("BAIXA_USD", 10)
KEEP_SOL_USD = E("BAIXA_KEEP_SOL_USD", 3)
DAILY_LOSS = E("BAIXA_DAILY_LOSS_USD", 20)
MIN_LIQ = E("BAIXA_MIN_LIQ", 250_000)
CYCLE_S = E("BAIXA_CYCLE_S", 900)
KEY = "baixa_bot"
_LOCK = threading.RLock()
_MEM = {}


def _load():
    with _LOCK:
        if not _MEM:
            d = {"enabled": False, "tokens": {}, "log": [], "last_buy": {}}
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
        logging.warning("baixa save: %s", type(exc).__name__)


def note(text):
    logging.info("baixa %s", text)
    with _LOCK:
        d = _load()
        d["log"] = (d.get("log") or [])[-49:] + [time.strftime("%d/%m %H:%M", time.gmtime()) + " " + text]
        _save()


def set_enabled(on):
    with _LOCK:
        _load()["enabled"] = bool(on)
        _save()


def enabled():
    return bool(_load().get("enabled"))


# ---------------------------------------------------------------- data
def resolve(get=None):
    """{symbol: {mint, pool, liq}} from GeckoTerminal search (most liquid Solana pool of that symbol)."""
    import solid_dip_bt as bt
    out = {}
    for sym in COINS:
        try:
            rows = (bt._gecko(bt.GECKO + "/search/pools?query=" + sym + "&network=solana", get) or {}).get("data") or []
        except Exception as exc:
            logging.info("baixa resolve %s: %s", sym, type(exc).__name__)
            continue
        best = None
        for p in rows:
            a = p.get("attributes") or {}
            base = ((((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id") or "")
            name = str(a.get("name") or "")
            if name.split("/")[0].strip().upper() != sym or not base.startswith("solana_"):
                continue
            liq = float(a.get("reserve_in_usd") or 0)
            if liq >= MIN_LIQ and (best is None or liq > best["liq"]):
                best = {"mint": base[len("solana_"):], "pool": a.get("address"), "liq": liq}
        if best:
            out[sym] = best
    return out


def signal(candles, drop=None, lookback=None):
    """(buy?, price, top) from hourly candles [{t,h,l,c}] oldest first: last close vs the highest high BEFORE it."""
    drop = DROP if drop is None else drop
    lookback = LOOKBACK_H if lookback is None else lookback
    if len(candles) < lookback + 1:
        return False, 0.0, 0.0
    px = candles[-1]["c"]
    top = max(c["h"] for c in candles[-lookback - 1:-1])
    return px <= top * (1 - drop), px, top


def exit_signal(ratio):
    if ratio >= 1 + TP:
        return "sell", "take_profit"
    if ratio <= 1 - SL:
        return "sell", "stop_loss"
    return "hold", "baixa_hold"


# ---------------------------------------------------------------- cycle
def _open():
    try:
        import live_trade_state as st
        return {m for m, p in st._load()["positions"].items() if p.get("mode") == "baixa"}
    except Exception:
        return set()


def day_pnl(now=None):
    day = time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))
    try:
        import live_trade_state as st
        return sum(float(h.get("pnl_usd") or 0) for h in st.history() if h.get("mode") == "baixa" and
                   time.strftime("%Y-%m-%d", time.gmtime(float(h.get("closed_at") or 0))) == day)
    except Exception:
        return 0.0


def _sol_ok(sol_usd):
    try:
        import live_engine
        import liq_bot as lb
        lam = lb.sol_lamports(__import__("pool_bot")._rpc(), live_engine.get_address())
        return lam / 1e9 * float(sol_usd) >= USD + KEEP_SOL_USD + 0.5
    except Exception:
        return False


def cycle(send=None, chat=None, now=None, get=None, buy=None, sol_ok=None, sol_usd=None):
    """One pass. Returns the symbols bought."""
    import solid_dip_bt as bt
    now = now or time.time()
    d = _load()
    if not d.get("enabled"):
        return []
    if not d.get("tokens") or now - float(d.get("resolved_at") or 0) > 86400:
        found = resolve(get)
        if found:
            with _LOCK:
                d["tokens"], d["resolved_at"] = found, now
                _save()
    if DAILY_LOSS and day_pnl(now) <= -DAILY_LOSS:
        return []
    held = _open()
    bought = []
    for sym, tk in (d.get("tokens") or {}).items():
        if tk["mint"] in held:
            continue
        cs = bt.gecko_hours(tk["pool"], (LOOKBACK_H + 24) // 24 + 1, now, get)
        ok, px, top = signal(cs)
        if not ok:
            continue
        if not (sol_ok or _sol_ok)(sol_usd if sol_usd is not None else __import__("copy_trader")._sol_usd()):
            note("ia comprar " + sym + " (caiu " + format((1 - px / top) * 100, ".0f") + "% do topo de 7 dias), mas falta SOL na carteira")
            continue
        pair = {"baseToken": {"address": tk["mint"], "symbol": sym}, "liquidity": {"usd": tk["liq"]}}
        settled = (buy or _buy)(pair, tk["mint"], sym, send, chat)
        note(("COMPROU " if settled else "compra NÃO confirmada ") + sym + " a US$ " + format(px, ".6g") + " (topo de 7 dias US$ " +
             format(top, ".6g") + ")")
        if settled:
            bought.append(sym)
    return bought


def _buy(pair, mint, sym, send, chat):
    import live_engine
    try:
        s = live_engine._execute_buy(pair, mint, sym, send, chat, live_engine.get_address(), None, "baixa", usd=USD)
    except Exception as exc:
        note("compra falhou " + sym + ": " + type(exc).__name__)
        return False
    return bool(s and s.get("status") == "settled_buy")


def status_text():
    d = _load()
    try:
        import live_trade_state as st
        h = [x for x in st.history() if x.get("mode") == "baixa"]
    except Exception:
        h = []
    total = sum(float(x.get("pnl_usd") or 0) for x in h)
    L = ["📉 ROBÔ BAIXA — " + ("🟢 LIGADO (💵 DINHEIRO REAL)" if d.get("enabled") else "⏸️ DESLIGADO"),
         "⚙️ Compra US$ " + format(USD, "g") + " quando a moeda cai " + format(DROP * 100, "g") + "% do topo de 7 dias · vende no alvo +" +
         format(TP * 100, "g") + "% ou no stop −" + format(SL * 100, "g") + "% · 1 posição por moeda · deixa US$ " + format(KEEP_SOL_USD, "g") +
         " em SOL · para no dia em que perder US$ " + format(DAILY_LOSS, "g"),
         "🪙 Moedas: " + (", ".join(s + " (" + t["mint"][:4] + "…, liquidez US$ " + format(t["liq"] / 1000, ",.0f") + " mil)"
                                   for s, t in (d.get("tokens") or {}).items()) or ", ".join(COINS) + " (procuro os contratos ao ligar)"),
         "📊 " + str(len(h)) + " operações fechadas · resultado " + format(total, "+.2f") + " USD · abertas " + str(len(_open())),
         "⚠️ No backtest essa regra PERDEU fora da amostra (−US$ 10,18 nos ~40 dias de teste). Você decidiu ligar mesmo assim."]
    for x in (d.get("log") or [])[-5:]:
        L.append("• " + x)
    return "\n".join(L)
