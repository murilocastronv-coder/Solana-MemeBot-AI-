"""ROBÔ BAIXA, REAL MONEY (owner, Nov 17): the rule of the memecoin backtest the owner chose, on the coins it traded.

Owner's decision, after being shown that this rule LOST money in the backtest's out-of-sample test (-US$ 10.18, no
target hit) and that the coin list was not memecoins: run it with real money anyway, only this robot.
Coins: the big Solana coins (MAJORS, official mints) plus BP, SPCX and NVDAX; the pool of each is the one with the most
real 24 h volume on DexScreener (paired with SOL/USDC/USDT, realistic liquidity).
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
# owner (Nov 17, later): all the big Solana coins. Official mints written here (never found by name, so a fake
# copy with the same symbol can't be bought); BP, SPCX and NVDAX (they traded in the backtest) are found by name with
# the volume/liquidity checks. ZEC and HYPE left out (their Solana pools barely trade).
MAJORS = {"JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
          "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "JTO": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
          "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R", "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
          "ORCA": "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE", "W": "85VBFQZC9TZkfaptBWjvUw7YbZjy52A6mjtPGjstQAmQ",
          "RENDER": "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof", "PENGU": "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",
          "TRUMP": "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN", "POPCAT": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
          "FARTCOIN": "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"}
BY_NAME = ("BP", "SPCX", "NVDAX")
COINS = tuple(MAJORS) + BY_NAME
BINANCE = {"JUP", "BONK", "WIF", "JTO", "RAY", "PYTH", "ORCA", "W", "RENDER", "PENGU", "TRUMP"}   # hourly candles from Binance
DROP = E("BAIXA_DROP", 0.05)
LOOKBACK_H = int(E("BAIXA_LOOKBACK_H", 168))
TP = E("BAIXA_TP", 0.08)                    # owner (Nov 17, later): +8% target, -20% stop (was +25% / -40%)
SL = E("BAIXA_SL", 0.20)
USD = E("BAIXA_USD", 23)                    # owner (Nov 17, later): US$ 23 per order
MAX_OPEN = int(E("BAIXA_MAX_OPEN", 2))       # owner: at most 2 orders open at a time
KEEP_SOL_USD = E("BAIXA_KEEP_SOL_USD", 3)
DAILY_LOSS = E("BAIXA_DAILY_LOSS_USD", 20)
MIN_LIQ = E("BAIXA_MIN_LIQ", 250_000)
MAX_LIQ = E("BAIXA_MAX_LIQ", 100_000_000)         # more than this in one Solana pool of these coins = fake numbers
MIN_VOL_24H = E("BAIXA_MIN_VOL_24H", 25_000)
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
RESOLVE_V = 3


def resolve(get=None):
    """{symbol: {mint, pool, liq, vol}}: DexScreener search. Fake copies of a symbol often show absurd liquidity and
    no real trading, so the pool must: be paired with SOL/USDC/USDT, have MIN_LIQ..MAX_LIQ of liquidity, liquidity not
    above 5x its market value, and >= MIN_VOL_24H of real 24 h volume; the one with the MOST VOLUME wins."""
    get = get or sg._get
    out = {}
    by_mint = {}
    mints = list(MAJORS.values())
    for i in range(0, len(mints), 30):
        try:
            for p in get(sg.DEX + "/tokens/v1/solana/" + ",".join(mints[i:i + 30])) or []:
                by_mint.setdefault((p.get("baseToken") or {}).get("address"), []).append(p)
        except Exception as exc:
            logging.info("baixa resolve majors: %s", type(exc).__name__)
    for sym in COINS:
        try:
            if sym in MAJORS:
                rows = by_mint.get(MAJORS[sym], [])
            else:
                rows = (get(sg.DEX + "/latest/dex/search?q=" + sym) or {}).get("pairs") or []
        except Exception as exc:
            logging.info("baixa resolve %s: %s", sym, type(exc).__name__)
            continue
        best = None
        for p in rows:
            b, q = p.get("baseToken") or {}, p.get("quoteToken") or {}
            if p.get("chainId") != "solana" or not b.get("address"):
                continue
            if (b["address"] != MAJORS[sym]) if sym in MAJORS else (str(b.get("symbol") or "").upper() != sym):
                continue
            if str(q.get("symbol") or "").upper() not in ("SOL", "WSOL", "USDC", "USDT"):
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            vol = float((p.get("volume") or {}).get("h24") or 0)
            fdv = float(p.get("marketCap") or p.get("fdv") or 0)
            if not MIN_LIQ <= liq <= MAX_LIQ or vol < MIN_VOL_24H or (fdv and liq > 5 * fdv):
                continue
            if best is None or vol > best["vol"]:
                best = {"mint": b["address"], "pool": p.get("pairAddress"), "liq": liq, "vol": vol}
        if best:
            out[sym] = best
        else:
            logging.info("baixa resolve %s: nenhuma pool confiável", sym)
    return out


def candles(sym, tk, days, now=None, get=None):
    """Hourly candles: Binance for the big coins listed there (stable source), else the GeckoTerminal pool."""
    import solid_dip_bt as bt
    if sym in BINANCE and get is None:
        try:
            cs = bt.fetch(sym + "USDT", None, days=days, now=now)
            if len(cs) > LOOKBACK_H:
                return cs
        except Exception as exc:
            logging.info("baixa binance %s: %s", sym, type(exc).__name__)
    return bt.gecko_hours(tk["pool"], days, now, get)


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
    if not d.get("tokens") or d.get("resolve_v") != RESOLVE_V or now - float(d.get("resolved_at") or 0) > 86400:
        found = resolve(get)
        with _LOCK:
            d["tokens"], d["resolved_at"], d["resolve_v"] = found, now, RESOLVE_V   # never keep a possibly fake contract
            _save()
    if DAILY_LOSS and day_pnl(now) <= -DAILY_LOSS:
        return []
    held = _open()
    bought = []
    for sym, tk in (d.get("tokens") or {}).items():
        if len(held) + len(bought) >= MAX_OPEN:
            break                                           # owner: only MAX_OPEN orders open at a time
        if tk["mint"] in held:
            continue
        cs = candles(sym, tk, (LOOKBACK_H + 24) // 24 + 1, now, get)
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
    """Never waits for a busy trade-state lock (the menu must answer at once)."""
    d = _load()
    h, n_open = [], "?"
    try:
        import live_trade_state as st
        if st._LOCK.acquire(timeout=0.3):
            try:
                h = [x for x in st.history() if x.get("mode") == "baixa"]
                n_open = str(len(_open()))
            finally:
                st._LOCK.release()
    except Exception:
        pass
    total = sum(float(x.get("pnl_usd") or 0) for x in h)
    L = ["📉 ROBÔ BAIXA — " + ("🟢 LIGADO (💵 DINHEIRO REAL)" if d.get("enabled") else "⏸️ DESLIGADO"),
         "⚙️ Compra US$ " + format(USD, "g") + " quando a moeda cai " + format(DROP * 100, "g") + "% do topo de 7 dias · vende no alvo +" +
         format(TP * 100, "g") + "% ou no stop −" + format(SL * 100, "g") + "% · 1 posição por moeda · deixa US$ " + format(KEEP_SOL_USD, "g") +
         " em SOL · máx. " + str(MAX_OPEN) + " abertas · para no dia em que perder US$ " + format(DAILY_LOSS, "g"),
         "🪙 Moedas: " + (", ".join(s + " (" + t["mint"][:4] + "…, liquidez US$ " + format(t["liq"] / 1000, ",.0f") + " mil, volume 24 h US$ " +
                                    format(t.get("vol", 0) / 1000, ",.0f") + " mil)"
                                   for s, t in (d.get("tokens") or {}).items()) or ", ".join(COINS) + " (procuro os contratos ao ligar)"),
         "📊 " + str(len(h)) + " operações fechadas · resultado " + format(total, "+.2f") + " USD · abertas " + n_open + "/" + str(MAX_OPEN),
         "🧪 Veja o resultado dessa regra no passado em 🧪 Backtest do robô."]
    for x in (d.get("log") or [])[-5:]:
        L.append("• " + x)
    return "\n".join(L)


# ---------------------------------------------------------------- backtest of THIS robot (owner, Nov 17)
BT_DAYS = int(E("BAIXA_BT_DAYS", 120))
_BT = threading.Lock()


def backtest(now=None, get=None, progress=None):
    """Same coins, same entry (DROP below the 7-day top), same exits (TP / SL), US$ USD per buy, US$ 60 capital,
    hourly GeckoTerminal candles; first 2/3 of the time vs the last 1/3 (no choice is made: the rule is fixed)."""
    import solid_dip_bt as bt
    now = now or time.time()
    tokens = _load().get("tokens") if _load().get("resolve_v") == RESOLVE_V else None
    if not tokens:
        tokens = resolve(get)
        with _LOCK:
            _load()["tokens"], _load()["resolved_at"], _load()["resolve_v"] = tokens, now, RESOLVE_V
            _save()
    if progress:
        progress("🧪 Contratos achados: " + (", ".join(tokens) or "nenhum") + ". Baixando " + str(BT_DAYS) +
                 " dias de preços (o GeckoTerminal limita as consultas; pode levar alguns minutos)…")
    data = {}
    for sym, tk in tokens.items():
        cs = candles(sym, tk, BT_DAYS, now, get)
        logging.info("baixa backtest %s: %s velas", sym, len(cs))
        if len(cs) > LOOKBACK_H + 24:
            data[sym] = cs
    if not data:
        return None
    start = min(c[0]["t"] for c in data.values())
    end = max(c[-1]["t"] for c in data.values()) + 3600
    cut = start + (end - start) * 2 // 3
    run = lambda a, b: bt.simulate(data, a, b, DROP, LOOKBACK_H, size=USD, capital=60, target=TP, stop=SL)
    seen = {}
    for sym, cs in data.items():                      # how often the entry condition happened (sanity check of the data)
        tp = bt.tops(cs, LOOKBACK_H)
        seen[sym] = {"hours": len(cs), "dips": sum(1 for i, c in enumerate(cs) if tp[i] and c["c"] <= tp[i] * (1 - DROP)),
                     "move": round((max(c["h"] for c in cs) / max(min(c["l"] for c in cs), 1e-18) - 1) * 100, 1)}
    return {"start": start, "end": end, "cut": cut, "coins": list(data), "first": run(start, cut), "last": run(cut, end),
            "full": run(start, end), "seen": seen, "tokens": {k: v.get("mint", "")[:6] for k, v in tokens.items()}}


def backtest_text(rep):
    import solid_dip_bt as bt
    if not rep:
        return ("⛔ Backtest do robô BAIXA: não consegui baixar os preços (o GeckoTerminal está bloqueando consultas agora). "
                "Tente de novo em ~10 min.")
    d = lambda t: time.strftime("%d/%m/%Y", time.gmtime(t))

    def f(s):
        n = s["trades"]
        return ("US$ 60 → " + format(s["final"], ".2f") + " (" + format(s["pnl"], "+.2f") + ") · " + str(s["wins"]) + " no alvo +" +
                format(TP * 100, "g") + "% · " + str(s["stops"]) + " no stop −" + format(SL * 100, "g") + "% · acerto " +
                (format(s["wins"] / n * 100, ".0f") if n else "0") + "% · abertas " + str(len(s["open"])) + " (" + format(s["open_pnl"], "+.2f") +
                ") · drawdown " + format(s["dd"], "g") + "%")
    L = ["🧪 BACKTEST DO ROBÔ BAIXA (a regra que está no robô real) — " + d(rep["start"]) + " a " + d(rep["end"]),
         "Moedas: " + ", ".join(rep["coins"]) + " · compra US$ " + format(USD, "g") + " quando cai " + format(DROP * 100, "g") +
         "% do topo de 7 dias · alvo +" + format(TP * 100, "g") + "% · stop −" + format(SL * 100, "g") + "% · velas de 1 h reais · custo " +
         format(bt.COST * 100, "g") + "% por lado",
         "• primeiros 2/3: " + f(rep["first"]), "• último 1/3 (" + d(rep["cut"]) + " em diante): " + f(rep["last"]),
         "• período todo: " + f(rep["full"])]
    for sym, v in (rep.get("seen") or {}).items():
        L.append("• dados " + sym + " (" + (rep.get("tokens") or {}).get(sym, "") + "…): " + str(v["hours"]) + " h de preços · subiu/caiu até " +
                 format(v["move"], "g") + "% do mínimo ao máximo · " + str(v["dips"]) + " h abaixo de −" + format(DROP * 100, "g") + "% do topo")
    pc = rep["full"]["per_coin"]
    if pc:
        L.append("• por moeda (vendas, lucro): " + ", ".join(c + " " + str(n) + " / " + format(p, "+.2f") for c, (n, p) in sorted(pc.items())))
    L.append("⚠️ Regra fixa (nada foi escolhido olhando o resultado). Alvo e stop na mesma vela de 1 h contam como stop. "
             "Passado não garante futuro.")
    return "\n".join(L)


def start_backtest(send, chat):
    if not _BT.acquire(blocking=False):
        return "já está rodando"

    def go():
        try:
            send(chat, backtest_text(backtest(progress=lambda m: send(chat, m))))
        except Exception as exc:
            logging.exception("baixa backtest")
            send(chat, "⛔ Backtest do robô BAIXA falhou: " + type(exc).__name__)
        finally:
            _BT.release()
    threading.Thread(target=go, daemon=True).start()
    return ""
