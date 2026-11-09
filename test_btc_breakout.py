import os
import random
import tempfile
import time
import unittest
from unittest.mock import patch

import btc_breakout as bb

T0 = 1_700_000_100 // 900 * 900


def series(prices, spread=0.001):
    out = []
    for i, p in enumerate(prices):
        o = prices[i - 1] if i else p
        out.append({"t": T0 + i * 900, "o": o, "h": max(o, p) * (1 + spread), "l": min(o, p) * (1 - spread), "c": p, "v": 1})
    return out


def walk(n, drift=0.0, vol=0.004, seed=1):
    rng, p, out = random.Random(seed), 60000.0, []
    for _ in range(n):
        p *= 1 + drift + rng.gauss(0, vol)
        out.append(p)
    return out


class RuleTests(unittest.TestCase):
    def test_breakout_with_trend(self):
        prices = [60000 + i * 5 for i in range(260)] + [62000]          # uptrend, then closes above the channel
        ind = bb.Indicators(series(prices))
        self.assertEqual(bb.signal_at(ind, len(prices) - 1), "long")
        self.assertIsNone(bb.signal_at(ind, len(prices) - 2))
        down = [60000 - i * 5 for i in range(260)] + [58000]
        self.assertEqual(bb.signal_at(bb.Indicators(series(down)), len(down) - 1), "short")

    def test_no_signal_against_trend(self):
        prices = [60000 - i * 5 for i in range(260)] + [59000]          # breaks the channel up but stays below the EMA
        self.assertIsNone(bb.signal_at(bb.Indicators(series(prices)), len(prices) - 1))

    def test_stop_is_twice_the_target_and_before_liquidation(self):
        tp, sl, liq = bb.levels("long", 60000, 300, 5)
        self.assertAlmostEqual(tp - 60000, 300)
        self.assertAlmostEqual(60000 - sl, 600)
        tp, sl, liq = bb.levels("long", 60000, 3000, 20)                 # huge ATR at 20x
        self.assertGreater(sl, liq)                                      # stop always before liquidation
        tp, sl, liq = bb.levels("short", 60000, 300, 10)
        self.assertLess(tp, 60000)
        self.assertGreater(sl, 60000)

    def test_signal_uses_only_past(self):
        c = series(walk(800, 0.0003))
        full, cut = bb.Indicators(c), bb.Indicators(c[:600])
        for i in (400, 599):
            self.assertEqual(bb.signal_at(full, i), bb.signal_at(cut, i))


class BacktestTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)

    def test_runs_all_leverages_and_reports(self):
        c = series(walk(4000, 0.0002, seed=3))
        text, results = bb.backtest_report(c)
        self.assertEqual([r["leverage"] for r in results], list(bb.LEVERAGES))
        self.assertTrue(all(r["trades"] > 0 for r in results))
        self.assertIn("BACKTEST 1 ANO", text)
        self.assertIn("é preciso acertar mais de 67%", text)
        self.assertIsNotNone(bb.backtest_for(5))

    def test_higher_leverage_amplifies(self):
        c = series(walk(4000, 0.0002, seed=5))
        lo, hi = bb.backtest(c, 2), bb.backtest(c, 20)
        self.assertGreater(abs(hi["return_pct"]), abs(lo["return_pct"]))


class LiveTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        self.sent = []

    def send(self, chat, text, *a):
        self.sent.append(text)

    def test_real_needs_backtest_first(self):
        with self.assertRaises(ValueError):
            bb.set_mode(True, True)
        with self.assertRaises(ValueError):
            bb.set_leverage(50)

    def test_sim_open_then_target(self):
        bb.set_mode(True, False)
        bb.set_leverage(10)
        prices = [60000 + i * 5 for i in range(260)] + [62000]
        c = series(prices)
        bb.cycle(self.send, "1", candles=c, price=62000)
        pos = bb._load()["position"]
        self.assertEqual(pos["side"], "long")
        self.assertAlmostEqual(pos["sl"] - 62000, -2 * (pos["tp"] - 62000))
        bb.cycle(self.send, "1", candles=c, price=pos["tp"] + 1)
        d = bb._load()
        self.assertIsNone(d["position"])
        self.assertEqual(d["history"][-1]["reason"], "alvo")
        self.assertIn("LUCRO", self.sent[-1])

    def test_same_candle_not_traded_twice(self):
        bb.set_mode(True, False)
        c = series([60000 + i * 5 for i in range(260)] + [62000])
        bb.cycle(self.send, "1", candles=c, price=62000)
        d = bb._load(); d["position"] = None; bb._save(d)
        bb.cycle(self.send, "1", candles=c, price=62000)
        self.assertIsNone(bb._load()["position"])

    def test_real_open_uses_jupiter(self):
        d = bb._load(); d["backtest"] = {"results": {"5": {"final": 60, "start": 50}}}; bb._save(d)
        bb.set_mode(True, True)
        c = series([60000 + i * 5 for i in range(260)] + [62000])
        with patch("btc_breakout._real_equity", return_value=200.0), patch("btc_usdc.ensure_usdc"), \
                patch("jupiter_perps.open_position", return_value={"price": 62010, "size_usd": 200, "collateral_usd": 40,
                                                                   "signature": "S1"}) as op:
            bb.cycle(self.send, "1", candles=c, price=62000)
        op.assert_called_once_with("long", 40.0, 5, 62000)
        self.assertIn("DINHEIRO REAL", self.sent[-1])
        self.assertEqual(bb._load()["real_position"]["entry"], 62010)
        self.assertIsNone(bb._load()["position"])                         # simulated book untouched


class TelegramTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json"), "TELEGRAM_CHAT_ID": "1"})
        p.start()
        self.addCleanup(p.stop)

    def tap(self, action):
        import telegram_bot as tb
        out = []
        with patch.object(tb, "send", side_effect=lambda chat, text, kb=None: out.append((text, kb))), \
                patch.object(tb, "request_api", return_value={}), patch.object(tb, "CHAT", "1"):
            tb.handle({"callback_query": {"id": "1", "data": action, "message": {"chat": {"id": 1, "type": "private"}}}})
        return out

    def test_buttons(self):
        import telegram_bot as tb
        data = [b["callback_data"] for row in tb.keyboard() for b in row]
        for x in ("brk_sim", "brk_real", "brk_off", "brk_status", "brk_lev", "brk_bt"):
            self.assertIn(x, data)
        self.assertIn("SIMULAÇÃO", self.tap("brk_sim")[0][0])
        self.assertIn("20x", self.tap("brk_lev_20")[0][0])
        self.assertIn("Rode primeiro", self.tap("brk_real")[0][0])

    def test_real_warns_when_backtest_lost(self):
        d = bb._load(); d["leverage"] = 20
        d["backtest"] = {"results": {"20": {"final": 3.0, "start": 50, "return_pct": -94, "max_dd_pct": 97, "liquidations": 4}}}
        bb._save(d)
        text, kb = self.tap("brk_real")[0]
        self.assertIn("PERDEU dinheiro", text)
        self.assertFalse(bb._load()["enabled"])
        self.assertIn("REAL LIGADO", self.tap("brk_real_ok")[0][0])
        self.assertEqual(bb._load()["mode"], "real")
