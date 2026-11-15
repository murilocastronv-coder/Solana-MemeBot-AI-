import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import btc_live as bl

T0 = 1_700_000_000 // 14400 * 14400


def candles(n=400, drop=True):
    """Uptrend (above the EMA200) and, at the end, two sharp red candles -> RSI2 < 10 -> LONG."""
    out, p = [], 50000.0
    for i in range(n):
        o = p
        p *= 1.002 if i % 3 else 0.999
        if drop and i >= n - 2:
            p = o * 0.985
        out.append({"t": T0 + i * 14400, "o": o, "h": max(o, p) * 1.001, "l": min(o, p) * 0.999, "c": p, "v": 1})
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.p = patch.object(bl, "_path", return_value=Path(self.dir) / "btc_live.json")
        self.p.start()
        self.addCleanup(self.p.stop)

    def run_cycle(self, cs, now, **kw):
        msgs = []
        opened = []
        opener = kw.pop("opener", lambda side, price, lev, usd: (opened.append((side, lev, usd)) or
                                                                 {"entry": price, "size_usd": usd * lev, "collateral_usd": usd}))
        r = bl.cycle(lambda chat, text: msgs.append(text), "1", cs=cs, now=now, opener=opener,
                     closer=kw.pop("closer", lambda side, price: {}))
        return r, msgs, opened


class BtcLiveTests(Base):
    def closed_now(self, cs):
        return cs[-1]["t"] + 14400 + 60          # one minute after the last candle closed

    def test_signal_on_synthetic_dip(self):
        sig, atr_v, t = bl.last_signal(candles())
        self.assertEqual(sig, "long")
        self.assertGreater(atr_v, 0)

    def test_off_by_default_does_nothing(self):
        cs = candles()
        r, msgs, opened = self.run_cycle(cs, self.closed_now(cs))
        self.assertEqual((r, opened), ("", []))

    def test_opens_once_per_signal_with_chosen_leverage(self):
        bl.set_enabled(True)
        bl.set_leverage(4)
        cs = candles()
        now = self.closed_now(cs)
        r, msgs, opened = self.run_cycle(cs, now)
        self.assertEqual((r, opened), ("opened", [("long", 4, 100.0)]))
        self.assertIn("LONG 4x", msgs[0])
        pos = bl._load()["positions"][0]
        self.assertLess(pos["stop"], pos["entry"])
        bl._save(dict(bl._load(), positions=[]))
        self.assertEqual(self.run_cycle(cs, now + 60)[0], "")     # same signal: no second entry

    def test_failed_open_is_not_retried_every_minute(self):
        bl.set_enabled(True)
        cs = candles()
        now = self.closed_now(cs)
        def boom(*a):
            raise RuntimeError("USDC insuficiente")
        self.assertEqual(self.run_cycle(cs, now, opener=boom)[0], "open_failed")
        self.assertEqual(self.run_cycle(cs, now + 60)[0], "")

    def test_late_signal_after_restart_is_skipped(self):
        bl.set_enabled(True)
        cs = candles()
        self.assertEqual(self.run_cycle(cs, self.closed_now(cs) + 2 * 3600)[0], "")

    def test_leverage_limited_to_5(self):
        with self.assertRaises(ValueError):
            bl.set_leverage(10)
        bl._save(dict(bl._load(), leverage=20))
        self.assertEqual(bl.leverage(), 5)

    def test_stop_closes_and_records_loss(self):
        bl.set_enabled(True)
        cs = candles()
        now = self.closed_now(cs)
        self.run_cycle(cs, now)
        pos = bl._load()["positions"][0]
        cs2 = cs + [{"t": cs[-1]["t"] + 14400, "o": pos["entry"], "h": pos["entry"], "l": pos["stop"] * 0.99,
                     "c": pos["stop"] * 0.99, "v": 1}]
        r, msgs, _ = self.run_cycle(cs2, now + 600)
        self.assertEqual(r, "closed")
        self.assertIn("STOP", msgs[0])
        self.assertLess(bl._load()["history"][-1]["pnl_usd"], 0)
        self.assertEqual(bl._load()["positions"], [])

    def test_time_limit_closes_after_4_days(self):
        bl.set_enabled(True)
        cs = candles()
        now = self.closed_now(cs)
        self.run_cycle(cs, now)
        pos = bl._load()["positions"][0]
        self.assertEqual(bl.exit_reason(pos, pos["entry"], None, 0, now + 24 * 14400), "time")
        self.assertEqual(bl.exit_reason(pos, pos["entry"], None, 0, now + 3600), "")
        self.assertEqual(bl.exit_reason(pos, pos["entry"], "short", pos["signal_t"] + 14400, now + 3600), "signal")

    def test_pauses_after_total_loss_of_50(self):
        bl.set_enabled(True)
        bl._save(dict(bl._load(), on_since=0, history=[{"pnl_usd": -45.0, "closed": 1}]))
        cs = candles()
        now = self.closed_now(cs)
        self.run_cycle(cs, now)
        pos = bl._load()["positions"][0]
        cs2 = cs + [{"t": cs[-1]["t"] + 14400, "o": pos["entry"], "h": pos["entry"], "l": pos["stop"] * 0.98,
                     "c": pos["stop"] * 0.98, "v": 1}]
        r, msgs, _ = self.run_cycle(cs2, now + 600)
        self.assertFalse(bl._load()["enabled"])
        self.assertTrue(any("PAUSADO" in m for m in msgs))

    def test_at_most_two_positions_and_chosen_amount(self):
        bl.set_enabled(True)
        bl.set_capital(30)
        cs = candles()
        now = self.closed_now(cs)
        self.assertEqual(self.run_cycle(cs, now)[2], [("long", 3, 30.0)])
        for k in (1, 2):                                   # two more dips -> new LONG signals
            last = cs[-1]
            cs = cs + [{"t": last["t"] + 14400, "o": last["c"], "h": last["c"], "l": last["c"] * 0.989,
                        "c": last["c"] * 0.99, "v": 1}]
            self.assertEqual(bl.last_signal(cs)[0], "long")
            # keep the stops out of the way
            bl._save(dict(bl._load(), positions=[dict(p, stop=1) for p in bl._load()["positions"]]))
            r, msgs, opened = self.run_cycle(cs, self.closed_now(cs))
            self.assertEqual((r, len(opened)), ("opened", 1) if k == 1 else ("", 0))
        self.assertEqual(len(bl._load()["positions"]), 2)

    def test_amount_limits(self):
        self.assertEqual(bl.set_capital("75,5"), 75.5)
        self.assertEqual(bl.max_loss(), 37.75)
        with self.assertRaises(ValueError):
            bl.set_capital(5)

    def test_old_single_position_is_migrated(self):
        bl._save({"open": {"side": "long", "entry": 1, "stop": 0.5, "at": 0, "signal_t": 0, "size_usd": 3,
                           "collateral_usd": 1, "lev": 3}})
        self.assertEqual(len(bl._load()["positions"]), 1)

    def test_status_text(self):
        self.assertIn("DESLIGADO", bl.status_text())
        self.assertIn("US$ 100", bl.status_text())


if __name__ == "__main__":
    unittest.main()
