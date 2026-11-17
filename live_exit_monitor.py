"""Monitor confirmed positions and raise sell alerts; no unsigned live orders.

Uses executable Jupiter token->USDC quotes, not DexScreener chart prices.
Sales are executed by live_engine.exit_cycle; this module only evaluates/alerts.
"""
import logging, os, time
from live_trade_state import _load
from live_exit_rules import trailing_exit_signal
import live_trade_state
from jupiter_v2_executor import order, SOL_MINT, USDC_MINT

_last_alert = {}
_quote_fail_since = {}  # mint -> monotonic time quotes started failing
_quote_err_logged = {}  # mint -> last time the reason was logged
LAST_VALUE = {}  # mint -> (time, executable USD value) of the last quote (web panel)
ARMED = []  # (mint, position, signal) whose trailing stop just started: live_engine tells the owner
BE_ARMED = []  # (mint, position, signal) copy positions whose stop just moved to the entry (+COPY_BE_ARM)
CONSENSUS_EXIT = {"stop_loss": "0.40", "arm": "1.25", "drop": "0.10"}   # removed consensus robot's positions
# Removed robots (signals, Dip Bot, Sniper): positions still open keep a safe exit until they are sold
REMOVED_BOT_EXIT = {"dip": {"stop_loss": "0.40", "arm": "1.12", "drop": "0.06"},
                    "signal": {"stop_loss": "0.40", "arm": "1.25", "drop": "0.10"}}
REMOVED_BOT_MAX_HOLD_S = 6 * 3600


def trail_text(position):
    """'sells X% below the top' for the position's trailing rule."""
    if position.get("mode") == "baixa":
        import baixa_bot
        return format(baixa_bot.TRAIL * 100, ".0f")
    if position.get("mode") == "consensus":
        return format(float(CONSENSUS_EXIT["drop"]) * 100, ".0f")
    if position.get("mode") == "grad":
        import grad_bot
        return format(grad_bot.DROP * 100, ".0f")
    if position.get("copy_orphan"):
        return format(float(ORPHAN_EXIT["drop"]) * 100, ".0f")
    if position.get("mode") == "rocket":
        import rocket_bot
        return format(rocket_bot.TRAIL_DROP * 100, ".0f")
    if position.get("mode") == "diamond":
        import diamond_bot
        return format(diamond_bot.TRAIL_DROP * 100, ".0f")
    if position.get("mode") in REMOVED_BOT_EXIT:
        return format(float(REMOVED_BOT_EXIT[position["mode"]]["drop"]) * 100, ".0f")
    if position.get("mode") == "copy":
        return format(float(copy_wallet_exit()["drop"]) * 100, ".0f")
    if position.get("mode") == "manual":
        return "0"
    return format(float(SNIPER_EXIT["drop"]) * 100, ".0f")
DEAD_AFTER_SECONDS = 10 * 60
CHECK_INTERVAL_SECONDS = 20
SNIPER_MAX_HOLD_SECONDS = 30 * 60  # sniper trade that never took off is closed

SNIPER_STOP_LOSS = os.getenv("SNIPER_STOP_LOSS", "0.40")      # sniper: stop -40%
# Copy positions with no known origin wallet: stop -40%, trailing stop from +25% selling 10% below the top
ORPHAN_EXIT = {"stop_loss": os.getenv("COPY_ORPHAN_STOP_LOSS", "0.40"),
               "arm": str(1 + float(os.getenv("COPY_ORPHAN_TRAIL_ARM", "0.25"))),
               "drop": os.getenv("COPY_ORPHAN_TRAIL_DROP", "0.10")}
SNIPER_EXIT = {"stop_loss": SNIPER_STOP_LOSS, "arm": "1.25", "drop": "0.12"}


# Owner's choice (Nov 14), copy positions that follow a wallet:
# - once the coin reached +COPY_BE_ARM, the stop goes to the entry (never turns into a loss);
# - at +100% sell 25% of what is left, then 25% more at every further +50% (+150%, +200%, ...).
COPY_BE_ARM = float(os.getenv("COPY_BE_ARM", "0.25"))
COPY_TP_FIRST = float(os.getenv("COPY_TP_FIRST", "2.0"))
COPY_TP_STEP = float(os.getenv("COPY_TP_STEP", "0.5"))
COPY_TP_PART = float(os.getenv("COPY_TP_PART", "0.25"))
COPY_HARD_STOP = float(os.getenv("COPY_HARD_STOP", "0.50"))     # owner (Nov 15): stop loss -50% on every copy


def copy_wallet_guard(position, ratio, peak):
    """Ladder / breakeven signal for a copy position, or None."""
    if COPY_HARD_STOP > 0 and ratio <= 1 - COPY_HARD_STOP:
        return {"action": "sell", "reason": "stop_loss", "change_pct": (ratio - 1) * 100, "peak_ratio": peak}
    done = int(position.get("copy_tp_done") or 0)
    nxt = COPY_TP_FIRST + COPY_TP_STEP * done
    if COPY_TP_PART > 0 and ratio >= nxt:
        # a coin that jumped several levels at once sells ONE part and counts every level it passed
        # (25% at a time, never 25% x many in a row)
        passed = int((ratio - COPY_TP_FIRST) / COPY_TP_STEP) + 1 if COPY_TP_STEP > 0 else done + 1
        return {"action": "sell_part", "fraction": COPY_TP_PART, "reason": "copy_ladder", "level": nxt,
                "levels_done": max(done + 1, passed), "change_pct": (ratio - 1) * 100, "peak_ratio": peak}
    if COPY_BE_ARM > 0 and peak >= 1 + COPY_BE_ARM and ratio <= 1.0:
        return {"action": "sell", "reason": "copy_breakeven", "change_pct": (ratio - 1) * 100, "peak_ratio": peak}
    return None


def copy_trail_enabled():
    return os.getenv("COPY_TRAIL_ENABLED", "true").strip().lower() != "false"


def copy_wallet_exit():
    """Copy trade (wallet exit mode): trailing stop from +COPY_TRAIL_ARM, selling COPY_TRAIL_DROP below the top.
    COPY_WALLET_STOP_LOSS=1 (default) means no fixed stop loss: below the arm level it only follows the wallet."""
    return {"stop_loss": os.getenv("COPY_WALLET_STOP_LOSS", "1"),
            "arm": str(1 + float(os.getenv("COPY_TRAIL_ARM", "0.25"))),
            "drop": os.getenv("COPY_TRAIL_DROP", "0.10")}
PUMPFUN_EXIT = {"stop_loss": "0.12", "arm": "1.25", "drop": "0.12"}  # old pump.fun positions only
HOT_EXIT = {"stop_loss": "0.06", "arm": "1.08", "drop": "0.05"}
HOT_MAX_HOLD_SECONDS = 45 * 60  # fast mover that did not move is closed


def quote_value_usd(mint, amount):
    """Executable value of `amount` tokens: token -> SOL -> USDC (Jupiter)."""
    sol_out = int(order(mint, SOL_MINT, int(amount))["outAmount"])
    if sol_out <= 0:
        raise ValueError("No executable SOL quote")
    return int(order(SOL_MINT, USDC_MINT, sol_out)["outAmount"]) / 1_000_000


def position_signal(position, cost, proceeds):
    """Exit decision shared by real trading and the simulator."""
    if position.get("mode") in ("sniper", "pumpfun"):
        rules = SNIPER_EXIT if position.get("mode") == "sniper" else PUMPFUN_EXIT
        signal = trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0), **rules)
        held = time.time() - float(position.get("opened_at") or time.time())
        if signal["action"] == "hold" and held > SNIPER_MAX_HOLD_SECONDS and signal["reason"] != "trailing":
            signal.update(action="sell", reason="time_stop")
        return signal
    if position.get("mode") == "rocket":
        # Rocket Sniper: no stop loss; from +30% trailing 15% below the top; after the -20% rebuy, all at +20%
        import rocket_bot
        if position.get("dca_done"):
            # rebought lower: close everything at +20% over the average price
            signal = trailing_exit_signal(cost, proceeds, 0, **rocket_bot.DCA_EXIT)
            if signal["action"] == "sell" and signal["reason"] != "stop_loss":
                signal["reason"] = "rocket_dca_target"
            return signal
        # fixed stop (ROCKET_STOP_LOSS), trailing from +30%; profit protection: once the coin reached
        # +15% the stop moves up to -5% (it never turns a good move into a big loss)
        signal = trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0), **rocket_bot.EXIT)
        ratio = float(proceeds) / float(cost) if cost else 0.0
        if (signal["action"] == "hold" and rocket_bot.PROTECT_ARM > 0 and
                signal["peak_ratio"] >= 1 + rocket_bot.PROTECT_ARM and ratio <= 1 + rocket_bot.PROTECT_STOP):
            signal.update(action="sell", reason="rocket_protect")
        held = time.time() - float(position.get("opened_at") or time.time())
        if (signal["action"] == "hold" and rocket_bot.NO_MOVE_S > 0 and held > rocket_bot.NO_MOVE_S and
                signal["peak_ratio"] < 1 + rocket_bot.NO_MOVE_ARM):
            signal.update(action="sell", reason="time_stop")      # never took off: free the slot
        return signal
    if position.get("mode") == "diamond":
        # Diamond: stop -10% (thesis broken), from +12% trailing 6% under the top
        import diamond_bot
        return trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0), **diamond_bot.EXIT)
    if position.get("mode") in REMOVED_BOT_EXIT:
        # robot removed: stop -40%, trailing stop once in profit, out after 6 h if it never took off
        signal = trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0), **REMOVED_BOT_EXIT[position["mode"]])
        held = time.time() - float(position.get("opened_at") or time.time())
        if signal["action"] == "hold" and held > REMOVED_BOT_MAX_HOLD_S and signal["reason"] != "trailing":
            signal.update(action="sell", reason="time_stop")
        return signal
    if position.get("mode") == "consensus":
        # robot removed; its open positions keep their rules: stop -40%, from +25% trailing 10% below the top
        return trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0), **CONSENSUS_EXIT)
    if position.get("mode") == "baixa":
        # Robô BAIXA (owner, Nov 17): fixed target +25% and stop -40%, same as its backtest
        import baixa_bot
        ratio = float(proceeds) / float(cost) if cost else 0.0
        peak = max(ratio, float(position.get("peak_ratio") or 0))
        action, reason = baixa_bot.exit_signal(ratio, peak)
        return {"action": action, "reason": reason, "change_pct": (ratio - 1) * 100, "peak_ratio": peak}
    if position.get("mode") == "snipe":
        # Sniper 24 h: same rule the backtest uses (snipe_bot.exit_signal)
        import snipe_bot
        ratio = float(proceeds) / float(cost) if cost else 0.0
        peak = max(ratio, float(position.get("peak_ratio") or 0))
        held = time.time() - float(position.get("opened_at") or time.time())
        action, reason = snipe_bot.exit_signal(ratio, peak, held)
        return {"action": action, "reason": reason or "snipe_hold", "change_pct": (ratio - 1) * 100, "peak_ratio": peak}
    if position.get("mode") == "grad":
        # Graduados: same rule as the graduation backtest (grad_bot.exit_signal)
        import grad_bot
        ratio = float(proceeds) / float(cost) if cost else 0.0
        peak = max(ratio, float(position.get("peak_ratio") or 0))
        held = time.time() - float(position.get("opened_at") or time.time())
        action, reason = grad_bot.exit_signal(ratio, peak, held)
        if position.get("liq_exit"):
            action, reason = "sell", "liquidity_drained"     # owner: pool drained, get out now
        if action == "hold" and peak >= 1 + grad_bot.ARM:
            reason = "trailing"                      # owner gets the "STOP MÓVEL ATIVADO" message once
        return {"action": action, "reason": reason or "grad_hold", "change_pct": (ratio - 1) * 100, "peak_ratio": peak}
    if position.get("mode") == "dev":
        # DEV robot: its own exits (dev sells, -8%, +15% / trailing) run in dev_bot; this is only a safety
        # stop in case its loop stops (-20%)
        ratio = float(proceeds) / float(cost) if cost else 0.0
        peak = max(ratio, float(position.get("peak_ratio") or 0))
        safety = float(os.getenv("DEV_SAFETY_STOP", "0"))      # owner: mirror the dev, no stop (0 = off)
        if safety > 0 and ratio <= 1 - safety:
            return {"action": "sell", "reason": "stop_loss", "change_pct": (ratio - 1) * 100, "peak_ratio": peak}
        return {"action": "hold", "reason": "dev_bot", "change_pct": (ratio - 1) * 100, "peak_ratio": peak}
    if position.get("mode") == "manual" or (position.get("mode") == "copy" and position.get("copy_orphan")):
        # owner's buys from a signal (and coins in the wallet with no copied wallet): same rules as the copy
        # trade - stop loss -50%, stop at the entry after +50%, 25% at +100% and every further +50%
        ratio = float(proceeds) / float(cost) if cost else 0.0
        peak = max(ratio, float(position.get("peak_ratio") or 0))
        guard = copy_wallet_guard(position, ratio, peak)
        return guard or {"action": "hold", "reason": "manual_hold", "change_pct": (ratio - 1) * 100, "peak_ratio": peak}
    if (position.get("mode") == "copy" and not position.get("copy_orphan")
            and os.getenv("COPY_EXIT_MODE", "wallet").strip().lower() == "wallet"):
        # Copy trade: sells when the copied wallet sells (copy_trader) AND, once in profit,
        # a trailing stop locks the gain (no fixed stop loss unless COPY_WALLET_STOP_LOSS is set).
        ratio = float(proceeds) / float(cost) if cost else 0.0
        peak = max(ratio, float(position.get("peak_ratio") or 0))
        guard = copy_wallet_guard(position, ratio, peak)
        if guard:
            return guard
        if copy_trail_enabled():
            return trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0), **copy_wallet_exit())
        return {"action": "hold", "reason": "follow_wallet", "change_pct": (ratio - 1) * 100, "peak_ratio": peak}
    if position.get("mode") in ("copy", "clone"):
        # Copy trade: target +50% / stop -15% (selling with the copied wallet is done by copy_trader).
        # A cloned wallet carries its own measured TP / SL / max hold on the position.
        signal = trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0),
                                      stop_loss=str(position.get("copy_sl") or os.getenv("COPY_STOP_LOSS", "0.15")),
                                      arm=str(1 + float(position.get("copy_tp") or os.getenv("COPY_TAKE_PROFIT", "0.50"))),
                                      drop="0")
        max_hold = position.get("copy_max_hold_s")
        held = time.time() - float(position.get("opened_at") or time.time())
        if signal["action"] == "hold" and max_hold and held > float(max_hold):
            signal.update(action="sell", reason="time_stop")
        return signal
    if position.get("mode") == "tradeB":
        # Strategy B: fixed take profit +15% / stop loss -8% (drop=0 -> sell as soon as +15% is reached)
        return trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0),
                                    stop_loss=os.getenv("SIM_STOP_LOSS", "0.08"),
                                    arm=str(1 + float(os.getenv("SIM_TAKE_PROFIT", "0.15"))), drop="0")
    if position.get("mode") == "launch":
        # Launch strategy removed; kept so any old position is still sold safely.
        signal = trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0),
                                      stop_loss="0.20", arm="1.15", drop="0.08")
        held = time.time() - float(position.get("opened_at") or time.time())
        if signal["action"] == "hold" and held > 90:
            signal.update(action="sell", reason="time_stop")
        return signal
    if position.get("mode") == "candle":
        # Closed by the candle loop; this is only the safety stop in between.
        return trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0), **HOT_EXIT)
    if position.get("mode") == "hot":
        signal = trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0), **HOT_EXIT)
        held = time.time() - float(position.get("opened_at") or time.time())
        if signal["action"] == "hold" and held > HOT_MAX_HOLD_SECONDS and signal["reason"] != "trailing":
            signal.update(action="sell", reason="time_stop")
        return signal
    return trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0))


def _evaluate():
    state = _load()
    for mint, position in state["positions"].items():
        if any(p.get("mint") == mint for p in state["pending"].values()):
            continue
        try:
            amount = int(position["token_amount_raw"])
            cost = float(position["cost_usd"])
            if amount <= 0 or cost <= 0:
                raise ValueError("Invalid position amount or cost")
            proceeds = quote_value_usd(mint, amount)
            LAST_VALUE[mint] = (time.time(), proceeds)
            signal = position_signal(position, cost, proceeds)
            if signal["peak_ratio"] > float(position.get("peak_ratio", 0)):
                live_trade_state.update_peak(mint, signal["peak_ratio"])
            if (position.get("mode") in ("copy", "manual") and COPY_BE_ARM > 0
                    and float(signal.get("peak_ratio") or 0) >= 1 + COPY_BE_ARM and not position.get("be_armed")):
                live_trade_state.set_position_fields(mint, be_armed=True)
                BE_ARMED.append((mint, position, signal))
            if signal.get("reason") == "trailing" and not position.get("trail_armed"):
                live_trade_state.set_position_fields(mint, trail_armed=True)
                ARMED.append((mint, position, signal))
            _quote_fail_since.pop(mint, None)
            yield mint, position, signal
        except Exception as exc:
            if time.monotonic() - _quote_err_logged.get(mint, -1e9) >= 600:
                _quote_err_logged[mint] = time.monotonic()
                logging.warning("Live position quote/monitoring unavailable for mint %s: %s: %s", mint[:8],
                                type(exc).__name__, str(exc)[:200])
            # Dead token (rug / no liquidity): Jupiter works for SOL but has no route for it.
            if not _jupiter_healthy():
                _quote_fail_since.pop(mint, None)
                continue
            since = _quote_fail_since.setdefault(mint, time.monotonic())
            if time.monotonic() - since >= DEAD_AFTER_SECONDS:
                yield mint, position, {"action": "write_off", "reason": "dead", "change_pct": -100.0}

def _jupiter_healthy():
    try:
        return int(order(SOL_MINT, USDC_MINT, 100_000_000)["outAmount"]) > 0
    except Exception:
        return False

def sell_signals():
    """Positions to exit: TP/SL/time stop ("sell") or dead tokens ("write_off")."""
    return [(m, p, s) for m, p, s in _evaluate() if s["action"] in ("sell", "sell_part", "write_off")]

def monitor(send, chat):
    """Alert-only mode, used while real trading is not active."""
    if not chat:
        return
    for mint, position, signal in sell_signals():
        if signal["action"] != "sell":
            continue
        key = (mint, signal["reason"])
        now = time.monotonic()
        if now - _last_alert.get(key, 0) < 900:
            continue
        _last_alert[key] = now
        label = "🎯 TAKE PROFIT" if signal["reason"] == "take_profit" else "🛑 STOP LOSS"
        send(chat, label + " ATINGIDO (estimativa Jupiter token → SOL → USD)\nToken: " + mint +
             "\nVariação estimada: " + format(signal["change_pct"], ".2f") +
             "%\n⚠️ Robô desligado (LIVE_TRADING_ENABLED/preflight): venda NÃO enviada. "
             "Ligue o robô ou venda pela Phantom.")
