import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import btc_fullmargin as bfm

T0 = 1_700_000_000 // 300 * 300
NOW = T0 + 900 * 300


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
        self.p = patch.object(bfm, "_path", side_effect=lambda mode="demo": Path(self.dir) / (mode + ".json"))
        self.p.start()
        self.addCleanup(self.p.stop)
        self.msgs = []

    def send(self, chat, text, kb=None):
        self.msgs.append(text)

    def turn_on(self, mode="demo", **kw):
        d = bfm._load(mode)
        d.update(dict(dict(on=True, day=bfm.today(NOW), capital=100.0), **kw))
        bfm._save(d)

    def cyc(self, cs, mode="demo", now=NOW):
        return bfm.cycle(mode, self.send, "1", now=now, klines=lambda: cs)


class ReadChart(Base):
    def test_downtrend_scores_negative(self):
        sc, a, text = bfm.read_chart(candles()[:-1])
        self.assertLessEqual(sc, -4)
        self.assertGreater(a, 0)
        self.assertIn("leitura", text)

    def test_uptrend_scores_positive(self):
        self.assertGreaterEqual(bfm.read_chart(candles(trend=1)[:-1])[0], 4)

    def test_stop_is_far_from_liquidation(self):
        cs = candles()[:-1]
        price = cs[-1]["c"]
        self.assertLessEqual(bfm.stop_distance(cs, price * 0.5, price, 10), price * 0.6 / 10 + 1e-9)


class Manage(unittest.TestCase):
    def pos(self):
        return {"entry": 100.0, "size_usd": 500.0, "collateral_usd": 100.0, "risk": 1.0, "stop": 101.0}

    def test_target(self):
        self.assertEqual(bfm.manage(self.pos(), 98.4)[0], "🎯 ALVO")

    def test_stop(self):
        self.assertEqual(bfm.manage(self.pos(), 101.0)[0], "🛑 STOP")

    def test_breakeven_and_trailing(self):
        p = self.pos()
        why, stop = bfm.manage(p, 99.3)
        self.assertEqual(why, "")
        self.assertLess(stop, 100.0)
        self.assertAlmostEqual(bfm.manage(p, 99.0)[1], 99.5)

    def test_pnl_short_with_fees(self):
        self.assertAlmostEqual(bfm.pnl_usd(self.pos(), 99.0), 5.0 - 0.6)


class Cycle(Base):
    def test_off_does_nothing(self):
        self.assertEqual(self.cyc(candles()), "desligado")

    def test_opens_short_with_the_whole_100(self):
        self.turn_on()
        self.assertEqual(self.cyc(candles(trend=1)), "abriu")       # first one of the day: right away
        d = bfm._load("demo")
        self.assertEqual(d["pos"]["collateral_usd"], 100.0)
        self.assertEqual(d["pos"]["size_usd"], 100.0 * bfm.DEFAULT_LEVERAGE)
        self.assertIn("CONTA DEMO", self.msgs[-1])

    def test_only_shorts_after_the_first(self):
        self.turn_on(trades=1)
        self.assertTrue(self.cyc(candles(trend=1)).startswith("sem sinal"))
        d = bfm._load("demo")
        d["last_bar"] = 0
        bfm._save(d)
        self.assertEqual(self.cyc(candles()), "abriu")

    def test_win_grows_the_account(self):
        self.turn_on()
        cs = candles()
        self.cyc(cs)
        pos = bfm._load("demo")["pos"]
        later = cs[:-1] + [dict(cs[-1], c=pos["entry"] - 2 * pos["risk"])]
        self.assertEqual(self.cyc(later), "fechou")
        d = bfm._load("demo")
        self.assertEqual(d["wins"], 1)
        self.assertGreater(d["capital"], 100.0)

    def test_daily_target_stops_until_tomorrow(self):
        self.turn_on(capital=252.0, trades=3)
        self.assertEqual(self.cyc(candles()), "META DO DIA BATIDA")
        self.assertIn("META DO DIA", self.msgs[-1])
        self.assertEqual(self.cyc(candles()), "parado até amanhã")
        d = bfm._load("demo")
        self.assertTrue(d["on"])
        self.assertIsNone(d["pos"])
        # next day: back to 100 and opens again
        self.assertEqual(self.cyc(candles(), now=NOW + 86400), "abriu")
        d = bfm._load("demo")
        self.assertEqual(d["pos"]["collateral_usd"], 100.0)
        self.assertEqual(d["days"][-1]["result"], 152.0)

    def test_target_closes_the_open_short(self):
        self.turn_on(capital=245.0, trades=1)
        cs = candles()
        price = cs[-1]["c"]
        d = bfm._load("demo")
        d["pos"] = {"entry": price * 1.02, "size_usd": 1000.0, "collateral_usd": 245.0, "risk": price, "stop": price * 2}
        bfm._save(d)
        self.assertEqual(self.cyc(cs), "META DO DIA BATIDA")
        self.assertIsNone(bfm._load("demo")["pos"])

    def test_daily_loss_limit(self):
        self.turn_on(capital=49.0, trades=2)
        self.assertEqual(self.cyc(candles()), "LIMITE DE PERDA DO DIA")

    def test_two_losses_pause(self):
        self.turn_on(trades=1)
        d = bfm._load("demo")
        for _ in range(2):
            d["pos"] = {"entry": 100.0, "size_usd": 250.0, "collateral_usd": 50.0, "risk": 1.0, "stop": 101.0}
            bfm._close(d, 101.0, "🛑 STOP")
        self.assertGreater(d["pause_until"], 0)
        self.assertEqual(d["losses"], 2)

    def test_accounts_are_separate(self):
        self.turn_on("demo")
        self.cyc(candles())
        self.assertIsNotNone(bfm._load("demo")["pos"])
        self.assertIsNone(bfm._load("real")["pos"])
        self.assertFalse(bfm._load("real")["on"])

    def test_real_opens_on_jupiter(self):
        self.turn_on("real")
        calls = []

        class JP:
            @staticmethod
            def ensure_usdc(usd, sol):
                calls.append(("usdc", usd))

            @staticmethod
            def open_position(side, usd, lev, price):
                calls.append((side, usd, lev))
                return {"price": price, "signature": "SIG"}

        with patch.dict("sys.modules", {"jupiter_perps": JP}), patch.object(bfm, "sol_usd", return_value=150.0):
            self.assertEqual(self.cyc(candles(), mode="real"), "abriu")
        self.assertEqual(calls, [("usdc", 100.0), ("short", 100.0, bfm.DEFAULT_LEVERAGE)])
        self.assertIn("solscan.io/tx/SIG", self.msgs[-1])


class Telegram(Base):
    def test_keyboard_callbacks_are_routed(self):
        cbs = [b["callback_data"] for row in bfm.keyboard() for b in row]
        self.assertIn("bfm_on_demo", cbs)
        self.assertIn("bfm_on_real", cbs)
        self.assertIn("wallet_deposit", cbs)
        self.assertIn("wallet_withdraw", cbs)
        for cb in cbs:
            self.assertIn(cb, bfm.ACTIONS | {"wallet_deposit", "wallet_withdraw", "wallet_balance"})

    def test_leverage_applies_to_both(self):
        bfm.handle("bfm_lev10", self.send, "1", lambda fn: fn())
        self.assertEqual(bfm._load("demo")["leverage"], 10)
        self.assertEqual(bfm._load("real")["leverage"], 10)

    def test_real_asks_confirmation(self):
        bfm.handle("bfm_on_real", self.send, "1", lambda fn: fn())
        self.assertFalse(bfm._load("real")["on"])
        self.assertIn("CONTA REAL", self.msgs[-1])

    def test_demo_start_opens(self):
        with patch.object(bfm, "klines_5m", candles):
            bfm.handle("bfm_on_demo", self.send, "1", lambda fn: fn())
        d = bfm._load("demo")
        self.assertTrue(d["on"])
        self.assertEqual(d["capital"], 100.0)
        self.assertIsNotNone(d["pos"])
        self.assertIn("CONTA DEMO", bfm.status_text())


if __name__ == "__main__":
    unittest.main()
