"""Real-money "buy the bottom" BTC robot (owner, Nov 15): the only strategy that operates.

Spot, no leverage (the backtest only made money at 1x; leverage lost to borrow fees and risks
liquidation): buys real BTC (Wormhole WBTC on Solana) with the bot wallet's SOL via Jupiter.
- 1st buy when BTC is 15% below its top (highest price since the last sale; at the start, the
  highest of the last 30 days);
- buys again at each further -15% below the previous buy, at most 3 buys;
- sells EVERYTHING back to SOL when the price is +7% above the average buy price;
- amount per buy chosen by the owner (US$ 10-1000, default 100). Starts OFF.
Checked every minute on the BTC price. Not a promise of profit: in a long fall it stays stuck.
"""
import json
import logging
import os
import tempfile
import threading
import time

import requests

DROP = 0.15
TP = 0.07
MAX_BUYS = 3
TOP_DAYS = 30
DEFAULT_USD = 100.0
MIN_USD, MAX_USD = 10.0, 1000.0
KEEP_SOL_LAMPORTS = 20_000_000          # 0.02 SOL kept for network fees
BTC_MINT = os.getenv("BTC_SPOT_MINT", "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh")   # Wormhole WBTC
BTC_DECIMALS = 8
SOL_MINT = "So11111111111111111111111111111111111111112"
CHECK_EVERY_S = 60
RETRY_AFTER_FAIL_S = 15 * 60

_LOCK = threading.RLock()
_http = requests.Session()
LAST = {"at": 0, "why": "", "price": None}


# ------------------------------------------------------------------ state
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("btc_dip_live.json")


def _load():
    d = {"enabled": False, "usd": DEFAULT_USD, "top": None, "buys": [], "history": [], "fail_at": 0}
    p = _path()
    if p.exists():
        try:
            d.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return d


def _save(d):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".btcdip.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def _update(**kw):
    with _LOCK:
        d = _load()
        d.update(kw)
        _save(d)
        return d


def set_enabled(on):
    with _LOCK:
        d = _load()
        d["enabled"] = bool(on)
        if on and not d["buys"]:
            d["top"] = None              # recomputed from the last 30 days on the next check
        _save(d)


def set_usd(usd):
    usd = round(float(str(usd).replace(",", ".").lstrip("$")), 2)
    if not MIN_USD <= usd <= MAX_USD:
        raise ValueError("valor permitido: US$ " + format(MIN_USD, ".0f") + " a US$ " + format(MAX_USD, ".0f"))
    _update(usd=usd)
    return usd


# ------------------------------------------------------------------ prices
def _get(url, params=None):
    r = _http.get(url, params=params, timeout=10, headers={"User-Agent": "BTCDip/1.0"})
    r.raise_for_status()
    return r.json()


def btc_price():
    for base in ("https://api.binance.com", "https://api.binance.us"):
        try:
            return float(_get(base + "/api/v3/ticker/price", {"symbol": "BTCUSDT"})["price"])
        except Exception:
            continue
    return float(_get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")["price"])


def top_30d():
    import btc_live
    cs = btc_live.klines_4h(limit=TOP_DAYS * 6)[-TOP_DAYS * 6:]
    return max(c["h"] for c in cs)


# ------------------------------------------------------------------ position math
def btc_qty(d):
    return sum(b["btc_raw"] for b in d["buys"]) / 10 ** BTC_DECIMALS


def invested(d):
    return sum(b["usd"] for b in d["buys"])


def avg_price(d):
    q = btc_qty(d)
    return invested(d) / q if q else None


def next_buy_price(d):
    if not d["buys"]:
        return d["top"] * (1 - DROP) if d.get("top") else None
    if len(d["buys"]) >= MAX_BUYS:
        return None
    return d["buys"][-1]["price"] * (1 - DROP)


# ------------------------------------------------------------------ real swaps (Jupiter)
def _token_raw(owner, mint):
    from live_swap_executor import rpc
    res = rpc("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
    return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in (res or {}).get("value", []))


def _swap(input_mint, output_mint, amount_raw):
    from bot_wallet_service import get_address
    from jupiter_v2_executor import execute_signed, order, sign_order
    data = order(input_mint, output_mint, int(amount_raw), taker=get_address())
    signed = sign_order(data, os.environ["BOT_WALLET_ENCRYPTION_PASSPHRASE"])
    return execute_signed(signed, data["requestId"])


def _buy_real(usd):
    """Swap SOL worth `usd` into BTC. Returns {btc_raw, usd, price, signature}."""
    import copy_trader as ct
    from bot_wallet_service import get_address
    from live_swap_executor import rpc
    sol_usd = float(ct._sol_usd())
    lamports = int(usd / sol_usd * 1e9)
    have = int((rpc("getBalance", [get_address(), {"commitment": "confirmed"}]) or {}).get("value", 0))
    if lamports > have - KEEP_SOL_LAMPORTS:
        raise RuntimeError("SOL insuficiente: precisa de ≈ " + format(lamports / 1e9 + 0.02, ".3f") + " SOL, a carteira tem " +
                           format(have / 1e9, ".3f") + " SOL. Deposite em 📥 Depositar.")
    out = _swap(SOL_MINT, BTC_MINT, lamports)
    spent = out["total_input_amount"] / 1e9 * sol_usd
    raw = int(out["total_output_amount"])
    return {"btc_raw": raw, "usd": round(spent, 2), "price": spent / (raw / 10 ** BTC_DECIMALS),
            "signature": out["signature"]}


def _sell_real(btc_raw):
    """Swap the robot's BTC back into SOL. Returns {usd, signature}."""
    import copy_trader as ct
    from bot_wallet_service import get_address
    raw = min(int(btc_raw), _token_raw(get_address(), BTC_MINT))
    if raw <= 0:
        raise RuntimeError("não há BTC na carteira do bot para vender")
    out = _swap(BTC_MINT, SOL_MINT, raw)
    return {"usd": round(out["total_output_amount"] / 1e9 * float(ct._sol_usd()), 2), "signature": out["signature"]}


def _tx(sig):
    return "\n🔗 https://solscan.io/tx/" + str(sig) if sig else ""


# ------------------------------------------------------------------ robot
def sell_all(send=None, chat=None, price=None, seller=None, why="tp", now=None):
    now = now or time.time()
    d = _load()
    if not d["buys"]:
        return "nothing"
    try:
        out = (seller or _sell_real)(sum(b["btc_raw"] for b in d["buys"]))
    except Exception as exc:
        if send and chat:
            send(chat, "⛔ BTC: não consegui vender (" + str(exc)[:150] + "). Tento de novo em 1 min.")
        return "sell_failed"
    pnl = out["usd"] - invested(d)
    with _LOCK:
        dd = _load()
        dd["history"] = (dd["history"] + [{"buys": dd["buys"], "avg": avg_price(dd), "sold_at": now, "exit": price,
                                           "usd_in": invested(dd), "usd_out": out["usd"], "pnl_usd": round(pnl, 2),
                                           "why": why}])[-200:]
        dd["buys"] = []
        dd["top"] = price or dd.get("top")
        _save(dd)
    if send and chat:
        send(chat, ("💰 LUCRO" if pnl >= 0 else "🔻 PREJUÍZO") + " — VENDEU TODO O BTC" +
             (" (+" + format(TP * 100, ".0f") + "% acima da média)" if why == "tp" else " (venda manual)") +
             "\n💵 Investido US$ " + format(invested(d), ".2f") + " → recebido US$ " + format(out["usd"], ".2f") +
             "\n📊 Resultado: " + format(pnl, "+.2f") + " USD (" + format(pnl / invested(d) * 100, "+.1f") + "%)" +
             "\n🔁 Agora espera cair " + format(DROP * 100, ".0f") + "% do topo de novo." + _tx(out.get("signature")))
    return "sold"


def cycle(send=None, chat=None, price=None, now=None, buyer=None, seller=None, top_fn=None):
    now = now or time.time()
    d = _load()
    if not d["enabled"] and not d["buys"]:
        return ""
    price = price if price is not None else btc_price()
    LAST.update(at=now, price=price)
    if d["buys"]:
        avg = avg_price(d)
        if price >= avg * (1 + TP):
            return sell_all(send, chat, price, seller, "tp", now)
    elif d.get("top") is None or price > d["top"]:
        d = _update(top=max(price, d.get("top") or 0, (top_fn or top_30d)() if d.get("top") is None else 0))
    target = next_buy_price(d)
    if not d["enabled"] or target is None:
        LAST["why"] = ("3 compras feitas: esperando +" + format(TP * 100, ".0f") + "% acima da média" if d["buys"]
                       else "desligado")
        return ""
    if price > target:
        LAST["why"] = ("esperando BTC chegar a US$ " + format(target, ",.0f") + " para a " + str(len(d["buys"]) + 1) +
                       "ª compra" + (" · venda em US$ " + format(avg_price(d) * (1 + TP), ",.0f") if d["buys"] else ""))
        return ""
    if now - float(d.get("fail_at") or 0) < RETRY_AFTER_FAIL_S:
        return ""
    n = len(d["buys"]) + 1
    try:
        b = (buyer or _buy_real)(float(d["usd"]))
    except Exception as exc:
        _update(fail_at=now)
        if send and chat:
            send(chat, "⛔ BTC: a " + str(n) + "ª compra não saiu: " + str(exc)[:200] + "\nTento de novo em 15 min.")
        return "buy_failed"
    b = dict(b, t=now)
    with _LOCK:
        dd = _load()
        dd["buys"] = dd["buys"] + [b]
        dd["fail_at"] = 0
        _save(dd)
    if send and chat:
        nxt = next_buy_price(dd)
        send(chat, "🟢 ₿ COMPROU BTC (" + str(n) + "/" + str(MAX_BUYS) + ") — US$ " + format(b["usd"], ".2f") + " a US$ " +
             format(b["price"], ",.0f") + "\n📉 Preço médio: US$ " + format(avg_price(dd), ",.0f") +
             " · investido US$ " + format(invested(dd), ".2f") + "\n🎯 Vende tudo em US$ " +
             format(avg_price(dd) * (1 + TP), ",.0f") + " (+" + format(TP * 100, ".0f") + "%)" +
             ("\n➕ Próxima compra se cair a US$ " + format(nxt, ",.0f") if nxt else "\n⛔ Foi a última compra (3/3)") +
             _tx(b.get("signature")))
    return "bought"


def status_text():
    d = _load()
    p = LAST.get("price")
    lines = ["₿ ROBÔ COMPRA NO FUNDO (dinheiro real, BTC à vista, sem alavancagem) — " +
             ("🟢 LIGADO" if d["enabled"] else "🔴 DESLIGADO"),
             "📉 Compra US$ " + format(float(d["usd"]), "g") + " quando o BTC cai " + format(DROP * 100, ".0f") +
             "% do topo, de novo a cada −" + format(DROP * 100, ".0f") + "% (máx. " + str(MAX_BUYS) + " compras = até US$ " +
             format(float(d["usd"]) * MAX_BUYS, "g") + "), vende tudo com +" + format(TP * 100, ".0f") + "% acima da média."]
    if d.get("top"):
        lines.append("🔝 Topo: US$ " + format(d["top"], ",.0f") + (" · 1ª compra em US$ " + format(d["top"] * (1 - DROP), ",.0f")
                                                                   if not d["buys"] else ""))
    if d["buys"]:
        avg = avg_price(d)
        now_usd = btc_qty(d) * p if p else None
        lines.append("📦 " + str(len(d["buys"])) + " compra(s): " + " + ".join("US$ " + format(b["price"], ",.0f") for b in d["buys"]) +
                     " · média US$ " + format(avg, ",.0f") + " · investido US$ " + format(invested(d), ".2f") +
                     (" · agora " + format(now_usd - invested(d), "+.2f") + " USD" if now_usd else ""))
        lines.append("🎯 Venda em US$ " + format(avg * (1 + TP), ",.0f") +
                     (" · próxima compra em US$ " + format(next_buy_price(d), ",.0f") if next_buy_price(d) else ""))
    h = d["history"]
    if h:
        lines.append("📊 " + str(len(h)) + " venda(s) · total " + format(sum(x["pnl_usd"] for x in h), "+.2f") + " USD")
    if LAST["why"] or p:
        lines.append("🔎 Agora: " + (LAST["why"] or "") + (" · BTC US$ " + format(p, ",.0f") if p else ""))
    lines.append("⚠️ Numa queda longa o BTC pode ficar meses abaixo da média: o robô espera, não vende no prejuízo. "
                 "O dinheiro fora do BTC fica em SOL. Não é promessa de lucro.")
    return "\n".join(lines)
