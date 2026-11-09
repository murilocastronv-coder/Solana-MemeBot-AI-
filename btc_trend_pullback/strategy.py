"""Signal rules, trend filter and position sizing. Shared by backtest and paper trading.

Candles are dicts: t (open time, unix seconds), o, h, l, c, v. 5-minute input;
the 1-hour series is built from it, so both timeframes always agree.
"""
import math

from . import indicators as ind

BAR = 300
HOUR = 3600


def resample_1h(c5):
    """Complete 1h candles from 5m candles (an hour with a missing 5m bar is dropped)."""
    out, bucket, cur = [], [], None
    for c in c5:
        h = c["t"] // HOUR * HOUR
        if h != cur:
            if len(bucket) == 12:
                out.append(_merge(cur, bucket))
            bucket, cur = [], h
        bucket.append(c)
    if len(bucket) == 12:
        out.append(_merge(cur, bucket))
    return out


def _merge(t, bars):
    return {"t": t, "o": bars[0]["o"], "h": max(b["h"] for b in bars), "l": min(b["l"] for b in bars),
            "c": bars[-1]["c"], "v": sum(b["v"] for b in bars)}


class Context:
    """Precomputed indicators for a 5m series. Values at i use only bars <= i."""

    def __init__(self, c5, cfg):
        self.c5, self.cfg = c5, cfg
        closes = [c["c"] for c in c5]
        self.ema_fast = ind.ema(closes, cfg.ema_fast)
        self.ema_slow = ind.ema(closes, cfg.ema_slow)
        self.rsi = ind.rsi(closes, cfg.rsi_len)
        self.atr = ind.atr(c5, cfg.atr_len)
        self.vol_avg = ind.sma_prev([c["v"] for c in c5], cfg.volume_len)
        self.trend_ok = self._trend(c5, cfg)

    @staticmethod
    def _trend(c5, cfg):
        """For each 5m bar: is the 1h filter valid using only 1h candles already closed?"""
        h1 = resample_1h(c5)
        e = ind.ema([h["c"] for h in h1], cfg.ema_trend)
        ok_h = []
        for k, h in enumerate(h1):
            back = k - cfg.ema_slope_lookback_h
            ready = e[k] is not None and back >= 0 and e[back] is not None
            ok_h.append(bool(ready and h["c"] > e[k] and e[k] >= e[back]))
        out, k = [], -1
        for c in c5:
            close_time = c["t"] + BAR
            while k + 1 < len(h1) and h1[k + 1]["t"] + HOUR <= close_time:
                k += 1
            out.append(ok_h[k] if k >= 0 else False)
        return out


def entry_signal(ctx, i):
    """(True, "ok") if bar i (already closed) is a valid LONG confirmation, else (False, reason)."""
    cfg, c5 = ctx.cfg, ctx.c5
    if i < 1 or None in (ctx.ema_fast[i], ctx.ema_slow[i], ctx.rsi[i], ctx.atr[i], ctx.vol_avg[i]):
        return False, "aquecimento"
    if not ctx.trend_ok[i]:
        return False, "filtro_1h"
    bar, prev = c5[i], c5[i - 1]
    touched = False
    for j in range(max(1, i - cfg.pullback_lookback), i + 1):
        ef, es = ctx.ema_fast[j], ctx.ema_slow[j]
        if ef is None or es is None:
            continue
        low = c5[j]["l"]
        if low <= ef * (1 + cfg.near_ema_pct) and low >= min(ef, es) * (1 - cfg.max_below_slow_pct):
            touched = True
            break
    if not touched:
        return False, "sem_recuo_ema"
    # confirmation: the bar closed up, above the previous high and above EMA20
    if not (bar["c"] > bar["o"] and bar["c"] > prev["h"] and bar["c"] > ctx.ema_fast[i]):
        return False, "sem_confirmacao"
    a = ctx.atr[i]
    if cfg.block_fast_drop:
        lo = max(0, i - cfg.fast_drop_bars)
        top = max(c["h"] for c in c5[max(0, lo - cfg.fast_drop_bars):i])
        if top - min(c["l"] for c in c5[lo:i + 1]) > cfg.fast_drop_atr * a:
            return False, "queda_acelerada"
    if cfg.block_extended and bar["c"] - ctx.ema_slow[i] > cfg.extended_atr * a:
        return False, "alta_estendida"
    if not bar["v"] > ctx.vol_avg[i]:
        return False, "volume_baixo"
    if ctx.rsi[i] > cfg.rsi_max:
        return False, "rsi_alto"
    return True, "ok"


def floor_step(qty, step):
    return math.floor(qty / step + 1e-9) * step


def ceil_step(qty, step):
    return math.ceil(qty / step - 1e-9) * step


def loss_per_unit(entry, stop, cfg):
    """Loss per BTC if the stop is hit, including fees and slippage on both sides."""
    fee = cfg.rules.taker_fee
    return (entry - stop) + cfg.slippage * (entry + stop) + fee * (entry + stop)


def size_position(equity, entry, stop, cfg, daily_budget_left=None):
    """Quantity in BTC, or (0, reason). Never risks more than risk_per_trade of equity."""
    rules, lev = cfg.rules, cfg.leverage
    if stop >= entry or equity <= 0:
        return 0.0, "stop_invalido"
    risk_usd = equity * cfg.risk_per_trade
    if daily_budget_left is not None:
        risk_usd = min(risk_usd, daily_budget_left)
    per_unit = loss_per_unit(entry, stop, cfg)
    qty_risk = risk_usd / per_unit
    qty_lev = equity * lev / entry
    qty = floor_step(min(qty_risk, qty_lev), rules.qty_step)
    min_qty = max(rules.min_qty, ceil_step(rules.min_notional / entry, rules.qty_step))
    if qty < min_qty - 1e-12:
        if min_qty * entry > equity * lev:
            return 0.0, "ordem_minima_acima_da_alavancagem"
        if daily_budget_left is not None and min_qty * per_unit > daily_budget_left:
            return 0.0, "limite_diario_restante"
        return 0.0, "ordem_minima_acima_do_risco"
    # isolated-margin liquidation must sit well below the stop
    liq = entry * (1 - 1 / lev + 0.005) if lev > 1 else 0.0
    if stop <= liq:
        return 0.0, "stop_abaixo_da_liquidacao"
    return qty, "ok"
