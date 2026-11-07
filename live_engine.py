"""Autonomous real-money trading cycle (buy + TP/SL sell) on Solana via Jupiter.

Runs only when ALL of these hold:
- LIVE_TRADING_ENABLED=true and every live_order_bridge.preflight() check passes;
- the owner pressed "Iniciar trading real" (persistent state enabled, not halted).

Rules: US$ 10 per buy, at most 3 positions, TP +15%, SL -8%, daily loss
limit US$ 3. One order at a time. Any uncertain order stays "pending" and
blocks all new orders until reconciled; nothing is ever resent automatically.
"""
import logging
import time

import live_order_bridge as bridge
import live_trade_state as state
from bot_wallet_service import get_address
from live_exit_monitor import sell_signals
from paper_auto import candidate

BUY_USDC_RAW = 10_000_000  # US$ 10 in USDC base units (bridge converts to SOL)
_tried = {}  # mint -> monotonic time of last buy attempt (avoid re-buying)
RETRY_MINT_AFTER_SECONDS = 24 * 3600
_last_notice = {}


def _notice(send, chat, key, text, every=3600):
    now = time.monotonic()
    if now - _last_notice.get(key, -every) >= every:
        _last_notice[key] = now
        send(chat, text)


def ready():
    """True when the owner switched the robot on and every safety check passes."""
    try:
        st = state.status()
        if not st["enabled"] or st["halted"]:
            return False
        bridge.preflight()
        return bool(get_address())
    except Exception:
        return False


def start():
    bridge.preflight()
    if not get_address():
        raise bridge.TradingNotReady("Crie a carteira do bot antes de ligar")
    return state.start()


def _settle(order_id, wallet):
    # The bridge already waited for finality; balances may lag a few seconds.
    for _ in range(5):
        result = state.reconcile_known_signature(order_id, wallet)
        if result.get("status") != "waiting_finality":
            return result
        time.sleep(3)
    return result


def _failure(send, chat, side, mint, exc):
    pending = state.pending_orders()
    if pending:
        send(chat, "⚠️ " + side.upper() + " com resultado incerto: " + mint +
             "\nRobô BLOQUEADO até conferir a transação na carteira (Solscan). "
             "Nenhuma ordem será reenviada automaticamente.")
    else:
        _notice(send, chat, ("fail", side, str(exc)[:80]),
                "⛔ " + side.capitalize() + " não enviada: " + str(exc)[:200])


def buy_cycle(rows, send, chat):
    """Buy at most one new token per scan (US$ 10)."""
    if not chat or not ready():
        return None
    allowed, reason = state.can_open()
    if not allowed:
        return None
    wallet = get_address()
    open_mints = set(state._load()["positions"])
    now = time.monotonic()
    for p in rows:
        if not candidate(p):
            continue
        mint = p["baseToken"]["address"]
        if mint in open_mints or now - _tried.get(mint, -RETRY_MINT_AFTER_SECONDS) < RETRY_MINT_AFTER_SECONDS:
            continue
        _tried[mint] = now
        symbol = str(p["baseToken"].get("symbol", "?"))[:20]
        send(chat, "🟡 COMPRANDO (dinheiro real) US$ 10 de " + symbol + "\n" + mint)
        try:
            result = bridge.submit_swap(mint, "buy", BUY_USDC_RAW, wallet)
            settled = _settle(result["order_id"], wallet)
        except Exception as exc:
            _failure(send, chat, "compra", mint, exc)
            return None
        if settled.get("status") == "settled_buy":
            send(chat, "✅ COMPRA CONFIRMADA: " + symbol + "\nValor: US$ 10\nTP +15% | SL -8% (venda automática)"
                 "\nTx: https://solscan.io/tx/" + settled["signature"])
        else:
            send(chat, "⚠️ Compra enviada mas não conciliada (" + str(settled.get("reason", settled.get("status"))) +
                 "). Robô bloqueado.\nTx: https://solscan.io/tx/" + str(result["signature"]))
        return settled
    return None


def exit_cycle(send, chat):
    """Sell positions that hit TP/SL. Runs even after 'Parar' to protect funds."""
    if not chat:
        return
    try:
        bridge.preflight()
    except Exception:
        return
    wallet = get_address()
    if not wallet:
        return
    for mint, position, signal in sell_signals():
        label = "🎯 TAKE PROFIT" if signal["reason"] == "take_profit" else "🛑 STOP LOSS"
        send(chat, label + " (" + format(signal["change_pct"], "+.2f") + "%) — VENDENDO\n" + mint)
        try:
            result = bridge.submit_swap(mint, "sell", int(position["token_amount_raw"]), wallet)
            settled = _settle(result["order_id"], wallet)
        except Exception as exc:
            _failure(send, chat, "venda", mint, exc)
            return
        if settled.get("status") == "settled_sell":
            send(chat, "✅ VENDA CONFIRMADA\nResultado: " + format(settled["pnl_usd"], "+.2f") +
                 " USD\nTx: https://solscan.io/tx/" + settled["signature"])
        else:
            send(chat, "⚠️ Venda enviada, conferir manualmente (" +
                 str(settled.get("reason", settled.get("status"))) + "). Robô pausado.\nTx: https://solscan.io/tx/" +
                 str(result["signature"]))
        return  # one order per cycle
