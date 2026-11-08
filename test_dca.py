import os, random, tempfile, time, unittest
from unittest.mock import patch
import dca_strategy as S
import dca_backtest as D
import btc_perps as b

CFG = S.Config()
T0 = D._ts(*D.START_DAY)


def drop_series(after=(), start=60000.0, t0=T0):
    """5m bars: 70 flat, 12 red candles (-0.4% each), one green candle closing above the previous high, then `after` closes."""
    bars, p = [], start
    for i in range(70):
        o = p; c = p + (4 if i % 2 else -4)
        bars.append([o, max(o, c) + 3, min(o, c) - 3, c]); p = c
    for _ in range(12):
        o = p; c = p * 0.996
        bars.append([o, o + 1, c - 1, c]); p = c
    o = p; c = bars[-1][1] + 5
    bars.append([o, c + 1, o - 1, c]); p = c
    for target in after:
        o = p; c = target
        bars.append([o, max(o, c) + 1, min(o, c) - 1, c]); p = c
    out = D.Bars()
    for k, (o, h, l, c) in enumerate(bars):
        out.add(t0 + k * 300, o, h, l, c)
    return out


def as_dicts(bars):
    return [{"t": bars.t[k], "o": bars.o[k], "h": bars.h[k], "l": bars.l[k], "c": bars.c[k], "v": 1.0} for k in range(len(bars))]


class StrategyMathTests(unittest.TestCase):
    def test_entry_signal_rules(self):
        bars = drop_series()
        closes, highs = list(bars.c), list(bars.h)
        rsi_v = S.rsi_list(closes)
        i = len(closes) - 1
        self.assertLess(rsi_v[i - 1], 30)
        self.assertTrue(S.entry_signal(CFG, closes, highs, rsi_v, i))
        self.assertFalse(S.entry_signal(CFG, closes, highs, rsi_v, i - 1))           # red candle: no confirmation
        self.assertFalse(S.entry_signal(CFG.with_(drop_4h=0.10), closes, highs, rsi_v, i))

    def test_tp_is_net_2_50_and_costs_counted(self):
        pos = S.add_buy(CFG, S.new_position(), 60000.0)
        S.add_buy(CFG, pos, 58500.0)
        self.assertEqual((pos["size"], pos["margin"]), (500.0, 50.0))
        tp = S.tp_price(CFG, pos, 5.0)
        self.assertAlmostEqual(S.net_pnl(CFG, pos, tp, 5.0), 2.50, places=6)
        self.assertGreater(tp, pos["avg"] * (1 + 2.5 / 500))                         # costs push the target up

    def test_stop_always_inside_liquidation(self):
        for stop_pct in (0.03, 0.05, 0.20):
            cfg = CFG.with_(stop_pct=stop_pct, max_loss=500.0)
            pos = S.new_position()
            for p in (60000, 58500, 56450):
                S.add_buy(cfg, pos, p)
            self.assertGreater(S.stop_price(cfg, pos, 1), S.liq_price(cfg, pos))

    def test_rebuy_levels_fixed_and_atr(self):
        pos = S.add_buy(CFG, S.new_position(), 60000.0)
        self.assertAlmostEqual(S.rebuy_price(CFG, pos, 500), 60000 * 0.975)
        S.add_buy(CFG, pos, 58500.0)
        self.assertAlmostEqual(S.rebuy_price(CFG, pos, 500), 58500 * 0.965)
        S.add_buy(CFG, pos, 56452.5)
        self.assertIsNone(S.rebuy_price(CFG, pos, 500))                               # never a 4th buy
        atr = CFG.with_(atr_mode=True)
        pos = S.add_buy(atr, S.new_position(), 60000.0)
        self.assertAlmostEqual(S.rebuy_price(atr, pos, 500), 60000 - 3 * 500)

    def test_h1_strong_downtrend(self):
        down = [60000 - 40 * k for k in range(260)]
        up = [60000 + 40 * k for k in range(260)]
        self.assertTrue(S.h1_strong_down_list(down)[-1])
        self.assertFalse(S.h1_strong_down_list(up)[-1])
        self.assertIsNone(S.h1_strong_down_list(down)[100])


class BacktestEngineTests(unittest.TestCase):
    def market(self, bars):
        return D.Market(bars, 300)

    def run_cfg(self, bars, cfg=None):
        return D.run(cfg or CFG.with_(h1_filter=False), self.market(bars), T0, T0 + 10 ** 7)

    def test_single_buy_takes_2_50(self):
        bottom = drop_series().c[-1]
        r = self.run_cfg(drop_series([bottom * 1.005, bottom * 1.01, bottom * 1.02]))
        self.assertEqual((r["cycles"], r["by_buys"]), (1, [1, 0, 0]))
        self.assertAlmostEqual(r["pnl"] - r["open_pnl"], 2.50, places=2)

    def test_three_buys_then_aggregate_tp(self):
        bottom = drop_series().c[-1]
        path = [bottom * 0.99, bottom * 0.974, bottom * 0.96, bottom * 0.94, bottom * 0.95, bottom * 0.97, bottom * 0.99]
        r = self.run_cfg(drop_series(path))
        self.assertEqual(r["by_buys"], [0, 0, 1])
        self.assertAlmostEqual(r["pnl"] - r["open_pnl"], 2.50, places=2)

    def test_emergency_stop_no_liquidation(self):
        bottom = drop_series().c[-1]
        path = [bottom * (1 - 0.01 * k) for k in range(1, 25)]
        r = self.run_cfg(drop_series(path))
        self.assertEqual((r["emergency"], r["liquidations"]), (1, 0))
        self.assertLess(r["worst"], -20)
        self.assertGreaterEqual(r["worst"], -50.5)                                    # max loss per cycle respected

    def test_entry_is_next_candle_open(self):
        bars = drop_series([60000.0])
        mk = self.market(bars)
        r = D.run(CFG.with_(h1_filter=False, tp_usd=1000), mk, T0, T0 + 10 ** 7)
        expected = bars.o[len(bars) - 1] * (1 + CFG.slip)
        last_close = bars.c[len(bars) - 1]
        pos = S.add_buy(CFG, S.new_position(), expected)
        self.assertAlmostEqual(r["open_pnl"], S.net_pnl(CFG, pos, last_close, 0), places=6)

    def test_h1_filter_blocks_in_strong_downtrend(self):
        mk = self.market(drop_series([60000.0]))
        mk.h1_down = [True] * len(mk.h1_down)
        r = D.run(CFG, mk, T0, T0 + 10 ** 7)
        self.assertEqual((r["cycles"], r["open"]), (0, False))

    def test_report_with_oos_on_synthetic_year(self):
        rnd = random.Random(5)
        start = T0 - D.WARMUP_DAYS * 86400
        bars, p, t = D.Bars(), 90000.0, start
        while t < D._ts(*D.END_DAY):
            o = p; c = o * (1 + rnd.gauss(0, 0.004))
            bars.add(t, o, max(o, c) * 1.001, min(o, c) * 0.999, c); p = c; t += 300
        msgs = D.report(D.Market(bars, 300), "teste")
        self.assertEqual(len(msgs), 3)
        self.assertIn("Resultado mensal", msgs[0])
        self.assertIn("2026-10", msgs[0])
        self.assertEqual(msgs[1].count("•"), 24)                                      # 2 distances x 2 H1 x 3 stops x 2 max-loss
        self.assertIn("FORA DA AMOSTRA", msgs[2])
        self.assertIn("CONCLUSÃO", msgs[2])

    def test_binary_cache_roundtrip(self):
        d = tempfile.mkdtemp()
        with patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")}):
            calls = []
            def get(url):
                calls.append(url)
                st = int(url.split("startTime=")[1].split("&")[0]) // 1000
                end = int(url.split("endTime=")[1]) // 1000
                return [[(st + 60 * k) * 1000, "1", "2", "0.5", "1.5"] for k in range(1000) if st + 60 * k <= end]
            a = D.load_1m(T0, T0 + 3000 * 60, get)
            n_calls = len(calls)
            b2 = D.load_1m(T0, T0 + 3000 * 60, get)
            self.assertEqual(len(a), 3000)
            self.assertEqual(len(calls), n_calls)                                       # second run served from cache
            self.assertEqual(list(a.t), list(b2.t))


class LiveDcaTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        self.env.start(); self.addCleanup(self.env.stop)
        data = b._load(); data.update(balance=150.0, start=150.0); b._save(data)
        b.set_enabled(True); b.set_option("strategy", "dip")
        self.h1 = (False, 600.0)
        p = patch("btc_perps.h1_state", side_effect=lambda: self.h1)
        p.start(); self.addCleanup(p.stop)
        self.sent = []
        self.c = as_dicts(drop_series())
        self.bottom = self.c[-1]["c"]

    def send(self, chat, text, *a):
        self.sent.append(text)

    def test_cycle_with_three_buys_closes_at_2_50(self):
        b.cycle(self.send, "1", candles=self.c, price=self.bottom)
        pos = b.summary()["position"]
        self.assertEqual((len(pos["entries"]), pos["collateral"], pos["size_usd"]), (1, 25.0, 250.0))
        b.cycle(self.send, "1", candles=self.c, price=self.bottom * 0.98)        # -2%: not yet
        self.assertEqual(len(b.summary()["position"]["entries"]), 1)
        b.cycle(self.send, "1", candles=self.c, price=self.bottom * 0.974)       # -2.6% -> 2nd
        p2 = self.bottom * 0.974
        b.cycle(self.send, "1", candles=self.c, price=p2 * 0.964)                # -3.6% from 2nd -> 3rd
        b.cycle(self.send, "1", candles=self.c, price=p2 * 0.95)                 # no 4th buy
        pos = b.summary()["position"]
        self.assertEqual((len(pos["entries"]), pos["collateral"]), (3, 75.0))
        view = b._dca_view(pos)
        target = S.tp_price(S.Config(), view, 0.01) * 1.0002
        b.cycle(self.send, "1", candles=self.c, price=target)
        s = b.summary()
        self.assertIsNone(s["position"])
        self.assertGreaterEqual(s["history"][-1]["pnl_usd"], 2.5)

    def test_emergency_and_h1_block(self):
        b.cycle(self.send, "1", candles=self.c, price=self.bottom)
        b.cycle(self.send, "1", candles=self.c, price=self.bottom * 0.94)        # -6% at once: stop (-5%) first
        self.assertEqual(b.summary()["history"][-1]["reason"], "emergency")
        self.h1 = (True, 600.0)
        data = b._load(); data["last_candle"] = 0; b._save(data)
        b.cycle(self.send, "1", candles=self.c, price=self.bottom)
        self.assertIsNone(b.summary()["position"])

    def test_choosing_strategy_forces_simulation(self):
        import telegram_bot as t, telegram_owner
        telegram_owner.claim(5)
        data = b._load(); data.update(mode="real"); b._save(data)
        out = []
        with patch.object(t, "send", side_effect=lambda c, x, k=None: out.append(x)), \
             patch.object(t, "request_api", return_value={}), patch("btc_perps.real_equity", return_value=150.0):
            t.handle({"callback_query": {"id": "1", "data": "strat_dip", "message": {"chat": {"id": 5, "type": "private"}}}})
        # BTC strategies are retired: the button only explains that.
        self.assertIn("desativados", out[-1])


if __name__ == "__main__":
    unittest.main()
