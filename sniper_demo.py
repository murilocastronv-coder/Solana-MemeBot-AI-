"""SNIPER DEMO (owner, Nov 18): US$ 200 of fake money on REAL DexScreener prices, to learn from its orders.

Coins: Pump.fun mints (address ending in 'pump') listed on DexScreener, liquidity US$ 10 mil - 200 mil, more than
2 h old (owner's filters), not blocked by the anti-scam check.
Entry (the sniper's own rule, snipe_bot): 5-min volume >= MIN_VOL_5M, buyers / sellers in 5 min >= MIN_RATIO,
5-min move between MIN_UP and MAX_UP % (moving, not a spike).
Exit (the sniper's own rule): take profit +TP, stop -STOP, out after MAX_HOLD_MIN minutes.
Costs: COST per side (swap + slippage on small coins). Every trade keeps the numbers it was bought with, so the
rule can be improved from the demo's orders ("o que deu certo / errado")."""
import logging
import os
import time

START = float(os.getenv("SNIPER_DEMO_USD", "200"))
SIZE = float(os.getenv("SNIPER_DEMO_SIZE", "20"))          # US$ 20 per coin
MAX_OPEN = int(os.getenv("SNIPER_DEMO_MAX_OPEN", "3"))
MIN_LIQ, MAX_LIQ = 10_000.0, 200_000.0                    # owner
MIN_AGE_H = 2.0                                           # owner: more than 2 h of life
MIN_VOL_5M = float(os.getenv("SNIPER_DEMO_MIN_VOL_5M", "3000"))
MIN_RATIO = float(os.getenv("SNIPER_DEMO_MIN_RATIO", "1.5"))
MIN_UP, MAX_UP = 5.0, 40.0
TP = float(os.getenv("SNIPER_DEMO_TP", "0.20"))
STOP = float(os.getenv("SNIPER_DEMO_STOP", "0.08"))
MAX_HOLD_MIN = float(os.getenv("SNIPER_DEMO_MAX_HOLD_MIN", "30"))
COST = float(os.getenv("SNIPER_DEMO_COST", "0.02"))       # per side
DAYS = 30
SAFETY_TRIES = 5                                          # anti-scam checks per cycle at most


def _num(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def features(p, now=None):
    now = now or time.time()
    m5 = ((p.get("txns") or {}).get("m5") or {})
    created = p.get("pairCreatedAt")
    return {"mint": (p.get("baseToken") or {}).get("address") or "", "sym": str((p.get("baseToken") or {}).get("symbol") or "?")[:16],
            "price": _num(p.get("priceUsd")), "liq": _num((p.get("liquidity") or {}).get("usd")),
            "mcap": _num(p.get("marketCap") or p.get("fdv")), "vol5": _num((p.get("volume") or {}).get("m5")),
            "buys": _num(m5.get("buys")), "sells": _num(m5.get("sells")), "up5": _num((p.get("priceChange") or {}).get("m5")),
            "up1h": _num((p.get("priceChange") or {}).get("h1")),
            "age_h": (now - _num(created) / 1000) / 3600 if created else None}


def entry_reason(f):
    """'' when the coin passes, else why not."""
    if not f["mint"].endswith("pump"):
        return "não é endereço pump.fun"
    if not MIN_LIQ <= f["liq"] <= MAX_LIQ:
        return "liquidez fora de 10–200 mil"
    if f["age_h"] is None or f["age_h"] < MIN_AGE_H:
        return "menos de 2 h"
    if f["price"] <= 0:
        return "sem preço"
    if f["vol5"] < MIN_VOL_5M:
        return "pouco volume em 5 min"
    if f["buys"] / max(f["sells"], 1) < MIN_RATIO:
        return "poucos compradores"
    if not MIN_UP <= f["up5"] <= MAX_UP:
        return "movimento de 5 min fora de 5–40%"
    return ""


def exit_reason(ratio, held_s):
    if TP and ratio >= 1 + TP:
        return "alvo +" + format(TP * 100, "g") + "%"
    if STOP and ratio <= 1 - STOP:
        return "stop −" + format(STOP * 100, "g") + "%"
    if MAX_HOLD_MIN and held_s >= MAX_HOLD_MIN * 60:
        return "tempo de " + format(MAX_HOLD_MIN, "g") + " min"
    return None


# ---------------------------------------------------------------- per account
def state(chat):
    import trade_panel as tp
    try:
        d = dict(tp.settings(chat).get("sniper_demo") or {})
    except Exception:
        d = {}
    d.setdefault("on", False)
    d.setdefault("cash", START)
    d.setdefault("start_usd", START)
    d.setdefault("pos", {})
    d.setdefault("hist", [])
    d.setdefault("seen", [])
    d.setdefault("started", 0)
    return d


def save(chat, d):
    import trade_panel as tp
    d["hist"] = list(d["hist"])[-500:]
    d["seen"] = list(d["seen"])[-3000:]
    tp.set_setting(chat, "sniper_demo", d)


def reset(chat, now=None):
    d = state(chat)
    d.update(cash=START, start_usd=START, pos={}, hist=[], seen=[], started=now or time.time(),
             curve=[[int(now or time.time()), START]])
    save(chat, d)
    return d


def value(d, prices):
    return d["cash"] + sum(p["qty"] * (prices.get(m) or p["entry"]) for m, p in d["pos"].items())


def _link(mint):
    return "https://dexscreener.com/solana/" + mint


def exits(chat, prices, send=None, now=None):
    now = now or time.time()
    d = state(chat)
    for m, p in list(d["pos"].items()):
        px = prices.get(m, 0.0)
        if px <= 0:
            continue
        ratio = px / p["entry"]
        p["peak"] = max(float(p.get("peak") or 1.0), ratio)
        why = exit_reason(ratio, now - p["t"])
        if not why:
            continue
        back = p["qty"] * px * (1 - COST)
        pnl = back - p["cost"]
        d["cash"] += back
        d["hist"].append({"sym": p["sym"], "mint": m, "pnl": round(pnl, 4), "pct": round((px / p["entry"] - 1) * 100, 2), "why": why,
                          "t": now, "held_min": round((now - p["t"]) / 60, 1), "peak_pct": round((p["peak"] - 1) * 100, 1), "f": p.get("f")})
        del d["pos"][m]
        logging.info("sniper demo trade: %s %s %+.1f%% held %.0f min peak %+.1f%% entry %s", p["sym"], why, (px / p["entry"] - 1) * 100,
                     (now - p["t"]) / 60, (p["peak"] - 1) * 100, p.get("f"))
        if send:
            h = d["hist"]
            send(chat, ("💰 LUCRO" if pnl >= 0 else "🔻 PREJUÍZO") + " — 🎯 SNIPER DEMO VENDEU " + p["sym"] + "\n"
                 "🪙 Moeda: " + p["sym"] + " · " + m + "\n"
                 "📉 Motivo: " + why + " · máximo que chegou: +" + format((p["peak"] - 1) * 100, ".1f") + "%\n"
                 "💲 Comprou a US$ " + format(p["entry"], ".6g") + " → vendeu a US$ " + format(px, ".6g") + " (" +
                 format((px / p["entry"] - 1) * 100, "+.1f") + "%) · ficou " + format((now - p["t"]) / 60, ".0f") + " min\n"
                 "💵 US$ " + format(p["cost"], ".2f") + " → US$ " + format(back, ".2f") + " · P/L " + format(pnl, "+.2f") + " USD (já com custos)\n"
                 "📊 Sniper demo: US$ " + format(value(d, prices), ".2f") + " de US$ " + format(d["start_usd"], "g") + " · " +
                 str(sum(1 for x in h if x["pnl"] > 0)) + "/" + str(len(h)) + " no lucro\n"
                 "📈 Gráfico: " + _link(m))
    save(chat, d)


def buys(chat, pairs, send=None, now=None, safety=None):
    """Buys the coins that pass (fake money), at most MAX_OPEN open, one entry per coin ever."""
    now = now or time.time()
    d = state(chat)
    done, checks = [], 0
    for p in pairs:
        if len(d["pos"]) >= MAX_OPEN or d["cash"] < SIZE:
            break
        f = features(p, now)
        if not f["mint"] or f["mint"] in d["pos"] or f["mint"] in d["seen"] or entry_reason(f):
            continue
        if safety is not False:
            if checks >= SAFETY_TRIES:
                break
            checks += 1
            try:
                why = (safety or __import__("signals").safety_reason)(f["mint"])
            except Exception:
                why = "anti-golpe indisponível"
            if why:
                d["seen"].append(f["mint"])
                continue
        d["seen"].append(f["mint"])
        d["pos"][f["mint"]] = {"sym": f["sym"], "entry": f["price"], "qty": SIZE * (1 - COST) / f["price"], "cost": SIZE,
                               "peak": 1.0, "t": now, "f": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in f.items()
                                                            if k not in ("mint", "sym")}}
        d["cash"] -= SIZE
        done.append(f["sym"])
        if send:
            send(chat, "🎯 SNIPER DEMO COMPROU " + f["sym"] + "\n"
                 "🪙 Moeda: " + f["sym"] + " · " + f["mint"] + "\n"
                 "💲 Preço real agora: US$ " + format(f["price"], ".6g") + " · liquidez US$ " + format(f["liq"], ",.0f") +
                 " · market cap US$ " + format(f["mcap"], ",.0f") + " · " + format(f["age_h"], ".1f") + " h de vida\n"
                 "⚡ Por que comprou: volume 5 min US$ " + format(f["vol5"], ",.0f") + " · " + format(f["buys"], ".0f") + " compras x " +
                 format(f["sells"], ".0f") + " vendas · subiu " + format(f["up5"], "+.1f") + "% em 5 min\n"
                 "💵 Valor: US$ " + format(SIZE, ".2f") + " de mentira · sobra US$ " + format(d["cash"], ".2f") + "\n"
                 "🎯 Alvo +" + format(TP * 100, "g") + "% · 🛑 stop −" + format(STOP * 100, "g") + "% · ⏱️ sai em " +
                 format(MAX_HOLD_MIN, "g") + " min\n"
                 "📈 Gráfico: " + _link(f["mint"]))
    save(chat, d)
    return done


def text(chat, prices=None, now=None):
    now = now or time.time()
    d = state(chat)
    prices = prices or {}
    val = value(d, prices)
    h = d["hist"]
    wins = [x for x in h if x["pnl"] > 0]
    days = (now - float(d["started"] or now)) / 86400
    L = ["🎯 SNIPER DEMO — " + ("🟢 LIGADO" if d["on"] else "🔴 DESLIGADO") + " (dinheiro de mentira, preços reais)",
         "Pump.fun no DexScreener · liquidez US$ 10–200 mil · mais de 2 h de vida · US$ " + format(SIZE, "g") + " por moeda, máx. " +
         str(MAX_OPEN) + " · alvo +" + format(TP * 100, "g") + "% · stop −" + format(STOP * 100, "g") + "% · sai em " + format(MAX_HOLD_MIN, "g") + " min",
         "Começou com US$ " + format(d["start_usd"], "g") + " · agora US$ " + format(val, ".2f") + " (" + format(val - d["start_usd"], "+.2f") + ", " +
         format((val / d["start_usd"] - 1) * 100, "+.1f") + "%) · 📅 dia " + format(min(days, 999), ".0f") + " de " + str(DAYS),
         "Vendas: " + str(len(h)) + " · no lucro " + str(len(wins)) + (" (" + format(len(wins) / len(h) * 100, ".0f") + "%)" if h else "") +
         " · alvo " + str(sum(1 for x in h if x["why"].startswith("alvo"))) + " · stop " + str(sum(1 for x in h if x["why"].startswith("stop"))) +
         " · tempo " + str(sum(1 for x in h if x["why"].startswith("tempo")))]
    for x in h[-5:]:
        L.append("• " + x["sym"] + ": " + format(x["pct"], "+.1f") + "% (" + x["why"] + ")")
    for m, p in d["pos"].items():
        px = prices.get(m)
        L.append("• aberta " + p["sym"] + ": " + (format((px / p["entry"] - 1) * 100, "+.1f") + "%" if px else "?") + " · há " +
                 format((now - p["t"]) / 60, ".0f") + " min")
    return "\n".join(L)


def keyboard_row(chat):
    d = state(chat)
    return [{"text": "🎯 Desligar SNIPER demo" if d["on"] else "🎯 Ligar SNIPER demo", "callback_data": "tsn_off" if d["on"] else "tsn_on"},
            {"text": "🔁 Zerar sniper", "callback_data": "tsn_reset"}]


def handle(action, send, chat):
    d = state(chat)
    if action == "tsn_reset" or (action == "tsn_on" and not d["started"]):
        d = reset(chat)
    if action in ("tsn_on", "tsn_off"):
        d["on"] = action == "tsn_on"
        save(chat, d)
    send(chat, {"tsn_on": "🎯 SNIPER DEMO ligado: US$ " + format(START, "g") + " de mentira nos preços reais. Cada compra e venda chega aqui.",
                "tsn_off": "🎯 Sniper demo desligado (o resultado fica guardado).",
                "tsn_reset": "🔁 Sniper demo zerado: US$ " + format(START, "g") + " de novo, dia 0 de " + str(DAYS) + "."}[action])


def cycle(send, chats, pairs_fn=None, prices_fn=None, safety=None):
    """Every minute: exits of the open demo coins, then new buys from the DexScreener lists."""
    on = [c for c in chats if state(c)["on"]]
    if not on:
        return
    import signals as sg
    pairs = (pairs_fn or sg.candidate_pairs)()
    prices = {}
    for p in pairs:
        m = (p.get("baseToken") or {}).get("address")
        if m:
            prices[m] = _num(p.get("priceUsd"))
    held = sorted({m for c in on for m in state(c)["pos"] if not prices.get(m)})
    if held:
        try:
            import big_bot
            prices.update((prices_fn or big_bot.dex_prices)(held))
        except Exception as exc:
            logging.info("sniper demo prices: %s", type(exc).__name__)
    pairs = sorted(pairs, key=lambda p: -_num((p.get("volume") or {}).get("m5")))
    import demo_report
    for c in on:
        try:
            exits(c, prices, send)
            buys(c, pairs, send, safety=safety)
            d = state(c)
            if demo_report.snapshot(d, value(d, prices)):          # balance over time (one point per hour)
                save(c, d)
        except Exception as exc:
            logging.info("sniper demo %s: %s", c, type(exc).__name__)
