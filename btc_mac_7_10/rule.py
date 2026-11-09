"""The exact rule MAc(7,10,0.01,0,0) and the original backtest formula.

Source: strategies/ma_strategy.py, walk_forward.py and utils/backtester.py of
github.com/octopus444/technical-trading-rules-based-strategies-btc
- SMA7 and SMA10 of 5-minute prices (Bitstamp last trade, 2017-2022);
- ratio = SMA7 / SMA10 - 1; band b = 0.01 (1%);
- trend rule would buy when ratio > b and sell when ratio < -b. "c" = contrarian, reversed:
  ratio < -1%  -> LONG (+1);  ratio > +1%  -> SHORT (-1);  inside the band -> keep the last signal;
- d = 0: no confirmation delay; c = 0: no minimum holding period;
- always long or short (1x, no leverage) after the first signal;
- cost: 10 bps per side, charged as 20 bps per position change (amortised over the period);
- return = sum of position[i] * log(p[i+1] / p[i]).
"""
import math

S, L, BAND, DELAY, HOLD = 7, 10, 0.01, 0, 0
TC = 0.001  # 10 bps per side, as documented

# Published walk-forward results (results/wfa_results.csv) used to check the reproduction
PUBLISHED = {"2019": (21, 95.95065987785489), "2020": (23, 413.8529410809964),
             "2021": (31, 246.3470674270551), "2022": (11, 186.74343224076128)}


def ratios(prices, s=S, l=L):
    """SMA_s / SMA_l - 1 for each index (None until l prices exist)."""
    out = [None] * len(prices)
    acc_s = acc_l = 0.0
    for i, p in enumerate(prices):
        acc_s += p
        acc_l += p
        if i >= s:
            acc_s -= prices[i - s]
        if i >= l:
            acc_l -= prices[i - l]
        if i >= l - 1:
            out[i] = (acc_s / s) / (acc_l / l) - 1
    return out


def signals(prices, b=BAND):
    """Contrarian signal +1/-1 (0 before the first band break). Uses prices up to i only."""
    sig, last = [], 0
    for r in ratios(prices):
        if r is not None:
            if r > b:
                last = -1
            elif r < -b:
                last = 1
        sig.append(last)
    return sig


def events(prices, b=BAND):
    """Fresh band breaks only: +1 when ratio first drops below -b, -1 when it first rises above +b."""
    out, prev = [], None
    for r in ratios(prices):
        e = 0
        if r is not None and prev is not None:
            if r < -b <= prev:
                e = 1
            elif r > b >= prev:
                e = -1
        out.append(e)
        prev = r if r is not None else prev
    return out


def original_backtest(prices, tc=TC):
    """The project's formula (gross log returns minus amortised 20 bps per position change)."""
    sig = signals(prices)
    n = len(prices)
    trades = sum(1 for a, c in zip(sig, sig[1:]) if a != c)
    gross = sum(sig[i] * math.log(prices[i + 1] / prices[i]) for i in range(n - 1))
    net = gross - trades * 2 * tc
    bh = math.log(prices[-1] / prices[0])
    return {"trades": trades, "gross_log": gross, "net_return_pct": (math.exp(net) - 1) * 100,
            "bh_return_pct": (math.exp(bh) - 1) * 100}
