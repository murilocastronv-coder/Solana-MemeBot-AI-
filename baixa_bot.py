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
import os
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
# owner (Nov 17, evening): the strategy of the "all coins" backtest - no target, trailing stop armed at +10% that sells
# 10% below the top; stop loss -35% (owner, later). BAIXA_SL=0 turns the stop off.
TP = E("BAIXA_TP", 0)
SL = E("BAIXA_SL", 0.35)                    # owner (Nov 17, evening): stop loss -35%
ARM = E("BAIXA_ARM", 0.10)
TRAIL = E("BAIXA_TRAIL", 0.10)
# owner (Nov 17): if a coin falls DCA_DROP below the first buy, buy a 2nd order of USD (max 2 orders per coin); the
# trailing stop then arms at +ARM over the AVERAGE price
DCA_DROP = E("BAIXA_DCA_DROP", 0.38)
MAX_BUYS_PER_COIN = 2
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
RESOLVE_V = 7                      # 5: wider search; 6: liquidity up to US$ 700 mil; 7: only Pump.fun coins (Nov 17)
# owner (Nov 17): only Pump.fun coins (mint ends in 'pump'): fixed supply, no mint/freeze authority, liquidity burned
# when they graduate; 10+ days of life. The anti-scam check still runs before every buy.
ONLY_PUMP = os.getenv("BAIXA_ONLY_PUMP", "true").strip().lower() != "false"


def resolve(get=None, now=None):
    """Coins this robot trades (owner, Nov 17 evening): every Solana coin with 10+ days of life and US$ 70 mil to
    400 mil of liquidity today (the same list as the "all coins" backtest), from GeckoTerminal; mint and pool kept."""
    now = now or time.time()
    found = all_coins(now, time.time() + 240, get)
    return {sym: {"mint": c["mint"], "pool": c["pool"], "liq": c["liq"], "created": c["created"]}
            for sym, c in found.items() if c.get("mint") and c.get("pool")}


def resolve_majors(get=None):
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


def candles(sym, tk, days, now=None, get=None, tries=5):
    """Hourly candles: Binance for the big coins listed there (stable source), else the GeckoTerminal pool."""
    import solid_dip_bt as bt
    if sym in BINANCE and get is None:
        try:
            cs = bt.fetch(sym + "USDT", None, days=days, now=now)
            if len(cs) > LOOKBACK_H:
                return cs
        except Exception as exc:
            logging.info("baixa binance %s: %s", sym, type(exc).__name__)
    return bt.gecko_hours(tk["pool"], days, now, get, tries=tries)


TOP_TTL_S = E("BAIXA_TOP_TTL_S", 3600)
_CHG = {}                       # mint -> prices 1 h / 6 h / 24 h ago (from DexScreener's price changes), this cycle


def dex_prices(mints, get=None):
    """{mint: price US$} now, from DexScreener (fast, 30 coins per call, no GeckoTerminal). Also keeps, per coin,
    the prices 1 h, 6 h and 24 h ago implied by DexScreener's price changes (they bound the recent top)."""
    get = get or sg._get
    out = {}
    for i in range(0, len(mints), 30):
        for p in get(sg.DEX + "/tokens/v1/solana/" + ",".join(mints[i:i + 30])) or []:
            m = (p.get("baseToken") or {}).get("address")
            try:
                px = float(p.get("priceUsd") or 0)
            except (TypeError, ValueError):
                px = 0.0
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if m and px > 0 and liq >= (out.get(m, (0, -1))[1]):
                out[m] = (px, liq)                           # the price of the most liquid pool
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


def record_prices(prices, now=None):
    """The robot's own price history (every cycle, kept 7 days in the database): its own 7-day top over time."""
    now = now or time.time()
    with _LOCK:
        d = _load()
        hist = d.setdefault("px", {})
        for m, px in prices.items():
            pts = [x for x in hist.get(m, []) if now - x[0] < LOOKBACK_H * 3600] + [[int(now), px]]
            hist[m] = pts[-800:]
        for m in [m for m in hist if m not in prices and not any(now - x[0] < LOOKBACK_H * 3600 for x in hist[m])]:
            del hist[m]
        _save()


def top_7d(sym, tk, now=None, get=None):
    """7-day top = the highest of: the GeckoTerminal candles (read at most once an hour, saved, one quick try),
    the robot's own saved prices of the last 7 days, and the prices 1 h / 6 h / 24 h ago from DexScreener."""
    now = now or time.time()
    d = _load()
    m = tk["mint"]
    g = (d.get("gtop") or {}).get(m)
    if (not g or now - g[0] >= TOP_TTL_S) and now - float((d.get("gfail") or {}).get(m, -1e12)) >= TOP_TTL_S:
        cs = candles(sym, tk, (LOOKBACK_H + 24) // 24 + 1, now, get, tries=1)
        with _LOCK:
            if len(cs) >= LOOKBACK_H:
                g = [now, max(c["h"] for c in cs[-LOOKBACK_H:])]
                d.setdefault("gtop", {})[m] = g
            else:
                d.setdefault("gfail", {})[m] = now            # GeckoTerminal refused: try again in an hour
            _save()
    cands = []
    if g and now - g[0] < LOOKBACK_H * 3600:
        cands.append(g[1])
    cands += [px for t, px in (d.get("px") or {}).get(m, []) if now - t < LOOKBACK_H * 3600]
    cands += _CHG.get(m, [])
    return max(cands) if cands else 0.0


def live_signal(sym, tk, now=None, get=None, prices=None):
    """(buy?, price now, 7-day top): DexScreener price vs the cached 7-day top."""
    px = (prices or {}).get(tk["mint"], 0.0)
    if px <= 0:
        return False, 0.0, 0.0
    top = top_7d(sym, tk, now, get)
    if top <= 0:
        return False, px, 0.0
    return px <= top * (1 - DROP), px, top


def signal(candles, drop=None, lookback=None):
    """(buy?, price, top) from hourly candles [{t,h,l,c}] oldest first: last close vs the highest high BEFORE it."""
    drop = DROP if drop is None else drop
    lookback = LOOKBACK_H if lookback is None else lookback
    if len(candles) < lookback + 1:
        return False, 0.0, 0.0
    px = candles[-1]["c"]
    top = max(c["h"] for c in candles[-lookback - 1:-1])
    return px <= top * (1 - drop), px, top


def exit_signal(ratio, peak=None):
    """ratio = value now / cost; peak = best ratio since the buy. Trailing stop: once the peak reached +ARM, sells when
    the value is TRAIL below the peak. Optional fixed target (TP) and stop loss (SL); both off by default."""
    peak = max(ratio, peak or 0)
    if SL and ratio <= 1 - SL:
        return "sell", "stop_loss"
    if TRAIL and peak >= 1 + ARM and ratio <= peak * (1 - TRAIL):
        return "sell", "trailing_stop"
    if TP and ratio >= 1 + TP:
        return "sell", "take_profit"
    return "hold", ("trailing" if TRAIL and peak >= 1 + ARM else "baixa_hold")


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


FEE_USD = 0.5                    # network fee + the new coin's token account, per buy


def need_usd():
    """Free SOL (in US$) one buy needs: the order + the reserve left in SOL + fees."""
    return USD + KEEP_SOL_USD + FEE_USD


def room(free_usd):
    """How many buys of US$ USD the free SOL pays for (same rule as the check before each buy)."""
    return int(max(0.0, float(free_usd) - KEEP_SOL_USD) // (USD + FEE_USD))


def free_sol_usd(sol_usd):
    """SOL in the bot wallet, in US$ (None if the balance can't be read)."""
    try:
        import live_engine
        import live_order_bridge as bridge
        return bridge.get_sol_balance(live_engine.get_address()) / 1e9 * float(sol_usd)
    except Exception as exc:
        logging.warning("baixa: saldo de SOL indisponível (%s)", type(exc).__name__)
        return None


def _sol_ok(sol_usd):
    """Enough SOL in the bot wallet for one buy + the reserve. Uses the live engine's own balance call (the old
    liq_bot/pool_bot helpers were removed from the project: that made this always say 'no SOL')."""
    free = free_sol_usd(sol_usd)
    _LAST_FREE[0] = free
    return free is not None and free >= need_usd()


_LAST_FREE = [None]


REPAIR_PREFIXES = ("8s11PkgQ",)          # BLOB, bought by this robot on Nov 17 and relabelled 'copy' by the copy trade


def repair_positions():
    """Positions this robot bought (its 'bought' list, or the known BLOB) that another part of the bot relabelled:
    back to mode 'baixa', copy fields removed. The top is reset (peak = 1) so the trailing stop only arms again at
    +ARM: the old top came while the position was under another rule and would sell it at once at a loss."""
    import live_trade_state as st
    d = _load()
    mine = set(d.get("bought") or {})
    fixed = []
    for m, p in list(st._load()["positions"].items()):
        if p.get("mode") == "baixa":
            continue
        if m in mine or m.startswith(REPAIR_PREFIXES):
            st.set_position_fields(m, mode="baixa", copy_orphan=False, copy_recovered=False, copy_wallet=None,
                                   peak_ratio=1.0, trail_armed=False)
            fixed.append(str(p.get("symbol") or m[:6]))
    if fixed:
        note("devolvi ao robô BAIXA: " + ", ".join(fixed) + " (o copy trade tinha pegado; topo zerado)")
    return fixed


RESOLVE_RETRY_S = E("BAIXA_RESOLVE_RETRY_S", 1800)


def _why(last):
    """One line: what the last cycle did and, if it bought nothing, why (owner: 'it stopped buying, why?')."""
    if not last:
        return ""
    t = time.strftime("%H:%M", time.gmtime(last["at"]))
    L = "🔎 Último ciclo (" + t + " UTC): " + str(last["coins"]) + " moedas · " + str(last["dips"]) + " caíram " + \
        format(DROP * 100, "g") + "%+ do topo de 7 dias"
    if last.get("bought"):
        return L + " · comprou " + ", ".join(last["bought"])
    if last.get("full"):
        return L + " · não compra: já tem " + str(MAX_OPEN) + "/" + str(MAX_OPEN) + " abertas"
    why = []
    if last.get("no_sol"):
        free = last.get("free_usd")
        why.append("falta SOL (livre US$ " + (format(free, ".2f") if free is not None else "?") + ", precisa US$ " +
                   format(need_usd(), ".2f") + " por compra)")
    if last.get("no_price"):
        why.append("preço do SOL indisponível")
    if last.get("unsafe"):
        why.append(str(last["unsafe"]) + " barradas no anti-golpe" + (" (ex.: " + last["unsafe_why"] + ")" if last.get("unsafe_why") else ""))
    if last.get("unchecked"):
        why.append(str(last["unchecked"]) + " sem checagem anti-golpe (serviço fora)")
    if last.get("blocked"):
        why.append(str(last["blocked"]) + " já barradas nas últimas 24 h")
    if last.get("failed"):
        why.append(str(last["failed"]) + " compra(s) não confirmada(s)")
    if not last["coins"]:
        why.append("nenhuma moeda na lista (GeckoTerminal não respondeu; tento de novo em até 30 min)")
    elif not last["dips"]:
        why.append("nenhuma moeda caiu o suficiente")
    return L + " · não comprou: " + ("; ".join(why) or "sem sinal")


def dca_check(send=None, chat=None, sol_usd=None, value=None, add_buy=None, sol_ok=None):
    """2nd order: an open position of this robot with one buy whose value fell DCA_DROP below its cost gets another
    US$ USD (the live engine adds it to the same position: cost and amount add up, so the exits use the average)."""
    import live_trade_state as st
    done = []
    for m, p in list(st._load()["positions"].items()):
        if p.get("mode") != "baixa" or int(p.get("buys") or 1) >= MAX_BUYS_PER_COIN or not DCA_DROP:
            continue
        cost = float(p.get("cost_usd") or 0)
        try:
            val = (value or __import__("live_exit_monitor").quote_value_usd)(m, int(p.get("token_amount_raw") or 0))
        except Exception:
            continue
        if not cost or val / cost > 1 - DCA_DROP:
            continue
        sym = str(p.get("symbol") or m[:6])
        if not (sol_ok or _sol_ok)(sol_usd if sol_usd is not None else __import__("copy_trader")._sol_usd()):
            note("ia comprar a 2ª ordem de " + sym + " (caiu " + format((1 - val / cost) * 100, ".0f") + "%), mas falta SOL")
            continue
        ok = (add_buy or _add_buy)(m, sym, send, chat)
        note(("2ª ORDEM comprada: " if ok else "2ª ordem NÃO confirmada: ") + sym + " (caiu " + format((1 - val / cost) * 100, ".0f") +
             "% da 1ª compra; agora o stop móvel liga em +" + format(ARM * 100, "g") + "% sobre o preço médio)")
        if ok:
            new_cost = float((st._load()["positions"].get(m) or {}).get("cost_usd") or cost + USD)
            st.set_position_fields(m, peak_ratio=(val + USD) / new_cost, trail_armed=False)
            done.append(sym)
    return done


def _add_buy(mint, sym, send, chat):
    import live_engine
    try:
        s = live_engine._execute_buy({"baseToken": {"address": mint, "symbol": sym}}, mint, sym, send, chat, live_engine.get_address(),
                                     None, "baixa", usd=USD, add_on=True)
    except Exception as exc:
        note("2ª ordem falhou " + sym + ": " + type(exc).__name__)
        return False
    return bool(s and s.get("status") == "settled_buy")


def cycle(send=None, chat=None, now=None, get=None, buy=None, sol_ok=None, sol_usd=None, safety=None):
    """One pass. Returns the symbols bought."""
    try:
        repair_positions()
    except Exception as exc:
        logging.warning("baixa repair: %s", type(exc).__name__)
    import solid_dip_bt as bt
    now = now or time.time()
    d = _load()
    if not d.get("enabled"):
        return []
    stale = not d.get("tokens") or d.get("resolve_v") != RESOLVE_V or now - float(d.get("resolved_at") or 0) > 86400
    if stale and now - float(d.get("resolve_fail_at") or 0) >= RESOLVE_RETRY_S:
        found = resolve(get, now)
        with _LOCK:
            if found:
                if d.get("resolve_v") != RESOLVE_V:
                    d["log"] = []                               # the old robot's notes go away with the old list
                d["tokens"], d["resolved_at"], d["resolve_v"] = found, now, RESOLVE_V
                d.pop("resolve_fail_at", None)
            else:
                # GeckoTerminal refused the list: keep the coins we have (an empty answer used to wipe the list and
                # leave the robot with NO coins for 24 h) and try again in RESOLVE_RETRY_S
                d["resolve_fail_at"] = now
                if d.get("resolve_v") != RESOLVE_V:
                    d["tokens"], d["resolve_v"] = {}, RESOLVE_V   # never trade an old robot's list
            _save()
    if DAILY_LOSS and day_pnl(now) <= -DAILY_LOSS:
        return []
    try:
        dca_check(send, chat, sol_usd, sol_ok=sol_ok)
    except Exception as exc:
        logging.warning("baixa dca: %s", type(exc).__name__)
    held = _open()
    bought = []
    last = {"at": now, "coins": len(d.get("tokens") or {}), "dips": 0, "bought": bought, "full": len(held) >= MAX_OPEN}
    try:
        prices = dex_prices([t["mint"] for t in (d.get("tokens") or {}).values()], get)
        record_prices(prices, now)
    except Exception:
        prices = {}
    no_sol = None                                           # SOL is checked once per cycle, not once per coin
    for sym, tk in (d.get("tokens") or {}).items():
        if len(held) + len(bought) >= MAX_OPEN:
            break                                           # owner: only MAX_OPEN orders open at a time
        if tk["mint"] in held:
            continue
        ok, px, top = live_signal(sym, tk, now, get, prices)
        if not ok:
            continue
        last["dips"] += 1
        if no_sol is None or bought:
            try:
                px_sol = sol_usd if sol_usd is not None else __import__("copy_trader")._sol_usd()
            except Exception:
                px_sol = None
            if px_sol is None:
                last["no_price"], no_sol = True, True
            else:
                _LAST_FREE[0] = None
                no_sol = not (sol_ok or _sol_ok)(px_sol)
                last["free_usd"] = _LAST_FREE[0]
        if no_sol:
            last["no_sol"] = last.get("no_sol", 0) + 1
            continue
        if time.time() - float((d.get("blocked") or {}).get(tk["mint"], 0)) < 86400:
            last["blocked"] = last.get("blocked", 0) + 1
            continue                                        # failed the anti-scam check in the last 24 h
        why = (safety or sg.safety_reason)(tk["mint"])      # mint/freeze authority, top holders, sell test, Token-2022, RugCheck
        if why and "indisponível" in why:
            last["unchecked"] = last.get("unchecked", 0) + 1
            note("não comprei " + sym + " agora: anti-golpe não conseguiu checar (" + why + "); tento no próximo ciclo")
            continue
        if why:
            last["unsafe"] = last.get("unsafe", 0) + 1
            last["unsafe_why"] = sym + ": " + why
            with _LOCK:
                d.setdefault("blocked", {})[tk["mint"]] = time.time()
                d["blocked"] = {m: t for m, t in d["blocked"].items() if time.time() - t < 86400}
                _save()
            note("NÃO comprei " + sym + " (caiu " + format((1 - px / top) * 100, ".0f") + "% do topo): anti-golpe: " + why)
            continue
        pair = {"baseToken": {"address": tk["mint"], "symbol": sym}, "liquidity": {"usd": tk["liq"]}}
        settled = (buy or _buy)(pair, tk["mint"], sym, send, chat)
        note(("COMPROU " if settled else "compra NÃO confirmada ") + sym + " a US$ " + format(px, ".6g") + " (topo de 7 dias US$ " +
             format(top, ".6g") + ")")
        if settled:
            bought.append(sym)
            with _LOCK:
                d.setdefault("bought", {})[tk["mint"]] = time.time()     # this robot's own positions
                _save()
        else:
            last["failed"] = last.get("failed", 0) + 1
    if last.get("no_sol") and not bought:
        free = last.get("free_usd")
        note("ia comprar " + str(last["no_sol"]) + " moeda(s) em queda, mas falta SOL: livre US$ " +
             (format(free, ".2f") if free is not None else "?") + ", precisa US$ " + format(need_usd(), ".2f") + " por compra")
    with _LOCK:
        d["last"] = dict(last, bought=list(bought))
        _save()
    return bought


def _buy(pair, mint, sym, send, chat):
    import live_engine
    try:
        s = live_engine._execute_buy(pair, mint, sym, send, chat, live_engine.get_address(), None, "baixa", usd=USD)
    except Exception as exc:
        note("compra falhou " + sym + ": " + type(exc).__name__)
        return False
    return bool(s and s.get("status") == "settled_buy")


def coins_line(d):
    """The coins of the CURRENT rule only (an old list from an earlier robot is never shown)."""
    toks = d.get("tokens") or {}
    if d.get("resolve_v") != RESOLVE_V:
        return ("🪙 Moedas: procurando as moedas da regra nova (10+ dias, liquidez US$ " + format(ALL_MIN_LIQ / 1000, "g") + "–" +
                format(ALL_MAX_LIQ / 1000, "g") + " mil) no próximo ciclo (até 15 min)")
    if not toks:
        return "🪙 Moedas: nenhuma moeda achada nessa faixa agora (procuro de novo a cada ciclo)"
    return "🪙 Moedas (" + str(len(toks)) + "): " + ", ".join(
        sym + " (liquidez US$ " + format(t["liq"] / 1000, ",.0f") + " mil, " + format((time.time() - float(t.get("created") or 0)) / 86400, ".0f") +
        " dias)" for sym, t in sorted(toks.items(), key=lambda kv: -kv[1]["liq"]))


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
         "⚙️ Compra US$ " + format(USD, "g") + " quando a moeda cai " + format(DROP * 100, "g") + "% do topo de 7 dias · sem alvo: stop móvel liga em +" +
         format(ARM * 100, "g") + "% e vende " + format(TRAIL * 100, "g") + "% abaixo do topo · " + ("stop −" + format(SL * 100, "g") + "%" if SL else "SEM stop") +
         " · 2ª ordem se cair " + format(DCA_DROP * 100, "g") + "% (máx. 2 por moeda)" +
         " · só moedas da Pump.fun com " + str(ALL_MIN_AGE_D) + "+ dias e liquidez US$ " + format(ALL_MIN_LIQ / 1000, "g") + "–" + format(ALL_MAX_LIQ / 1000, "g") +
         " mil · máx. " + str(MAX_OPEN) + " abertas · deixa US$ " + format(KEEP_SOL_USD, "g") + " em SOL",
         "🛡️ Anti-golpe antes de cada compra: criador sem poder de criar/congelar, 10 maiores donos, teste de venda na Jupiter, "
         "Token-2022 sem armadilhas, RugCheck sem perigo e liquidez travada (se não der para checar, não compra)",
         coins_line(d),
         "📊 " + str(len(h)) + " operações fechadas · resultado " + format(total, "+.2f") + " USD · abertas " + n_open + "/" + str(MAX_OPEN),
         "🧪 Veja o resultado dessa regra no passado em 🧪 Backtest do robô."]
    if d.get("enabled") and _why(d.get("last")):
        L.insert(4, _why(d.get("last")))
    for x in ((d.get("log") or []) if d.get("resolve_v") == RESOLVE_V else [])[-5:]:      # only this robot's notes
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
    run = lambda a, b: bt.simulate(data, a, b, DROP, LOOKBACK_H, size=USD, capital=60, target=TP or 9.0, stop=SL, arm=ARM, trail=TRAIL,
                                   dca=DCA_DROP)
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
        return ("US$ 60 → " + format(s["final"], ".2f") + " (" + format(s["pnl"], "+.2f") + ") · " + str(s["trails"]) + " no stop móvel · " +
                str(s["wins"]) + " no alvo · " + str(s["stops"]) + " no stop · no lucro " + (format(s["profit_trades"] / n * 100, ".0f") if n else "0") +
                "% · abertas " + str(len(s["open"])) + " (" + format(s["open_pnl"], "+.2f") + ") · drawdown " + format(s["dd"], "g") + "%")
    L = ["🧪 BACKTEST DO ROBÔ BAIXA (a regra que está no robô real) — " + d(rep["start"]) + " a " + d(rep["end"]),
         "Moedas: " + ", ".join(rep["coins"]) + " · compra US$ " + format(USD, "g") + " quando cai " + format(DROP * 100, "g") +
         "% do topo de 7 dias · stop móvel +" + format(ARM * 100, "g") + "% / " + format(TRAIL * 100, "g") + "% abaixo do topo · " +
         ("stop −" + format(SL * 100, "g") + "%" if SL else "sem stop") + " · velas de 1 h reais · custo " +
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
    """The robot's own coins are found again every cycle, and the saved list can hold just 1-2 coins (Nov 17: only
    MIHAO, with no prices). So this button tests the same rule on every coin the robot can buy (Pump.fun, 10+ days,
    US$ 70-700 mil of liquidity): the same test as 'TODAS as moedas'."""
    return start_backtest_all(send, chat)


def start_backtest_saved(send, chat):
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
ALL_MAX_LIQ = E("BAIXA_ALL_MAX_LIQ", 700_000)            # owner (Nov 17, later): liquidity between US$ 70 mil and 700 mil
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


LIST_SORTS = ("h24_volume_usd_desc", "h24_tx_count_desc")     # owner (Nov 17): more coins in the 70-400 mil range


def all_coins(now, deadline, get=None, pages=10):
    """Solana coins in the liquidity range with 10+ days today: GeckoTerminal pool lists (most volume AND most trades,
    up to 10 pages each = ~400 pools; stops at the deadline)."""
    import solid_dip_bt as bt
    out = {}
    for sort, page in [(srt, pg) for srt in LIST_SORTS for pg in range(1, pages + 1)]:
        if not get and time.time() > deadline:
            break
        try:
            url = bt.GECKO + "/networks/solana/pools?page=" + str(page) + "&sort=" + sort
            d = get(url) if get else _gecko_once(url, deadline)
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
            if not sym or sym in NOT_COINS or quote not in ("SOL", "WSOL", "USDC", "USDT") or not ALL_MIN_LIQ <= liq <= ALL_MAX_LIQ:
                continue
            if now - created < ALL_MIN_AGE_D * 86400:
                continue
            base = ((((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id") or "")
            mint = base[len("solana_"):] if base.startswith("solana_") else ""
            if ONLY_PUMP and not mint.endswith("pump"):
                continue                                      # not a Pump.fun coin
            if sym not in out or liq > out[sym]["liq"]:
                out[sym] = {"pool": a.get("address"), "liq": liq, "created": created, "mint": mint}
    return out                                            # the big coins have far more than ALL_MAX_LIQ: not in this test


def backtest_all(now=None, get=None, progress=None):
    """Robot rule on every coin found, last ALL_DAYS days, 4-hour candles (7-day top = 42 candles), US$ USD per buy,
    US$ 60 capital (so at most 2 open), never buys a coin before 7 days of life. Stops downloading at the deadline."""
    import solid_dip_bt as bt
    now = now or time.time()
    deadline = time.time() + ALL_DEADLINE_S
    coins = all_coins(now, deadline, get)
    if progress:
        progress("🧪 Achei " + str(len(coins)) + " moedas com " + str(ALL_MIN_AGE_D) + "+ dias e liquidez de US$ " + format(ALL_MIN_LIQ, ",.0f") +
                 " a US$ " + format(ALL_MAX_LIQ, ",.0f") +
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
    run = lambda a, b, st=SL: bt.simulate(data, a, b, DROP, 42, size=USD, capital=60, stop=st, arm=ALL_ARM, trail=ALL_TRAIL, dca=DCA_DROP)
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
                str(s["stops"]) + " no stop −" + format(SL * 100, "g") + "% · no lucro " + (format(s["profit_trades"] / n * 100, ".0f") if n else "0") + "% · abertas " +
                str(len(s["open"])) + " (" + format(s["open_pnl"], "+.2f") + ") · drawdown " + format(s["dd"], "g") + "%")
    pc = rep["full"]["per_coin"]
    best = sorted(pc.items(), key=lambda kv: -kv[1][1])
    L = ["🧪 BACKTEST: TODAS AS MOEDAS (" + str(ALL_MIN_AGE_D) + "+ dias, liquidez US$ " + format(ALL_MIN_LIQ, ",.0f") + " a " +
         format(ALL_MAX_LIQ, ",.0f") + ") — " +
         d(rep["start"]) + " a " + d(rep["end"]),
         "Moedas testadas: " + str(len(rep["coins"])) + " de " + str(rep["found"]) + " achadas" +
         (" (sem dados a tempo: " + ", ".join(rep["skipped"][:15]) + ")" if rep["skipped"] else "") +
         " · compra US$ " + format(USD, "g") + " quando cai " + format(DROP * 100, "g") + "% do topo de 7 dias · SEM alvo: stop móvel liga em +" +
         format(ALL_ARM * 100, "g") + "% e vende " + format(ALL_TRAIL * 100, "g") + "% abaixo do topo · stop −" + format(SL * 100, "g") +
         "% · máx. 2 abertas (US$ 60) · velas de 4 h reais · custo " + format(bt.COST * 100, "g") + "% por lado",
         "• primeiros 2/3: " + f(rep["first"]), "• último 1/3 (" + d(rep["cut"]) + " em diante): " + f(rep["last"]),
         "• período todo (~" + format((rep["end"] - rep["start"]) / 86400 / 30, ".1f") + " meses): " + f(rep["full"]),
         "• vendas: " + str(rep["full"]["trades"]) + " no período",
         "• SÓ o stop móvel, SEM stop: último 1/3 " + format(rep["nostop_last"]["pnl"], "+.2f") + " · período todo " + f(rep["nostop_full"])]
    if best:
        L.append("• melhores: " + ", ".join(c + " " + str(n) + "/" + format(p, "+.2f") for c, (n, p) in best[:6]))
        L.append("• piores: " + ", ".join(c + " " + str(n) + "/" + format(p, "+.2f") for c, (n, p) in best[-4:]))
    L.append("⚠️ Só moedas que HOJE têm US$ " + format(ALL_MIN_LIQ, ",.0f") + " a " + format(ALL_MAX_LIQ, ",.0f") + " de liquidez (as que morreram não aparecem: o real tende a ser pior). "
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


def wallet_text(sol_usd=None):
    """The wallet THIS robot buys and sells with: address, SOL, US$ value, coins held (owner, Nov 17)."""
    import live_engine
    import live_order_bridge as bridge
    addr = live_engine.get_address()
    if not addr:
        return "⛔ A carteira do robô ainda não foi criada."
    try:
        px = float(sol_usd if sol_usd is not None else __import__("copy_trader")._sol_usd())
    except Exception:
        px = 0.0
    try:
        sol = bridge.get_sol_balance(addr) / 1e9
        sol_line = "◎ " + format(sol, ".4f") + " SOL" + (" (≈ US$ " + format(sol * px, ",.2f") + ")" if px else "")
        n_room = room(sol * px) if px else 0
    except Exception as exc:
        sol_line, n_room = "◎ saldo de SOL indisponível agora (" + type(exc).__name__ + ")", 0
    coins = []
    try:
        import wallet_holdings
        held = wallet_holdings.coins_held(addr) or {}          # refreshed every 30 s in the background
        coins = sorted(held.items(), key=lambda kv: -kv[1])
    except Exception:
        pass
    L = ["💰 CARTEIRA DO ROBÔ BAIXA (é com ela que ele compra e vende)", addr, "https://solscan.io/account/" + addr, sol_line]
    if coins:
        L.append("🪙 Moedas: " + ", ".join(m[:4] + "… US$ " + format(v, ",.2f") for m, v in coins[:8]))
    L.append("🛒 Dá para " + str(min(n_room, MAX_OPEN)) + " compra(s) de US$ " + format(USD, "g") + " agora (cada uma precisa de US$ " +
             format(need_usd(), ".2f") + " livres em SOL: ordem + US$ " + format(KEEP_SOL_USD, "g") + " de reserva + taxas)." if px
             else "🛒 Sem preço do SOL agora para calcular as compras.")
    L.append("Confira se esse endereço é o mesmo da carteira onde você vê o seu SOL (por exemplo, no Phantom).")
    return "\n".join(L)
