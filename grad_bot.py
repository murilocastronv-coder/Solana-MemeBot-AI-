"""GRADUADOS (owner, Nov 16): buys pump.fun coins right after they graduate to PumpSwap, with REAL money.

Same rule as the best combination of grad_backtest.py (enter ~30 s after the graduation; stop -25%;
from +50% a trailing stop 20% below the top; out after 4 h). US$ 10 per buy, max 2 open, stops for the
day after a US$ 20 loss. The out-of-sample part of the backtest did NOT confirm a profit yet (17 trades,
-3 USD); the owner chose to run it live anyway, with these small limits.

Detection: snipe_bot.scan_launches feeds every new launch here (add_launches); every CHECK_S the
bonding-curve 'complete' flag of the launches of the last WATCH_H hours is read (100 per call). A coin
whose flag turned on graduated; ~DELAY_S later the bot buys it (DexScreener pair for the price/liquidity).
"""
import logging
import threading
import time

import signals as sg

E = sg._f
USD = E("GRAD_USD", 10)
MAX_OPEN = int(E("GRAD_MAX_OPEN", 4))          # owner (Nov 16): up to 4 at a time
DAILY_LOSS = E("GRAD_DAILY_LOSS_USD", 20)
DELAY_S = E("GRAD_DELAY_S", 30)
MAX_LATE_S = E("GRAD_MAX_LATE_S", 150)          # never buy a graduation older than this
STOP = E("GRAD_STOP", 0.25)
ARM = E("GRAD_ARM", 0.50)
DROP = E("GRAD_DROP", 0.20)
HOLD_MIN = E("GRAD_HOLD_MIN", 240)
# owner (Nov 16): a coin whose pool was drained (the big holders sold everything) is sold at once
MIN_ENTRY_LIQ = E("GRAD_MIN_ENTRY_LIQ_USD", 8_000)     # do not buy a pool already this thin
EXIT_LIQ = E("GRAD_EXIT_LIQ_USD", 5_000)               # sell when the pool falls under this...
EXIT_LIQ_DROP = E("GRAD_EXIT_LIQ_DROP", 0.60)           # ...or lost this share of the liquidity it had at the buy
WATCH_H = E("GRAD_WATCH_H", 2)
CHECK_S = E("GRAD_CHECK_S", 15)
KEY = "grad_bot"
_LOCK = threading.RLock()
_MEM = {}
_PDA = {}


def exit_signal(ratio, peak, held_s):
    """Same exit as grad_backtest.trade with 'sl25 trail+50/20%'."""
    if ratio <= 1 - STOP:
        return "sell", "stop_loss"
    if peak >= 1 + ARM and ratio <= peak * (1 - DROP):
        return "sell", "take_profit"
    if held_s >= HOLD_MIN * 60:
        return "sell", "time_stop"
    return "hold", ""


# ---------------------------------------------------------------- state
def _load():
    with _LOCK:
        if not _MEM:
            d = {"enabled": False, "watch": {}, "grads": {}, "seen": {}, "log": []}
            try:
                import pg_store
                if pg_store.enabled():
                    got = pg_store.get(KEY)
                    if got:
                        d.update(got)
            except Exception:
                pass
            _MEM.update(d)
        return _MEM


def _save():
    try:
        import pg_store
        if pg_store.enabled():
            pg_store.put(KEY, dict(_MEM))
    except Exception as exc:
        logging.warning("grad: postgres save failed (%s)", type(exc).__name__)


def enabled():
    return bool(_load().get("enabled"))


def set_enabled(on):
    with _LOCK:
        _load()["enabled"] = bool(on)
        _save()


def note(text):
    logging.info("grad %s", text)
    with _LOCK:
        d = _load()
        d["log"] = (d.get("log") or [])[-49:] + [time.strftime("%d/%m %H:%M", time.gmtime()) + " " + text]


def add_launches(added):
    """{mint: created} from the launch scanner."""
    if not added:
        return
    with _LOCK:
        d = _load()
        done = set(d.get("grads") or {}) | set(d.get("seen") or {})
        d.setdefault("watch", {}).update({m: int(t) for m, t in added.items() if m not in done})


def _history():
    try:
        import live_trade_state
        return [h for h in live_trade_state.history() if h.get("mode") == "grad"]
    except Exception:
        return []


def _open():
    try:
        import live_trade_state
        return [m for m, p in live_trade_state._load()["positions"].items() if p.get("mode") == "grad"]
    except Exception:
        return []


def day_pnl(now=None):
    day = time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))
    return sum(float(h.get("pnl_usd") or 0) for h in _history()
               if time.strftime("%Y-%m-%d", time.gmtime(float(h.get("closed_at") or 0))) == day)


# ---------------------------------------------------------------- loop
def check_graduations(rpc, now=None, completed=None):
    """Read the curve flags of the watched launches; newly graduated ones go to 'grads'. Returns them."""
    import grad_backtest as gb
    now = now or time.time()
    with _LOCK:
        d = _load()
        d["watch"] = {m: t for m, t in (d.get("watch") or {}).items() if now - t < WATCH_H * 3600}
        mints = list(d["watch"])
    flags = (completed or gb.completed)(rpc, mints) if mints else {}
    with _LOCK:
        d = _load()
        new = [m for m, ok in flags.items() if ok and m not in d.get("grads", {}) and m not in d.get("seen", {})]
        for m in new:
            d["watch"].pop(m, None)
            d.setdefault("grads", {})[m] = now
        for m, ok in flags.items():
            if ok:
                d["watch"].pop(m, None)
        d["grads"] = {m: t for m, t in d["grads"].items() if now - t < 3600}
    return new


def _pair(mint, fetch=None):
    rows = (fetch or sg._get)(sg.DEX + "/tokens/v1/solana/" + mint) or []
    best = None
    for p in rows if isinstance(rows, list) else []:
        if str(p.get("dexId") or "").lower() == "pumpfun" or (p.get("baseToken") or {}).get("address") != mint:
            continue                     # only the coin's own PumpSwap pair (not a pair where it is the quote)
        if best is None or float((p.get("liquidity") or {}).get("usd") or 0) > float((best.get("liquidity") or {}).get("usd") or 0):
            best = p
    return best


def cycle(send=None, chat=None, rpc=None, now=None, buy=None, fetch=None, completed=None):
    """Find graduations and buy the ones that are DELAY_S old. Returns the mint bought (or None)."""
    now = now or time.time()
    if rpc is not None:
        try:
            for m in check_graduations(rpc, now, completed):
                note("graduou " + m[:8])
        except Exception as exc:
            logging.info("grad check: %s", type(exc).__name__)
    try:
        check_liquidity(now, fetch)                    # also when the robot is off: open positions still protected
    except Exception as exc:
        logging.info("grad liquidity: %s", type(exc).__name__)
    if not enabled():
        _save()
        return None
    if DAILY_LOSS > 0 and day_pnl(now) <= -DAILY_LOSS:
        _save()
        return None
    bought = None
    with _LOCK:
        d = _load()
        ready = [m for m, t in sorted(d.get("grads", {}).items(), key=lambda kv: kv[1])
                 if DELAY_S <= now - t <= MAX_LATE_S and m not in d.get("seen", {})]
    for mint in ready:
        if len(_open()) >= MAX_OPEN:
            break
        try:
            p = _pair(mint, fetch)
        except Exception:
            p = None
        if not p:
            continue                                  # PumpSwap pair not listed yet: try next cycle
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if liq < MIN_ENTRY_LIQ:
            with _LOCK:
                _load().setdefault("seen", {})[mint] = now
            note("pulei " + ((p.get("baseToken") or {}).get("symbol") or mint[:6]) + ": liquidez US$ " + format(liq, ",.0f"))
            continue
        with _LOCK:
            d = _load()
            d.setdefault("seen", {})[mint] = now
            d["seen"] = {m: t for m, t in d["seen"].items() if now - t < 7 * 86400}
        sym = (p.get("baseToken") or {}).get("symbol") or mint[:6]
        ok = (buy or _buy_real)(p, mint, sym, send, chat)
        note(("COMPROU " if ok else "compra NÃO confirmada ") + sym + " · mcap US$ " +
             format(float(p.get("marketCap") or 0), ",.0f"))
        if ok:
            bought = mint
            with _LOCK:
                _load().setdefault("entry_liq", {})[mint] = liq
    _save()
    return bought


def check_liquidity(now=None, fetch=None, open_mints=None, mark=None):
    """Open positions whose pool was drained get a forced sale (exit monitor sells them). Returns those mints."""
    mints = list(open_mints if open_mints is not None else _open())
    if not mints:
        return []
    d = _load()
    entry = d.get("entry_liq") or {}
    out = []
    for i in range(0, len(mints), 30):
        rows = (fetch or sg._get)(sg.DEX + "/tokens/v1/solana/" + ",".join(mints[i:i + 30])) or []
        liq = {}
        for p in rows if isinstance(rows, list) else []:
            m = (p.get("baseToken") or {}).get("address")
            if m in mints and str(p.get("dexId") or "").lower() != "pumpfun":
                liq[m] = max(liq.get(m, 0.0), float((p.get("liquidity") or {}).get("usd") or 0))
        for m in mints[i:i + 30]:
            if m not in liq:
                continue                               # no data this time: never sell on missing data
            low = liq[m] < EXIT_LIQ
            drained = entry.get(m, 0) > 0 and liq[m] <= entry[m] * (1 - EXIT_LIQ_DROP)
            if low or drained:
                out.append(m)
                (mark or _mark_exit)(m, "liquidez US$ " + format(liq[m], ",.0f") +
                                     (" (era US$ " + format(entry[m], ",.0f") + ")" if entry.get(m) else ""))
    with _LOCK:
        open_set = set(mints)
        d["entry_liq"] = {m: v for m, v in entry.items() if m in open_set or m in (d.get("seen") or {})}
    return out


def _mark_exit(mint, why):
    import live_trade_state
    p = live_trade_state._load()["positions"].get(mint) or {}
    if p.get("liq_exit"):
        return
    live_trade_state.set_position_fields(mint, liq_exit=True)
    note("vendendo " + str(p.get("symbol") or mint[:6]) + " por falta de liquidez: " + why)


def _buy_real(p, mint, sym, send, chat):
    import live_engine
    try:
        settled = live_engine._execute_buy({"baseToken": p.get("baseToken"), "liquidity": p.get("liquidity"),
                                            "marketCap": p.get("marketCap")}, mint, sym, send, chat,
                                           live_engine.get_address(), None, "grad", usd=USD)
    except Exception as exc:
        note("compra falhou " + sym + ": " + type(exc).__name__)
        return False
    return bool(settled and settled.get("status") == "settled_buy")


def status_text():
    d = _load()
    h = _history()
    wins = [x for x in h if float(x.get("pnl_usd") or 0) > 0]
    total = sum(float(x.get("pnl_usd") or 0) for x in h)
    lines = ["🎓 GRADUADOS — " + ("🟢 LIGADO (💵 DINHEIRO REAL)" if d.get("enabled") else "⏸️ DESLIGADO"),
             "📥 Compra a moeda que acabou de graduar na pump.fun (foi para a PumpSwap), ~" + format(DELAY_S, "g") + " s depois",
             "📤 Sai: stop −" + format(STOP * 100, ".0f") + "% · de +" + format(ARM * 100, ".0f") + "% stop móvel " +
             format(DROP * 100, ".0f") + "% abaixo do topo · no máximo " + format(HOLD_MIN / 60, "g") + " h",
             "⚙️ US$ " + format(USD, "g") + " por compra · máx. " + str(MAX_OPEN) + " abertas · para no dia em que perder US$ " +
             format(DAILY_LOSS, "g") + " (hoje " + format(day_pnl(), "+.2f") + ")",
             "📊 " + str(len(h)) + " operações · " + str(len(wins)) + " no lucro · total " + format(total, "+.2f") +
             " USD · abertas " + str(len(_open())) + "/" + str(MAX_OPEN),
             "👀 Vigiando " + str(len(d.get("watch") or {})) + " lançamentos das últimas " + format(WATCH_H, "g") +
             " h · graduaram na última hora: " + str(len(d.get("grads") or {})),
             "💧 Não compra pool com menos de US$ " + format(MIN_ENTRY_LIQ / 1000, "g") + " mil; vende na hora se a liquidez cair abaixo de US$ " +
             format(EXIT_LIQ / 1000, "g") + " mil ou perder " + format(EXIT_LIQ_DROP * 100, ".0f") + "% da que tinha na compra",
             "⚠️ No backtest, a parte de conferência (30% finais) ainda não confirmou lucro."]
    for x in (d.get("log") or [])[-5:]:
        lines.append("• " + x)
    return "\n".join(lines)
