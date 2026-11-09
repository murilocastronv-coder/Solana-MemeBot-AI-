"""Plain-Python indicators. Each returns a list aligned with the input, None until ready.

Every value at index i uses only data up to and including i (no look-ahead).
"""


def ema(values, n):
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def sma_prev(values, n):
    """Average of the n values BEFORE index i (the current value is excluded)."""
    out = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        if i >= n:
            out[i] = s / n
            s -= values[i - n]
        s += v
    return out


def rsi(closes, n=14):
    """Wilder's RSI."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / n, losses / n

    def value(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1 + g / l)

    out[n] = value(avg_g, avg_l)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
        out[i] = value(avg_g, avg_l)
    return out


def atr(candles, n=14):
    """Wilder's ATR on dict candles with h/l/c."""
    out = [None] * len(candles)
    if len(candles) <= n:
        return out
    trs = [candles[0]["h"] - candles[0]["l"]]
    for i in range(1, len(candles)):
        c, pc = candles[i], candles[i - 1]["c"]
        trs.append(max(c["h"] - c["l"], abs(c["h"] - pc), abs(c["l"] - pc)))
    prev = sum(trs[1:n + 1]) / n
    out[n] = prev
    for i in range(n + 1, len(candles)):
        prev = (prev * (n - 1) + trs[i]) / n
        out[i] = prev
    return out
