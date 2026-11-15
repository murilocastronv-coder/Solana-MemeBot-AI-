"""DEV copy trading (owner's spec, Nov 15) - SIMULATION ONLY, no real money.

Finds the developer (creator) of each new Solana memecoin, rates the dev from its own on-chain trading
history and, when an approved dev buys its token, buys in simulation; when the dev sells, sells the same
share. Independent stop loss / take profit / trailing stop. Every decision is logged ("devbot ...").

Honest limits: the dev is the wallet that paid the token's first transaction (creator); related wallets
are not merged automatically. Prices are DexScreener's at the moment the bot sees the trade (seconds after
the dev), with estimated swap cost and slippage. Nothing here promises profit.
Storage: PostgreSQL on Render (DATABASE_URL, table kv/events) with the persistent disk (dev_bot.json) as backup.
"""
import json
import logging
import os
import tempfile
import threading
import time

import signals as sg

E = lambda k, d: sg._f(k, d)   # noqa: E731 - every number configurable on Render

START_USD = E("DEV_SIM_START_USD", 500)
BUY_USD = E("DEV_BUY_USD", 5)
MIN_SCORE = E("DEV_MIN_SCORE", 80)
MIN_LIQ = E("DEV_MIN_LIQUIDITY_USD", 20_000)
MIN_VOL_5M = E("DEV_MIN_VOLUME_5M", 5_000)
MAX_OPEN = int(E("DEV_MAX_OPEN", 3))
MAX_RUNUP_5M = E("DEV_MAX_RUNUP_5M", 30)          # % in 5 min: too late to enter
# owner (Nov 15, later): mirror the dev - no stop loss, no take profit, no time limit; it sells only when
# the dev sells (the same share). 0 = off. The anti-scam exit (dev hides its coins) stays.
MIRROR_DEV = os.getenv("DEV_MIRROR", "true").strip().lower() != "false"
STOP_LOSS = E("DEV_STOP_LOSS", 0)
TP1 = E("DEV_TP1", 0)
TP1_PART = E("DEV_TP1_PART", 0.50)
TRAIL = E("DEV_TRAIL", 0.08)
MAX_HOLD_S = E("DEV_MAX_HOLD_HOURS", 0) * 3600
COST = E("DEV_COST_PER_SIDE", 0.02)               # swap fee + slippage per side (estimated)
NET_FEE_USD = E("DEV_NETWORK_FEE_USD", 0.01)
CYCLE_S = E("DEV_CYCLE_SECONDS", 30)
NEW_TOKENS_PER_CYCLE = int(E("DEV_NEW_TOKENS_PER_CYCLE", 12))
DEV_AUDITS_PER_CYCLE = int(E("DEV_AUDITS_PER_CYCLE", 5))
QUICK_SIGS = int(E("DEV_QUICK_MIN_TX", 40))     # fewer transactions than this can't hold 20 buy+sell trades
DEV_AUDIT_TX = int(E("DEV_AUDIT_TX", 300))
DEV_REAUDIT_S = E("DEV_REAUDIT_HOURS", 12) * 3600
WATCH_S = E("DEV_WATCH_HOURS", 24) * 3600
# Approval (second spec): hit rate, closed trades, distinct tokens, profit factor, no rug pattern
TIERS = (("ELITE", 0.90, 30, 2.5), ("PREMIUM", 0.85, 25, 2.0), ("APROVADO", 0.80, 20, 2.0))
MIN_TOKENS = int(E("DEV_MIN_TOKENS", 10))
MAX_TOP_COIN_SHARE = E("DEV_MAX_TOP_COIN_SHARE", 0.60)     # profit from one coin only = excluded
RUG_LOSS = 0.80
# owner (Nov 15): the dev can hold at most 35% of the coin's supply (more = it can crash the price)
MAX_DEV_SHARE = E("DEV_MAX_HOLD_SHARE", 0.35)
# anti-scam: the dev moves this share (or more) of its coins to another wallet without selling -> exit + block
TRANSFER_EXIT = E("DEV_TRANSFER_EXIT", 0.25)
MAX_RUNUP_DEV = E("DEV_MAX_RUNUP_SINCE_DEV", 0.15)   # no buy if the price rose 15%+ since the dev's buy
DAILY_LOSS_USD = E("DEV_DAILY_LOSS_USD", 0)        # 0 = no daily loss limit (owner's choice); e.g. 15 stops buys for the day
BT_PERIODS = (7, 30, 90, 180)
BT_MAX_RET = E("DEV_BT_MAX_RESULT", 10.0)      # a dev trade above +1000% (free creator tokens, near-zero cost) is not
BT_MIN_COST_USD = E("DEV_BT_MIN_COST_USD", 5)    # something a follower could get: left out of the backtests


def bt_ok(h):
    """A dev trade a follower could really have copied (no free tokens / dust cost / absurd return)."""
    r = float(h.get("result_pct") or 0)
    cost = h.get("cost_usd")
    return r <= BT_MAX_RET and (cost is None or float(cost) >= BT_MIN_COST_USD)
BT_PERIOD_CAPITAL = E("DEV_BT_PERIOD_CAPITAL", 500)
CURVE_MIN_MCAP = E("DEV_CURVE_MIN_MCAP", 5_000)      # coins still on the pump.fun curve (no pool yet)
CURVE_MIN_VOL_5M = E("DEV_CURVE_MIN_VOLUME_5M", 1_000)
# owner (Nov 15): only pump.fun coins (launch curve, PumpSwap or a pump.fun mint)
PUMP_ONLY = os.getenv("DEV_PUMP_ONLY", "true").strip().lower() != "false"
BT_CAPITAL = E("DEV_BT_CAPITAL", 60)
BT_DAYS = E("DEV_BT_DAYS", 365)                    # owner: 1 year
BT_DEVS = int(E("DEV_BT_DEVS", 300))               # devs downloaded per click (the next click continues)
BT_TARGET = int(E("DEV_BT_TARGET", 10_000))        # owner: test with 10,000 devs (reached over several clicks)
BT_MAX_S = E("DEV_BT_MAX_MINUTES", 15) * 60        # one click never runs longer than this

_LOCK = threading.RLock()
LAST = {"at": 0, "why": "", "tokens": 0, "devs": 0}


def _path():
    import live_trade_state
    return live_trade_state._path().with_name("dev_bot.json")


def _load():
    d = {"enabled": True, "cash": START_USD, "start": START_USD, "positions": {}, "history": [], "tokens": {},
         "devs": {}, "equity_peak": START_USD, "max_dd": 0.0, "fees": 0.0, "log": [], "backtest": ""}
    import pg_store
    if pg_store.enabled():
        try:
            got = pg_store.get("dev_bot")
            if got is not None:
                d.update(got)
                return d
        except Exception:
            pass                                 # database down: read the disk copy below
    p = _path()
    if p.exists():
        try:
            d.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return d


def _save(d):
    import pg_store
    if pg_store.enabled():
        try:
            pg_store.put("dev_bot", d)
        except Exception as exc:
            logging.warning("devbot: postgres save failed (%s), disk only", type(exc).__name__)
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".devbot.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def note(text):
    """Decision log (Render logs + the last 200 in the state for the panel)."""
    logging.info("devbot %s", text)
    try:
        import pg_store
        if pg_store.enabled():
            pg_store.event("devbot", {"text": text})
    except Exception:
        pass
    with _LOCK:
        d = _load()
        d["log"] = (d.get("log") or [])[-199:] + [time.strftime("%d/%m %H:%M", time.gmtime()) + " " + text]
        _save(d)


def set_enabled(on):
    with _LOCK:
        d = _load()
        d["enabled"] = bool(on)
        _save(d)


def enabled():
    return bool(_load().get("enabled"))


def set_buy_usd(usd):
    usd = round(float(str(usd).replace(",", ".")), 2)
    if not 1 <= usd <= 1000:
        raise ValueError("valor entre US$ 1 e US$ 1000")
    with _LOCK:
        d = _load()
        d["buy_usd"] = usd
        _save(d)
    return usd


def real():
    return bool(_load().get("real"))


def set_real(on):
    with _LOCK:
        d = _load()
        d["real"] = bool(on)
        _save(d)


def buy_usd():
    return float(_load().get("buy_usd") or BUY_USD)


# ---------------------------------------------------------------- dev rating
def rate_dev(closed, now=None):
    """Stats, DEV SCORE 0-100 and tier from the dev's closed trades (wallet_profile positions)."""
    now = now or time.time()
    n = len(closed)
    net = [float(p.get("pnl_usd") or 0) - 2 * COST * float(p.get("cost_closed") or 0) for p in closed]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    pnl = sum(net)
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) < 0 else (99.0 if wins else 0.0)
    win = len(wins) / n if n else 0.0
    by_coin = {}
    for p, x in zip(closed, net):
        by_coin[p.get("mint")] = by_coin.get(p.get("mint"), 0.0) + x
    tokens = len(by_coin)
    top_share = (max(by_coin.values()) / pnl) if pnl > 0 and by_coin else 1.0
    rugs = sum(1 for p in closed if float(p.get("result_pct") or 0) <= -RUG_LOSS)
    eq, peak, dd = 0.0, 0.0, 0.0
    for p, x in sorted(zip(closed, net), key=lambda t: t[0].get("closed", 0)):
        eq += x
        peak = max(peak, eq)
        base = peak + 1000.0                      # drawdown relative to a 1,000 bankroll (measurable proxy)
        dd = max(dd, (peak - eq) / base)
    holds = sorted(float(p.get("hold_min") or 0) for p in closed)
    weeks = {}
    for p, x in zip(closed, net):
        k = int((now - float(p.get("closed", now))) // (7 * 86400))
        weeks[k] = weeks.get(k, 0.0) + x
    week_share = (sum(1 for v in weeks.values() if v > 0) / len(weeks)) if weeks else 0.0
    score = (25 * min(tokens / 20, 1) + 20 * min((holds[len(holds) // 2] if holds else 0) / 30, 1)
             + 20 * (1 - min(rugs / max(n, 1) / 0.2, 1)) + 15 * min(pf / 3, 1) + 10 * min(n / 40, 1)
             + 10 * week_share)
    stats = {"trades": n, "win": round(win, 3), "pnl": round(pnl, 2), "pf": round(pf, 2), "tokens": tokens,
             "rugs": rugs, "top_share": round(top_share, 2), "max_dd": round(dd, 3), "score": round(score, 1)}
    stats["tier"], stats["why"] = tier(stats)
    return stats


def tier(s):
    """('ELITE' | 'PREMIUM' | 'APROVADO' | 'MONITORADO', why)."""
    if s["trades"] < TIERS[-1][2]:
        return "MONITORADO", "poucas operações (" + str(s["trades"]) + ")"
    if s["tokens"] < MIN_TOKENS:
        return "MONITORADO", "poucos tokens diferentes (" + str(s["tokens"]) + ")"
    if s["pnl"] <= 0:
        return "MONITORADO", "sem lucro líquido"
    if s["rugs"] > 0:
        return "MONITORADO", "tem operações que viraram rug (" + str(s["rugs"]) + ")"
    if s["top_share"] > MAX_TOP_COIN_SHARE:
        return "MONITORADO", "lucro concentrado em uma moeda"
    if s["max_dd"] > 0.20:
        return "MONITORADO", "drawdown alto"
    for name, w, n, pf in TIERS:
        if s["win"] >= w and s["trades"] >= n and s["pf"] >= pf:
            if s["score"] < MIN_SCORE:
                return "MONITORADO", "DEV SCORE " + format(s["score"], ".0f") + " < " + format(MIN_SCORE, ".0f")
            return name, ""
    return "MONITORADO", "acerto " + format(s["win"] * 100, ".0f") + "% / PF " + format(s["pf"], ".1f")


def approved(dev_info):
    return (dev_info or {}).get("tier") in ("ELITE", "PREMIUM", "APROVADO")


# ---------------------------------------------------------------- chain helpers
def creator_of(mint, rpc, get_tx):
    """Fee payer of the token's oldest reachable transaction (the creator). None if not found."""
    sigs, before = [], None
    for _ in range(3):
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        page = rpc("getSignaturesForAddress", [mint, opts]) or []
        if not page:
            break
        sigs += page
        before = page[-1]["signature"]
        if len(page) < 1000:
            break
    else:
        return None                                   # too many trades to reach the creation
    if not sigs:
        return None
    tx = get_tx(rpc, sigs[-1]["signature"])
    keys = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    if not keys:
        return None
    k = keys[0]
    return k.get("pubkey") if isinstance(k, dict) else str(k)


def dev_share(rpc, mint, held_raw):
    """Share of the token supply the dev holds after its buy (None if the supply can't be read)."""
    try:
        v = (rpc("getTokenSupply", [mint]) or {}).get("value") or {}
        supply = int(v.get("amount") or 0)
    except Exception:
        return None
    return held_raw / supply if supply > 0 else None


def dev_move(tx, mint, dev, after=False):
    """('buy'|'sell'|'transfer'|None, fraction of the dev's position sold, raw tokens before - or after)."""
    meta = (tx or {}).get("meta") or {}
    if meta.get("err"):
        return None, 0.0, 0
    pre = sum(int((b.get("uiTokenAmount") or {}).get("amount") or 0) for b in meta.get("preTokenBalances") or []
              if b.get("owner") == dev and b.get("mint") == mint)
    post = sum(int((b.get("uiTokenAmount") or {}).get("amount") or 0) for b in meta.get("postTokenBalances") or []
               if b.get("owner") == dev and b.get("mint") == mint)
    import wallet_copy
    _, sol = wallet_copy._tx_deltas(tx)
    sol_d = int(sol.get(dev, 0))
    ref = post if after else pre
    if post > pre:
        return ("buy" if sol_d < 0 else "transfer"), 0.0, ref
    if post < pre:
        frac = (pre - post) / pre if pre else 1.0
        return ("sell" if sol_d > 0 else "transfer"), frac, ref
    return None, 0.0, ref


# ---------------------------------------------------------------- simulation
def _equity(d, prices=None):
    prices = prices or {}
    val = d["cash"]
    for m, p in d["positions"].items():
        val += p["qty"] * prices.get(m, p["last"]) * (1 - COST)
    return val


def _track(d, prices=None):
    eq = _equity(d, prices)
    d["equity_peak"] = max(float(d.get("equity_peak") or eq), eq)
    if d["equity_peak"] > 0:
        d["max_dd"] = max(float(d.get("max_dd") or 0), (d["equity_peak"] - eq) / d["equity_peak"])


def sim_buy(p, dev, reason, now=None, send=None, chat=None, lag=None):
    now = now or time.time()
    if DAILY_LOSS_USD > 0 and metrics(now=now)["pnl_day"] <= -DAILY_LOSS_USD:
        note("limite de perda do dia (US$ " + format(DAILY_LOSS_USD, "g") + ") atingido: sem compras até amanhã")
        return False
    mint = p["baseToken"]["address"]
    price = float(p.get("priceUsd") or 0)
    usd = buy_usd()
    is_real = real()
    with _LOCK:
        d = _load()
        if price <= 0 or mint in d["positions"] or len(d["positions"]) >= MAX_OPEN:
            return False
        if not is_real and d["cash"] < usd + NET_FEE_USD:
            return False
        if any(h.get("mint") == mint for h in d["history"]):
            return False                          # one entry per token
    if is_real:
        try:
            import live_engine
            settled = live_engine._execute_buy({"baseToken": p.get("baseToken"), "liquidity": p.get("liquidity"),
                                                "marketCap": p.get("marketCap")}, mint,
                                               (p.get("baseToken") or {}).get("symbol") or mint[:6], send, chat,
                                               live_engine.get_address(), None, "dev", usd=usd)
        except Exception as exc:
            note("compra REAL falhou " + mint[:6] + ": " + type(exc).__name__)
            return False
        if not settled or settled.get("status") != "settled_buy":
            note("compra REAL não confirmada " + mint[:6])
            return False
    with _LOCK:
        d = _load()
        d["cash"] -= usd + NET_FEE_USD
        d["fees"] = float(d.get("fees") or 0) + usd * COST + NET_FEE_USD
        d["positions"][mint] = {"symbol": (p.get("baseToken") or {}).get("symbol") or mint[:6], "dev": dev,
                                "entry": price, "last": price, "peak": 1.0, "qty": usd * (1 - COST) / price,
                                "cost": usd + NET_FEE_USD, "at": now, "tp1": False, "reason": reason,
                                "realized": 0.0, "real": is_real, "lag_s": lag}
        _track(d)
        _save(d)
    note("COMPROU (" + ("REAL" if is_real else "simulação") + ") " + d["positions"][mint]["symbol"] + " US$ " + format(usd, "g") + " a " +
         format(price, ".8g") + " — " + reason)
    if send and chat and not is_real:
        send(chat, "🧑‍💻 DEV BOT (SIMULAÇÃO) COMPROU " + d["positions"][mint]["symbol"] + " — US$ " + format(usd, "g") +
             "\n📝 " + reason + "\n📄 `" + mint + "`")
    return True


def _sell(d, mint, frac, price, why, now):
    pos = d["positions"][mint]
    qty = pos["qty"] * min(frac, 1.0)
    got = qty * price * (1 - COST) - NET_FEE_USD
    d["fees"] = float(d.get("fees") or 0) + qty * price * COST + NET_FEE_USD
    pos["qty"] -= qty
    pos["realized"] += got
    d["cash"] += got
    if frac < 0.999 and pos["qty"] > 0:
        return None
    d["positions"].pop(mint)
    h = {"mint": mint, "symbol": pos["symbol"], "dev": pos["dev"], "entry": pos["entry"], "exit": price,
         "cost": pos["cost"], "proceeds": round(pos["realized"], 4), "pnl": round(pos["realized"] - pos["cost"], 4),
         "opened": pos["at"], "closed": now, "why_in": pos["reason"], "why_out": why, "lag_s": pos.get("lag_s")}
    d["history"] = d["history"][-999:] + [h]
    return h


def exits(prices, now=None, send=None, chat=None, dev_sells=None):
    """SL / TP1 (50%) / trailing / max hold / dev sells {mint: fraction}."""
    now = now or time.time()
    dev_sells = dev_sells or {}
    out, real_orders = [], []
    with _LOCK:
        d = _load()
        for mint, pos in list(d["positions"].items()):
            price = prices.get(mint)
            if not price:
                continue
            pos["last"] = price
            r = price / pos["entry"]
            pos["peak"] = max(pos["peak"], r)
            why, frac = None, 0.0
            sold = dev_sells.get(mint)
            if sold is not None and MIRROR_DEV:
                # owner (Nov 15): do exactly what the dev does - the same share it sold
                why, frac = ("DEV vendeu tudo: vendeu tudo" if sold >= 0.95 else
                             "DEV vendeu " + format(sold * 100, ".0f") + "%: vendeu a mesma parte"), (1.0 if sold >= 0.95 else sold)
            elif sold is not None and sold >= 0.75:
                why, frac = "DEV vendeu " + format(sold * 100, ".0f") + "%: encerrou", 1.0
            elif sold is not None and sold >= 0.25:
                why, frac = "DEV vendeu " + format(sold * 100, ".0f") + "%: vendeu a mesma parte", (0.5 if sold >= 0.5 else 0.25)
            elif STOP_LOSS > 0 and r <= 1 - STOP_LOSS:
                why, frac = "stop loss −" + format(STOP_LOSS * 100, ".0f") + "%", 1.0
            elif TP1 > 0 and not pos["tp1"] and r >= 1 + TP1:
                why, frac = "take profit +" + format(TP1 * 100, ".0f") + "% (vendeu " + format(TP1_PART * 100, ".0f") + "%)", TP1_PART
                pos["tp1"] = True
            elif pos["tp1"] and r <= pos["peak"] * (1 - TRAIL):
                why, frac = "stop móvel " + format(TRAIL * 100, ".0f") + "% abaixo do topo", 1.0
            elif MAX_HOLD_S > 0 and now - pos["at"] > MAX_HOLD_S:
                why, frac = "tempo máximo", 1.0
            if not why:
                continue
            if pos.get("real"):
                real_orders.append((mint, frac, why))
            h = _sell(d, mint, frac, price, why, now)
            out.append((pos["symbol"], why, h))
        _track(d, prices)
        _save(d)
    for mint, frac, why in real_orders:
        try:
            import live_engine
            if frac >= 0.999:
                live_engine.sell_now(mint, "dev_exit", send, chat)
            else:
                live_engine.sell_part(mint, frac, "dev_exit", send, chat, note="robô DEV: " + why)
        except Exception as exc:
            note("venda REAL falhou " + mint[:6] + ": " + type(exc).__name__ + " (" + why + ")")
    for sym, why, h in out:
        note("VENDEU (simulação) " + sym + " — " + why + ("" if not h else " · P/L " + format(h["pnl"], "+.2f")))
        if send and chat:
            send(chat, ("💰" if (h and h["pnl"] >= 0) else "🔻" if h else "💵") + " DEV BOT (SIMULAÇÃO) " + sym + " — " + why +
                 ("" if not h else "\nP/L " + format(h["pnl"], "+.2f") + " USD"))
    return out


def buy_reason(p, info):
    """'' when the coin passes the entry filters, else why not."""
    liq = float((p.get("liquidity") or {}).get("usd") or 0)
    vol5 = float((p.get("volume") or {}).get("m5") or 0)
    up5 = float((p.get("priceChange") or {}).get("m5") or 0)
    if PUMP_ONLY and not sg.is_pump(p):
        return "não é moeda da pump.fun"
    if not approved(info):
        return "DEV não aprovado (" + (info or {}).get("tier", "sem análise") + ")"
    if float(info.get("score") or 0) < MIN_SCORE:
        return "DEV SCORE baixo"
    if p.get("dexId") == "pumpfun":
        # still on the bonding curve: no pool liquidity yet - judge by market cap and activity instead
        mcap = float(p.get("marketCap") or p.get("fdv") or 0)
        if mcap < CURVE_MIN_MCAP:
            return "curva da pump.fun: market cap US$ " + format(mcap, ",.0f") + " < US$ " + format(CURVE_MIN_MCAP, ",.0f")
        if vol5 < CURVE_MIN_VOL_5M:
            return "curva da pump.fun: volume 5 min US$ " + format(vol5, ",.0f") + " < US$ " + format(CURVE_MIN_VOL_5M, ",.0f")
    else:
        if liq < MIN_LIQ:
            return "liquidez US$ " + format(liq, ",.0f") + " < US$ " + format(MIN_LIQ, ",.0f")
        if vol5 < MIN_VOL_5M:
            return "volume 5 min US$ " + format(vol5, ",.0f") + " < US$ " + format(MIN_VOL_5M, ",.0f")
    if up5 > MAX_RUNUP_5M:
        return "já subiu " + format(up5, ".0f") + "% em 5 min (entrada atrasada)"
    return ""


# ---------------------------------------------------------------- 24/7 cycle
def dev_moves(tx, dev):
    """Every coin the dev's balance changed in this tx: [{mint, kind, frac, held, sol, tokens}]."""
    meta = (tx or {}).get("meta") or {}
    if meta.get("err"):
        return []
    mints = {b.get("mint") for b in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or [])
             if b.get("owner") == dev and b.get("mint")}
    out = []
    for m in mints:
        kind, frac, held = dev_move(tx, m, dev, after=True)
        if not kind:
            continue
        dec = 0
        pre = post = 0.0
        for side, rows in (("pre", meta.get("preTokenBalances") or []), ("post", meta.get("postTokenBalances") or [])):
            for b in rows:
                if b.get("owner") == dev and b.get("mint") == m:
                    ui = b.get("uiTokenAmount") or {}
                    dec = int(ui.get("decimals") or 0)
                    v = int(ui.get("amount") or 0) / (10 ** dec)
                    if side == "pre":
                        pre += v
                    else:
                        post += v
        import wallet_copy
        _, sol = wallet_copy._tx_deltas(tx)
        out.append({"mint": m, "kind": kind, "frac": frac, "held": held, "sol": int(sol.get(dev, 0)),
                    "tokens": abs(post - pre)})
    return out


def handle_move(mv, dev, info, tx, by_mint, rpc, sol_usd, now, send=None, chat=None, safety=None, dev_sells=None):
    """React to one dev move: copy a buy (filters), mirror a sell, exit on a hidden transfer."""
    mint, kind, frac = mv["mint"], mv["kind"], mv["frac"]
    dev_sells = dev_sells if dev_sells is not None else {}
    held_pos = mint in _load()["positions"]
    sym = ((by_mint.get(mint) or {}).get("baseToken") or {}).get("symbol") or mint[:6]
    if kind == "buy":
        if held_pos:
            return
        p = by_mint.get(mint) or _pair_of(mint)
        why = "sem dados da DexScreener (moeda nova demais)" if p is None else buy_reason(p, info)
        dev_price = (abs(mv["sol"]) / 1e9 * float(sol_usd or 0) / mv["tokens"]) if mv["tokens"] and sol_usd else None
        if not why and dev_price:
            now_price = float(p.get("priceUsd") or 0)
            run = now_price / dev_price - 1 if dev_price > 0 else 0
            if run > MAX_RUNUP_DEV:
                why = ("preço já subiu " + format(run * 100, ".0f") + "% desde a compra do DEV (máx. " +
                       format(MAX_RUNUP_DEV * 100, ".0f") + "%)")
        if not why:
            share = dev_share(rpc, mint, mv["held"])
            if share is None:
                why = "não consegui medir quanto o DEV tem da moeda"
            elif share > MAX_DEV_SHARE:
                why = "DEV tem " + format(share * 100, ".0f") + "% da moeda (máx. " + format(MAX_DEV_SHARE * 100, ".0f") + "%)"
        if not why:
            r = (safety or sg.safety_reason)(mint)
            why = ("anti-golpe: " + r) if r else ""
        lag = now - float((tx or {}).get("blockTime") or now)
        if why:
            note("DEV " + dev[:6] + "… comprou " + sym + " — NÃO copiei: " + why)
            return
        sim_buy(p, dev, "DEV " + info["tier"] + " (acerto " + format(info["win"] * 100, ".0f") + "%, score " +
                format(info["score"], ".0f") + ") comprou · " + format(lag, ".0f") + " s depois do DEV", now, send, chat, lag=lag)
    elif kind == "sell":
        dev_sells[mint] = max(dev_sells.get(mint, 0), frac)
        note("DEV " + dev[:6] + "… vendeu " + format(frac * 100, ".0f") + "% de " + sym)
    elif kind == "transfer" and frac >= TRANSFER_EXIT:
        dev_sells[mint] = 1.0
        note("🚨 DEV " + dev[:6] + "… moveu " + format(frac * 100, ".0f") + "% dos tokens de " + sym +
             " para outra carteira (pode ser golpe): vendo tudo e bloqueando o DEV")
        with _LOCK:
            dd = _load()
            dd["devs"].setdefault(dev, {}).update(tier="BLOQUEADO", why="transferiu tokens escondido (suspeita de golpe)",
                                                  rated=now + 30 * 86400)
            _save(dd)
        if send and chat and held_pos:
            send(chat, "🚨 DEV BOT: o DEV de " + sym + " moveu " + format(frac * 100, ".0f") +
                 "% dos tokens para outra carteira sem vender — possível golpe. Vendendo tudo e bloqueando esse DEV.")


def _pair_of(mint):
    try:
        rows = sg._get(sg.DEX + "/tokens/v1/solana/" + mint) or []
        rows = [r for r in rows if isinstance(r, dict)]
        return max(rows, key=lambda r: float((r.get("liquidity") or {}).get("usd") or 0)) if rows else None
    except Exception:
        return None


def emergency_stop(send=None, chat=None, now=None):
    """Owner's panic button: robot paused (no buys) and every open position sold now."""
    set_enabled(False)
    d = _load()
    prices = {m: p["last"] for m, p in d["positions"].items()}
    out = exits(prices, now, send, chat, {m: 1.0 for m in d["positions"]})
    note("🛑 PARADA DE EMERGÊNCIA: robô pausado, " + str(len(out)) + " posição(ões) vendida(s)")
    return len(out)


def audit_devs(rpc, sol_usd, now=None, analyze=None, limit=None):
    """Rate the devs waiting in line (never rated first). A wallet with too few transactions to have
    MIN trades is dismissed with one cheap call instead of a full history read. Returns devs rated."""
    now = now or time.time()
    d = _load()
    queue = [(k, v) for k, v in sorted(d["devs"].items(), key=lambda kv: kv[1].get("rated", 0))
             if now - float(v.get("rated") or 0) >= DEV_REAUDIT_S][:limit or DEV_AUDITS_PER_CYCLE]
    done = 0
    for dev, info in queue:
        stats = None
        try:
            sigs = rpc("getSignaturesForAddress", [dev, {"limit": QUICK_SIGS}]) or []
            if len(sigs) < QUICK_SIGS:
                stats = {"tier": "MONITORADO", "why": "carteira com pouco histórico (" + str(len(sigs)) + " transações)",
                         "trades": 0, "hist": [], "hist_v2": True}
        except Exception:
            pass
        if stats is None:
            try:
                import wallet_profile
                _, closed, _ = (analyze or wallet_profile.analyze)(dev, rpc, sol_usd, max_tx=DEV_AUDIT_TX)
                stats = rate_dev(closed, now)
                stats["hist"] = _hist(closed)[-300:]
            except Exception as exc:
                stats = {"tier": "ERRO", "why": type(exc).__name__}
        with _LOCK:
            dd = _load()
            dd["devs"].setdefault(dev, {"tokens_seen": []}).update(stats, rated=now)
            _save(dd)
        done += 1
        note("DEV " + dev[:6] + "… " + stats.get("tier", "?") + (" — " + stats["why"] if stats.get("why") else "") +
             (" · acerto " + format(stats["win"] * 100, ".0f") + "% · " + str(stats["trades"]) + " ops · PF " +
              format(stats["pf"], ".1f") + " · score " + format(stats["score"], ".0f") if "win" in stats else ""))
    return done


def cycle(send=None, chat=None, rpc=None, pairs=None, now=None, get_tx=None, sol_usd=None, safety=None,
          analyze=None, prices_fn=None, do_audit=True):
    """One pass: new tokens -> their dev; rate devs; watch approved devs' buys / sells; exits."""
    now = now or time.time()
    d = _load()
    if not d.get("enabled"):
        return
    import dex_wallets
    get_tx = get_tx or dex_wallets._get_tx
    pairs = sg.candidate_pairs() if pairs is None else pairs
    by_mint = {(p.get("baseToken") or {}).get("address"): p for p in pairs}
    # 1) new tokens (born in the last WATCH_S) -> creator
    fresh = [p for p in pairs if p.get("pairCreatedAt") and now - float(p["pairCreatedAt"]) / 1000 < WATCH_S
             and (not PUMP_ONLY or sg.is_pump(p))]
    found = 0
    for p in fresh:
        mint = p["baseToken"]["address"]
        if mint in d["tokens"] or found >= NEW_TOKENS_PER_CYCLE:
            continue
        found += 1
        try:
            dev = creator_of(mint, rpc, get_tx)
        except Exception as exc:
            logging.warning("devbot creator failed %s: %s", mint[:8], type(exc).__name__)
            continue
        with _LOCK:
            dd = _load()
            dd["tokens"][mint] = {"dev": dev, "seen": now, "symbol": (p.get("baseToken") or {}).get("symbol"),
                                  "last_sig": None}
            if dev:
                dd["devs"].setdefault(dev, {"tokens_seen": [], "tier": "NA FILA", "rated": 0})
                if mint not in dd["devs"][dev]["tokens_seen"]:
                    dd["devs"][dev]["tokens_seen"] = (dd["devs"][dev]["tokens_seen"] + [mint])[-20:]
            dd["tokens"] = {m: t for m, t in dd["tokens"].items() if now - t["seen"] < 3 * WATCH_S}
            _save(dd)
    # 2) rate devs (oldest rating first) - the 24/7 loop does this in its own thread (audit_devs)
    if do_audit:
        audit_devs(rpc, sol_usd, now, analyze)
    # 3) approved devs: read ALL their new transactions (new launches / first buys on any pump.fun coin,
    #    sells and hidden transfers of coins we hold)
    d = _load()
    dev_sells = {}
    for dev, info in list(d["devs"].items()):
        if not approved(info):
            continue
        try:
            opts = {"limit": 25}
            if info.get("last_sig"):
                opts["until"] = info["last_sig"]
            sigs = rpc("getSignaturesForAddress", [dev, opts]) or []
        except Exception:
            continue
        if sigs:
            with _LOCK:
                dd = _load()
                dd["devs"].setdefault(dev, {})["last_sig"] = sigs[0]["signature"]
                _save(dd)
        if not info.get("last_sig"):
            continue                                 # first read: only remember where we are
        for s in reversed(sigs):
            if s.get("err"):
                continue
            try:
                tx = get_tx(rpc, s["signature"])
            except Exception:
                continue
            for mv in dev_moves(tx, dev):
                handle_move(mv, dev, info, tx, by_mint, rpc, sol_usd, now, send, chat, safety, dev_sells)
    # 4) exits with live prices
    d = _load()
    if d["positions"]:
        prices = (prices_fn or _prices)(list(d["positions"]), by_mint)
        exits(prices, now, send, chat, dev_sells)
    LAST.update(at=now, tokens=len(_load()["tokens"]), devs=len(_load()["devs"]))


def _prices(mints, by_mint):
    out = {m: float(by_mint[m].get("priceUsd") or 0) for m in mints if m in by_mint and by_mint[m].get("priceUsd")}
    missing = [m for m in mints if m not in out]
    if missing:
        try:
            for p in sg._get(sg.DEX + "/tokens/v1/solana/" + ",".join(missing[:30])) or []:
                m = (p.get("baseToken") or {}).get("address")
                if m in missing and p.get("priceUsd"):
                    out[m] = float(p["priceUsd"])
        except Exception:
            pass
    return out


def close_all(now=None):
    now = now or time.time()
    with _LOCK:
        d = _load()
        for m, pos in list(d["positions"].items()):
            _sell(d, m, 1.0, pos["last"], "encerrada pelo dono", now)
        _track(d)
        _save(d)


# ---------------------------------------------------------------- report / backtest
def metrics(d=None, now=None):
    d = d or _load()
    now = now or time.time()
    h = d["history"]
    wins = [x["pnl"] for x in h if x["pnl"] > 0]
    losses = [x["pnl"] for x in h if x["pnl"] <= 0]
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    return {"equity": round(_equity(d), 2), "cash": round(d["cash"], 2), "start": d["start"],
            "pnl_total": round(_equity(d) - d["start"], 2),
            "pnl_day": round(sum(x["pnl"] for x in h if time.strftime("%Y-%m-%d", time.gmtime(x["closed"])) == day), 2),
            "open": len(d["positions"]), "trades": len(h), "win": round(len(wins) / len(h), 3) if h else 0.0,
            "pf": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) < 0 else None,
            "max_dd": round(float(d.get("max_dd") or 0), 3), "fees": round(float(d.get("fees") or 0), 2),
            "worst": round(min((x["pnl"] for x in h), default=0.0), 2)}


STRATEGIES = (("A: todos os DEVs", None), ("B: acerto ≥ 70%", 0.70), ("C: acerto ≥ 80%", 0.80),
              ("D: acerto ≥ 90%", 0.90))
PERIOD_STRATEGIES = (("A: acerto ≥ 70%", 0.70), ("B: acerto ≥ 80%", 0.80), ("C: acerto ≥ 90%", 0.90))


def backtest(dev_histories, start=500.0, usd=5.0, delay_cost=0.03, train_share=0.7, strategies=STRATEGIES):
    """Approximate follower backtest from the devs' REAL closed trades (dev_histories: {dev: [positions]}).
    The follower enters each dev trade with the dev's result minus delay, swap costs and slippage.
    Selection uses only trades the dev had CLOSED before each entry (no future data). Chronological split:
    thresholds are compared on the first `train_share` of time and reported on the rest (out of sample)."""
    events = sorted(((p.get("opened", 0), dev, p) for dev, ps in dev_histories.items() for p in ps
                     if p.get("closed") and p.get("opened")), key=lambda t: t[0])
    if not events:
        return None
    cut = events[int(len(events) * train_share)][0] if len(events) > 3 else events[-1][0]
    out = {}
    for name, need in strategies:
        res = {}
        for part, lo, hi in (("treino", -1, cut), ("teste", cut, float("inf"))):
            cash, peak, dd, n, wins, gw, gl, worst, fees = start, start, 0.0, 0, 0, 0.0, 0.0, 0.0, 0.0
            for t, dev, p in events:
                if not lo <= t < hi:
                    continue
                prior = [q for q in dev_histories[dev] if q.get("closed") and q["closed"] < t]
                if need is not None:
                    if len(prior) < 20 or sum(1 for q in prior if float(q.get("pnl_usd") or 0) > 0) / len(prior) < need:
                        continue
                if cash < usd:
                    break
                ret = float(p.get("result_pct") or 0) - delay_cost - 2 * COST
                pnl = usd * ret - 2 * NET_FEE_USD
                fees += usd * 2 * COST + 2 * NET_FEE_USD
                cash += pnl
                n += 1
                wins += pnl > 0
                gw += max(pnl, 0)
                gl += min(pnl, 0)
                worst = min(worst, pnl)
                peak = max(peak, cash)
                dd = max(dd, (peak - cash) / peak if peak else 0)
            res[part] = {"final": round(cash, 2), "pnl": round(cash - start, 2), "trades": n,
                         "win": round(wins / n, 3) if n else 0.0, "pf": round(gw / abs(gl), 2) if gl < 0 else None,
                         "max_dd": round(dd, 3), "worst": round(worst, 2), "fees": round(fees, 2)}
        out[name] = res
    return out


def _hist(closed):
    return [{"opened": c.get("opened"), "closed": c.get("closed"), "result_pct": round(float(c.get("result_pct") or 0), 4),
             "pnl_usd": round(float(c.get("pnl_usd") or 0), 2), "mint": c.get("mint"), "platform": c.get("platform"),
             "cost_usd": round(float(c.get("cost_closed") or 0), 2) if c.get("cost_closed") is not None else None}
            for c in closed]


def pump_trade(h):
    """A dev trade on a pump.fun coin (launch curve / PumpSwap / pump.fun mint)."""
    return str(h.get("mint") or "").endswith("pump") or str(h.get("platform") or "").lower() in ("pump.fun", "pumpswap")


def run_backtest(rpc=None, sol_usd=None, analyze=None, capital=None, days=None, now=None):
    """Owner's button: download the history of up to BT_DEVS devs not analysed yet, then backtest the last
    `days` (2 months) with `capital` (US$ 60). Reports how much history was really available."""
    now = now or time.time()
    capital = float(capital or BT_CAPITAL)
    days = float(days or BT_DAYS)
    if rpc is not None:
        d = _load()
        # devs never downloaded, or downloaded before the coin / platform was stored (pump.fun filter)
        todo = [k for k, v in d["devs"].items() if "hist" not in v or (v["hist"] and "mint" not in v["hist"][0])
                or (not v["hist"] and not v.get("hist_v2"))][:BT_DEVS]
        started = time.time()
        for dev in todo:
            if time.time() - started > BT_MAX_S:
                break                                   # the next click continues from here
            stats = None
            try:
                sigs = rpc("getSignaturesForAddress", [dev, {"limit": QUICK_SIGS}]) or []
                if len(sigs) < QUICK_SIGS:
                    stats = {"tier": "MONITORADO", "why": "carteira com pouco histórico (" + str(len(sigs)) + " transações)",
                             "trades": 0, "hist": []}
            except Exception:
                pass
            if stats is None:
                try:
                    import wallet_profile
                    _, closed, _ = (analyze or wallet_profile.analyze)(dev, rpc, sol_usd, max_tx=max(DEV_AUDIT_TX, 1000))
                    stats = rate_dev(closed, now)
                    stats["hist"] = _hist(closed)[-1000:]
                except Exception as exc:
                    stats = {"tier": "ERRO", "why": type(exc).__name__}
            with _LOCK:
                dd = _load()
                dd["devs"].setdefault(dev, {"tokens_seen": []}).update(stats, rated=now, hist_v2=True)
                _save(dd)
    d = _load()
    since = now - days * 86400
    hist = {k: [h for h in v["hist"] if (h.get("opened") or 0) >= since and (not PUMP_ONLY or pump_trade(h)) and bt_ok(h)]
            for k, v in d["devs"].items() if v.get("hist")}
    hist = {k: v for k, v in hist.items() if v}
    dropped = sum(1 for v in d["devs"].values() for h in v.get("hist") or []
                  if (h.get("opened") or 0) >= since and (not PUMP_ONLY or pump_trade(h)) and not bt_ok(h))
    first = min((h["opened"] for v in hist.values() for h in v), default=None)
    usd = max(1.0, min(buy_usd(), capital / 10))
    text = backtest_text(backtest(hist, start=capital, usd=usd), len(hist), capital, usd)
    if first:
        text += ("\n📅 Histórico disponível: " + format((now - first) / 86400, ".0f") + " dias (pedido: " +
                 format(days, ".0f") + "). " + str(sum(len(v) for v in hist.values())) + " operações reais dos DEVs" +
                 (" (" + str(dropped) + " fora: tokens de graça/custo quase zero ou ganho acima de +" +
                  format(BT_MAX_RET * 100, ".0f") + "%, que quem copia não consegue)" if dropped else "") + ".")
    d = _load()
    got = sum(1 for v in d["devs"].values() if v.get("hist_v2"))
    left = sum(1 for v in d["devs"].values() if not v.get("hist_v2"))
    text += ("\n👥 DEVs: " + str(len(d["devs"])) + " descobertos · " + str(got) + " baixados (meta " + format(BT_TARGET, ",d") +
             ") · " + str(len(hist)) + " com operações na pump.fun no período · " + str(left) + " na fila" +
             (" — toque de novo para baixar mais" if left else "") + ".")
    text += "\n\n" + periods_text(d, now)
    with _LOCK:
        dd = _load()
        dd["backtest"] = text
        _save(dd)
    lines = text.split("\n")
    note("backtest: " + (lines[-2] if len(lines) > 1 else lines[0])[:150])
    return text


def periods_text(d=None, now=None, capital=None):
    """Owner's spec: 7 / 30 / 90 / 180 days with US$ 500, strategies 70% / 80% / 90% / 80% + independent buyers.
    Whole period (no train/test split) - a short window is too small to split. Honest about missing data."""
    d = d or _load()
    now = now or time.time()
    capital = float(capital or BT_PERIOD_CAPITAL)
    usd = max(1.0, buy_usd())
    lines = ["📆 BACKTEST POR PERÍODO (US$ " + format(capital, "g") + " · US$ " + format(usd, "g") + " por operação · " +
             "período inteiro, sem separar treino/teste):"]
    for days in BT_PERIODS:
        since = now - days * 86400
        hist = {k: [h for h in v["hist"] if (h.get("closed") or 0) and (h.get("opened") or 0) >= since
                    and (not PUMP_ONLY or pump_trade(h)) and bt_ok(h)]
                for k, v in d["devs"].items() if v.get("hist")}
        # the DEV's rating uses its full past (also before the window) - only entries inside the window count
        full = {k: [h for h in v["hist"] if not PUMP_ONLY or pump_trade(h)] for k, v in d["devs"].items() if v.get("hist")}
        lines.append("— " + str(days) + " dias:")
        for name, need in PERIOD_STRATEGIES:
            r = _window(full, hist, capital, usd, need)
            lines.append("  • " + name + ": " + (
                "sem operações" if not r["trades"] else
                "final US$ " + format(r["final"], ",.2f") + " (" + format(r["final"] - capital, "+.2f") + ") · " +
                str(r["trades"]) + " ops · acerto " + format(r["win"] * 100, ".0f") + "% · DD " +
                format(r["max_dd"] * 100, ".0f") + "%"))
        lines.append("  • D: acerto ≥ 80% + compradores independentes: NÃO CALCULADO — o histórico baixado não traz "
                     "quem mais comprou cada moeda (precisaria ler todas as compras de cada moeda).")
    lines.append("⚠️ Faltam dados para otimizar stop/alvo: o histórico tem só entrada e saída de cada operação do DEV, "
                 "não o caminho do preço. Por isso o robô fica com a sua regra: sai só quando o DEV sai.")
    return "\n".join(lines)


def _window(full, hist, capital, usd, need, delay_cost=0.03):
    events = sorted(((p["opened"], dev, p) for dev, ps in hist.items() for p in ps), key=lambda t: t[0])
    cash, peak, dd, n, wins = capital, capital, 0.0, 0, 0
    for t, dev, p in events:
        prior = [q for q in full[dev] if q.get("closed") and q["closed"] < t]
        if len(prior) < 20 or sum(1 for q in prior if float(q.get("pnl_usd") or 0) > 0) / len(prior) < need:
            continue
        if cash < usd:
            break
        pnl = usd * (float(p.get("result_pct") or 0) - delay_cost - 2 * COST) - 2 * NET_FEE_USD
        cash += pnl
        n += 1
        wins += pnl > 0
        peak = max(peak, cash)
        dd = max(dd, (peak - cash) / peak if peak else 0)
    return {"final": round(cash, 2), "trades": n, "win": wins / n if n else 0.0, "max_dd": dd}


def lag_line(d=None):
    xs = [x["lag_s"] for x in ((d or _load())["history"] + list((d or _load())["positions"].values())) if x.get("lag_s") is not None]
    if not xs:
        return "⏱️ Atraso DEV → bot: ainda sem compras copiadas"
    return ("⏱️ Atraso DEV → bot: média " + format(sum(xs) / len(xs), ".0f") + " s · pior " + format(max(xs), ".0f") +
            " s (" + str(len(xs)) + " compras)")


def backtest_text(res, devs_n, capital=500.0, usd=5.0):
    if not res:
        return "🧪 Backtest DEV: ainda sem histórico de DEVs analisados. Deixe o robô rodar algumas horas."
    lines = ["🧪 BACKTEST DEV (aproximação com as operações REAIS de " + str(devs_n) + " DEVs analisados" +
             (", só moedas da pump.fun)" if PUMP_ONLY else ")"),
             "Capital inicial US$ " + format(capital, "g") + " · US$ " + format(usd, "g") +
             " por operação · atraso + custos estimados (3% + 2×" +
             format(COST * 100, ".0f") + "%) · escolha dos DEVs só com dados anteriores a cada entrada.",
             "Resultado FORA DA AMOSTRA (últimos 30% do período):"]
    best = None
    for name, r in res.items():
        t = r["teste"]
        lines.append("• " + name + ": final US$ " + format(t["final"], ",.2f") + " (" + format(t["pnl"], "+.2f") + ") · " +
                     str(t["trades"]) + " ops · acerto " + format(t["win"] * 100, ".0f") + "% · PF " +
                     ("—" if t["pf"] is None else format(t["pf"], ".2f")) + " · DD " + format(t["max_dd"] * 100, ".0f") +
                     "% · pior " + format(t["worst"], "+.2f") + " · taxas US$ " + format(t["fees"], ".2f"))
        score = t["pnl"] - capital * t["max_dd"] if t["trades"] >= 10 else None
        if score is not None and (best is None or score > best[0]):
            best = (score, name)
    lines.append("🏆 Melhor ajustado ao risco: " + (best[1] if best else "nenhuma estratégia com 10+ operações no teste"))
    lines.append("⚠️ Aproximação: o bot entra depois do DEV e a preço diferente; amostra pequena não prova vantagem.")
    return "\n".join(lines)


def status_text():
    d = _load()
    m = metrics(d)
    devs = d["devs"]
    ok = [(k, v) for k, v in devs.items() if approved(v)]
    lines = ["🧑‍💻 ROBÔ DEV — " + ("🟢 RODANDO" if d.get("enabled") else "⏸️ PAUSADO") +
             (" (💵 DINHEIRO REAL)" if d.get("real") else " (SIMULAÇÃO, sem dinheiro real)"),
             "🛡️ Só moedas da pump.fun · só copia se o DEV tiver no máximo " + format(MAX_DEV_SHARE * 100, ".0f") +
             "% da moeda · DEV que esconde tokens em outra carteira é bloqueado e o bot vende tudo",
             "💼 Saldo simulado US$ " + format(m["equity"], ",.2f") + " (início US$ " + format(m["start"], ",.0f") + ") · hoje " +
             format(m["pnl_day"], "+.2f") + " · total " + format(m["pnl_total"], "+.2f"),
             "📦 Abertas " + str(m["open"]) + "/" + str(MAX_OPEN) + " · " + str(m["trades"]) + " operações · acerto " +
             format(m["win"] * 100, ".0f") + "% · PF " + ("—" if m["pf"] is None else format(m["pf"], ".2f")) +
             " · drawdown máx " + format(m["max_dd"] * 100, ".1f") + "% · taxas US$ " + format(m["fees"], ".2f"),
             "🔎 Tokens vigiados: " + str(len(d["tokens"])) + " · DEVs aprovados vigiados em qualquer moeda nova · DEVs analisados: " +
             str(sum(1 for v in devs.values() if v.get("rated"))) + " · aprovados: " + str(len(ok)),
             "⚙️ US$ " + format(buy_usd(), "g") + " por compra · score ≥ " + format(MIN_SCORE, ".0f") + " · liquidez ≥ US$ " +
             format(MIN_LIQ / 1000, ".0f") + " mil · volume 5 min ≥ US$ " + format(MIN_VOL_5M / 1000, ".0f") +
             " mil · " + ("vende só quando o DEV vende (a mesma parte), sem stop e sem alvo" if MIRROR_DEV and STOP_LOSS <= 0
                          else "stop −" + format(STOP_LOSS * 100, ".0f") + "% · TP +" + format(TP1 * 100, ".0f") + "%")]
    lines.append("🎯 Filtros de entrada: preço no máximo +" + format(MAX_RUNUP_DEV * 100, ".0f") +
                 "% acima da compra do DEV · moeda na curva da pump.fun: market cap ≥ US$ " + format(CURVE_MIN_MCAP, ",.0f") +
                 " e volume 5 min ≥ US$ " + format(CURVE_MIN_VOL_5M, ",.0f") + " (sem exigir liquidez) · impacto de preço ≤ 1% · " +
                 ("limite de perda do dia US$ " + format(DAILY_LOSS_USD, "g") if DAILY_LOSS_USD > 0 else "sem limite de perda diário"))
    lines.append(lag_line(d))
    for k, v in sorted(ok, key=lambda kv: -kv[1].get("score", 0))[:5]:
        lines.append("🏅 " + v["tier"] + " `" + k + "` acerto " + format(v["win"] * 100, ".0f") + "% · " + str(v["trades"]) +
                     " ops · PF " + format(v["pf"], ".1f") + " · US$ " + format(v["pnl"], "+,.0f"))
    for m_, p in d["positions"].items():
        lines.append("📈 " + p["symbol"] + " entrada " + format(p["entry"], ".6g") + " → " + format(p["last"], ".6g") + " (" +
                     format((p["last"] / p["entry"] - 1) * 100, "+.1f") + "%)")
    for x in (d.get("log") or [])[-5:]:
        lines.append("• " + x)
    return "\n".join(lines)
