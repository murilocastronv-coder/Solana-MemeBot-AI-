"""Order engine for spot positions: exits (stop/trailing), recovery and sync.

The memecoin buying strategies (volume, sniper, pump.fun, 1-min candle,
launch sniper) were removed; the bot now trades only MEV. This module keeps
the generic order executor and the exit/recovery logic so that any position
still open is sold safely.
"""
import logging
import time

import live_order_bridge as bridge
import live_trade_state as state
from bot_wallet_service import get_address
from live_exit_monitor import sell_signals

import os
import requests
from live_swap_executor import rpc
from jupiter_v2_executor import SOL_MINT

ORDER_FRACTION = float(os.getenv("ORDER_FRACTION", "0.10"))
DAILY_LOSS_FRACTION = 0.15
MIN_ORDER_USD = 3.0
_safety = {}  # mint -> (ok, reason) cache for anti-rug checks
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
    try:
        return _settle_now(order_id, wallet)
    finally:
        state.IN_FLIGHT.pop(order_id, None)     # from now on the background check may take over


def _settle_now(order_id, wallet):
    # The bridge already waited for finality; balances may lag a few seconds.
    for _ in range(5):
        result = state.reconcile_known_signature(order_id, wallet)
        if result.get("status") != "waiting_finality":
            return result
        time.sleep(3)
    return result


def _failure(send, chat, side, mint, exc):
    logging.warning("%s failed for %s: %s", side, mint[:8], exc)
    pending = state.pending_orders()
    if pending:
        send(chat, "⏳ " + side.capitalize() + " ainda sem confirmação: " + mint +
             "\nMotivo: " + str(exc)[:200] +
             "\nVou conferir sozinho os saldos na blockchain em ~2 min e liberar o robô. "
             "Nada é reenviado em dobro.")
    else:
        _notice(send, chat, ("fail", side, mint, str(exc)[:60]),
                "⛔ " + side.capitalize() + " não enviada (" + mint[:6] + "…): " + str(exc)[:220] +
                ("\nTento de novo em 1 min." if side == "venda" else ""), 900)


def account_value_usd(wallet):
    """Free SOL (in USD) plus cost of open positions."""
    price = float(bridge.sol_usd_price())
    sol = bridge.get_sol_balance(wallet) / 1e9
    positions = state._load()["positions"].values()
    return sol * price + sum(float(p.get("cost_usd", 0)) for p in positions)


def order_size_usd(wallet, fraction=None):
    value = account_value_usd(wallet)
    state.set_daily_loss_limit(value * DAILY_LOSS_FRACTION)
    fraction = ORDER_FRACTION if fraction is None else fraction
    return round(min(value * fraction, bridge.MAX_ORDER_USD), 2), value


def _why_not_ready():
    st = state.status()
    if not st["enabled"] or st["halted"]:
        return None  # owner switched it off (or a limit halted it): nothing to report
    try:
        bridge.preflight()
    except Exception as exc:
        return str(exc)[:200]
    if not get_address():
        return "Carteira do bot não encontrada"
    return None


REPORT_EVERY_SECONDS = 3600
SELL_RETRY_SECONDS = 60
MAX_SELL_FAILURES = 5  # after this many rejected sells (honeypot / no route) the position is written off
_sell_failed = {}  # mint -> monotonic time of last failed sell attempt
_sell_fail_count = {}


def _write_off(send, chat, mint, symbol, no_route):
    result = state.write_off(mint)
    _sell_fail_count.pop(mint, None)
    _sell_failed.pop(mint, None)
    if not result:
        return
    why = ("sem rota de venda há 10 min (moeda morta / golpe)" if no_route
           else "não foi possível vender após " + str(MAX_SELL_FAILURES) + " tentativas")
    send(chat, "💀 MOEDA ZERADA — " + symbol + "\n" + why + ".\nRegistrei perda de US$ " +
         format(result["cost_usd"], ".2f") + " e liberei a vaga. O robô continua operando." +
         ("\n⚠️ Isso atingiu o limite de perda do dia: robô pausado. Toque em ▶️ Iniciar para religar."
          if result["halted"] else "") + "\n📄 " + mint)
EXIT_TEXT = {
    "normal": "🛑 Stop -8% | 🚀 a partir de +15% deixa o lucro correr (vende 10% abaixo do topo)",
    "tradeB": "🎯 Alvo +15% | 🛑 Stop −8%",
    "copy": "👥 Vende quando a carteira copiada vender (sem alvo/stop fixo)",
    "clone": "🤖 Sai com o alvo, o stop e o tempo máximo medidos na carteira de referência",
    "launch": "🆕 Lançamento: vende TUDO assim que der +3% de lucro líquido, na hora se o criador vender, sai cedo se cair 10% ou não subir +4% em 25 s, stop -15%, máx. 2 min",
    "candle": "🎯 Vende no alvo +1,5% durante a vela | 🛑 stop -3% | 🕯️ senão vende quando a vela de 1 min fechar",
    "hot": "🛑 Stop -6% | 🚀 a partir de +8% deixa correr (vende 5% abaixo do topo) | ⏱️ sai em 45 min se não andar",
    "sniper": "🛑 Stop -" + format(float(os.getenv("SNIPER_STOP_LOSS", "0.40")) * 100, ".0f") + "% | 🚀 a partir de +25% deixa correr (vende 12% abaixo do topo) | ⏱️ sai em 30 min se não decolar",
    "consensus": "🛑 Stop -40% | 🚀 a partir de +25% stop móvel (vende 10% abaixo do topo)",
    "rocket": "🚀 Sniper: 🛑 stop -40% | 💰 2x vende metade, 5x mais metade | 🚀 a partir de +30% stop móvel (15% abaixo do topo) | ⏱️ sai em 90 min se não decolar",
    "dip": "📉 Dip Bot: 🛑 stop -40% | 🚀 a partir de +12% stop móvel (vende 6% abaixo do topo) | ⏱️ sai em 6 h se não subir",
    "signal": "📡 Sinal: vende 1/2 no TP1, 1/4 no TP2 e o resto no TP3 | 🛑 stop do sinal (abaixo do fundo) | ⏱️ fecha em 24 h se não andar",
    "pumpfun": "🛑 Stop -12% | 🚀 a partir de +25% deixa correr (vende 12% abaixo do topo) | ⏱️ sai em 30 min se não decolar",
}


def _execute_buy(p, mint, symbol, send, chat, wallet, fraction, mode, usd=None, add_on=False):
    try:
        if usd is not None:   # fixed-size order (Strategy B: US$ 10)
            value = account_value_usd(wallet)
            order_usd = round(min(float(usd), bridge.MAX_ORDER_USD), 2)
        else:
            order_usd, value = order_size_usd(wallet, fraction)
    except Exception as exc:
        _notice(send, chat, "sizing", "⛔ Não consegui calcular o saldo: " + str(exc)[:150], REPORT_EVERY_SECONDS)
        return None
    pct = (format(fraction * 100, ".0f") + "%") if usd is None else "valor fixo"
    if order_usd < MIN_ORDER_USD:
        _notice(send, chat, "low_balance_" + mode, "💤 Saldo baixo: conta vale US$ " + format(value, ".2f") +
                "; " + pct + " = US$ " + format(order_usd, ".2f") + " (mínimo US$ 3). Deposite SOL para operar.",
                REPORT_EVERY_SECONDS)
        return None
    _tried[mint] = time.monotonic()
    tag = {"sniper": "🎯 SNIPER ", "pumpfun": "🟣 PUMP.FUN ", "tradeB": "📈 ESTRATÉGIA B ", "copy": "👥 COPY ", "clone": "🤖 ESTRATÉGIA ", "consensus": "🤝 CONSENSO ", "signal": "📡 SINAL ", "dip": "📉 DIP BOT ", "rocket": "🚀 SNIPER "}.get(mode, "")
    send(chat, "🟡 " + tag + "COMPRANDO (dinheiro real) US$ " + format(order_usd, ".2f") + " de " + symbol +
         " (" + pct + " da conta de US$ " + format(value, ".2f") + ")\n" + mint)
    try:
        result = bridge.submit_swap(mint, "buy", int(round(order_usd * 1_000_000)), wallet, add_on=add_on)
        settled = _settle(result["order_id"], wallet)
    except Exception as exc:
        _failure(send, chat, "compra", mint, exc)
        return None
    if settled.get("status") == "settled_buy":
        state.set_position_label(mint, symbol, mode)
        n = state.status()["positions"]
        send(chat, "🟢 " + tag + "COMPROU " + symbol + "\n"
             "💵 Valor: US$ " + format(order_usd, ".2f") + " (" + format(result["input_amount_raw"] / 1e9, ".4f") + " SOL)\n"
             + EXIT_TEXT[mode] + "\n"
             "🛡️ Anti-golpe OK\n"
             "📦 Posições abertas: " + str(n) + "/" + str(state.MAX_POSITIONS) + "\n"
             "📄 " + mint + "\n"
             "🔗 https://solscan.io/tx/" + settled["signature"])
    else:
        send(chat, "⏳ Compra enviada, conferindo saldo (" + str(settled.get("reason", settled.get("status"))) +
             "). Libero sozinho em ~2 min.\nTx: https://solscan.io/tx/" + str(result["signature"]))
    return settled


def sell_part(mint, fraction, reason, send, chat, note=None):
    """Sell `fraction` of a position (the copied wallet took part of its profit). Returns like sell_now."""
    if fraction is None or float(fraction) >= 0.95:
        return sell_now(mint, reason, send, chat)
    position = state._load()["positions"].get(mint)
    wallet = get_address()
    if not position or not wallet:
        return "gone"
    try:
        held = bridge.get_token_balance(wallet, mint)
    except Exception:
        held = None
    if held == 0:
        state.drop_position(mint)
        return "gone"
    tracked = int(position["token_amount_raw"])
    if held is not None and held < tracked:
        state.set_position_amount(mint, held)
        tracked = held
    amount = int(tracked * float(fraction))
    if amount <= 0:
        return "gone"
    symbol = position.get("symbol") or mint[:6]
    result = bridge.submit_swap(mint, "sell", amount, wallet, partial=True)
    settled = _settle(result["order_id"], wallet)
    if settled.get("status") == "settled_sell":
        pnl = settled["pnl_usd"]
        send(chat, ("💰 LUCRO" if pnl >= 0 else "🔻 PREJUÍZO") + " — VENDEU " + format(float(fraction) * 100, ".0f") + "% de " +
             symbol + " (" + (note or "a carteira copiada vendeu essa parte") + ")\n💵 Parte: US$ " + format(settled["cost_usd"], ".2f") +
             " → US$ " + format(settled["proceeds_usd"], ".2f") + " · P/L " + format(pnl, "+.2f") + " USD\n" +
             "📦 O resto continua aberto" + (" com stop móvel." if note else " e vende junto com a carteira.") + "\n🔗 https://solscan.io/tx/" + settled["signature"])
        return "sold"
    send(chat, "⏳ Venda parcial enviada, conferindo saldo. Libero sozinho em ~2 min.\nTx: https://solscan.io/tx/" +
         str(result["signature"]))
    return "pending"


def sell_now(mint, reason, send, chat):
    """Sell one whole position right away (used when the candle closes)."""
    position = state._load()["positions"].get(mint)
    wallet = get_address()
    if not position or not wallet:
        return "gone"
    try:
        held = bridge.get_token_balance(wallet, mint)
    except Exception:
        held = None
    if held == 0:
        state.drop_position(mint)
        return "gone"
    amount = int(position["token_amount_raw"])
    if held is not None and held < amount:
        state.set_position_amount(mint, held)
        amount = held
    symbol = position.get("symbol") or mint[:6]
    state.mark_exit_reason(mint, reason)
    result = bridge.submit_swap(mint, "sell", amount, wallet)
    settled = _settle(result["order_id"], wallet)
    if settled.get("status") == "settled_sell":
        send(chat, sell_message(symbol, {"reason": reason}, settled))
        return "sold"
    send(chat, "⏳ Venda enviada, conferindo saldo. Libero sozinho em ~2 min.\nTx: https://solscan.io/tx/" +
         str(result["signature"]))
    return "pending"


def sync_positions(send, chat):
    """Drop positions the wallet no longer holds (sold by hand, e.g. in Phantom).

    Only runs with no pending order; a balance below 1% of the recorded amount
    counts as gone (dust left after a manual sale).
    """
    wallet = get_address()
    if not wallet or state.pending_orders():
        return []
    dropped = []
    for mint, position in list(state._load()["positions"].items()):
        try:
            held = bridge.get_token_balance(wallet, mint)
        except Exception:
            continue  # RPC trouble: never drop on uncertainty
        recorded = int(position.get("token_amount_raw") or 0)
        if held == 0 or (recorded > 0 and held * 100 < recorded):
            state.drop_position(mint)
            symbol = position.get("symbol") or mint[:6]
            dropped.append(symbol)
            if chat:
                send(chat, "🧹 " + symbol + " não está mais na carteira (vendida fora do bot). "
                     "Tirei da lista de posições; o robô volta a comprar.\n" + mint)
    return dropped


def sell_message(symbol, signal, settled):
    pnl = settled["pnl_usd"]
    pct = pnl / settled["cost_usd"] * 100 if settled.get("cost_usd") else 0.0
    why = {"take_profit": "🚀 Stop móvel (topo +" + format(signal.get("peak_pct", 0), ".1f") + "%)",
           "time_stop": "⏱️ Tempo esgotado (não andou a tempo)",
           "candle_close": "🕯️ Vela de 1 min fechou",
           "candle_peak": "🎯 Alvo da vela atingido",
           "launch_profit": "💰 Alvo de lucro atingido",
           "launch_peak": "🎯 Vendeu perto do pico (stop móvel)",
           "launch_drop": "📉 Começou a cair antes de dar lucro",
           "launch_flat": "😴 Não subiu rápido",
           "launch_dev_sold": "🚨 Criador vendeu (sinal de rug): saiu na hora",
           "launch_time": "⏱️ Tempo máximo encerrado",
           "copy_follow": "👥 A carteira copiada vendeu",
           "signal_tp1": "✅ Sinal: TP1 atingido (vendeu metade)",
           "signal_tp2": "🚀 Sinal: TP2 atingido (vendeu parte)",
           "signal_tp3": "🌙 Sinal: TP3 atingido (potencial)",
           "signal_sl": "🛑 Sinal: stop loss",
           "signal_be": "↩️ Sinal: voltou à entrada depois do TP1",
           "signal_tp2_back": "↩️ Sinal: voltou ao TP1 depois do TP2",
           "signal_expired": "⏱️ Sinal: tempo esgotado",
           "signal_safety_stop": "🛑 Sinal: stop de segurança"}.get(signal["reason"], "🛑 Stop loss")
    summary = state.pnl_summary()
    won, closed = summary["trades_won"], summary["trades_closed"]
    return (("💰 LUCRO" if pnl >= 0 else "🔻 PREJUÍZO") + " — VENDEU " + symbol + "\n"
            + why + "\n"
            "💵 Comprou: US$ " + format(settled["cost_usd"], ".2f") +
            " → Vendeu: US$ " + format(settled["proceeds_usd"], ".2f") + "\n"
            "📊 P/L: " + format(pnl, "+.2f") + " USD (" + format(pct, "+.1f") + "%)\n"
            "📅 P/L hoje: " + format(summary["day_pnl_usd"], "+.2f") + " USD\n"
            "🏦 P/L total: " + format(summary["total_pnl_usd"], "+.2f") + " USD (" +
            str(won) + "/" + str(closed) + " operações no lucro)\n"
            "🔗 https://solscan.io/tx/" + settled["signature"])


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
    now = time.monotonic()
    signals = sell_signals()
    from live_exit_monitor import ARMED, trail_text
    while ARMED:
        m, p, sig = ARMED.pop(0)
        send(chat, "🚀 STOP MÓVEL ATIVADO — " + str(p.get("symbol") or m[:6]) + " " + format(sig["change_pct"], "+.0f") +
             "%\nAgora vendo se cair " + trail_text(p) + "% abaixo do topo (o stop sobe junto com o preço).\n" + m)
    for mint, position, signal in signals:
        symbol = position.get("symbol") or mint[:6]
        if signal["action"] == "write_off" or _sell_fail_count.get(mint, 0) >= MAX_SELL_FAILURES:
            _write_off(send, chat, mint, symbol, signal["action"] == "write_off")
            continue
        if now - _sell_failed.get(mint, -SELL_RETRY_SECONDS) < SELL_RETRY_SECONDS:
            continue
        try:
            held = bridge.get_token_balance(wallet, mint)
        except Exception:
            held = None
        if held == 0:
            state.drop_position(mint)
            send(chat, "⚠️ A carteira não tem mais " + symbol + " (" + mint + "). Foi vendida fora do bot ou a moeda "
                 "sumiu. Removi da lista para o robô continuar.")
            continue
        amount = int(position["token_amount_raw"])
        if held is not None and held < amount:
            state.set_position_amount(mint, held)  # transfer-fee tokens: sell what we really hold
            amount = held
        label = {"take_profit": "🚀 STOP MÓVEL", "time_stop": "⏱️ TEMPO ESGOTADO"}.get(signal["reason"], "🛑 STOP LOSS")
        send(chat, label + " (" + format(signal["change_pct"], "+.2f") + "%) — VENDENDO " + symbol + "\n" + mint)
        try:
            state.mark_exit_reason(mint, signal["reason"])
            result = bridge.submit_swap(mint, "sell", amount, wallet)
            settled = _settle(result["order_id"], wallet)
        except Exception as exc:
            _sell_failed[mint] = time.monotonic()
            if not state.pending_orders():
                _sell_fail_count[mint] = _sell_fail_count.get(mint, 0) + 1
            _failure(send, chat, "venda", mint, exc)
            if state.pending_orders():
                return  # uncertain: wait for automatic reconciliation
            continue  # nothing was sent: try the other positions
        if settled.get("status") == "settled_sell":
            send(chat, sell_message(position.get("symbol") or mint[:6], signal, settled))
        else:
            send(chat, "⏳ Venda enviada, conferindo saldo (" +
                 str(settled.get("reason", settled.get("status"))) + "). Libero sozinho em ~2 min.\nTx: https://solscan.io/tx/" +
                 str(result["signature"]))
        return  # one order per cycle


def recover(send, chat):
    """Resolve stuck pending orders from on-chain balances and tell the owner."""
    if not chat or not state.pending_orders():
        return []
    wallet = get_address()
    if not wallet:
        return []
    results = state.auto_reconcile(wallet)
    for r in results:
        mint = r.get("mint", "?")
        if r["status"] == "settled_buy":
            send(chat, "✅ Compra confirmada pela conferência automática: " + mint + "\nRobô liberado.")
        elif r["status"].startswith("settled_sell"):
            send(chat, "✅ Venda confirmada pela conferência automática: " + mint +
                 ("\nP/L: " + format(r["pnl_usd"], "+.2f") + " USD" if "pnl_usd" in r else "") +
                 ("\nVenda parcial: o resto continua com stop." if r.get("partial") else "") + "\nRobô liberado.")
        elif r["status"] == "discarded":
            what = "Compra" if r.get("side") == "buy" else "Venda"
            send(chat, "ℹ️ " + what + " não foi executada na blockchain (" + mint + "). Nada mudou na carteira. Robô liberado" +
                 ("; a venda será tentada de novo." if r.get("side") == "sell" else "."))
        elif r["status"] == "error":
            logging.warning("Pending recovery failed for %s: %s", mint[:8], r.get("error"))
            _notice(send, chat, ("recover_err", mint), "⚠️ Não consegui conferir a ordem pendente de " + mint +
                    " ainda (" + str(r.get("error", "?")) + "); tento de novo automaticamente.", REPORT_EVERY_SECONDS)
    return results


def why_stopped():
    """Plain-language reason the robot is not running (None if running)."""
    st = state.status()
    if st["pending"]:
        return "ordem pendente sem confirmação (toque em 🔧 Destravar robô)"
    if ready():
        return None
    if not st["enabled"] or st["halted"]:
        if st["daily_loss_usd"] >= state.daily_loss_limit():
            return "limite de perda do dia atingido; toque em ▶️ Iniciar amanhã ou agora para religar"
        return "desligado (toque em ▶️ Iniciar trading real)"
    return _why_not_ready() or "verificação de segurança falhou"


def unstick(send, chat):
    """Owner button: show stuck orders and resolve them now from balances."""
    pending = state.pending_orders()
    wallet = get_address()
    if not pending:
        reason = why_stopped()
        send(chat, "✅ Nenhuma ordem pendente." + ("" if reason is None else "\nRobô parado: " + reason))
        return []
    lines = ["🔧 ORDENS PENDENTES:"]
    for order_id, p in pending.items():
        age = int(state._age_seconds(p))
        lines.append("• " + ("Compra" if p["side"] == "buy" else "Venda") + " " + p["mint"] +
                     "\n  etapa: " + str(p.get("stage", "reservada")) + " | há " + str(age // 60) + " min" +
                     ("\n  tx: https://solscan.io/tx/" + p["signature"] if p.get("signature") else "\n  sem assinatura (talvez nem tenha sido enviada)"))
    send(chat, "\n".join(lines))
    if not wallet:
        send(chat, "⚠️ Carteira do bot não encontrada.")
        return []
    results = state.auto_reconcile(wallet, min_age_seconds=60)
    for r in results:
        if r["status"] == "waiting":
            send(chat, "⏳ Ordem muito recente; tente de novo em " + str(r["seconds_left"]) + " s.")
        elif r["status"] == "error":
            send(chat, "❌ Erro ao conferir " + r.get("mint", "?") + ": " + str(r.get("error")) +
                 "\nMande este print para o suporte.")
    recover_messages = [r for r in results if r["status"] not in ("waiting", "error")]
    for r in recover_messages:
        mint = r.get("mint", "?")
        if r["status"] == "settled_buy":
            send(chat, "✅ Compra confirmada: " + mint)
        elif r["status"].startswith("settled_sell"):
            send(chat, "✅ Venda confirmada: " + mint + ("\nP/L: " + format(r["pnl_usd"], "+.2f") + " USD" if "pnl_usd" in r else ""))
        elif r["status"] == "discarded":
            send(chat, "ℹ️ Ordem não executada na blockchain (" + mint + "). Liberada.")
    reason = why_stopped()
    send(chat, "🟢 Robô ligado e operando." if reason is None else "🔴 Robô parado: " + reason)
    return results
