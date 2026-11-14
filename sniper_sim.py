"""Sniper simulation account (owner's choice Nov 14): the Sniper keeps choosing coins with its real rules
and the live DexScreener prices, but buys and sells with VIRTUAL money only (no order goes on-chain).

Same exits as the real Sniper (live_exit_monitor rocket rules: fixed stop, profit protection, trailing
stop, no-move time stop), the 2x / 5x ladder and the basket stop. Costs: SIM_COST per side (swap fees +
slippage). Every buy / sell goes to Telegram marked SIMULAÇÃO and to the log ("sniper sim closed ...")
so the hourly review can measure it.
"""
import logging
import time

import signals as sg

START_USD = sg._f("ROCKET_SIM_START_USD", 150)
SIM_COST = sg._f("ROCKET_SIM_COST", 0.015)      # 1.5% per side: small coins slip more than big ones
DEAD_S = 6 * 3600                               # no price for 6 h: written off


def _rb():
    import rocket_bot
    return rocket_bot


def _state(d):
    sim = d.setdefault("sim", {})
    sim.setdefault("cash", START_USD)
    sim.setdefault("positions", {})
    sim.setdefault("history", [])
    return sim


def paper():
    return bool(_rb()._load().get("paper", False))


def to_simulation_once():
    """Owner's request (Nov 14): Sniper to the simulation account. Runs once."""
    rb = _rb()
    with rb._LOCK:
        d = rb._load()
        if d.get("paper_v1"):
            return False
        d.update(paper_v1=True, paper=True, sim={"cash": START_USD, "positions": {}, "history": []})
        rb._save(d)
    return True


def set_paper(on):
    rb = _rb()
    with rb._LOCK:
        d = rb._load()
        d["paper"] = bool(on)
        _state(d)
        rb._save(d)


def held():
    return list(_state(_rb()._load())["positions"])


def buy(p, send=None, chat=None, now=None):
    """Virtual buy at the real price. '' when bought, else why not."""
    rb = _rb()
    now = now or time.time()
    mint = p["baseToken"]["address"]
    symbol = (p.get("baseToken") or {}).get("symbol") or mint[:6]
    price = float(p.get("priceUsd") or 0)
    if price <= 0:
        return "sem preço"
    usd = rb.ORDER_USD
    with rb._LOCK:
        d = rb._load()
        sim = _state(d)
        if mint in sim["positions"]:
            return "já tem a moeda"
        if len(sim["positions"]) >= rb.MAX_OPEN:
            return "já tem " + str(rb.MAX_OPEN) + " compras do Sniper abertas"
        if sim["cash"] < usd:
            return "saldo simulado US$ " + format(sim["cash"], ".2f")
        sim["cash"] = round(sim["cash"] - usd, 6)
        qty = usd * (1 - SIM_COST) / price
        sim["positions"][mint] = {"symbol": symbol, "cost": usd, "entry": price, "qty": qty, "qty0": qty,
                                  "peak": 1.0, "at": now, "seen": now, "last": price, "realized": 0.0}
        rb._save(d)
    ch = p.get("priceChange") or {}
    if send and chat:
        send(chat, "🧪 SNIPER (SIMULAÇÃO, sem dinheiro real) COMPROU " + symbol + " — US$ " + format(usd, ".0f") +
             " a `" + format(price, ".10g") + "` (" + format(float(ch.get("m5") or 0), "+.0f") + "% em 5 min)\n📄 `" + mint + "`")
    logging.info("sniper sim bought %s (%s) at %s", symbol, mint[:8], price)
    return ""


def _sell(sim, mint, fraction, price, reason, now):
    pos = sim["positions"][mint]
    qty = pos["qty"] * fraction
    proceeds = qty * price * (1 - SIM_COST)
    pos["qty"] -= qty
    pos["realized"] = pos.get("realized", 0.0) + proceeds
    sim["cash"] = round(sim["cash"] + proceeds, 6)
    if fraction < 0.999:
        return None
    sim["positions"].pop(mint)
    h = {"symbol": pos["symbol"], "mint": mint, "cost": pos["cost"], "proceeds": round(pos["realized"], 4),
         "pnl": round(pos["realized"] - pos["cost"], 4), "peak_pct": round((pos["peak"] - 1) * 100, 1),
         "reason": reason, "opened": pos["at"], "closed": now, "held_min": round((now - pos["at"]) / 60, 1)}
    sim["history"] = sim["history"][-300:] + [h]
    return h


REASON = {"stop_loss": "🛑 stop fixo", "take_profit": "🚀 stop móvel", "trailing": "🚀 stop móvel",
          "rocket_protect": "🛡️ proteção de lucro", "time_stop": "⏱️ não andou a tempo",
          "rocket_basket_stop": "🛑 as ordens juntas caíram", "dead": "☠️ moeda sem preço (perda total)"}


def exits(prices, send=None, chat=None, now=None):
    """Apply the Sniper exits to the virtual positions with the real prices {mint: price}."""
    import live_exit_monitor as lem
    rb = _rb()
    now = now or time.time()
    closed, notes = [], []
    with rb._LOCK:
        d = rb._load()
        sim = _state(d)
        live = {}
        for mint, pos in list(sim["positions"].items()):
            price = prices.get(mint)
            if price and price > 0:
                pos.update(last=price, seen=now)
            elif now - float(pos.get("seen") or now) > DEAD_S:
                closed.append(_sell(sim, mint, 1.0, 0.0, "dead", now))
                continue
            else:
                continue
            open_cost = pos["cost"] * pos["qty"] / pos["qty0"]
            value = pos["qty"] * price * (1 - SIM_COST)
            ratio = value / open_cost if open_cost else 0
            pos["peak"] = max(float(pos["peak"]), ratio)
            for mult, frac, key, label in rb.LADDER:
                if ratio >= mult and not pos.get(key):
                    pos[key] = True
                    _sell(sim, mint, frac, price, key, now)
                    notes.append("💰 🧪 SNIPER (SIMULAÇÃO): " + pos["symbol"] + " " + label)
                    open_cost = pos["cost"] * pos["qty"] / pos["qty0"]
                    value = pos["qty"] * price * (1 - SIM_COST)
            sig = lem.position_signal({"mode": "rocket", "peak_ratio": pos["peak"], "opened_at": pos["at"]},
                                      open_cost, value)
            if sig["action"] == "sell":
                closed.append(_sell(sim, mint, 1.0, price, sig["reason"], now))
            else:
                live[mint] = (open_cost, value)
        if (rb.BASKET_DROP > 0 and len(live) >= rb.MAX_OPEN and len(live) == len(sim["positions"]) and
                sum(v for _, v in live.values()) <= (1 - rb.BASKET_DROP) * sum(c for c, _ in live.values())):
            for mint in list(live):
                closed.append(_sell(sim, mint, 1.0, sim["positions"][mint]["last"], "rocket_basket_stop", now))
        rb._save(d)
    for n in notes:
        if send and chat:
            send(chat, n)
    for h in closed:
        logging.info("sniper sim closed %s (%s) pnl=%+.2f peak=%+.1f%% held=%.0fmin reason=%s",
                     h["symbol"], h["mint"][:8], h["pnl"], h["peak_pct"], h["held_min"], h["reason"])
        if send and chat:
            send(chat, ("💰 LUCRO" if h["pnl"] >= 0 else "🔻 PREJUÍZO") + " — 🧪 SNIPER (SIMULAÇÃO) VENDEU " + h["symbol"] +
                 "\n" + REASON.get(h["reason"], h["reason"]) + " · topo +" + format(h["peak_pct"], ".1f") + "% · " +
                 format(h["held_min"], ".0f") + " min\n💵 US$ " + format(h["cost"], ".2f") + " → US$ " +
                 format(h["proceeds"], ".2f") + " (" + format(h["pnl"], "+.2f") + ")\n🏦 Saldo simulado: US$ " +
                 format(value_usd(), ".2f"))
    return closed


def prices_for(pairs, fetch=None):
    """{mint: price} from the scan, plus a direct DexScreener read of held coins missing from it."""
    out = {}
    for p in pairs or []:
        m = (p.get("baseToken") or {}).get("address")
        if m and m not in out and float(p.get("priceUsd") or 0) > 0:
            out[m] = float(p["priceUsd"])
    missing = [m for m in held() if m not in out]
    if missing:
        try:
            rows = (fetch or sg._get)(sg.DEX + "/tokens/v1/solana/" + ",".join(missing[:30]))
        except Exception:
            rows = []
        best = {}
        for p in rows if isinstance(rows, list) else []:
            m = (p.get("baseToken") or {}).get("address")
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if m in missing and float(p.get("priceUsd") or 0) > 0 and liq >= best.get(m, (-1, 0))[0]:
                best[m] = (liq, float(p["priceUsd"]))
        out.update({m: v[1] for m, v in best.items()})
    return out


def value_usd():
    sim = _state(_rb()._load())
    return sim["cash"] + sum(p["qty"] * p["last"] * (1 - SIM_COST) for p in sim["positions"].values())


def status_lines():
    sim = _state(_rb()._load())
    h = sim["history"]
    lines = ["🧪 CONTA DE SIMULAÇÃO (sem dinheiro real, preços reais): saldo US$ " + format(value_usd(), ".2f") +
             " (começou com US$ " + format(START_USD, ".0f") + ") · " + str(len(h)) + " fechadas · " +
             str(sum(1 for x in h if x["pnl"] > 0)) + " no lucro · total " + format(sum(x["pnl"] for x in h), "+.2f") + " USD"]
    for m, p in sim["positions"].items():
        r = p["qty"] * p["last"] * (1 - SIM_COST) / (p["cost"] * p["qty"] / p["qty0"]) - 1
        lines.append("📦 " + p["symbol"] + " " + format(r * 100, "+.1f") + "% (topo " + format((p["peak"] - 1) * 100, "+.1f") + "%)")
    for x in h[-3:]:
        lines.append("• " + x["symbol"] + " " + format(x["pnl"], "+.2f") + " USD (" + REASON.get(x["reason"], x["reason"]) + ")")
    return lines
