"""Autonomous real-money trading cycle (buy + TP/SL sell) on Solana via Jupiter.

Runs only when ALL of these hold:
- LIVE_TRADING_ENABLED=true and every live_order_bridge.preflight() check passes;
- the owner pressed "Iniciar trading real" (persistent state enabled, not halted).

Rules: each buy uses ORDER_FRACTION (20%) of account value (SOL + open
positions), capped by MAX_ORDER_USD; at most 3 positions. Buys only tokens
in an uptrend with buy pressure whose mint/freeze authority is revoked.
Exits: SL -8%; from +15% a trailing stop sells 10% below the best value.
Daily loss limit = 15% of account value (min US$ 3). One order at a time. Any uncertain order stays "pending" and
blocks all new orders until reconciled; nothing is ever resent automatically.
"""
import logging
import time

import live_order_bridge as bridge
import live_trade_state as state
from bot_wallet_service import get_address
from live_exit_monitor import sell_signals
from paper_auto import candidate

import os
from live_swap_executor import rpc
from jupiter_v2_executor import SOL_MINT

ORDER_FRACTION = float(os.getenv("ORDER_FRACTION", "0.20"))
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


def momentum_ok(p):
    """Uptrend now: positive 1h and 6h, not dumping in 5m, more buys than sells in 1h."""
    try:
        ch = p.get("priceChange", {}) or {}
        tx = (p.get("txns", {}) or {}).get("h1", {}) or {}
        buys, sells = int(tx.get("buys") or 0), int(tx.get("sells") or 0)
        return (float(ch.get("h1") or 0) > 0 and float(ch.get("h6") or 0) > 0 and
                float(ch.get("m5") or 0) > -5 and buys >= 20 and buys > sells * 1.1)
    except (TypeError, ValueError):
        return False


def live_candidate(p):
    return candidate(p) and momentum_ok(p)


def token_safety(mint):
    """Anti-rug: creator must not be able to mint more or freeze holders."""
    if mint in _safety:
        return _safety[mint]
    try:
        info = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        parsed = (((info or {}).get("value") or {}).get("data") or {}).get("parsed") or {}
        data = parsed.get("info") or {}
        if parsed.get("type") != "mint":
            result = (False, "não é um token válido")
        elif data.get("mintAuthority"):
            result = (False, "criador ainda pode criar mais moedas")
        elif data.get("freezeAuthority"):
            result = (False, "criador pode congelar carteiras")
        else:
            result = (True, "")
    except Exception:
        return (False, "verificação anti-golpe indisponível")  # do not cache network errors
    _safety[mint] = result
    return result


def account_value_usd(wallet):
    """Free SOL (in USD) plus cost of open positions."""
    price = float(bridge.sol_usd_price())
    sol = bridge.get_sol_balance(wallet) / 1e9
    positions = state._load()["positions"].values()
    return sol * price + sum(float(p.get("cost_usd", 0)) for p in positions)


def order_size_usd(wallet):
    value = account_value_usd(wallet)
    state.set_daily_loss_limit(value * DAILY_LOSS_FRACTION)
    return round(min(value * ORDER_FRACTION, bridge.MAX_ORDER_USD), 2), value


def _skip_reasons(rows):
    """Count why scanned tokens did not qualify (for the hourly report)."""
    counts = {"liquidez < US$ 30 mil": 0, "volume 24h < US$ 20 mil": 0, "idade < 24h": 0,
              "FDV/liquidez > 100": 0, "variação 24h fora de -20%..+80%": 0, "sem tendência de alta agora": 0}
    now_ms = time.time() * 1000
    for p in rows:
        try:
            liq = float(p.get("liquidity", {}).get("usd") or 0)
            vol = float(p.get("volume", {}).get("h24") or 0)
            fdv = float(p.get("fdv") or 0)
            age = (now_ms - float(p.get("pairCreatedAt") or now_ms)) / 3_600_000
            change = float(p.get("priceChange", {}).get("h24") or 0)
        except (TypeError, ValueError):
            continue
        if liq < 30000: counts["liquidez < US$ 30 mil"] += 1
        if vol < 20000: counts["volume 24h < US$ 20 mil"] += 1
        if age < 24: counts["idade < 24h"] += 1
        if not (fdv > 0 and liq > 0 and fdv / liq <= 100): counts["FDV/liquidez > 100"] += 1
        if not -20 <= change <= 80: counts["variação 24h fora de -20%..+80%"] += 1
        if not momentum_ok(p): counts["sem tendência de alta agora"] += 1
    return counts


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


def buy_cycle(rows, send, chat):
    """Buy at most one new token per scan (20% of account value)."""
    if not chat:
        return None
    if not ready():
        problem = _why_not_ready()
        if problem:
            _notice(send, chat, "not_ready", "⚠️ Robô ligado mas bloqueado: " + problem, REPORT_EVERY_SECONDS)
        return None
    allowed, reason = state.can_open()
    if not allowed:
        labels = {"Position limit": "3 posições abertas; espera vender para comprar de novo",
                  "Daily loss limit": "limite de perda do dia (US$ " + format(state.daily_loss_limit(), ".2f") + ") atingido",
                  "Unreconciled transaction": "transação pendente de conferência"}
        _notice(send, chat, "cannot_open", "⏸️ Sem novas compras: " + labels.get(reason, reason), REPORT_EVERY_SECONDS)
        return None
    wallet = get_address()
    open_mints = set(state._load()["positions"])
    now = time.monotonic()
    qualified = 0
    unsafe = []
    for p in rows:
        if not live_candidate(p):
            continue
        qualified += 1
        mint = p["baseToken"]["address"]
        if mint in open_mints or now - _tried.get(mint, -RETRY_MINT_AFTER_SECONDS) < RETRY_MINT_AFTER_SECONDS:
            continue
        symbol = str(p["baseToken"].get("symbol", "?"))[:20]
        safe, why = token_safety(mint)
        if not safe:
            unsafe.append(symbol + ": " + why)
            continue
        try:
            order_usd, value = order_size_usd(wallet)
        except Exception as exc:
            _notice(send, chat, "sizing", "⛔ Não consegui calcular o saldo: " + str(exc)[:150], REPORT_EVERY_SECONDS)
            return None
        if order_usd < MIN_ORDER_USD:
            _notice(send, chat, "low_balance", "💤 Saldo baixo: conta vale US$ " + format(value, ".2f") +
                    "; 20% = US$ " + format(order_usd, ".2f") + " (mínimo US$ 3). Deposite SOL para operar.",
                    REPORT_EVERY_SECONDS)
            return None
        _tried[mint] = now
        send(chat, "🟡 COMPRANDO (dinheiro real) US$ " + format(order_usd, ".2f") + " de " + symbol +
             " (20% da conta de US$ " + format(value, ".2f") + ")\n" + mint)
        try:
            result = bridge.submit_swap(mint, "buy", int(round(order_usd * 1_000_000)), wallet)
            settled = _settle(result["order_id"], wallet)
        except Exception as exc:
            _failure(send, chat, "compra", mint, exc)
            return None
        if settled.get("status") == "settled_buy":
            state.set_position_label(mint, symbol)
            n = state.status()["positions"]
            send(chat, "🟢 COMPROU " + symbol + "\n"
                 "💵 Valor: US$ " + format(order_usd, ".2f") + " (" + format(result["input_amount_raw"] / 1e9, ".4f") + " SOL)\n"
                 "🛑 Stop -8% | 🚀 a partir de +15% deixa o lucro correr (vende 10% abaixo do topo)\n"
                 "🛡️ Anti-golpe OK (sem mint/freeze authority)\n"
                 "📦 Posições abertas: " + str(n) + "/3\n"
                 "📄 " + mint + "\n"
                 "🔗 https://solscan.io/tx/" + settled["signature"])
        else:
            send(chat, "⚠️ Compra enviada mas não conciliada (" + str(settled.get("reason", settled.get("status"))) +
                 "). Robô bloqueado.\nTx: https://solscan.io/tx/" + str(result["signature"]))
        return settled
    reasons = sorted(_skip_reasons(rows).items(), key=lambda kv: -kv[1])
    detail = "\n".join("• " + str(n) + " com " + label for label, n in reasons if n)
    extra = " (já compradas, tentadas nas últimas 24h ou reprovadas no anti-golpe)" if qualified else ""
    if unsafe:
        detail_unsafe = "\n🛡️ Anti-golpe recusou: " + "; ".join(unsafe[:5])
    else:
        detail_unsafe = ""
    _notice(send, chat, "no_candidate",
            "🔎 Robô ligado e procurando. Última varredura: " + str(len(rows)) + " moedas, " +
            str(qualified) + " passaram nos filtros" + extra + ".\nMotivos de recusa:\n" + (detail or "• nenhuma moeda recebida") +
            detail_unsafe + "\nPróximo resumo em 1 hora.", REPORT_EVERY_SECONDS)
    return None


def sell_message(symbol, signal, settled):
    pnl = settled["pnl_usd"]
    pct = pnl / settled["cost_usd"] * 100 if settled.get("cost_usd") else 0.0
    why = ("🚀 Stop móvel (topo +" + format(signal.get("peak_pct", 0), ".1f") + "%)"
           if signal["reason"] == "take_profit" else "🛑 Stop loss")
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
    for mint, position, signal in sell_signals():
        label = "🚀 STOP MÓVEL" if signal["reason"] == "take_profit" else "🛑 STOP LOSS"
        send(chat, label + " (" + format(signal["change_pct"], "+.2f") + "%) — VENDENDO\n" + mint)
        try:
            result = bridge.submit_swap(mint, "sell", int(position["token_amount_raw"]), wallet)
            settled = _settle(result["order_id"], wallet)
        except Exception as exc:
            _failure(send, chat, "venda", mint, exc)
            return
        if settled.get("status") == "settled_sell":
            send(chat, sell_message(position.get("symbol") or mint[:6], signal, settled))
        else:
            send(chat, "⚠️ Venda enviada, conferir manualmente (" +
                 str(settled.get("reason", settled.get("status"))) + "). Robô pausado.\nTx: https://solscan.io/tx/" +
                 str(result["signature"]))
        return  # one order per cycle
