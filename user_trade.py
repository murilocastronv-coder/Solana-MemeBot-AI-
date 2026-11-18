"""Manual trading for every Telegram account with its OWN wallet (owner, Nov 18): buy / sell any Solana coin,
take profit, stop loss, trailing stop and limit buy orders. Accounts that are not the owner pay FEE_RATE (2%) of
each buy and each sell, inside the same transaction, to the fee wallet (FEE_WALLET).

The owner trades with the main bot wallet through live_engine (trade_panel); this module is for everybody else.
Storage: one JSON file per account next to its encrypted wallet (user_wallets base dir)."""
import json
import logging
import os
import tempfile
import threading
import time
import uuid

import user_copy as uc
import user_wallets

SOL_MINT = uc.SOL_MINT
FEE_RATE = float(os.getenv("TRADE_FEE_PERCENT", "2")) / 100
RESERVE_LAMPORTS = int(float(os.getenv("TRADE_SOL_RESERVE", "0.01")) * 1e9)   # network fees + token account
BUY_PRESETS = ((0.05, 0.1, 0.25, 0.5), (0.1, 0.25, 0.5, 1.0), (0.25, 0.5, 1.0, 2.0), (0.5, 1.0, 2.0, 5.0))
SLIPPAGE_CHOICES = (50, 100, 200, 300)               # Jupiter quote accepts at most 3%
SPEEDS = {"eco": ("medium", 500_000), "fast": ("high", 1_500_000), "turbo": ("veryHigh", 5_000_000)}   # priority fee cap
LIMIT_TTL_S = 7 * 86400
MAX_LIMITS = 10
_LOCK = threading.RLock()
_USER_LOCKS = {}


def _lock(chat):
    with _LOCK:
        return _USER_LOCKS.setdefault(str(chat), threading.RLock())


def _path(chat):
    return user_wallets._base_dir() / "user_trade" / (user_wallets._chat(chat) + ".json")


def default():
    return {"terms_at": None, "buy": list(BUY_PRESETS[0]), "slippage_bps": 300, "auto_buy": 0.0, "speed": "fast",
            "confirm": False, "sell_protect": False, "watch": [], "dca": {},
            "tp": 0.0, "sl": 0.0, "trail": 0.0, "positions": {}, "limits": {}, "history": [],
            "fees_paid_lamports": 0, "fees_owed_lamports": 0, "volume_lamports": 0}


def load(chat):
    d = default()
    path = _path(chat)
    if path.exists():
        try:
            d.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return d


def save(chat, d):
    path = _path(chat)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def users():
    folder = user_wallets._base_dir() / "user_trade"
    return [f.stem for f in folder.glob("*.json")] if folder.is_dir() else []


def update(chat, fn):
    with _lock(chat):
        d = load(chat)
        out = fn(d)
        save(chat, d)
        return out


def accept_terms(chat):
    update(chat, lambda d: d.__setitem__("terms_at", int(time.time())))


# ---------------------------------------------------------------- chain
def sol_usd():
    import copy_trader as ct
    return float(ct._sol_usd())


def wallet(chat):
    """(address, SOL, US$) of the account's wallet."""
    address = user_wallets.get_address(chat) or user_wallets.create_or_get(chat)[0]
    sol = uc.sol_balance(address) / 1e9
    return address, sol, sol * sol_usd()


def _record_fee(chat, lamports, volume):
    def f(d):
        d["fees_paid_lamports"] = int(d.get("fees_paid_lamports", 0)) + int(lamports)
        d["volume_lamports"] = int(d.get("volume_lamports", 0)) + int(volume)
    update(chat, f)


def _swap(chat, input_mint, output_mint, amount, fee_lamports=None):
    """Swap with the fee inside the same transaction; when the route cannot take it, swap then send the fee.
    Returns the fee lamports actually charged. Raises uc.NotSent (nothing happened) or uc.Uncertain."""
    try:
        d = load(chat)
        return uc.atomic_swap(chat, input_mint, output_mint, amount, fee_lamports=fee_lamports, fee_rate=FEE_RATE,
                              slippage_bps=int(d.get("slippage_bps") or 300),
                              priority=SPEEDS.get(d.get("speed"), SPEEDS["fast"]))["fee_lamports"]
    except uc.NotSent as exc:
        if not exc.fallback:
            raise
    try:
        od = uc.quote_order(chat, input_mint, output_mint, amount)
    except Exception as exc:
        raise uc.NotSent(type(exc).__name__)
    try:
        res = uc.execute(chat, od)
    except Exception:
        raise uc.Uncertain("sent, not confirmed")
    fee = fee_lamports if fee_lamports is not None else int(int(res.get("total_output_amount") or 0) * FEE_RATE)
    uc._charge_fee(chat, fee)           # on failure kept as owed and retried by user_copy.upkeep
    return fee


# ---------------------------------------------------------------- buy / sell
def buy(chat, mint, sol, send=None, symbol=None):
    """Buy `sol` SOL of the coin (the fee comes out of that amount). Returns a message."""
    if not load(chat).get("terms_at"):
        return "⛔ Aceite a taxa antes de operar (toque em 🟢 Comprar de novo)."
    address, _ = uc._keypair(chat)
    lamports = int(float(sol) * 1e9)
    if lamports <= 0:
        return "⛔ Valor inválido."
    balance = uc.sol_balance(address)
    if balance < lamports + RESERVE_LAMPORTS:
        return ("⛔ Saldo insuficiente: a carteira tem " + format(balance / 1e9, ".4f") + " SOL; a compra precisa de " +
                format((lamports + RESERVE_LAMPORTS) / 1e9, ".4f") + " SOL (com 0,01 SOL para as taxas da rede). Deposite em 📥 Depositar.")
    fee = int(lamports * FEE_RATE)
    before = uc.token_balance(address, mint)
    price = sol_usd()
    try:
        fee = _swap(chat, SOL_MINT, mint, lamports - fee, fee_lamports=fee)
    except uc.NotSent as exc:
        return "⛔ Compra não feita (nada foi gasto): " + str(exc)[:120]
    except uc.Uncertain:
        update(chat, lambda d: d["positions"].setdefault(mint, {"symbol": symbol or mint[:6], "tokens": 0, "cost_usd": 0.0,
                                                                "opened_at": time.time()}).update(check_cost=float(sol) * price,
                                                                                                   check_before=before))
        return "⏳ Compra enviada, conferindo na blockchain. Aviso quando confirmar."
    _record_fee(chat, fee, lamports)
    got = max(0, uc.token_balance(address, mint) - before)

    def f(d):
        p = d["positions"].setdefault(mint, {"symbol": symbol or mint[:6], "tokens": 0, "cost_usd": 0.0, "opened_at": time.time(),
                                             "tp": d["tp"], "sl": d["sl"], "trail": d["trail"], "peak": 1.0})
        p["tokens"] = int(p.get("tokens", 0)) + got
        p["cost_usd"] = float(p.get("cost_usd", 0)) + float(sol) * price
        p["buys"] = int(p.get("buys", 0)) + 1
    update(chat, f)
    return ("🟢 COMPROU " + (symbol or mint[:6]) + "\n💵 " + format(float(sol), "g") + " SOL (US$ " + format(float(sol) * price, ".2f") +
            ") · taxa " + format(FEE_RATE * 100, "g") + "%: " + format(fee / 1e9, ".4f") + " SOL\n📄 " + mint)


def sell(chat, mint, pct, send=None, reason=None):
    """Sell `pct`% of what the wallet holds of the coin. Returns a message."""
    address, _ = uc._keypair(chat)
    held = uc.token_balance(address, mint)
    if held <= 0:
        update(chat, lambda d: d["positions"].pop(mint, None))
        return "⛔ Sua carteira não tem essa moeda."
    amount = held if int(pct) >= 100 else int(held * int(pct) / 100)
    if amount <= 0:
        return "⛔ Quantidade muito pequena."
    p = load(chat)["positions"].get(mint) or {}
    before = uc.sol_balance(address)
    price = sol_usd()
    try:
        fee = _swap(chat, mint, SOL_MINT, amount)
    except uc.NotSent as exc:
        return "⛔ Venda não feita: " + str(exc)[:120]
    except uc.Uncertain:
        return "⏳ Venda enviada, conferindo na blockchain. Confira o saldo em 💼 Posições daqui a 1 min."
    received = max(0, uc.sol_balance(address) - before)           # already without the fee
    _record_fee(chat, fee, received + fee)
    part = amount / held
    cost = float(p.get("cost_usd") or 0) * part
    net = received / 1e9 * price
    pnl = net - cost

    def f(d):
        q = d["positions"].get(mint)
        if q:
            if part >= 0.999:
                d["positions"].pop(mint, None)
            else:
                q["tokens"] = max(0, int(q.get("tokens", held)) - amount)
                q["cost_usd"] = float(q.get("cost_usd") or 0) - cost
        d["history"] = (d["history"] + [{"mint": mint, "symbol": p.get("symbol") or mint[:6], "pnl_usd": round(pnl, 4),
                                         "closed_at": time.time(), "part": round(part, 3)}])[-200:]
    update(chat, f)
    why = {"tp": "🎯 Alvo atingido", "sl": "🛑 Stop loss", "trail": "🚀 Stop móvel"}.get(reason, "")
    return (("💰 LUCRO" if pnl >= 0 else "🔻 PREJUÍZO") + " — VENDEU " + format(part * 100, ".0f") + "% de " + str(p.get("symbol") or mint[:6]) +
            ("\n" + why if why else "") + "\n💵 Recebeu " + format(received / 1e9, ".4f") + " SOL (US$ " + format(net, ".2f") + ")" +
            (" · custo US$ " + format(cost, ".2f") + " · P/L " + format(pnl, "+.2f") + " USD" if cost else "") +
            " · taxa " + format(FEE_RATE * 100, "g") + "%: " + format(fee / 1e9, ".4f") + " SOL")


def value_usd(mint, tokens):
    import live_exit_monitor as mon
    return mon.quote_value_usd(mint, int(tokens))


# ---------------------------------------------------------------- automatic exits and limit orders
def exit_reason(p, ratio, peak, arm=0.10):
    if p.get("sl") and ratio <= 1 - float(p["sl"]):
        return "sl"
    if p.get("tp") and ratio >= 1 + float(p["tp"]):
        return "tp"
    if p.get("trail") and peak >= 1 + arm and ratio <= peak * (1 - float(p["trail"])):
        return "trail"
    return None


def limit_hit(order, price):
    return price > 0 and price <= float(order["price"])


def add_limit(chat, mint, symbol, price_now, drop, sol):
    def f(d):
        if len(d["limits"]) >= MAX_LIMITS:
            raise ValueError("máximo de " + str(MAX_LIMITS) + " ordens limite")
        oid = uuid.uuid4().hex[:8]
        d["limits"][oid] = {"mint": mint, "symbol": symbol, "price": float(price_now) * (1 - float(drop)), "drop": float(drop),
                            "sol": float(sol), "created": time.time()}
        return oid
    return update(chat, f)


def cancel_limit(chat, oid):
    return update(chat, lambda d: d["limits"].pop(oid, None) is not None)


def _dex_prices(mints):
    import signals as sg
    out = {}
    mints = list(mints)
    for i in range(0, len(mints), 30):
        for r in sg._get(sg.DEX + "/tokens/v1/solana/" + ",".join(mints[i:i + 30])) or []:
            m = (r.get("baseToken") or {}).get("address")
            liq = float((r.get("liquidity") or {}).get("usd") or 0)
            if m and (m not in out or liq > out[m][1]):
                out[m] = (float(r.get("priceUsd") or 0), liq)
    return {m: v[0] for m, v in out.items()}


def upkeep(send, chats=None, value=None, prices=None, buy_fn=None, sell_fn=None):
    """Every minute: confirm uncertain buys, sell positions at their TP / SL / trailing stop, fill limit orders."""
    value = value or value_usd
    buy_fn = buy_fn or buy
    sell_fn = sell_fn or sell
    chats = users() if chats is None else chats
    want = set()
    for chat in chats:
        d = load(chat)
        for oid, o in list(d["limits"].items()):
            if time.time() - float(o.get("created") or 0) > LIMIT_TTL_S:
                cancel_limit(chat, oid)
                uc._notify(send, chat, "⌛ Ordem limite de " + str(o.get("symbol")) + " expirou (7 dias).")
            else:
                want.add(o["mint"])
        for mint, p in list(d["positions"].items()):
            try:
                if p.get("check_cost") is not None:
                    address = user_wallets.get_address(chat)
                    got = uc.token_balance(address, mint) - int(p.get("check_before") or 0)

                    def fix(dd, got=got, mint=mint):
                        q = dd["positions"].get(mint)
                        if q:
                            if got > 0:
                                q["tokens"] = int(q.get("tokens", 0)) + got
                                q["cost_usd"] = float(q.get("cost_usd", 0)) + float(q.pop("check_cost"))
                                q.pop("check_before", None)
                            elif time.time() - float(q.get("opened_at") or 0) > 300:
                                dd["positions"].pop(mint, None)
                    update(chat, fix)
                    if got > 0:
                        uc._notify(send, chat, "🟢 Compra confirmada: " + str(p.get("symbol") or mint[:6]))
                    continue
                if not (p.get("tp") or p.get("sl") or p.get("trail")) or not p.get("tokens") or not p.get("cost_usd"):
                    continue
                ratio = value(mint, p["tokens"]) / float(p["cost_usd"])
                peak = max(ratio, float(p.get("peak") or 1.0))
                if peak > float(p.get("peak") or 1.0):
                    update(chat, lambda dd, mint=mint, peak=peak: dd["positions"].get(mint, {}).update(peak=peak))
                why = exit_reason(p, ratio, peak)
                if why:
                    uc._notify(send, chat, sell_fn(chat, mint, 100, send, reason=why))
            except Exception as exc:
                logging.info("user trade upkeep %s: %s", mint[:6], type(exc).__name__)
    if not want:
        return
    try:
        px = prices(want) if prices else _dex_prices(want)
    except Exception:
        return
    for chat in chats:
        for oid, o in list(load(chat)["limits"].items()):
            if limit_hit(o, px.get(o["mint"], 0)) and cancel_limit(chat, oid):
                uc._notify(send, chat, "📋 Ordem limite atingida: " + str(o["symbol"]) + " chegou a US$ " +
                           format(px[o["mint"]], ".8g") + ". Comprando " + format(o["sol"], "g") + " SOL…")
                try:
                    uc._notify(send, chat, buy_fn(chat, o["mint"], o["sol"], send, symbol=o["symbol"]))
                except Exception as exc:
                    uc._notify(send, chat, "⛔ Ordem limite: " + type(exc).__name__)


# ---------------------------------------------------------------- withdraw
def withdraw(chat, to_address, lamports):
    _, kp = uc._keypair(chat)
    return uc.transfer_sol(kp, to_address, int(lamports), wait_s=60)


# ---------------------------------------------------------------- owner report
def owner_summary():
    rows = [load(c) for c in users()]
    paid = sum(int(d.get("fees_paid_lamports", 0)) for d in rows) / 1e9
    vol = sum(int(d.get("volume_lamports", 0)) for d in rows) / 1e9
    active = sum(1 for d in rows if d.get("positions") or d.get("history"))
    return ("👤 Contas: " + str(len(rows)) + " (" + str(active) + " já operaram) · volume " + format(vol, ",.3f") + " SOL · "
            "taxas recebidas " + format(paid, ".4f") + " SOL (" + format(FEE_RATE * 100, "g") + "% por compra e venda)")
