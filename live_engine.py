"""Autonomous real-money trading cycle (buy + TP/SL sell) on Solana via Jupiter.

Runs only when ALL of these hold:
- LIVE_TRADING_ENABLED=true and every live_order_bridge.preflight() check passes;
- the owner pressed "Iniciar trading real" (persistent state enabled, not halted).

Rules: each buy uses ORDER_FRACTION (10%) of account value (SOL + open
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


# High-volume, fast-moving memecoins ("hot" strategy).
HOT_MIN_LIQUIDITY = float(os.getenv("HOT_MIN_LIQUIDITY_USD", "100000"))
HOT_MIN_VOLUME_24H = float(os.getenv("HOT_MIN_VOLUME_24H_USD", "1000000"))
HOT_MIN_VOLUME_1H = float(os.getenv("HOT_MIN_VOLUME_1H_USD", "150000"))
HOT_MIN_TURNOVER_1H = 0.3      # 1h volume >= 30% of liquidity: money is moving fast
HOT_MIN_TXNS_5M = 100          # busy right now
HOT_MIN_AGE_HOURS = 6          # skip the first hours, when most rugs happen
HOT_RETRY_SECONDS = 2 * 3600   # a hot token can be traded again after 2 h


def _f(d, *path):
    for key in path:
        d = (d or {}).get(key) if isinstance(d, dict) else None
    try:
        return float(d or 0)
    except (TypeError, ValueError):
        return 0.0


def hot_reasons(p, now_ms=None):
    """Why a pair is NOT a hot candidate (empty list = buy signal)."""
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    liq = _f(p, "liquidity", "usd")
    vol24, vol1h = _f(p, "volume", "h24"), _f(p, "volume", "h1")
    m5, h1, h6 = _f(p, "priceChange", "m5"), _f(p, "priceChange", "h1"), _f(p, "priceChange", "h6")
    b5, s5 = _f(p, "txns", "m5", "buys"), _f(p, "txns", "m5", "sells")
    b1, s1 = _f(p, "txns", "h1", "buys"), _f(p, "txns", "h1", "sells")
    fdv = _f(p, "fdv")
    created = _f(p, "pairCreatedAt")
    age = (now_ms - created) / 3_600_000 if created else 0
    reasons = []
    mint = (p.get("baseToken") or {}).get("address")
    if p.get("chainId", "solana") != "solana" or not mint or _f(p, "priceUsd") <= 0:
        reasons.append("dados inválidos")
    if liq < HOT_MIN_LIQUIDITY:
        reasons.append("liquidez < US$ " + format(HOT_MIN_LIQUIDITY / 1000, ".0f") + " mil")
    if vol24 < HOT_MIN_VOLUME_24H:
        reasons.append("volume 24h < US$ " + format(HOT_MIN_VOLUME_24H / 1e6, ".1f") + " mi")
    if vol1h < HOT_MIN_VOLUME_1H or (liq > 0 and vol1h / liq < HOT_MIN_TURNOVER_1H):
        reasons.append("pouco volume na última hora")
    if b5 + s5 < HOT_MIN_TXNS_5M or b5 <= s5 or b1 <= s1 * 1.05:
        reasons.append("sem pressão compradora agora")
    if not (-1 <= m5 <= 8 and 1 <= h1 <= 50 and h6 > 0):
        reasons.append("sem subida saudável (5m/1h/6h)")
    if age < HOT_MIN_AGE_HOURS:
        reasons.append("idade < " + str(HOT_MIN_AGE_HOURS) + "h")
    if not (fdv > 0 and liq > 0 and fdv / liq <= 50):
        reasons.append("FDV/liquidez > 50")
    return reasons


def _near_misses(rows, limit=3):
    """Highest-volume tokens that failed only one rule (to show what is close)."""
    near = []
    for p in sorted((r for r in rows if isinstance(r, dict)), key=lambda r: -_f(r, "volume", "h1")):
        reasons = hot_reasons(p)
        if len(reasons) == 1:
            near.append(str((p.get("baseToken") or {}).get("symbol", "?"))[:12] + ": " + reasons[0])
        if len(near) >= limit:
            break
    return near


def hot_candidate(p):
    return not hot_reasons(p)


def live_candidate(p):
    return hot_candidate(p)


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


ROUNDTRIP_MIN = 0.92  # buy then sell back must return >= 92% (catches taxes/honeypots/thin pools)
_roundtrip_cache = {}


def roundtrip_ok(mint, usd=10.0):
    """Sell test before buying: quote SOL->token, then token->SOL for the tokens received."""
    hit = _roundtrip_cache.get(mint)
    if hit and time.monotonic() - hit[0] < 120:
        return hit[1]
    try:
        lamports = int(usd / float(bridge.sol_usd_price()) * 1e9)
        tokens = int(bridge.order(SOL_MINT, mint, lamports)["outAmount"])
        back = int(bridge.order(mint, SOL_MINT, tokens)["outAmount"])
        ratio = back / lamports if lamports else 0
        result = (ratio >= ROUNDTRIP_MIN, "teste de venda devolveu " + format(ratio * 100, ".0f") + "%")
    except Exception:
        return (False, "teste de venda falhou (sem rota de venda)")
    _roundtrip_cache[mint] = (time.monotonic(), result)
    return result


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


def _skip_reasons(rows):
    """Count why scanned tokens did not qualify (for the hourly report)."""
    counts = {}
    for p in rows:
        if not isinstance(p, dict):
            continue
        for reason in hot_reasons(p):
            counts[reason] = counts.get(reason, 0) + 1
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
BUYS_PER_SCAN = 2  # up to 2 buys per scan; never more than 3 open positions


EXIT_TEXT = {
    "normal": "🛑 Stop -8% | 🚀 a partir de +15% deixa o lucro correr (vende 10% abaixo do topo)",
    "hot": "🛑 Stop -6% | 🚀 a partir de +8% deixa correr (vende 5% abaixo do topo) | ⏱️ sai em 45 min se não andar",
    "sniper": "🛑 Stop -12% | 🚀 a partir de +25% deixa correr (vende 12% abaixo do topo) | ⏱️ sai em 30 min se não decolar",
    "pumpfun": "🛑 Stop -12% | 🚀 a partir de +25% deixa correr (vende 12% abaixo do topo) | ⏱️ sai em 30 min se não decolar",
}


def _execute_buy(p, mint, symbol, send, chat, wallet, fraction, mode):
    try:
        order_usd, value = order_size_usd(wallet, fraction)
    except Exception as exc:
        _notice(send, chat, "sizing", "⛔ Não consegui calcular o saldo: " + str(exc)[:150], REPORT_EVERY_SECONDS)
        return None
    pct = format(fraction * 100, ".0f") + "%"
    if order_usd < MIN_ORDER_USD:
        _notice(send, chat, "low_balance_" + mode, "💤 Saldo baixo: conta vale US$ " + format(value, ".2f") +
                "; " + pct + " = US$ " + format(order_usd, ".2f") + " (mínimo US$ 3). Deposite SOL para operar.",
                REPORT_EVERY_SECONDS)
        return None
    _tried[mint] = time.monotonic()
    tag = {"sniper": "🎯 SNIPER ", "pumpfun": "🟣 PUMP.FUN "}.get(mode, "")
    send(chat, "🟡 " + tag + "COMPRANDO (dinheiro real) US$ " + format(order_usd, ".2f") + " de " + symbol +
         " (" + pct + " da conta de US$ " + format(value, ".2f") + ")\n" + mint)
    try:
        result = bridge.submit_swap(mint, "buy", int(round(order_usd * 1_000_000)), wallet)
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
             "📦 Posições abertas: " + str(n) + "/3\n"
             "📄 " + mint + "\n"
             "🔗 https://solscan.io/tx/" + settled["signature"])
    else:
        send(chat, "⏳ Compra enviada, conferindo saldo (" + str(settled.get("reason", settled.get("status"))) +
             "). Libero sozinho em ~2 min.\nTx: https://solscan.io/tx/" + str(result["signature"]))
    return settled


LAST_SCAN = {"at": 0.0, "total": 0, "qualified": 0, "blocked": "", "reasons": {}, "unsafe": [], "near": []}


def buy_cycle(rows, send, chat):
    """Buy up to BUYS_PER_SCAN new tokens per scan (10% of account value each).

    The total number of open positions is still capped at 3 (live_trade_state).
    """
    if not chat:
        return None
    LAST_SCAN.update(at=time.time(), total=len(rows or []), qualified=0, blocked="", unsafe=[],
                     reasons=_skip_reasons(rows or []), near=_near_misses(rows or []))
    if not ready():
        problem = _why_not_ready()
        LAST_SCAN["blocked"] = problem or "trading desligado"
        if problem:
            _notice(send, chat, "not_ready", "⚠️ Robô ligado mas bloqueado: " + problem, REPORT_EVERY_SECONDS)
        return None
    allowed, reason = state.can_open()
    if not allowed:
        labels = {"Position limit": "3 posições abertas; espera vender para comprar de novo",
                  "Daily loss limit": "limite de perda do dia (US$ " + format(state.daily_loss_limit(), ".2f") + ") atingido",
                  "Unreconciled transaction": "transação pendente de conferência"}
        LAST_SCAN["blocked"] = labels.get(reason, reason)
        _notice(send, chat, "cannot_open", "⏸️ Sem novas compras: " + labels.get(reason, reason), REPORT_EVERY_SECONDS)
        return None
    wallet = get_address()
    open_mints = set(state._load()["positions"])
    now = time.monotonic()
    qualified = 0
    unsafe = []
    bought = []
    # Most traded in the last hour first.
    ranked = sorted((p for p in rows if isinstance(p, dict)), key=lambda p: -_f(p, "volume", "h1"))
    for p in ranked:
        if not live_candidate(p):
            continue
        qualified += 1
        mint = p["baseToken"]["address"]
        if mint in open_mints or now - _tried.get(mint, -HOT_RETRY_SECONDS) < HOT_RETRY_SECONDS:
            continue
        symbol = str(p["baseToken"].get("symbol", "?"))[:20]
        safe, why = token_safety(mint)
        if safe:
            safe, why = roundtrip_ok(mint)
        if not safe:
            unsafe.append(symbol + ": " + why)
            continue
        result = _execute_buy(p, mint, symbol, send, chat, wallet, ORDER_FRACTION, "hot")
        if not result or result.get("status") != "settled_buy":
            return result  # failed, uncertain or no balance: stop buying this scan
        bought.append(result)
        open_mints.add(mint)
        if len(bought) >= BUYS_PER_SCAN or not state.can_open()[0]:
            break
    LAST_SCAN.update(qualified=qualified, unsafe=unsafe[:5])
    if bought:
        return bought[-1]
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
           "time_stop": "⏱️ Tempo esgotado (não andou a tempo)"}.get(signal["reason"], "🛑 Stop loss")
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
    for mint, position, signal in sell_signals():
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


# ---------------------------------------------------------------- sniper mode
# Brand-new tokens (< 60 min) with strong buying right now. Very high risk:
# most new tokens go to zero; smaller size, wider stop, time stop.
SNIPER_FRACTION = float(os.getenv("SNIPER_FRACTION", "0.05"))
SNIPER_MAX_POSITIONS = 2
SNIPER_MAX_AGE_MINUTES = 60
SNIPER_MIN_LIQUIDITY = 15000
SNIPER_MIN_AGE_MINUTES = 5  # skip the first minutes, when most rugs happen
SNIPER_FEEDS = ("https://api.dexscreener.com/token-profiles/latest/v1",
                "https://api.dexscreener.com/token-boosts/latest/v1")
_sniper_seen = {}  # mint -> monotonic time first evaluated
_http = requests.Session()


def _dex(url):
    r = _http.get(url, timeout=12, headers={"Accept": "application/json", "User-Agent": "SolanaMemeBotSniper/1.0"})
    r.raise_for_status()
    return r.json()


_fetch_cache = [0.0, []]


def sniper_fetch():
    """Newest Solana tokens with pair data (cached 15 s; shared by real and simulated sniper)."""
    if time.monotonic() - _fetch_cache[0] < 15:
        return _fetch_cache[1]
    rows = _sniper_fetch()
    _fetch_cache[:] = [time.monotonic(), rows]
    return rows


def _sniper_fetch():
    mints = []
    for url in SNIPER_FEEDS:
        try:
            data = _dex(url)
        except Exception:
            continue
        for x in data if isinstance(data, list) else []:
            if isinstance(x, dict) and x.get("chainId") == "solana" and x.get("tokenAddress"):
                mints.append(x["tokenAddress"])
    mints = list(dict.fromkeys(mints))[:60]
    pairs = []
    for i in range(0, len(mints), 30):
        try:
            batch = _dex("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i + 30]))
            pairs += [p for p in batch if isinstance(p, dict)] if isinstance(batch, list) else []
        except Exception:
            continue
    best = {}
    for p in pairs:
        mint = (p.get("baseToken") or {}).get("address")
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if mint and (mint not in best or liq > float((best[mint].get("liquidity") or {}).get("usd") or 0)):
            best[mint] = p
    return list(best.values())


def sniper_candidate(p):
    """New token (5-60 min) with real, broad buying that is not already parabolic."""
    try:
        age_min = (time.time() * 1000 - float(p.get("pairCreatedAt") or 0)) / 60000
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        fdv = float(p.get("fdv") or 0)
        ch = p.get("priceChange") or {}
        tx = p.get("txns") or {}
        m5, h1 = tx.get("m5") or {}, tx.get("h1") or {}
        buys, sells = int(m5.get("buys") or 0), int(m5.get("sells") or 0)
        vol5 = float((p.get("volume") or {}).get("m5") or 0)
        m5_change, h1_change = float(ch.get("m5") or 0), float(ch.get("h1") or 0)
        return (SNIPER_MIN_AGE_MINUTES <= age_min <= SNIPER_MAX_AGE_MINUTES and liq >= SNIPER_MIN_LIQUIDITY and
                0 < fdv / liq <= 20 and float(p.get("priceUsd") or 0) > 0 and
                0 < m5_change <= 40 and h1_change <= 300 and
                buys >= 25 and buys > sells * 1.3 and int(h1.get("buys") or 0) >= 100 and
                5000 <= vol5 <= liq * 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return False


def holders_ok(mint):
    """Top 10 holders (excluding the biggest account = pool/curve) must own < 25%."""
    try:
        supply = int(rpc("getTokenSupply", [mint])["value"]["amount"])
        largest = rpc("getTokenLargestAccounts", [mint])["value"]
        amounts = sorted((int(a["amount"]) for a in largest), reverse=True)
        if supply <= 0:
            return False, "supply inválido"
        share = sum(amounts[1:11]) / supply
        if share >= 0.25:
            return False, "10 maiores donos têm " + format(share * 100, ".0f") + "%"
        return True, ""
    except Exception:
        return False, "não consegui ver os maiores donos"


def sniper_cycle(send, chat):
    """Fast cycle (~20 s): buy at most one brand-new token with strong momentum."""
    if not chat or not state.sniper_enabled() or not ready():
        return None
    allowed, _ = state.can_open()
    if not allowed or state.positions_by_mode("sniper") >= SNIPER_MAX_POSITIONS:
        return None
    wallet = get_address()
    open_mints = set(state._load()["positions"])
    now = time.monotonic()
    for p in sniper_fetch():
        mint = (p.get("baseToken") or {}).get("address")
        if not mint or mint in open_mints or mint in _sniper_seen and now - _sniper_seen[mint] < 600:
            continue
        if not sniper_candidate(p):
            continue
        _sniper_seen[mint] = now  # evaluate each new token at most every 10 min
        if now - _tried.get(mint, -RETRY_MINT_AFTER_SECONDS) < RETRY_MINT_AFTER_SECONDS:
            continue
        symbol = str((p.get("baseToken") or {}).get("symbol", "?"))[:20]
        safe, why = token_safety(mint)
        if safe:
            safe, why = holders_ok(mint)
        if safe:
            safe, why = roundtrip_ok(mint)
        if not safe:
            logging.info("Sniper skipped %s: %s", symbol, why)
            continue
        return _execute_buy(p, mint, symbol, send, chat, wallet, SNIPER_FRACTION, "sniper")
    if len(_sniper_seen) > 5000:
        _sniper_seen.clear()
    return None


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
