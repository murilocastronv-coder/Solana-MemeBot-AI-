"""Realistic simulation of MAc(7,10) with a 50 USD account.

Decision at the close of bar i (prices up to i), fill at the OPEN of bar i+1 with
slippage. Costs: fee per side on the traded value, slippage per side, funding
every 8 h on perpetual positions (longs pay a positive rate, shorts receive it).
Isolated margin; liquidation when the adverse move eats the margin (minus 0.5%
maintenance). Leverage is never above 2x.

Modes:
- "original": always in the market like the paper (long/short, no stop), notional = equity x leverage;
- "controlado": stop defined BEFORE each entry (stop_pct from the fill), position sized so the
  loss at the stop is at most risk_per_trade of equity (fees included), daily loss limit blocks
  new entries until the next UTC day. After a stop, re-entry only on a NEW band break.
long_only=True turns every short signal into "flat".
No martingale, no grid, no adding to losers, leverage is fixed (never raised after losses).
"""
import math
import random
from dataclasses import dataclass

from . import rule

DAY = 86400


@dataclass(frozen=True)
class SimConfig:
    mode: str = "original"            # "original" | "controlado"
    long_only: bool = False
    leverage: float = 1.0             # capped at 2
    fee: float = 0.001                # 10 bps per side (project's documented cost)
    slippage: float = 0.0005          # 5 bps per side
    funding_8h: float = 0.0001        # used when no funding history is given
    stop_pct: float = 0.10            # controlado: stop 10% from the fill (fixed before testing)
    risk_per_trade: float = 0.02      # controlado: 2% of equity at the stop, fees included
    daily_loss_limit: float = 0.06    # controlado: 6% of the day's starting equity
    min_notional: float = 5.0         # broker minimum order (USDT)
    start_equity: float = 50.0
    goal_multiple: float = 10.0       # 50 -> 500, only measured

    @property
    def lev(self):
        return min(max(self.leverage, 0.0), 2.0)

    def label(self):
        return "%s%s %.0fx" % (self.mode, " só-long" if self.long_only else " long/short", self.lev)


def run(candles, cfg, funding=None):
    """candles: dicts t,o,h,l,c (5m). Returns dict with trades, curve and counters."""
    closes = [c["c"] for c in candles]
    sig = rule.signals(closes)
    ev = rule.events(closes)
    cash = cfg.start_equity
    pos = None
    trades, curve = [], []
    target = None
    day, day_start, day_pnl, locked_day, limit_hits = None, cash, 0.0, None, 0
    liquidations, skipped_min = 0, 0
    ruined = False
    cost_side = cfg.fee + cfg.slippage

    def close(t, price, reason):
        nonlocal cash, pos, day_pnl, locked_day, limit_hits
        side, qty = pos["side"], pos["qty"]
        fill = price * (1 - cfg.slippage * side) if reason != "liquidacao" else price
        fee = qty * fill * cfg.fee
        gross = side * qty * (fill - pos["entry"])
        if reason == "liquidacao":
            gross = -pos["margin"]
            fee = 0.0
        cash += gross - fee
        pnl = gross - fee - pos["fee"] - pos["funding"]
        trades.append({"entry_t": pos["t"], "exit_t": t, "side": side, "entry": pos["entry"], "exit": fill,
                       "qty": qty, "pnl": pnl, "equity_before": pos["equity_before"], "reason": reason})
        pos = None
        day_pnl += pnl
        if cfg.mode == "controlado" and day_pnl <= -cfg.daily_loss_limit * day_start and locked_day != day:
            locked_day = day
            limit_hits += 1

    def open_(t, price, side):
        nonlocal cash, pos, skipped_min
        fill = price * (1 + cfg.slippage * side)
        if cfg.mode == "original":
            notional = cash * cfg.lev
            stop = None
        else:
            stop = fill * (1 - cfg.stop_pct * side)
            per_unit = abs(fill - stop) + 2 * cost_side * fill
            notional = min(cash * cfg.risk_per_trade / per_unit * fill, cash * cfg.lev)
        if notional < cfg.min_notional or cash <= 0:
            skipped_min += 1
            return
        qty = notional / fill
        fee = notional * cfg.fee
        cash -= fee
        margin = notional / cfg.lev if cfg.lev > 0 else notional
        liq_move = (1 / cfg.lev - 0.005) if cfg.lev > 1 else (None if side == 1 else 0.995)
        liq = None if liq_move is None else fill * (1 - liq_move * side)
        pos = {"t": t, "side": side, "qty": qty, "entry": fill, "stop": stop, "liq": liq, "fee": fee,
               "funding": 0.0, "margin": margin, "equity_before": cash + fee}

    for i, c in enumerate(candles):
        d = c["t"] // DAY
        if d != day:
            day, day_start, day_pnl = d, cash, 0.0
        # 1) open of bar i: execute the decision taken at the close of i-1
        if target is not None:
            want = target
            target = None
            if pos and pos["side"] != want:
                close(c["t"], c["o"], "sinal")
            if want and not pos and not ruined:
                open_(c["t"], c["o"], want)
        # 2) inside the bar: funding, liquidation, stop (worst case first)
        if pos:
            if c["t"] % (8 * 3600) == 0 and c["t"] > pos["t"]:
                rate = funding.get(c["t"], cfg.funding_8h) if funding else cfg.funding_8h
                f = pos["side"] * pos["qty"] * c["o"] * rate
                cash -= f
                pos["funding"] += f
            adverse = c["l"] if pos["side"] == 1 else c["h"]
            hit = lambda lvl: lvl is not None and (adverse <= lvl if pos["side"] == 1 else adverse >= lvl)
            if hit(pos["liq"]) and not (pos["stop"] is not None and hit(pos["stop"]) and
                                        abs(pos["stop"] - pos["entry"]) < abs(pos["liq"] - pos["entry"])):
                close(c["t"], pos["liq"], "liquidacao")
                liquidations += 1
            elif hit(pos["stop"]):
                gap = c["o"] if (pos["side"] == 1 and c["o"] <= pos["stop"]) or \
                    (pos["side"] == -1 and c["o"] >= pos["stop"]) else pos["stop"]
                close(c["t"], gap, "stop")
        if cash < cfg.start_equity * 0.01:
            ruined = True
        # 3) close of bar i: decide for the next open
        s = sig[i] if not cfg.long_only else max(sig[i], 0)
        if cfg.mode == "original":
            want = s
            if pos is None or pos["side"] != want:
                target = want
        else:
            if pos and sig[i] != pos["side"]:
                target = s       # opposite signal: exit (and reverse if allowed)
            elif not pos and ev[i] and (ev[i] == 1 or not cfg.long_only):
                target = ev[i]
            if target and locked_day == day:   # daily limit: exits allowed, no new entries
                target = 0 if pos else None
        if ruined:
            target = 0 if pos else None
        mtm = cash
        if pos:
            worst = c["l"] if pos["side"] == 1 else c["h"]
            mtm_low = cash + pos["side"] * pos["qty"] * (worst - pos["entry"])
            mtm = cash + pos["side"] * pos["qty"] * (c["c"] - pos["entry"])
        else:
            mtm_low = cash
        curve.append((c["t"], mtm_low, mtm))
    if pos:
        close(candles[-1]["t"], candles[-1]["c"], "fim")
        curve[-1] = (curve[-1][0], min(curve[-1][1], cash), cash)
    return {"trades": trades, "curve": curve, "cash": cash, "limit_hits": limit_hits,
            "liquidations": liquidations, "skipped_min": skipped_min, "ruined": ruined}


def max_drawdown(curve):
    peak, worst = None, 0.0
    for _, low, close in curve:
        peak = close if peak is None else max(peak, close)
        if peak > 0:
            worst = max(worst, (peak - low) / peak)
    return worst


def goal_windows(curve, multiple, days=30):
    """Daily start points; how many 30-day windows reached `multiple` x the starting equity."""
    pts = curve[::288]
    hits = total = 0
    best = 0.0
    j = 0
    closes = [c for _, _, c in curve]
    times = [t for t, _, _ in curve]
    for k in range(len(pts)):
        t0, _, e0 = pts[k]
        if t0 + days * DAY > times[-1] or e0 <= 0:
            continue
        total += 1
        while j < len(times) and times[j] < t0:
            j += 1
        hi = j
        peak = e0
        while hi < len(times) and times[hi] <= t0 + days * DAY:
            peak = max(peak, closes[hi])
            hi += 1
        best = max(best, peak / e0)
        hits += peak / e0 >= multiple
    return {"janelas": total, "atingiu": hits, "melhor_multiplo": best}


def ruin_monte_carlo(trades, start, n_trades=None, sims=5000, seed=7):
    """Bootstrap of per-trade returns (as % of equity). Probability of losing half / almost all."""
    rets = [t["pnl"] / t["equity_before"] for t in trades if t["equity_before"] > 0]
    if not rets:
        return {"p_perder_50": 0.0, "p_perder_90": 0.0, "mediana_final": start}
    n = n_trades or len(rets)
    rng = random.Random(seed)
    lose50 = lose90 = 0
    finals = []
    for _ in range(sims):
        e = low = start
        for _ in range(n):
            e *= 1 + max(rng.choice(rets), -1.0)
            low = min(low, e)
        lose50 += low <= start * 0.5
        lose90 += low <= start * 0.1
        finals.append(e)
    finals.sort()
    return {"p_perder_50": lose50 / sims, "p_perder_90": lose90 / sims, "mediana_final": finals[sims // 2]}


def metrics(res, candles, cfg):
    tr = res["trades"]
    pnls = [t["pnl"] for t in tr]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw, gl = sum(wins), -sum(losses)
    streak = worst = 0
    for p in pnls:
        streak = streak + 1 if p <= 0 else 0
        worst = max(worst, streak)
    bh_entry = candles[0]["o"] * (1 + cfg.slippage)
    bh_qty = cfg.start_equity / (bh_entry * (1 + cfg.fee))
    bh_curve = [(c["t"], bh_qty * c["l"], bh_qty * c["c"]) for c in candles]
    bh_final = bh_qty * candles[-1]["c"] * (1 - cfg.slippage - cfg.fee)
    return {
        "saldo_final": res["cash"],
        "retorno_liquido_pct": (res["cash"] / cfg.start_equity - 1) * 100,
        "drawdown_max_pct": max_drawdown(res["curve"]) * 100,
        "operacoes": len(tr),
        "taxa_acerto_pct": len(wins) / len(tr) * 100 if tr else 0.0,
        "fator_lucro": gw / gl if gl > 0 else (math.inf if gw > 0 else 0.0),
        "ganho_medio": gw / len(wins) if wins else 0.0,
        "perda_media": -gl / len(losses) if losses else 0.0,
        "pior_seq_perdas": worst,
        "liquidacoes": res["liquidations"],
        "limite_diario_atingido": res["limit_hits"],
        "perdeu_tudo": res["ruined"],
        "meta_30d": goal_windows(res["curve"], cfg.goal_multiple),
        "risco_ruina": ruin_monte_carlo(tr, cfg.start_equity),
        "bh_retorno_pct": (bh_final / cfg.start_equity - 1) * 100,
        "bh_drawdown_pct": max_drawdown(bh_curve) * 100,
    }


def verdict(m):
    reasons = []
    if m["operacoes"] == 0:
        reasons.append("sem operações")
    if m["fator_lucro"] <= 1:
        reasons.append("fator de lucro %.2f <= 1 (não supera os custos)" % m["fator_lucro"])
    if m["retorno_liquido_pct"] <= 0:
        reasons.append("retorno líquido negativo")
    if m["perdeu_tudo"] or m["liquidacoes"]:
        reasons.append("liquidação/perda total")
    return {"passou": not reasons, "motivos": reasons}
