import os
import random
import tempfile
import unittest
from unittest.mock import patch

from btc_trend_pullback import backtest as B
from btc_trend_pullback import indicators as I
from btc_trend_pullback import strategy as S
from btc_trend_pullback.config import StrategyConfig, assert_paper_only, preset
from btc_trend_pullback.engine import Engine, SimBroker
from btc_trend_pullback.paper import PaperTrader

T0 = 1_700_000_000 // 3600 * 3600


def bars(prices, vol=10.0, start=T0):
    out = []
    for i, p in enumerate(prices):
        o = prices[i - 1] if i else p
        out.append({"t": start + i * 300, "o": o, "h": max(o, p) * 1.0005, "l": min(o, p) * 0.9995, "c": p, "v": vol})
    return out


def synthetic(days, seed=1):
    """Bull, bear and sideways stretches. Only for exercising the code, NOT evidence."""
    rng, p, out = random.Random(seed), 30000.0, []
    regimes = [0.00004, -0.00004, 0.0, 0.00003]
    n = days * 288
    for i in range(n):
        drift = regimes[(i * len(regimes)) // n]
        o = p
        p = max(1000.0, p * (1 + drift + rng.gauss(0, 0.0015)))
        out.append({"t": T0 + i * 300, "o": o, "h": max(o, p) * (1 + abs(rng.gauss(0, 0.0007))),
                    "l": min(o, p) * (1 - abs(rng.gauss(0, 0.0007))), "c": p, "v": rng.uniform(5, 15)})
    return out


class FakeCtx:
    def __init__(self, c5, atr=100.0, trend=True):
        self.c5, self.atr = c5, [atr] * len(c5)
        self.trend_ok = trend if isinstance(trend, list) else [trend] * len(c5)


class IndicatorTests(unittest.TestCase):
    def test_ema_constant_and_sma_excludes_current(self):
        self.assertAlmostEqual(I.ema([5.0] * 30, 10)[-1], 5.0)
        self.assertEqual(I.sma_prev([1, 1, 1, 100], 3)[3], 1)

    def test_rsi_and_atr_ranges(self):
        r = I.rsi([100 + i for i in range(30)], 14)
        self.assertEqual(r[-1], 100.0)
        c = bars([100.0] * 30)
        self.assertGreater(I.atr(c, 14)[-1], 0)

    def test_future_bars_do_not_change_past_values(self):
        c = synthetic(40)
        cfg = StrategyConfig()
        full, cut = S.Context(c, cfg), S.Context(c[:9000], cfg)
        for i in (8000, 8999):
            self.assertEqual(full.ema_fast[i], cut.ema_fast[i])
            self.assertEqual(full.trend_ok[i], cut.trend_ok[i])


class TrendFilterTests(unittest.TestCase):
    def test_uses_only_closed_hours(self):
        cfg = StrategyConfig(ema_trend=5, ema_slope_lookback_h=2)
        c = bars([100 + i * 0.01 for i in range(12 * 10)] + [50.0] * 6)  # crash in the open hour
        ok = S.Context._trend(c, cfg)
        self.assertTrue(ok[12 * 10 + 4])   # hour not closed yet -> still the previous hour's verdict
        down = bars([100 - i * 0.01 for i in range(12 * 20)])
        self.assertFalse(any(S.Context._trend(down, cfg)))


class SignalTests(unittest.TestCase):
    def ctx_for(self, last, vol_last=30.0, rsi=55.0):
        cfg = StrategyConfig()
        c = bars([100.0] * 60, vol=10.0)
        c[-2].update({"o": 100.1, "h": 100.15, "l": 99.98, "c": 100.0})
        c.append({"t": c[-1]["t"] + 300, "v": vol_last, **last})
        ctx = S.Context.__new__(S.Context)
        n = len(c)
        ctx.c5, ctx.cfg = c, cfg
        ctx.ema_fast, ctx.ema_slow = [100.0] * n, [99.9] * n
        ctx.rsi, ctx.atr, ctx.vol_avg, ctx.trend_ok = [rsi] * n, [0.2] * n, [10.0] * n, [True] * n
        return ctx

    def test_confirmation_candle_passes(self):
        ctx = self.ctx_for({"o": 100.0, "h": 100.3, "l": 99.99, "c": 100.25})
        self.assertEqual(S.entry_signal(ctx, len(ctx.c5) - 1), (True, "ok"))

    def test_rejections(self):
        n = lambda ctx: S.entry_signal(ctx, len(ctx.c5) - 1)[1]
        self.assertEqual(n(self.ctx_for({"o": 100.2, "h": 100.3, "l": 100.0, "c": 100.1})), "sem_confirmacao")
        self.assertEqual(n(self.ctx_for({"o": 100.0, "h": 100.3, "l": 99.99, "c": 100.25}, vol_last=5)), "volume_baixo")
        self.assertEqual(n(self.ctx_for({"o": 100.0, "h": 100.3, "l": 99.99, "c": 100.25}, rsi=75)), "rsi_alto")
        ctx = self.ctx_for({"o": 100.0, "h": 100.3, "l": 99.99, "c": 100.25})
        ctx.trend_ok[-1] = False
        self.assertEqual(n(ctx), "filtro_1h")


class SizingTests(unittest.TestCase):
    def test_spot_risk_with_fees_is_at_most_half_percent(self):
        cfg = StrategyConfig(profile="spot")
        qty, why = S.size_position(50.0, 100000.0, 99500.0, cfg)
        self.assertEqual(why, "ok")
        self.assertLessEqual(qty * S.loss_per_unit(100000.0, 99500.0, cfg), 0.25 + 1e-9)
        self.assertLessEqual(qty * 100000.0, 50.0)  # spot: 1x

    def test_futures_minimum_order_too_big_for_50_usd(self):
        qty, why = S.size_position(50.0, 100000.0, 99600.0, StrategyConfig(profile="futures"))
        self.assertEqual(qty, 0.0)
        self.assertIn(why, ("ordem_minima_acima_do_risco", "ordem_minima_acima_da_alavancagem"))

    def test_leverage_never_above_2x(self):
        cfg = StrategyConfig(max_leverage=10, rules_override={"min_qty": 0.00001, "qty_step": 0.00001,
                                                              "min_notional": 1, "max_leverage": 20})
        self.assertEqual(cfg.leverage, 2.0)
        qty, _ = S.size_position(50.0, 100000.0, 99999.0, cfg)
        self.assertLessEqual(qty * 100000.0, 100.0 + 1e-6)


def spot_engine(**kw):
    cfg = StrategyConfig(profile="spot", start_equity=1000.0)
    return cfg, Engine(cfg, kw.pop("broker", None) or SimBroker(cfg), **kw)


class EngineTests(unittest.TestCase):
    def run_bars(self, eng, ctx, signal_at=(0,)):
        with patch("btc_trend_pullback.engine.entry_signal", lambda c, i: (i in signal_at, "x")):
            for i in range(len(ctx.c5)):
                eng.on_bar(ctx, i)

    def test_stop_is_assumed_before_target_in_same_bar(self):
        c = bars([30000.0] * 3)
        c[1].update({"o": 30000.0, "h": 31000.0, "l": 29000.0})
        cfg, eng = spot_engine()
        self.run_bars(eng, FakeCtx(c))
        self.assertEqual(eng.trades[0]["reason"], "stop")
        self.assertLess(eng.trades[0]["pnl"], 0)
        self.assertLessEqual(-eng.trades[0]["pnl"], 1000 * 0.005 * 1.01)

    def test_target_and_trend_exit(self):
        c = bars([30000.0] * 4)
        c[2].update({"h": 30400.0})
        cfg, eng = spot_engine()
        self.run_bars(eng, FakeCtx(c))
        self.assertEqual(eng.trades[0]["reason"], "alvo")
        cfg, eng = spot_engine()
        self.run_bars(eng, FakeCtx(bars([30000.0] * 4), trend=[True, False, False, False]))
        self.assertEqual(eng.trades[0]["reason"], "filtro_1h_invalido")

    def test_daily_loss_limit_blocks_until_next_day(self):
        c = []
        for k in range(12):   # signal, then a bar that hits the stop, repeated
            c += [{"t": T0 + k * 600, "o": 30000, "h": 30010, "l": 29990, "c": 30000, "v": 1},
                  {"t": T0 + k * 600 + 300, "o": 30000, "h": 30010, "l": 29000, "c": 29900, "v": 1}]
        cfg, eng = spot_engine()
        self.run_bars(eng, FakeCtx(c), signal_at=set(range(0, len(c), 2)))
        day_loss = -sum(t["pnl"] for t in eng.trades)
        self.assertLessEqual(day_loss, 1000 * 0.02 + 1e-6)
        self.assertGreater(eng.skips["limite_diario"] + eng.skips["limite_diario_restante"], 0)

    def test_unconfirmed_stop_closes_and_locks(self):
        cfg = StrategyConfig(profile="spot", start_equity=1000.0)
        alerts = []
        eng = Engine(cfg, SimBroker(cfg, fail_stop=True), alert=alerts.append)
        self.run_bars(eng, FakeCtx(bars([30000.0] * 4)), signal_at={0, 2})
        self.assertEqual(eng.trades[0]["reason"], "stop_nao_confirmado")
        self.assertIsNone(eng.pos)
        self.assertTrue(eng.lock_reason)
        self.assertEqual(eng.skips["bloqueio_seguranca"], 1)
        self.assertTrue(alerts)

    def test_duplicate_order_locks(self):
        cfg = StrategyConfig(profile="spot", start_equity=1000.0)
        c = bars([30000.0] * 3)
        eng = Engine(cfg, SimBroker(cfg, used_ids=["tp-%d-buy" % c[0]["t"]]))
        self.run_bars(eng, FakeCtx(c))
        self.assertIn("duplicada", eng.lock_reason)
        self.assertFalse(eng.trades)


class BacktestTests(unittest.TestCase):
    def test_refuses_less_than_12_months(self):
        with self.assertRaises(SystemExit):
            B.evaluate(synthetic(120), StrategyConfig(profile="spot"))

    def test_full_run_reports_everything(self):
        res = B.evaluate(synthetic(420), StrategyConfig(profile="spot"))
        for seg in ("desenvolvimento", "fora_da_amostra", "completo"):
            m = res[seg]["metricas"]
            for key in ("retorno_liquido_pct", "drawdown_max_pct", "operacoes", "taxa_acerto_pct", "fator_lucro",
                        "ganho_medio", "perda_media", "seq_max_perdas"):
                self.assertIn(key, m)
            self.assertIn("retorno_liquido_pct", res[seg]["comprar_e_manter"])
        self.assertLess(res["desenvolvimento"]["fim"], res["fora_da_amostra"]["fim"])
        self.assertIn("PASSOU", res["veredito_final"])
        self.assertIn("RESULTADO", B.report(res, "spot"))

    def test_verdict_rules(self):
        self.assertFalse(B.verdict({"operacoes": 50, "fator_lucro": 1.0, "drawdown_max_pct": 3})["passou"])
        self.assertFalse(B.verdict({"operacoes": 50, "fator_lucro": 1.5, "drawdown_max_pct": 10.5})["passou"])
        self.assertTrue(B.verdict({"operacoes": 50, "fator_lucro": 1.2, "drawdown_max_pct": 9})["passou"])


class AggressiveTests(unittest.TestCase):
    def test_preset_rules(self):
        cfg = preset("agressivo")
        self.assertEqual((cfg.risk_per_trade, cfg.reward_risk, cfg.daily_loss_limit, cfg.max_entries_per_day,
                          cfg.kill_switch_drawdown, cfg.leverage), (0.02, 2.0, 0.06, 3, 0.20, 2.0))
        self.assertNotEqual(cfg.fingerprint(), preset("conservador").fingerprint())

    def engine(self, **kw):
        cfg = StrategyConfig(**{**preset("agressivo", "spot").__dict__, **kw})
        return cfg, Engine(cfg, SimBroker(cfg))

    def test_max_three_entries_per_day(self):
        c = bars([30000.0] * 40, start=T0 // 86400 * 86400)
        for k in range(1, 40, 4):          # each trade hits its target a bar later
            c[k + 1].update({"h": 30500.0})
        cfg, eng = self.engine(start_equity=1000.0)
        with patch("btc_trend_pullback.engine.entry_signal", lambda ctx, i: (True, "x")):
            for i in range(len(c)):
                eng.on_bar(FakeCtx(c), i)
        self.assertEqual(len(eng.trades), 3)
        self.assertGreater(eng.skips["max_entradas_dia"], 0)

    def test_kill_switch_at_minus_20_percent(self):
        c = []
        for k in range(40):   # one losing trade per day, every day
            d = T0 + k * 86400
            c += [{"t": d, "o": 30000, "h": 30010, "l": 29990, "c": 30000, "v": 1},
                  {"t": d + 300, "o": 30000, "h": 30010, "l": 29000, "c": 29900, "v": 1}]
        cfg, eng = self.engine()
        with patch("btc_trend_pullback.engine.entry_signal", lambda ctx, i: (i % 2 == 0, "x")):
            for i in range(len(c)):
                eng.on_bar(FakeCtx(c), i)
        self.assertTrue(eng.halted)
        self.assertGreater(eng.cash, 50 * 0.8 * 0.97)   # stops at about -20%, never far below
        self.assertGreater(eng.skips["estrategia_interrompida"], 0)

    def test_fast_drop_and_extended_filters(self):
        base = SignalTests()
        ctx = base.ctx_for({"o": 100.0, "h": 100.3, "l": 99.99, "c": 100.25})
        ctx.cfg = preset("agressivo")
        self.assertEqual(S.entry_signal(ctx, len(ctx.c5) - 1), (True, "ok"))
        ctx.c5[-8]["h"] = 101.5            # fell 1.5 (> 3 x ATR 0.2) in a few bars
        self.assertEqual(S.entry_signal(ctx, len(ctx.c5) - 1)[1], "queda_acelerada")
        ctx.c5[-8]["h"] = 100.1
        ctx.ema_slow = [99.0] * len(ctx.c5)  # close 1.25 above EMA50 (> 3 x ATR)
        ctx.c5[-1]["l"] = 99.99
        ctx.ema_fast = [100.0] * len(ctx.c5)
        self.assertEqual(S.entry_signal(ctx, len(ctx.c5) - 1)[1], "alta_estendida")

    def test_aggressive_evaluation_and_verdict(self):
        res = B.evaluate(synthetic(420), preset("agressivo", "spot"))
        self.assertIsNotNone(res["completo"]["meta_30d"])
        self.assertIn("limite_diario_atingido", res["completo"]["metricas"])
        v = B.verdict({"operacoes": 50, "fator_lucro": 1.3, "drawdown_max_pct": 15, "retorno_liquido_pct": -1},
                      0.20, True)
        self.assertFalse(v["passou"])
        self.assertIn("Meta de simulação", B.report(res, "spot"))


class FakeFeed:
    def __init__(self, c5):
        self.c5, self.fail = c5, False

    def closed_candles(self, now):
        if self.fail:
            raise ConnectionError("offline")
        return [c for c in self.c5 if c["t"] + 300 <= now]


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.c5 = synthetic(40)
        self.feed = FakeFeed(self.c5)
        self.alerts = []
        self.trader = PaperTrader(StrategyConfig(profile="spot"), os.path.join(self.dir, "s.json"),
                                  feed=self.feed, alert=self.alerts.append)

    def test_starts_without_backfilling_and_processes_new_bars(self):
        now = self.c5[9000]["t"] + 310
        self.trader.step(now)
        self.assertEqual(self.trader.engine.last_bar_t, self.c5[9000]["t"])
        self.trader.step(now + 300 * 50)
        self.assertEqual(self.trader.engine.last_bar_t, self.c5[9050]["t"])
        again = PaperTrader(StrategyConfig(profile="spot"), self.trader.path, feed=self.feed, alert=self.alerts.append)
        self.assertEqual(again.engine.last_bar_t, self.c5[9050]["t"])
        self.assertIn("dias mínimos", again.report(now))

    def test_connection_failure_and_stale_price_lock(self):
        now = self.c5[9000]["t"] + 310
        self.trader.step(now)
        self.feed.fail = True
        for _ in range(3):
            self.trader.step(now)
        self.assertIn("conexão", self.trader.engine.lock_reason)
        self.trader.engine.lock_reason = None
        self.feed.fail = False
        self.trader.step(self.c5[-1]["t"] + 300 + 3600)
        self.assertIn("desatualizado", self.trader.engine.lock_reason)

    def test_live_flag_must_stay_false(self):
        with patch.dict(os.environ, {"LIVE_TRADING_ENABLED": "true"}):
            with self.assertRaises(SystemExit):
                assert_paper_only()


if __name__ == "__main__":
    unittest.main()
