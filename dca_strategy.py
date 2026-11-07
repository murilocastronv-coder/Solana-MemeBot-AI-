"""LONG DCA M5 — buy BTC after strong drops, average down at most 3 times, take US$ 2.50 net.

Shared by the live robot (btc_perps) and the backtest so both use exactly the same rules.

Entry (on a CLOSED 5m candle):
  - RSI(14) < 30
  - close at least 2% below the highest high of the last 4 hours (48 candles)
  - recovery confirmation: close above the previous candle's high
  - optional H1 filter: no entry while H1 is in a strong downtrend
    (close < EMA50 < EMA200 and EMA50 falling over the last 6 hours)
Re-buys (one at a time, US$ 25 margin each, max 3 buys):
  - fixed: 2nd at -2.5% from the 1st buy, 3rd at -3.5% from the 2nd buy
  - ATR variant: 2nd at 3 x ATR(14, H1) below the 1st, 3rd at 4 x ATR(H1) below the 2nd
Exit:
  - close 100% when the aggregated NET profit (entry+exit fees, slippage, borrow) reaches US$ 2.50
  - emergency: price stop below the weighted average price and a max loss per cycle, whichever comes
    first; the stop is always kept inside the liquidation distance.
"""
from dataclasses import dataclass, replace

DEFAULT = dict(
    leverage=10, margin=25.0, max_buys=3, tp_usd=2.50,
    rsi_max=30.0, drop_4h=0.02, lookback=48,
    gap2=0.025, gap3=0.035, atr_mode=False, atr_k2=3.0, atr_k3=4.0,
    h1_filter=True, stop_pct=0.05, max_loss=50.0,
    fee=0.0006, slip=0.0002, borrow_h=0.00003, liq_loss=0.90,
)


@dataclass(frozen=True)
class Config:
    leverage: int = DEFAULT["leverage"]
    margin: float = DEFAULT["margin"]
    max_buys: int = DEFAULT["max_buys"]
    tp_usd: float = DEFAULT["tp_usd"]
    rsi_max: float = DEFAULT["rsi_max"]
    drop_4h: float = DEFAULT["drop_4h"]
    lookback: int = DEFAULT["lookback"]
    gap2: float = DEFAULT["gap2"]
    gap3: float = DEFAULT["gap3"]
    atr_mode: bool = DEFAULT["atr_mode"]
    atr_k2: float = DEFAULT["atr_k2"]
    atr_k3: float = DEFAULT["atr_k3"]
    h1_filter: bool = DEFAULT["h1_filter"]
    stop_pct: float = DEFAULT["stop_pct"]
    max_loss: float = DEFAULT["max_loss"]
    fee: float = DEFAULT["fee"]
    slip: float = DEFAULT["slip"]
    borrow_h: float = DEFAULT["borrow_h"]
    liq_loss: float = DEFAULT["liq_loss"]

    def label(self):
        dist = ("ATR " + format(self.atr_k2, "g") + "/" + format(self.atr_k3, "g") if self.atr_mode
                else "fixa " + format(self.gap2 * 100, "g") + "/" + format(self.gap3 * 100, "g") + "%")
        return ("recompra " + dist + " · filtro H1 " + ("ON" if self.h1_filter else "OFF") +
                " · stop −" + format(self.stop_pct * 100, "g") + "% / máx −$" + format(self.max_loss, "g"))

    def with_(self, **kw):
        return replace(self, **kw)


# ------------------------------------------------------------------ indicators on plain lists
def rsi_list(closes, n=14):
    """Wilder RSI for every index (50 until enough data)."""
    out = [50.0] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def ema_list(values, n):
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def atr_list(highs, lows, closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    trs = [highs[0] - lows[0]] + [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                                  for i in range(1, len(closes))]
    a = sum(trs[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(closes)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def h1_strong_down_list(h1_closes):
    """Per closed hour: strong downtrend = close < EMA50 < EMA200 and EMA50 lower than 6 hours ago."""
    e50, e200 = ema_list(h1_closes, 50), ema_list(h1_closes, 200)
    out = []
    for i, c in enumerate(h1_closes):
        if e200[i] is None or i < 6 or e50[i - 6] is None:
            out.append(None)                      # unknown (not enough history)
        else:
            out.append(c < e50[i] < e200[i] and e50[i] < e50[i - 6])
    return out


def entry_signal(cfg, closes, highs, rsi_v, i):
    """Signal on closed 5m candle i (uses only data up to i)."""
    if i < max(cfg.lookback, 15) or rsi_v[i] >= cfg.rsi_max:
        return False
    hi4 = max(highs[i - cfg.lookback + 1:i + 1])
    return closes[i] <= hi4 * (1 - cfg.drop_4h) and closes[i] > highs[i - 1]


# ------------------------------------------------------------------ position math
def new_position():
    return {"entries": [], "size": 0.0, "coins": 0.0, "margin": 0.0, "avg": 0.0}


def add_buy(cfg, pos, price):
    size = cfg.margin * cfg.leverage
    pos["entries"].append(price)
    pos["size"] += size
    pos["coins"] += size / price
    pos["margin"] += cfg.margin
    pos["avg"] = pos["size"] / pos["coins"]
    return pos


def costs(cfg, pos, hours):
    """Entry+exit fees, slippage both sides and borrow, in US$."""
    return pos["size"] * (2 * cfg.fee + 2 * cfg.slip + cfg.borrow_h * max(hours, 0.0))


def net_pnl(cfg, pos, price, hours):
    return pos["size"] * (price / pos["avg"] - 1) - costs(cfg, pos, hours)


def tp_price(cfg, pos, hours):
    return pos["avg"] * (1 + (cfg.tp_usd + costs(cfg, pos, hours)) / pos["size"])


def liq_price(cfg, pos):
    return pos["avg"] * (1 - cfg.liq_loss * pos["margin"] / pos["size"])


def stop_price(cfg, pos, hours):
    """Highest (closest) of: % stop, max-loss stop, and 70% of the liquidation distance."""
    pct = pos["avg"] * (1 - cfg.stop_pct)
    loss = pos["avg"] * (1 - (cfg.max_loss - costs(cfg, pos, hours)) / pos["size"])
    safety = pos["avg"] * (1 - 0.70 * cfg.liq_loss * pos["margin"] / pos["size"])
    return max(pct, loss, safety)


def rebuy_price(cfg, pos, atr_h1):
    n = len(pos["entries"])
    if n >= cfg.max_buys:
        return None
    last = pos["entries"][-1]
    if cfg.atr_mode and atr_h1:
        return last - (cfg.atr_k2 if n == 1 else cfg.atr_k3) * atr_h1
    return last * (1 - (cfg.gap2 if n == 1 else cfg.gap3))
