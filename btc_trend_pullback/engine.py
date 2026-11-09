"""One state machine used by BOTH the backtest and paper trading.

Order of events on every closed 5m bar i:
1. new UTC day -> reset the daily loss counter (the day's starting balance is fixed here);
2. at the bar's OPEN: run what was decided at the previous close (entry or trend exit);
3. funding (futures), then stop / target inside the bar. If both could have been
   touched in the same bar, the STOP is assumed first (worst case);
4. at the bar's CLOSE: if in a position and the 1h filter broke -> exit at next open;
   if flat -> check the entry signal and size the trade.

Decisions only use bars that are already closed. Fills happen at the next open.
"""
from collections import Counter

from .strategy import entry_signal, size_position

DAY = 86400


class DuplicateOrder(Exception):
    pass


class SimBroker:
    """Simulated orders (backtest and paper). There is no real exchange behind it."""

    def __init__(self, cfg, used_ids=None, fail_stop=False):
        self.cfg, self.used_ids, self.fail_stop = cfg, set(used_ids or []), fail_stop

    def _claim(self, client_id):
        if client_id in self.used_ids:
            raise DuplicateOrder(client_id)
        self.used_ids.add(client_id)

    def buy(self, qty, ref_price, client_id):
        self._claim(client_id)
        return ref_price * (1 + self.cfg.slippage)

    def place_stop(self, qty, stop_price, client_id):
        """Returns True only if the (simulated) exchange accepted the stop."""
        self._claim(client_id)
        return not self.fail_stop

    def sell(self, qty, ref_price, slippage=True):
        return ref_price * (1 - self.cfg.slippage) if slippage else ref_price


class Engine:
    def __init__(self, cfg, broker, funding=None, alert=None, state=None):
        self.cfg, self.broker, self.funding = cfg, broker, funding
        self.alert = alert or (lambda msg: None)
        s = state or {}
        self.cash = s.get("cash", cfg.start_equity)
        self.pos = s.get("pos")
        self.pending = s.get("pending")
        self.day = s.get("day")
        self.day_start = s.get("day_start", self.cash)
        self.day_pnl = s.get("day_pnl", 0.0)
        self.daily_locked_day = s.get("daily_locked_day")
        self.lock_reason = s.get("lock_reason")      # safety lock: manual unlock only
        self.last_bar_t = s.get("last_bar_t")
        self.trades = s.get("trades", [])
        self.skips = Counter(s.get("skips", {}))
        self.signals = s.get("signals", 0)
        self.entries_today = s.get("entries_today", 0)
        self.daily_limit_hits = s.get("daily_limit_hits", 0)
        self.halted = s.get("halted")                 # kill switch: strategy stopped for good
        self.curve = [tuple(x) for x in s.get("curve", [])]   # (t, equity_low, equity_close)

    # ---- state for paper trading persistence
    def state(self):
        return {"cash": self.cash, "pos": self.pos, "pending": self.pending, "day": self.day,
                "day_start": self.day_start, "day_pnl": self.day_pnl, "daily_locked_day": self.daily_locked_day,
                "lock_reason": self.lock_reason, "last_bar_t": self.last_bar_t, "trades": self.trades,
                "skips": dict(self.skips), "signals": self.signals, "curve": self.curve,
                "entries_today": self.entries_today, "daily_limit_hits": self.daily_limit_hits,
                "halted": self.halted}

    def lock(self, reason):
        if not self.lock_reason:
            self.lock_reason = reason
            self.alert("🚫 Novas ordens BLOQUEADAS: " + reason)

    # ---- main step
    def on_bar(self, ctx, i):
        bar = ctx.c5[i]
        if self.last_bar_t is not None and bar["t"] <= self.last_bar_t:
            return  # already processed (paper restarts / overlapping fetches)
        self._roll_day(bar)
        self._run_pending(bar)
        if self.pos:
            self._charge_funding(bar)
            self._check_stop_target(bar)
        self._on_close(ctx, i, bar)
        self.last_bar_t = bar["t"]
        if self.pos:
            q, e = self.pos["qty"], self.pos["entry"]
            self.curve.append((bar["t"], self.cash + q * (bar["l"] - e), self.cash + q * (bar["c"] - e)))
        else:
            self.curve.append((bar["t"], self.cash, self.cash))

    def _roll_day(self, bar):
        d = bar["t"] // DAY
        if d != self.day:
            self.day, self.day_start, self.day_pnl, self.entries_today = d, self.cash, 0.0, 0

    def _daily_budget_left(self):
        return self.cfg.daily_loss_limit * self.day_start + self.day_pnl

    def _run_pending(self, bar):
        p, self.pending = self.pending, None
        if not p:
            return
        if p["action"] == "exit" and self.pos:
            self._close(bar["t"], self.broker.sell(self.pos["qty"], bar["o"]), p["reason"])
        elif p["action"] == "enter":
            self._enter(bar, p)

    def _enter(self, bar, p):
        cfg = self.cfg
        if self.pos:
            self.lock("ordem duplicada: já existe posição aberta")
            return
        cid = "tp-%d" % p["signal_t"]
        est = bar["o"] * (1 + cfg.slippage)
        qty, why = size_position(self.cash, est, est - cfg.stop_atr_mult * p["atr"], cfg,
                                 self._daily_budget_left())
        if not qty:
            self.skips[why] += 1
            return
        try:
            fill = self.broker.buy(qty, bar["o"], cid + "-buy")
        except DuplicateOrder:
            self.lock("ordem duplicada (" + cid + ")")
            return
        stop = fill - cfg.stop_atr_mult * p["atr"]
        target = fill + cfg.reward_risk * (fill - stop)
        fee = qty * fill * cfg.rules.taker_fee
        self.cash -= fee
        self.entries_today += 1
        self.pos = {"t": bar["t"], "entry": fill, "qty": qty, "stop": stop, "target": target,
                    "fee": fee, "funding": 0.0, "risk_usd": qty * (fill - stop)}
        try:
            confirmed = self.broker.place_stop(qty, stop, cid + "-stop")
        except DuplicateOrder:
            confirmed = False
        if not confirmed:
            self._close(bar["t"], self.broker.sell(qty, bar["o"]), "stop_nao_confirmado")
            self.lock("stop-loss não confirmado pela corretora; posição encerrada")

    def _charge_funding(self, bar):
        if not self.cfg.rules.has_funding or bar["t"] <= self.pos["t"]:
            return
        if self.funding is not None:
            rate = self.funding.get(bar["t"])
        else:
            rate = self.cfg.default_funding_8h if bar["t"] % (8 * 3600) == 0 else None
        if rate:
            cost = self.pos["qty"] * bar["o"] * rate   # positive rate: longs pay
            self.cash -= cost
            self.pos["funding"] += cost

    def _check_stop_target(self, bar):
        p = self.pos
        if bar["o"] <= p["stop"]:
            self._close(bar["t"], self.broker.sell(p["qty"], bar["o"]), "stop_gap")
        elif bar["l"] <= p["stop"]:
            self._close(bar["t"], self.broker.sell(p["qty"], p["stop"]), "stop")
        elif bar["h"] >= p["target"]:
            self._close(bar["t"], self.broker.sell(p["qty"], p["target"], slippage=False), "alvo")

    def _close(self, t, price, reason):
        p, cfg = self.pos, self.cfg
        exit_fee = p["qty"] * price * cfg.rules.taker_fee
        gross = p["qty"] * (price - p["entry"])
        self.cash += gross - exit_fee
        pnl = gross - p["fee"] - exit_fee - p["funding"]
        self.trades.append({"entry_t": p["t"], "exit_t": t, "entry": p["entry"], "exit": price, "qty": p["qty"],
                            "stop": p["stop"], "target": p["target"], "pnl": pnl, "fees": p["fee"] + exit_fee,
                            "funding": p["funding"], "reason": reason, "risk_usd": p["risk_usd"]})
        self.pos = None
        self.day_pnl += pnl
        if self.day_pnl <= -cfg.daily_loss_limit * self.day_start:
            self._daily_limit_hit()
        kill = cfg.kill_switch_drawdown
        if kill and not self.halted and self.cash <= cfg.start_equity * (1 - kill):
            self.halted = "saldo %.2f caiu %.0f%% ou mais desde %.2f" % (self.cash, kill * 100, cfg.start_equity)
            self.alert("🛑 Estratégia INTERROMPIDA: " + self.halted)

    def _daily_limit_hit(self):
        """Daily loss limit reached (or the room left today is smaller than one minimum trade)."""
        if self.daily_locked_day != self.day:
            self.daily_locked_day = self.day
            self.daily_limit_hits += 1
            self.alert("⛔ Limite de perda diária atingido (%.2f USD hoje). Sem novas entradas até amanhã (UTC)."
                       % self.day_pnl)

    def _on_close(self, ctx, i, bar):
        if self.pos:
            if not ctx.trend_ok[i]:
                self.pending = {"action": "exit", "reason": "filtro_1h_invalido"}
            return
        if self.pending:
            return
        ok, _ = entry_signal(ctx, i)
        if not ok:
            return
        self.signals += 1
        if self.lock_reason:
            self.skips["bloqueio_seguranca"] += 1
            return
        if self.halted:
            self.skips["estrategia_interrompida"] += 1
            return
        if self.daily_locked_day == self.day:
            self.skips["limite_diario"] += 1
            return
        if self.cfg.max_entries_per_day and self.entries_today >= self.cfg.max_entries_per_day:
            self.skips["max_entradas_dia"] += 1
            return
        est = bar["c"] * (1 + self.cfg.slippage)
        a = ctx.atr[i]
        qty, why = size_position(self.cash, est, est - self.cfg.stop_atr_mult * a, self.cfg,
                                 self._daily_budget_left())
        if not qty:
            self.skips[why] += 1
            if why == "limite_diario_restante":
                self._daily_limit_hit()
            return
        self.pending = {"action": "enter", "signal_t": bar["t"], "atr": a}

    def force_close(self, bar, reason="fim_do_periodo"):
        self.pending = None
        if self.pos:
            self._close(bar["t"], self.broker.sell(self.pos["qty"], bar["c"]), reason)
