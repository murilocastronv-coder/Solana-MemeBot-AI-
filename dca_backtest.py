"""Backtest of the LONG DCA M5 strategy on real BTCUSDT history.

- Period: 07/11/2025 -> 07/11/2026 (plus ~10 days of warm-up for the H1 filter).
- Data: 1-minute candles (Binance; cached on the persistent disk) aggregated to 5m and 1h. Inside each
  5m candle the position is simulated minute by minute; inside a minute, green bars go open->low->high
  and red bars open->high->low (a stop and a target in the same leg count as the stop).
  If 1m data is unavailable, 5m candles are used with the same rule (reported).
- No look-ahead: signals use closed candles only; entries happen at the next candle's open.
- Costs: 0.06% fee per side, 0.02% slippage per side, borrow per hour; margin and liquidation modelled.
- Robustness: a grid (fixed vs ATR re-buy distance, H1 filter on/off, emergency stops) is ranked on the
  first 9 months only; the chosen configuration is then run once on the last 3 months (out of sample).
"""
import array
import calendar
import json
import os
import time

import dca_strategy as S

START_DAY = (2025, 11, 7)
END_DAY = (2026, 11, 7)
OOS_DAY = (2026, 8, 7)          # last 3 months kept for out-of-sample validation
WARMUP_DAYS = 10
ACCOUNT = 150.0


def _ts(y, m, d):
    return calendar.timegm((y, m, d, 0, 0, 0))


# ------------------------------------------------------------------ data
class Bars:
    """Compact OHLC arrays."""
    def __init__(self):
        self.t = array.array("q"); self.o = array.array("d"); self.h = array.array("d")
        self.l = array.array("d"); self.c = array.array("d")

    def add(self, t, o, h, l, c):
        self.t.append(int(t)); self.o.append(o); self.h.append(h); self.l.append(l); self.c.append(c)

    def __len__(self):
        return len(self.t)


def _cache_path():
    wallet = os.environ.get("BOT_WALLET_FILE", "")
    base = os.path.dirname(wallet) if wallet else "/tmp"
    return os.path.join(base, "btc_1m_cache.bin")


def _save_cache(path, start, bars):
    try:
        with open(path + ".tmp", "wb") as f:
            f.write(array.array("q", [start, len(bars)]).tobytes())
            for arr in (bars.t, bars.o, bars.h, bars.l, bars.c):
                f.write(arr.tobytes())
        os.replace(path + ".tmp", path)
    except Exception:
        pass


def _read_cache(path, start):
    bars = Bars()
    try:
        with open(path, "rb") as f:
            head = array.array("q"); head.frombytes(f.read(16))
            if head[0] != start:
                return bars
            n = head[1]
            for arr, size in ((bars.t, 8), (bars.o, 8), (bars.h, 8), (bars.l, 8), (bars.c, 8)):
                arr.frombytes(f.read(n * size))
    except Exception:
        return Bars()
    return bars


def load_1m(start, end, get, progress=None):
    """1m bars in [start, end) from Binance with a compact binary disk cache; returns Bars or raises."""
    path = _cache_path()
    bars = _read_cache(path, start)
    before = len(bars)
    cursor = bars.t[-1] + 60 if len(bars) else start
    total = max(end - start, 1)
    while cursor < end:
        batch = get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1000&startTime=" +
                    str(cursor * 1000) + "&endTime=" + str(end * 1000 - 1))
        if not batch:
            break
        for r in batch:
            bars.add(int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        cursor = bars.t[-1] + 60
        if progress:
            progress((cursor - start) / total)
    if len(bars) > before:
        _save_cache(path, start, bars)
    if len(bars) < 1000:
        raise RuntimeError("1m data unavailable")
    if bars.t[-1] >= end:
        trimmed = Bars()
        for k in range(len(bars)):
            if bars.t[k] < end:
                trimmed.add(bars.t[k], bars.o[k], bars.h[k], bars.l[k], bars.c[k])
        bars = trimmed
    return bars


def load_5m(start, end, get):
    bars, cursor = Bars(), start
    while cursor < end:
        batch = get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=1000&startTime=" +
                    str(cursor * 1000) + "&endTime=" + str(end * 1000 - 1))
        if not batch:
            break
        for r in batch:
            bars.add(int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        cursor = bars.t[-1] + 300
    return bars


def aggregate(sub, secs):
    """Group sub-bars into `secs` buckets. Returns (Bars, list of (first_idx, last_idx) into `sub`)."""
    out, spans = Bars(), []
    i, n = 0, len(sub)
    while i < n:
        bucket = sub.t[i] // secs
        j = i
        hi, lo = sub.h[i], sub.l[i]
        while j + 1 < n and sub.t[j + 1] // secs == bucket:
            j += 1
            hi = max(hi, sub.h[j]); lo = min(lo, sub.l[j])
        out.add(bucket * secs, sub.o[i], hi, lo, sub.c[j])
        spans.append((i, j))
        i = j + 1
    return out, spans


class Market:
    """Precomputed 5m/H1 series used by every backtest run."""
    def __init__(self, sub, sub_secs):
        self.sub = sub
        self.sub_secs = sub_secs
        if sub_secs == 300:
            self.m5, self.spans = sub, [(i, i) for i in range(len(sub))]
        else:
            self.m5, self.spans = aggregate(sub, 300)
        h1, _ = aggregate(self.m5, 3600)
        closes = list(self.m5.c)
        self.rsi = S.rsi_list(closes)
        down = S.h1_strong_down_list(list(h1.c))
        atr = S.atr_list(list(h1.h), list(h1.l), list(h1.c))
        # per 5m candle: state of the last CLOSED hour (no look-ahead)
        self.h1_down, self.h1_atr = [], []
        k = 0
        for t in self.m5.t:
            while k + 1 < len(h1.t) and h1.t[k + 1] + 3600 <= t + 300:
                k += 1
            closed = h1.t[k] + 3600 <= t + 300
            self.h1_down.append(down[k] if closed else None)
            self.h1_atr.append(atr[k] if closed else None)
        self.highs = list(self.m5.h)
        self.closes = closes


# ------------------------------------------------------------------ engine
def run(cfg, mk, t_from, t_to, account=ACCOUNT):
    m5, sub = mk.m5, mk.sub
    cash, pos = account, None
    cycles, monthly = [], {}
    peak, max_dd, broke = account, 0.0, None
    fees_total = 0.0
    open_t = 0
    start_i = next((i for i, t in enumerate(m5.t) if t >= t_from), len(m5.t))

    def close(price, reason, t):
        nonlocal cash, pos, fees_total
        hours = (t - open_t) / 3600
        pnl = max(S.net_pnl(cfg, pos, price, hours), -pos["margin"])
        fees_total += S.costs(cfg, pos, hours)
        cash += pos["margin"] + pnl
        cycles.append({"pnl": pnl, "buys": len(pos["entries"]), "reason": reason, "hours": hours, "t": t})
        m = time.strftime("%Y-%m", time.gmtime(t))
        monthly[m] = monthly.get(m, 0.0) + pnl
        pos = None

    def step(level_low, level_high, green, t, atr_h1):
        """Walk one (sub)bar: lows trigger re-buys / stops / liquidation, highs trigger the target."""
        nonlocal cash, pos
        legs = ("low", "high") if green else ("high", "low")
        for leg in legs:
            if pos is None:
                return
            hours = (t - open_t) / 3600
            if leg == "high":
                tp = S.tp_price(cfg, pos, hours)
                if level_high >= tp:
                    close(tp, "take_profit", t)
            else:
                while True:
                    rb = S.rebuy_price(cfg, pos, atr_h1)
                    if rb is None or level_low > rb or cash < cfg.margin:
                        break
                    if rb <= S.stop_price(cfg, pos, hours):   # stop comes first on the way down
                        break
                    cash -= cfg.margin
                    S.add_buy(cfg, pos, rb * (1 + cfg.slip))
                stop, liq = S.stop_price(cfg, pos, hours), S.liq_price(cfg, pos)
                if level_low <= stop:
                    close(stop * (1 - cfg.slip), "emergency", t)
                elif level_low <= liq:
                    close(liq, "liquidated", t)

    pending = False
    last_i = start_i
    for i in range(start_i, len(m5.t)):
        t = m5.t[i]
        if t >= t_to:
            break
        last_i = i
        atr_h1 = mk.h1_atr[i]
        if pending and pos is None:
            if cash >= cfg.margin:
                cash -= cfg.margin
                pos = S.add_buy(cfg, S.new_position(), m5.o[i] * (1 + cfg.slip))
                open_t = t
                pos["atr"] = atr_h1
            elif broke is None:
                broke = t
            pending = False
        if pos is not None:
            a, b = mk.spans[i]
            for j in range(a, b + 1):
                step(sub.l[j], sub.h[j], sub.c[j] >= sub.o[j], sub.t[j], pos["atr"] if pos else None)
                if pos is None:
                    break
        # signal on the CLOSE of candle i -> entry at the open of candle i+1
        if pos is None and S.entry_signal(cfg, mk.closes, mk.highs, mk.rsi, i):
            down = mk.h1_down[i]
            if not cfg.h1_filter or down is False:
                pending = True
        equity = cash + ((pos["margin"] + S.net_pnl(cfg, pos, m5.c[i], (t - open_t) / 3600)) if pos else 0.0)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
    open_pnl = S.net_pnl(cfg, pos, m5.c[last_i], (m5.t[last_i] - open_t) / 3600) if pos else 0.0
    final = cash + ((pos["margin"] + open_pnl) if pos else 0.0)
    wins = [c for c in cycles if c["pnl"] > 0]
    durations = [c["hours"] for c in cycles]
    return {"cfg": cfg, "start": account, "final": final, "pnl": final - account, "cycles": len(cycles),
            "wins": len(wins), "win_rate": len(wins) / len(cycles) * 100 if cycles else 0.0,
            "max_dd": max_dd * 100, "liq_loss": -sum(c["pnl"] for c in cycles if c["reason"] == "liquidated"),
            "liquidations": sum(1 for c in cycles if c["reason"] == "liquidated"),
            "emergency": sum(1 for c in cycles if c["reason"] == "emergency"),
            "by_buys": [sum(1 for c in cycles if c["buys"] == k) for k in (1, 2, 3)],
            "avg_hours": sum(durations) / len(durations) if durations else 0.0,
            "max_hours": max(durations) if durations else 0.0, "fees": fees_total,
            "monthly": monthly, "open_pnl": open_pnl, "open": pos is not None, "broke": broke,
            "worst": min((c["pnl"] for c in cycles), default=0.0)}


def grid():
    base = S.Config()
    out = []
    for atr_mode in (False, True):
        for h1 in (True, False):
            for stop_pct in (0.03, 0.05, 0.07):
                for max_loss in (30.0, 50.0):
                    out.append(base.with_(atr_mode=atr_mode, h1_filter=h1, stop_pct=stop_pct, max_loss=max_loss))
    return out


def score(r):
    """Net profit penalised by drawdown; configurations with too few cycles are not trusted."""
    if r["cycles"] < 10:
        return -1e9 + r["pnl"]
    return r["pnl"] - 0.5 * r["max_dd"] * r["start"] / 100


# ------------------------------------------------------------------ report
def _money(v):
    return ("+" if v >= 0 else "−") + "$" + format(abs(v), ".2f")


def _res_line(r):
    return ((("🟢 " if r["pnl"] > 0 else "🔴 ") + _money(r["pnl"])) + " · " + str(r["cycles"]) + " ciclos · " +
            format(r["win_rate"], ".0f") + "% vencedores · DD " + format(r["max_dd"], ".0f") + "%")


def _block(title, r):
    lines = [title,
             "💵 Lucro líquido: " + ("🟢 " if r["pnl"] > 0 else "🔴 ") + _money(r["pnl"]) + " ($" +
             format(r["start"], ".0f") + " → $" + format(r["final"], ".2f") + ")",
             "Ciclos: " + str(r["cycles"]) + " · vencedores " + format(r["win_rate"], ".0f") + "% · 1/2/3 compras: " +
             "/".join(str(x) for x in r["by_buys"]),
             "Stops de emergência: " + str(r["emergency"]) + " · liquidações: " + str(r["liquidations"]) +
             " (perda " + _money(-r["liq_loss"]) + ") · pior ciclo " + _money(r["worst"]),
             "Drawdown máx: " + format(r["max_dd"], ".1f") + "% · duração média " + format(r["avg_hours"], ".1f") +
             " h (máx " + format(r["max_hours"], ".0f") + " h) · custos $" + format(r["fees"], ".2f")]
    if r["open"]:
        lines.append("Posição aberta no fim: " + _money(r["open_pnl"]) + " (incluída)")
    if r["broke"]:
        lines.append("⚠️ Sem saldo para comprar a partir de " + time.strftime("%d/%m/%Y", time.gmtime(r["broke"])))
    return lines


def report(mk, data_note):
    t0, t_oos, t1 = _ts(*START_DAY), _ts(*OOS_DAY), _ts(*END_DAY)
    spec = S.Config()
    full = run(spec, mk, t0, t1)
    msgs = []
    head = ["📊 BACKTEST — LONG DCA M5 (BTCUSDT) · 07/11/2025 → 07/11/2026",
            "Conta $" + format(ACCOUNT, ".0f") + " · $25 × 10x por compra · até 3 compras ($750) · TP +$2,50 líquido",
            "Entrada: RSI<30 + queda ≥2% em 4 h + fechou acima da máxima anterior · " + data_note,
            "Custos: taxa 0,06% + slippage 0,02% por lado + juros ~0,003%/h · liquidação modelada", ""]
    lines = head + _block("⚙️ CONFIGURAÇÃO DO PEDIDO (" + spec.label() + "), ano inteiro:", full)
    lines += ["", "📅 Resultado mensal:"]
    for m in sorted(set(list(full["monthly"]) + _month_keys(t0, t1))):
        v = full["monthly"].get(m)
        lines.append("  " + m + ": " + ("sem ciclos" if v is None else ("🟢 " if v > 0 else "🔴 ") + _money(v)))
    msgs.append("\n".join(lines))

    results = [(c, run(c, mk, t0, t_oos)) for c in grid()]
    results.sort(key=lambda x: -score(x[1]))
    lines = ["🔬 COMPARAÇÃO DE VARIANTES — escolhidas só com 07/11/2025 → 07/08/2026 (9 meses):"]
    for c, r in results:
        lines.append("• " + c.label() + ": " + _res_line(r))
    msgs.append("\n".join(lines))

    best_cfg = results[0][0]
    oos = run(best_cfg, mk, t_oos, t1)
    oos_spec = run(spec, mk, t_oos, t1)
    lines = ["✅ VALIDAÇÃO FORA DA AMOSTRA — 07/08/2026 → 07/11/2026 (3 meses que a escolha não viu)",
             "Melhor nos 9 meses: " + best_cfg.label(), ""]
    lines += _block("Melhor variante, fora da amostra:", oos)
    lines += ["", "Configuração do pedido, fora da amostra: " + _res_line(oos_spec), ""]
    robust = oos["pnl"] > 0 and results[0][1]["pnl"] > 0
    lines.append("🧭 CONCLUSÃO: " + (
        "a melhor variante lucrou dentro E fora da amostra. Ainda assim, teste em 🧪 simulação antes do real."
        if robust else
        "nenhuma configuração mostrou lucro consistente dentro e fora da amostra depois dos custos. "
        "Mantenha em 🧪 simulação — não ative dinheiro real com esta estratégia."))
    lines.append("⚠️ Resultado passado não garante o futuro. Operações reais não foram ativadas.")
    msgs.append("\n".join(lines))
    return msgs


def _month_keys(t0, t1):
    out, t = [], t0
    while t < t1:
        out.append(time.strftime("%Y-%m", time.gmtime(t)))
        t += 86400 * 28
    return sorted(set(out))


def build_market(get, progress=None):
    start = _ts(*START_DAY) - WARMUP_DAYS * 86400
    end = min(_ts(*END_DAY), int(time.time()) // 60 * 60)
    try:
        return Market(load_1m(start, end, get, progress), 60), "intrabar com velas de 1 min"
    except Exception:
        return Market(load_5m(start, end, get), 300), "⚠️ sem dados de 1 min: intrabar estimado em velas de 5 min"


def run_report(get, progress=None):
    mk, note = build_market(get, progress)
    return report(mk, note)
