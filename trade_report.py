"""Plain-language trade history and per-strategy statistics for Telegram."""
REASONS = {"stop_loss": "stop", "take_profit": "stop móvel", "time_stop": "tempo", "dead": "zerada", "?": "?"}
MODES = {"normal": "Normal", "sniper": "Sniper"}


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
    for mode in ("normal", "sniper"):
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
    lines += ["", "N = normal, S = sniper. Avalie depois de 20–30 operações."]
    return "\n".join(lines)
