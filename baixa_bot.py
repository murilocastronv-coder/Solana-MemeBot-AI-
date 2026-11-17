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


# ---------------------------------------------------------------- quick backtest on ALL coins (owner, Nov 17)
ALL_MIN_LIQ = E("BAIXA_ALL_MIN_LIQ", 70_000)          # owner (Nov 17, later): US$ 70 mil, 10+ days, longer test
ALL_MIN_AGE_D = int(E("BAIXA_ALL_MIN_AGE_D", 10))
ALL_DAYS = int(E("BAIXA_ALL_DAYS", 166))           # one GeckoTerminal call of 1000 4-hour candles = 166 days
ALL_DEADLINE_S = E("BAIXA_ALL_SECONDS", 170)
# owner (Nov 17, later): in this test, no take profit - a trailing stop armed at +10% that sells 10% below the top;
# shown with the -20% stop loss as protection and without any stop
ALL_ARM = E("BAIXA_ALL_ARM", 0.10)
ALL_TRAIL = E("BAIXA_ALL_TRAIL", 0.10)
TF4 = 4 * 3600
NOT_COINS = {"USDC", "USDT", "USDS", "PYUSD", "USDG", "EURC", "USD1", "USDE", "SUSDE", "FDUSD", "USX", "USDY", "SYRUPUSDC",
             "SOL", "WSOL", "MSOL", "JITOSOL", "BSOL", "INF", "JUPSOL", "BBSOL", "HSOL", "DSOL", "CBBTC", "WBTC", "ETH", "WETH",
             "LBTC", "XBTC", "ONYC", "JLP"}


def to_4h(cs):
    """1-hour candles -> complete 4-hour candles."""
    groups = {}
    for c in cs:
        groups.setdefault(c["t"] // TF4 * TF4, []).append(c)
    return [{"t": t, "h": max(x["h"] for x in g), "l": min(x.get("l", x["c"]) for x in g), "c": g[-1]["c"]}
            for t, g in sorted(groups.items()) if len(g) == 4]


def _gecko_once(url, deadline):
    """One GeckoTerminal call that never waits past the deadline (a 429 skips that coin)."""
    import solid_dip_bt as bt
    for _ in range(2):
        if time.time() > deadline:
            return None
        wait = bt._gk[0] + 2.2 - time.time()
        if wait > 0:
            time.sleep(wait)
        bt._gk[0] = time.time()
        sg._gecko_block[0] = max(sg._gecko_block[0], time.time() + 60)
        r = bt._http.get(url, timeout=15, headers={"Accept": "application/json", "User-Agent": "DipBacktest/1.0"})
        if r.status_code == 429:
            time.sleep(min(10, max(0, deadline - time.time())))
            continue
        r.raise_for_status()
        return r.json()
    return None


def all_coins(now, deadline, get=None, pages=6):
    """Solana coins with >= ALL_MIN_LIQ liquidity and 7+ days today: GeckoTerminal top pools + the big coins."""
    import solid_dip_bt as bt
    out = {}
    for page in range(1, pages + 1):
        try:
            d = get(bt.GECKO + "/networks/solana/pools?page=" + str(page) + "&sort=h24_volume_usd_desc") if get else \
                _gecko_once(bt.GECKO + "/networks/solana/pools?page=" + str(page) + "&sort=h24_volume_usd_desc", deadline)
        except Exception:
            d = None
        for p in ((d or {}).get("data") or []):
            a = p.get("attributes") or {}
            name = str(a.get("name") or "")
            sym = name.split("/")[0].strip().upper()
            quote = name.split("/")[-1].strip().upper() if "/" in name else ""
            liq = float(a.get("reserve_in_usd") or 0)
            try:
                created = time.mktime(time.strptime(str(a.get("pool_created_at"))[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            except Exception:
                continue
            if not sym or sym in NOT_COINS or quote not in ("SOL", "WSOL", "USDC", "USDT") or liq < ALL_MIN_LIQ:
                continue
            if now - created < ALL_MIN_AGE_D * 86400:
                continue
            if sym not in out or liq > out[sym]["liq"]:
                out[sym] = {"pool": a.get("address"), "liq": liq, "created": created}
    for sym in BINANCE:                                   # the big coins are always in (years old, deep liquidity)
        out.setdefault(sym, {"pool": None, "liq": 0, "created": 0})
    return out


def backtest_all(now=None, get=None, progress=None):
    """Robot rule on every coin found, last ALL_DAYS days, 4-hour candles (7-day top = 42 candles), US$ USD per buy,
    US$ 60 capital (so at most 2 open), never buys a coin before 7 days of life. Stops downloading at the deadline."""
    import solid_dip_bt as bt
    now = now or time.time()
    deadline = time.time() + ALL_DEADLINE_S
    coins = all_coins(now, deadline, get)
    if progress:
        progress("🧪 Achei " + str(len(coins)) + " moedas com " + str(ALL_MIN_AGE_D) + "+ dias e liquidez ≥ US$ " + format(ALL_MIN_LIQ, ",.0f") +
                 ". Baixando ~" + format(ALL_DAYS / 30, ".1f") + " meses de preços (até ~3 min)…")
    data, skipped = {}, []
    for sym, c in sorted(coins.items(), key=lambda kv: kv[0] not in BINANCE):      # fast Binance coins first
        if time.time() > deadline:
            skipped.append(sym)
            continue
        cs = []
        try:
            if sym in BINANCE and get is None:
                cs = to_4h(bt.fetch(sym + "USDT", None, days=ALL_DAYS, now=now))
            elif c["pool"]:
                url = (bt.GECKO + "/networks/solana/pools/" + c["pool"] + "/ohlcv/hour?aggregate=4&limit=1000&currency=usd&before_timestamp=" +
                       str(int(now)))
                d = get(url) if get else _gecko_once(url, deadline)
                rows = ((d or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
                cs = sorted(({"t": int(r[0]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])} for r in rows
                             if int(r[0]) >= now - ALL_DAYS * 86400 and int(r[0]) + TF4 <= now), key=lambda x: x["t"])
        except Exception as exc:
            logging.info("baixa all %s: %s", sym, type(exc).__name__)
        cs = [x for x in cs if x["t"] >= c["created"] + ALL_MIN_AGE_D * 86400]
        if len(cs) > 42 + 6:
            data[sym] = cs
        else:
            skipped.append(sym)
    if not data:
        return None
    start = min(x[0]["t"] for x in data.values())
    end = max(x[-1]["t"] for x in data.values()) + TF4
    cut = start + (end - start) * 2 // 3
    run = lambda a, b, st=SL: bt.simulate(data, a, b, DROP, 42, size=USD, capital=60, stop=st, arm=ALL_ARM, trail=ALL_TRAIL)
    return {"start": start, "end": end, "cut": cut, "coins": sorted(data), "skipped": skipped, "found": len(coins),
            "first": run(start, cut), "last": run(cut, end), "full": run(start, end),
            "nostop_last": run(cut, end, 0), "nostop_full": run(start, end, 0)}


def backtest_all_text(rep):
    import solid_dip_bt as bt
    if not rep:
        return "⛔ Backtest de todas as moedas: não consegui baixar os preços agora. Tente de novo em alguns minutos."
    d = lambda t: time.strftime("%d/%m/%Y", time.gmtime(t))

    def f(s):
        n = s["trades"]
        return ("US$ 60 → " + format(s["final"], ".2f") + " (" + format(s["pnl"], "+.2f") + ") · " + str(s["trails"]) + " no stop móvel · " +
                str(s["stops"]) + " no stop −20% · no lucro " + (format(s["profit_trades"] / n * 100, ".0f") if n else "0") + "% · abertas " +
                str(len(s["open"])) + " (" + format(s["open_pnl"], "+.2f") + ") · drawdown " + format(s["dd"], "g") + "%")
    pc = rep["full"]["per_coin"]
    best = sorted(pc.items(), key=lambda kv: -kv[1][1])
    L = ["🧪 BACKTEST: REGRA DO ROBÔ EM TODAS AS MOEDAS (" + str(ALL_MIN_AGE_D) + "+ dias, liquidez ≥ US$ " + format(ALL_MIN_LIQ, ",.0f") + ") — " +
         d(rep["start"]) + " a " + d(rep["end"]),
         "Moedas testadas: " + str(len(rep["coins"])) + " de " + str(rep["found"]) + " achadas" +
         (" (sem dados a tempo: " + ", ".join(rep["skipped"][:15]) + ")" if rep["skipped"] else "") +
         " · compra US$ " + format(USD, "g") + " quando cai " + format(DROP * 100, "g") + "% do topo de 7 dias · SEM alvo: stop móvel liga em +" +
         format(ALL_ARM * 100, "g") + "% e vende " + format(ALL_TRAIL * 100, "g") + "% abaixo do topo · stop −" + format(SL * 100, "g") +
         "% · máx. 2 abertas (US$ 60) · velas de 4 h reais · custo " + format(bt.COST * 100, "g") + "% por lado",
         "• primeiros 2/3: " + f(rep["first"]), "• último 1/3 (" + d(rep["cut"]) + " em diante): " + f(rep["last"]),
         "• período todo (~" + format((rep["end"] - rep["start"]) / 86400 / 30, ".1f") + " meses): " + f(rep["full"]),
         "• vendas: " + str(rep["full"]["trades"]) + " no período",
         "• SÓ o stop móvel, SEM stop −20%: último 1/3 " + format(rep["nostop_last"]["pnl"], "+.2f") + " · período todo " + f(rep["nostop_full"])]
    if best:
        L.append("• melhores: " + ", ".join(c + " " + str(n) + "/" + format(p, "+.2f") for c, (n, p) in best[:6]))
        L.append("• piores: " + ", ".join(c + " " + str(n) + "/" + format(p, "+.2f") for c, (n, p) in best[-4:]))
    L.append("⚠️ Só moedas que AINDA têm ≥ US$ " + format(ALL_MIN_LIQ, ",.0f") + " de liquidez hoje (as que morreram não aparecem: o real tende a ser pior). "
             "Velas de 4 h: o stop móvel usa o topo de ANTES da vela (o caso pior). Passado não garante futuro.")
    return "\n".join(L)


def start_backtest_all(send, chat):
    if not _BT.acquire(blocking=False):
        return "já tem um backtest rodando"

    def go():
        try:
            send(chat, backtest_all_text(backtest_all(progress=lambda m: send(chat, m))))
        except Exception as exc:
            logging.exception("baixa backtest all")
            send(chat, "⛔ Backtest de todas as moedas falhou: " + type(exc).__name__)
        finally:
            _BT.release()
    threading.Thread(target=go, daemon=True).start()
    return ""
