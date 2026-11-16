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
MAX_OPEN = int(E("GRAD_MAX_OPEN", 2))
DAILY_LOSS = E("GRAD_DAILY_LOSS_USD", 20)
DELAY_S = E("GRAD_DELAY_S", 30)
MAX_LATE_S = E("GRAD_MAX_LATE_S", 150)          # never buy a graduation older than this
STOP = E("GRAD_STOP", 0.25)
ARM = E("GRAD_ARM", 0.50)
DROP = E("GRAD_DROP", 0.20)
HOLD_MIN = E("GRAD_HOLD_MIN", 240)
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
        w = _load().setdefault("watch", {})
        w.update({m: int(t) for m, t in added.items()})


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
    new = [m for m, ok in flags.items() if ok]
    with _LOCK:
        d = _load()
        for m in new:
            d["watch"].pop(m, None)
            d.setdefault("grads", {})[m] = now
        d["grads"] = {m: t for m, t in d["grads"].items() if now - t < 3600}
    return new


def _pair(mint, fetch=None):
    rows = (fetch or sg._get)(sg.DEX + "/tokens/v1/solana/" + mint) or []
    best = None
    for p in rows if isinstance(rows, list) else []:
        if str(p.get("dexId") or "").lower() == "pumpfun":
            continue
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
    _save()
    return bought


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
             "⚠️ No backtest, a parte de conferência (30% finais) ainda não confirmou lucro."]
    for x in (d.get("log") or [])[-5:]:
        lines.append("• " + x)
    return "\n".join(lines)
