"""Plain-language trade history and per-strategy statistics for Telegram."""
REASONS = {"stop_loss": "stop", "take_profit": "stop móvel", "time_stop": "tempo", "dead": "zerada", "?": "?",
           "trailing": "stop móvel", "reversal": "virada", "liquidated": "LIQUIDADO"}
MODES = {"hot": "Volume", "normal": "Normal", "sniper": "Sniper", "pumpfun": "Pump.fun", "btc": "BTC"}


def stats(trades):
    closed = [t for t in trades if t.get("pnl_usd") is not None]
    wins = [t["pnl_usd"] for t in closed if t["pnl_usd"] > 0]
    losses = [t["pnl_usd"] for t in closed if t["pnl_usd"] <= 0]
    return {"n": len(closed), "wins": len(wins), "pnl": sum(t["pnl_usd"] for t in closed),
            "avg_win": sum(wins) / len(wins) if wins else 0.0,
            "avg_loss": sum(losses) / len(losses) if losses else 0.0}


def _block(title, trades):
    lines = [title]
    if not trades:
        return lines + ["Nenhuma operação fechada ainda."]
    for mode in ("hot", "normal", "sniper", "pumpfun"):
        s = stats([t for t in trades if t.get("mode", "normal") == mode])
        if not s["n"]:
            continue
        rate = s["wins"] / s["n"] * 100
        lines.append("• " + MODES[mode] + ": " + str(s["n"]) + " operações | acerto " + format(rate, ".0f") + "% | P/L " +
                     format(s["pnl"], "+.2f") + " USD\n  ganho médio " + format(s["avg_win"], "+.2f") +
                     " | perda média " + format(s["avg_loss"], "+.2f"))
    lines.append("Últimas:")
    for t in trades[-10:][::-1]:
        pnl = t.get("pnl_usd")
        pct = (pnl / t["cost_usd"] * 100) if pnl is not None and t.get("cost_usd") else None
        lines.append(("💰 " if (pnl or 0) > 0 else "🔻 ") + str(t.get("symbol", "?")) + " [" +
                     MODES.get(t.get("mode", "normal"), "?")[0] + "] " +
                     ("?" if pnl is None else format(pnl, "+.2f") + " USD (" + format(pct, "+.0f") + "%)") +
                     " · " + REASONS.get(t.get("reason", "?"), t.get("reason", "?")) +
                     ("" if t.get("minutes") is None else " · " + format(t["minutes"], ".0f") + " min"))
    return lines


def history_text(real_trades, sim):
    lines = _block("📜 HISTÓRICO — DINHEIRO REAL", real_trades)
    lines += [""]
    lines += _block("🧪 SIMULAÇÃO (preço real, dinheiro de mentira) — " + ("LIGADA" if sim["enabled"] else "DESLIGADA") +
                    "\nSaldo: US$ " + format(sim["equity"], ".2f") + " (começou com US$ " + format(sim["start"], ".2f") +
                    ", " + format((sim["equity"] / sim["start"] - 1) * 100 if sim["start"] else 0, "+.1f") + "%) | abertas: " +
                    str(sim["open"]) + "/3", sim["history"])
    lines += ["", "N = normal, S = sniper, P = pump.fun. Avalie depois de 20–30 operações."]
    return "\n".join(lines)


def btc_history_text(btc):
    trades = btc["history"]
    closed = [t for t in trades if t.get("pnl_usd") is not None]
    lines = ["📜 HISTÓRICO — ₿ BTC ALAVANCADO (simulação com preço real)",
             "Saldo: $" + format(btc["equity"], ".2f") + " (início $" + format(btc["start"], ".2f") + ", " +
             format((btc["equity"] / btc["start"] - 1) * 100 if btc["start"] else 0, "+.1f") + "%) | alavancagem " +
             str(btc["leverage"]) + "x"]
    if not closed:
        return "\n".join(lines + ["Nenhuma operação fechada ainda. A estratégia de tendência pode levar dias para dar sinal."])
    for side in ("LONG", "SHORT"):
        s = stats([t for t in closed if side in t["symbol"]])
        if s["n"]:
            lines.append("• " + side + ": " + str(s["n"]) + " operações | acerto " + format(s["wins"] / s["n"] * 100, ".0f") +
                         "% | P/L " + format(s["pnl"], "+.2f") + " USD | ganho médio " + format(s["avg_win"], "+.2f") +
                         " | perda média " + format(s["avg_loss"], "+.2f"))
    lines.append("Últimas:")
    for t in closed[-10:][::-1]:
        lines.append(("💰 " if t["pnl_usd"] > 0 else "🔻 ") + t["symbol"] + " " + format(t["pnl_usd"], "+.2f") + " USD (" +
                     format(t["pnl_usd"] / t["cost_usd"] * 100 if t["cost_usd"] else 0, "+.0f") + "% garantia) · " +
                     REASONS.get(t["reason"], t["reason"]) + " · " + format((t.get("minutes") or 0) / 60, ".1f") + " h")
    lines.append("Em tendência, é normal acertar 35–45% e lucrar porque os ganhos são maiores que as perdas.")
    return "\n".join(lines)
