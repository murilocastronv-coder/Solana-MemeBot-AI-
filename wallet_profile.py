"""Reverse-engineer a wallet's memecoin strategy from its on-chain history.

Reads the wallet's latest transactions (Solana RPC) and rebuilds every position:
when it bought, how much, on which platform, how old the coin was, and how it got
out (the profit or loss of each sale = its real take profit / stop loss), how long
it held and whether it bought or sold in parts. The decision logic itself lives off
chain; this only measures what the wallet actually did.

Analytics only: never signs or sends anything.
"""
import os
import statistics
import time

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTES = {SOL_MINT, USDC_MINT, USDT_MINT}

PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium",
    "LanMV9sAd7wArD4vJFVAy9WdQ9UvT8xY7f2BzEaSq3V": "Raydium LaunchLab",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUJoi5QNyVTaV4": "Jupiter",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca",
}

MAX_SIGNATURES = int(os.getenv("PROFILE_MAX_TX", "400"))


def _keys(tx):
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return keys + list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])


def parse_swap(tx, wallet):
    """One trade of `wallet` in a transaction: dict(side, mint, tokens, sol, usd_stable, platform, t) or None.
    sol = SOL (incl. wrapped SOL) paid (+) or received (-) by the wallet; tokens = token amount moved."""
    meta = (tx or {}).get("meta") or {}
    if meta.get("err") is not None:
        return None
    deltas = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") != wallet:
                continue
            ui = b.get("uiTokenAmount") or {}
            dec = int(ui.get("decimals") or 0)
            amount = int(ui.get("amount") or 0) / (10 ** dec)
            deltas[b.get("mint")] = deltas.get(b.get("mint"), 0.0) + sign * amount
    keys = _keys(tx)
    sol = 0.0
    if wallet in keys:
        i = keys.index(wallet)
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if i < len(pre) and i < len(post):
            sol = (post[i] - pre[i]) / 1e9
    sol += deltas.get(SOL_MINT, 0.0)
    stable = deltas.get(USDC_MINT, 0.0) + deltas.get(USDT_MINT, 0.0)
    tokens = {m: v for m, v in deltas.items() if m and m not in QUOTES and abs(v) > 0}
    if len(tokens) != 1:
        return None
    mint, change = next(iter(tokens.items()))
    platform = next((name for k, name in PROGRAMS.items() if k in keys), "outro")
    t = int(tx.get("blockTime") or 0)
    if change > 0 and (sol < 0 or stable < 0):
        return {"side": "buy", "mint": mint, "tokens": change, "sol": -sol, "stable": -stable,
                "platform": platform, "t": t}
    if change < 0 and (sol > 0 or stable > 0):
        return {"side": "sell", "mint": mint, "tokens": -change, "sol": sol, "stable": stable,
                "platform": platform, "t": t}
    return None


def build_positions(trades, sol_usd=150.0):
    """Group trades (oldest first) into positions per coin. Each sale gets its % result vs the
    average cost, which is the wallet's real take profit / stop loss for that exit."""
    open_pos, closed = {}, []
    for tr in sorted(trades, key=lambda x: x["t"]):
        value = tr["sol"] * sol_usd + tr["stable"]          # USD paid or received
        p = open_pos.get(tr["mint"])
        if tr["side"] == "buy":
            if p is None:
                p = open_pos[tr["mint"]] = {"mint": tr["mint"], "opened": tr["t"], "platform": tr["platform"],
                                            "tokens": 0.0, "cost": 0.0, "buys": 0, "sells": [], "proceeds": 0.0,
                                            "first_buy_usd": value,
                                            "entry_price_usd": value / tr["tokens"] if tr["tokens"] else 0.0}
            p["tokens"] += tr["tokens"]
            p["cost"] += value
            p["buys"] += 1
            continue
        if p is None or p["tokens"] <= 0:
            continue                                         # sold something bought before the window
        sold = min(tr["tokens"], p["tokens"])
        avg = p["cost"] / p["tokens"] if p["tokens"] else 0
        cost_part = avg * sold
        got = value * (sold / tr["tokens"])
        p["sells"].append({"t": tr["t"], "pct": (got / cost_part - 1) if cost_part > 0 else 0.0,
                           "fraction": sold / p["tokens"], "minutes": (tr["t"] - p["opened"]) / 60})
        p["proceeds"] += got
        p["cost"] -= cost_part
        p["tokens"] -= sold
        p.setdefault("cost_closed", 0.0)
        p["cost_closed"] += cost_part
        if p["tokens"] <= sold * 1e-6 or p["cost"] <= 0.01:
            p["closed"] = tr["t"]
            p["result_pct"] = p["proceeds"] / p["cost_closed"] - 1 if p["cost_closed"] > 0 else 0.0
            p["pnl_usd"] = p["proceeds"] - p["cost_closed"]
            p["hold_min"] = (tr["t"] - p["opened"]) / 60
            closed.append(p)
            del open_pos[tr["mint"]]
    return closed, list(open_pos.values())


def _pct(values, q):
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def summarize(closed, open_now, token_age_min=None, entry_mcaps=None):
    """Numbers that describe the strategy + suggested bot settings."""
    if not closed:
        return {"positions": 0}
    results = [p["result_pct"] for p in closed]
    wins = [p for p in closed if p["pnl_usd"] > 0]
    losses = [p for p in closed if p["pnl_usd"] <= 0]
    win_exits = [s["pct"] for p in closed for s in p["sells"] if s["pct"] > 0]
    loss_exits = [s["pct"] for p in closed for s in p["sells"] if s["pct"] <= 0]
    holds = [p["hold_min"] for p in closed]
    sizes = [p["first_buy_usd"] for p in closed + open_now]
    platforms = {}
    for p in closed + open_now:
        platforms[p["platform"]] = platforms.get(p["platform"], 0) + 1
    hours = {}
    for p in closed + open_now:
        h = time.gmtime(p["opened"]).tm_hour
        hours[h] = hours.get(h, 0) + 1
    span_days = max(1e-9, (max(p.get("closed", p["opened"]) for p in closed) - min(p["opened"] for p in closed)) / 86400)
    s = {
        "positions": len(closed), "open": len(open_now),
        "win_rate": len(wins) / len(closed),
        "pnl_usd": sum(p["pnl_usd"] for p in closed),
        "median_result": statistics.median(results),
        "avg_win": statistics.mean([p["result_pct"] for p in wins]) if wins else None,
        "avg_loss": statistics.mean([p["result_pct"] for p in losses]) if losses else None,
        "tp_median": _pct(win_exits, 0.5), "tp_p25": _pct(win_exits, 0.25), "tp_p75": _pct(win_exits, 0.75),
        "sl_median": _pct(loss_exits, 0.5), "sl_p25": _pct(loss_exits, 0.25),
        "hold_median": statistics.median(holds), "hold_p90": _pct(holds, 0.9), "hold_p10": _pct(holds, 0.1),
        "size_median": statistics.median(sizes), "size_p25": _pct(sizes, 0.25), "size_p75": _pct(sizes, 0.75),
        "scale_in": sum(1 for p in closed if p["buys"] > 1) / len(closed),
        "scale_out": sum(1 for p in closed if len(p["sells"]) > 1) / len(closed),
        "platforms": dict(sorted(platforms.items(), key=lambda kv: -kv[1])),
        "top_hours": [h for h, _ in sorted(hours.items(), key=lambda kv: -kv[1])[:4]],
        "trades_per_day": len(closed) / span_days,
        "token_age_median": statistics.median(token_age_min) if token_age_min else None,
        "token_age_p10": _pct(token_age_min, 0.1) if token_age_min else None,
        "token_age_p90": _pct(token_age_min, 0.9) if token_age_min else None,
        "mcap_median": statistics.median(entry_mcaps) if entry_mcaps else None,
        "mcap_p10": _pct(entry_mcaps, 0.1) if entry_mcaps else None,
        "mcap_p90": _pct(entry_mcaps, 0.9) if entry_mcaps else None,
    }
    # Suggested settings for our bot: exit like this wallet exits.
    s["suggest_tp"] = s["tp_median"]
    s["suggest_sl"] = abs(s["sl_median"]) if s["sl_median"] is not None else None
    s["suggest_max_hold_min"] = s["hold_p90"]
    return s


def _fmt_pct(x):
    return "-" if x is None else format(x * 100, "+.0f") + "%"


def _fmt_usd(x):
    if x is None:
        return "-"
    return format(x / 1e6, ".1f") + " mi" if x >= 1e6 else format(x / 1e3, ".0f") + " mil"


def _fmt_min(m):
    if m is None:
        return "-"
    return format(m, ".0f") + " min" if m < 120 else format(m / 60, ".1f") + " h"


def report(address, s):
    if not s.get("positions"):
        return ("🧠 ESTRATÉGIA DE " + address[:6] + "…" + address[-4:] + "\n"
                "Não achei operações fechadas (compra + venda) nas últimas transações. "
                "A carteira pode operar pouco, só segurar, ou usar outro formato de negociação.")
    plats = ", ".join(k + " " + format(v * 100 / max(1, sum(s["platforms"].values())), ".0f") + "%"
                      for k, v in list(s["platforms"].items())[:4])
    lines = [
        "🧠 ESTRATÉGIA DE " + address[:6] + "…" + address[-4:],
        "solscan.io/account/" + address,
        "",
        "📊 RESULTADO: " + str(s["positions"]) + " posições fechadas (" + str(s["open"]) + " abertas) · acerto " +
        format(s["win_rate"] * 100, ".0f") + "% · lucro " + format(s["pnl_usd"], "+.0f") + " USD",
        "Ganho médio " + _fmt_pct(s["avg_win"]) + " · perda média " + _fmt_pct(s["avg_loss"]) +
        " · resultado mediano " + _fmt_pct(s["median_result"]),
        "",
        "🟢 ENTRADA",
        "Valor por moeda: US$ " + format(s["size_median"], ".0f") + " (normalmente entre " +
        format(s["size_p25"], ".0f") + " e " + format(s["size_p75"], ".0f") + ")",
        "Onde compra: " + plats,
        "Compra em partes: " + format(s["scale_in"] * 100, ".0f") + "% das posições",
        "Idade da moeda quando entra: " + (_fmt_min(s["token_age_median"]) + " (de " + _fmt_min(s["token_age_p10"]) +
                                           " a " + _fmt_min(s["token_age_p90"]) + ")" if s["token_age_median"] is not None
                                           else "sem dado"),
        "Valor de mercado da moeda quando entra: " + (
            "US$ " + _fmt_usd(s.get("mcap_median")) + " (de " + _fmt_usd(s.get("mcap_p10")) + " a " +
            _fmt_usd(s.get("mcap_p90")) + ")" if s.get("mcap_median") is not None else "sem dado"),
        "Horários com mais entradas (UTC): " + ", ".join(str(h) + "h" for h in s["top_hours"]),
        "Ritmo: ~" + format(s["trades_per_day"], ".1f") + " operações por dia",
        "",
        "🔴 SAÍDA",
        "Take profit real (vendas no lucro): mediana " + _fmt_pct(s["tp_median"]) + " (de " + _fmt_pct(s["tp_p25"]) +
        " a " + _fmt_pct(s["tp_p75"]) + ")",
        "Stop loss real (vendas no prejuízo): mediana " + _fmt_pct(s["sl_median"]) + " (pior quarto " +
        _fmt_pct(s["sl_p25"]) + ")",
        "Tempo segurando: mediana " + _fmt_min(s["hold_median"]) + " (de " + _fmt_min(s["hold_p10"]) + " a " +
        _fmt_min(s["hold_p90"]) + ")",
        "Vende em partes: " + format(s["scale_out"] * 100, ".0f") + "% das posições",
        "",
        "⚙️ PARA O SEU BOT IMITAR A SAÍDA DELE (Render → Environment):",
    ]
    if s["suggest_tp"] is not None:
        lines.append("COPY_TAKE_PROFIT = " + format(s["suggest_tp"], ".2f"))
    if s["suggest_sl"] is not None:
        lines.append("COPY_STOP_LOSS = " + format(s["suggest_sl"], ".2f"))
    lines += ["", "Robô próprio com esta estratégia (sem copiar): /estrategia " + address,
              "Copiar as entradas dele: /clonar " + address,
              "⚠️ Medido só no histórico recente. Lucro passado não garante lucro futuro."]
    return "\n".join(lines)


def analyze(address, rpc, sol_usd, max_tx=MAX_SIGNATURES, get_json=None, tx_version=None):
    """Full analysis. Returns (summary dict, closed positions, report text)."""
    tx_version = tx_version if tx_version is not None else int(os.getenv("SOLANA_MAX_TX_VERSION", "1"))
    sigs, before = [], None
    while len(sigs) < max_tx:
        opts = {"limit": min(1000, max_tx - len(sigs))}
        if before:
            opts["before"] = before
        page = rpc("getSignaturesForAddress", [address, opts]) or []
        if not page:
            break
        sigs += page
        before = page[-1]["signature"]
    trades = []
    for s in sigs:
        if s.get("err"):
            continue
        try:
            tx = rpc("getTransaction", [s["signature"], {"encoding": "json", "commitment": "confirmed",
                                                         "maxSupportedTransactionVersion": tx_version}])
        except Exception:
            continue
        tr = parse_swap(tx, address) if isinstance(tx, dict) else None
        if tr:
            trades.append(tr)
    closed, open_now = build_positions(trades, sol_usd)
    ages = []
    if get_json and (closed or open_now):
        created = {}
        mints = list(dict.fromkeys(p["mint"] for p in closed + open_now))[:120]
        for i in range(0, len(mints), 30):
            try:
                for pair in get_json("/tokens/v1/solana/" + ",".join(mints[i:i + 30])) or []:
                    m = (pair.get("baseToken") or {}).get("address")
                    t = pair.get("pairCreatedAt")
                    if m and t:
                        created[m] = min(created.get(m, 1e18), int(t) / 1000)
            except Exception:
                continue
        ages = [(p["opened"] - created[p["mint"]]) / 60 for p in closed + open_now
                if p["mint"] in created and p["opened"] >= created[p["mint"]]]
    mcaps = []
    for p in (closed + open_now)[:120]:
        try:
            sup = rpc("getTokenSupply", [p["mint"]]) or {}
            supply = float((sup.get("value") or {}).get("uiAmount") or 0)
        except Exception:
            continue
        if supply > 0 and p.get("entry_price_usd"):
            mcaps.append(p["entry_price_usd"] * supply)
    summary = summarize(closed, open_now, ages, mcaps)
    return summary, closed, report(address, summary)


def holds_seconds(closed):
    """Hold times (s) of closed positions, to start copying a wallet right away."""
    return [int(p["hold_min"] * 60) for p in closed][-30:]
