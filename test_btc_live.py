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
        opener = kw.pop("opener", lambda side, price, lev: (opened.append((side, lev)) or
                                                            {"entry": price, "size_usd": 100 * lev, "collateral_usd": 100}))
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
        self.assertEqual((r, opened), ("opened", [("long", 4)]))
        self.assertIn("LONG 4x", msgs[0])
        pos = bl._load()["open"]
        self.assertLess(pos["stop"], pos["entry"])
        bl._save(dict(bl._load(), open=None))
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
        pos = bl._load()["open"]
        cs2 = cs + [{"t": cs[-1]["t"] + 14400, "o": pos["entry"], "h": pos["entry"], "l": pos["stop"] * 0.99,
                     "c": pos["stop"] * 0.99, "v": 1}]
        r, msgs, _ = self.run_cycle(cs2, now + 600)
        self.assertEqual(r, "closed")
        self.assertIn("STOP", msgs[0])
        self.assertLess(bl._load()["history"][-1]["pnl_usd"], 0)
        self.assertIsNone(bl._load()["open"])

    def test_time_limit_closes_after_4_days(self):
        bl.set_enabled(True)
        cs = candles()
        now = self.closed_now(cs)
        self.run_cycle(cs, now)
        pos = bl._load()["open"]
        self.assertEqual(bl.exit_reason(pos, pos["entry"], None, 0, now + 24 * 14400), "time")
        self.assertEqual(bl.exit_reason(pos, pos["entry"], None, 0, now + 3600), "")
        self.assertEqual(bl.exit_reason(pos, pos["entry"], "short", pos["signal_t"] + 14400, now + 3600), "signal")

    def test_pauses_after_total_loss_of_50(self):
        bl.set_enabled(True)
        bl._save(dict(bl._load(), history=[{"pnl_usd": -45.0}]))
        cs = candles()
        now = self.closed_now(cs)
        self.run_cycle(cs, now)
        pos = bl._load()["open"]
        cs2 = cs + [{"t": cs[-1]["t"] + 14400, "o": pos["entry"], "h": pos["entry"], "l": pos["stop"] * 0.98,
                     "c": pos["stop"] * 0.98, "v": 1}]
        r, msgs, _ = self.run_cycle(cs2, now + 600)
        self.assertFalse(bl._load()["enabled"])
        self.assertTrue(any("PAUSADO" in m for m in msgs))

    def test_status_text(self):
        self.assertIn("DESLIGADO", bl.status_text())
        self.assertIn("US$ 100", bl.status_text())


class TelegramRobotTests(Base):
    def press(self, data):
        import telegram_bot as tb
        out = []
        with patch.object(tb, "CHAT", "1"), patch.object(tb, "send", side_effect=lambda chat, text, kb=None: out.append(text)), \
                patch.object(tb, "request_api", return_value={}):
            tb.handle({"callback_query": {"id": "1", "data": data, "message": {"chat": {"id": 1, "type": "private"}}}})
        return out

    def test_buttons(self):
        self.assertIn("5x", self.press("bl_lev5")[0])
        self.assertEqual(bl.leverage(), 5)
        self.assertIn("LIGADO", self.press("bl_on")[0])
        self.assertTrue(bl._load()["enabled"])
        self.press("bl_off")
        self.assertFalse(bl._load()["enabled"])


if __name__ == "__main__":
    unittest.main()
