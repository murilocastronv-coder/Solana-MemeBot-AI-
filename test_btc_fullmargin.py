import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import btc_fullmargin as bfm

T0 = 1_700_000_000 // 300 * 300


def candles(n=800, trend=-1):
    """Steady trend (down by default), 5-minute candles, oldest first; the last one is still forming."""
    out, p = [], 60000.0
    for i in range(n):
        o = p
        p *= (1 + 0.0008 * trend) if i % 4 else (1 - 0.0004 * trend)
        out.append({"t": T0 + i * 300, "o": o, "h": max(o, p) * 1.0003, "l": min(o, p) * 0.9997, "c": p})
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.p = patch.object(bfm, "_path", return_value=Path(self.dir) / "bfm.json")
        self.p.start()
        self.addCleanup(self.p.stop)
        self.msgs = []

    def send(self, chat, text, kb=None):
        self.msgs.append(text)

    def turn_on(self, **kw):
        d = bfm._load()
        d.update(dict(dict(on=True, mode="demo", start_usd=50.0, demo_usd=50.0), **kw))
        bfm._save(d)


class ReadChart(Base):
    def test_downtrend_scores_negative(self):
        sc, a, text = bfm.read_chart(candles()[:-1])
        self.assertLessEqual(sc, -4)
        self.assertGreater(a, 0)
        self.assertIn("leitura", text)

    def test_uptrend_scores_positive(self):
        sc, _, _ = bfm.read_chart(candles(trend=1)[:-1])
        self.assertGreaterEqual(sc, 4)

    def test_stop_is_far_from_liquidation(self):
        cs = candles()[:-1]
        price = cs[-1]["c"]
        dist = bfm.stop_distance(cs, price * 0.5, price, 10)      # huge ATR
        self.assertLessEqual(dist, price * 0.6 / 10 + 1e-9)


class Manage(unittest.TestCase):
    def pos(self):
        return {"entry": 100.0, "size_usd": 500.0, "collateral_usd": 100.0, "risk": 1.0, "stop": 101.0}

    def test_target(self):
        self.assertEqual(bfm.manage(self.pos(), 98.4)[0], "🎯 ALVO")

    def test_stop(self):
        self.assertEqual(bfm.manage(self.pos(), 101.0)[0], "🛑 STOP")

    def test_breakeven_and_trailing(self):
        p = self.pos()
        why, stop = bfm.manage(p, 99.3)            # +0.7 R: stop to the entry
        self.assertEqual(why, "")
        self.assertLess(stop, 100.0)
        why, stop = bfm.manage(p, 99.0)            # +1 R: trailing 0.5 R above the price
        self.assertAlmostEqual(stop, 99.5)

    def test_pnl_short_with_fees(self):
        p = self.pos()
        self.assertAlmostEqual(bfm.pnl_usd(p, 99.0), 5.0 - 0.6)


class Cycle(Base):
    def test_off_does_nothing(self):
        self.assertEqual(bfm.cycle(self.send, "1", klines=candles), "desligado")

    def test_opens_short_right_away_when_switched_on(self):
        self.turn_on()
        up = candles(trend=1)                       # even against the reading: first entry on switching on
        self.assertEqual(bfm.cycle(self.send, "1", klines=lambda: up), "abriu")
        d = bfm._load()
        self.assertEqual(d["pos"]["collateral_usd"], 50.0)
        self.assertEqual(d["pos"]["size_usd"], 50.0 * bfm.DEFAULT_LEVERAGE)
        self.assertIn("SHORT aberto", self.msgs[-1])

    def test_only_shorts_after_the_first(self):
        self.turn_on(trades=1)
        r = bfm.cycle(self.send, "1", klines=lambda: candles(trend=1))
        self.assertTrue(r.startswith("sem sinal"))
        self.assertIsNone(bfm._load()["pos"])
        d = bfm._load()
        d["last_bar"] = 0                           # next 5-minute candle
        bfm._save(d)
        self.assertEqual(bfm.cycle(self.send, "1", klines=lambda: candles(trend=-1)), "abriu")

    def test_closes_at_target_and_compounds(self):
        self.turn_on()
        cs = candles()
        bfm.cycle(self.send, "1", klines=lambda: cs)
        pos = bfm._load()["pos"]
        later = cs[:-1] + [dict(cs[-1], c=pos["entry"] - 2 * pos["risk"])]
        self.assertEqual(bfm.cycle(self.send, "1", klines=lambda: later), "fechou")
        d = bfm._load()
        self.assertIsNone(d["pos"])
        self.assertEqual(d["wins"], 1)
        self.assertGreater(d["demo_usd"], 50.0)

    def test_two_losses_pause(self):
        self.turn_on(trades=1)
        d = bfm._load()
        for _ in range(2):
            d["pos"] = {"entry": 100.0, "size_usd": 250.0, "collateral_usd": 50.0, "risk": 1.0, "stop": 101.0}
            bfm._close(d, 101.0, "🛑 STOP")
        self.assertGreater(d["pause_until"], 0)
        self.assertEqual(d["losses"], 2)

    def test_session_target_switches_off(self):
        self.turn_on(demo_usd=160.0)
        self.assertEqual(bfm.cycle(self.send, "1", klines=candles), "META BATIDA")
        self.assertFalse(bfm._load()["on"])

    def test_loss_limit_switches_off(self):
        self.turn_on(demo_usd=20.0)
        self.assertEqual(bfm.cycle(self.send, "1", klines=candles), "LIMITE DE PERDA")


class Telegram(Base):
    def test_keyboard_and_leverage(self):
        cbs = [b["callback_data"] for row in bfm.keyboard() for b in row]
        self.assertIn("bfm_demo", cbs)
        self.assertIn("bfm_real", cbs)
        bfm.handle("bfm_lev10", self.send, "1", lambda fn: fn())
        self.assertEqual(bfm._load()["leverage"], 10)
        for cb in cbs:
            self.assertIn(cb, bfm.ACTIONS | {"fx"})

    def test_real_asks_confirmation(self):
        bfm.handle("bfm_real", self.send, "1", lambda fn: fn())
        self.assertFalse(bfm._load()["on"])
        self.assertIn("MODO REAL", self.msgs[-1])

    def test_demo_start_opens(self):
        with patch.object(bfm, "klines_5m", candles):
            bfm.handle("bfm_demo", self.send, "1", lambda fn: fn())
        d = bfm._load()
        self.assertTrue(d["on"])
        self.assertEqual(d["start_usd"], 50.0)
        self.assertIsNotNone(d["pos"])


if __name__ == "__main__":
    unittest.main()
