import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import btc_dip_live as fd


def buyer(price):
    def buy(usd, lev, p):
        if lev > 1:
            return {"usd": usd, "size": usd * lev, "price": price, "signature": "S"}
        return {"btc_raw": int(usd / price * 1e8), "usd": usd, "price": price, "signature": "S"}
    return buy


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.object(fd, "_path", return_value=Path(d) / "dip.json")
        p.start()
        self.addCleanup(p.stop)
        self.msgs = []

    def tick(self, price, **kw):
        kw.setdefault("buyer", buyer(price))
        kw.setdefault("top_fn", lambda: 100000.0)
        kw.setdefault("seller", lambda d, p: {"usd": fd.invested(d) + fd.open_pnl(d, price), "signature": "S2"})
        kw.setdefault("size_fn", lambda: 1000.0)
        return fd.cycle(lambda c, t: self.msgs.append(t), "1", price=price, now=kw.pop("now", 1000), **kw)


class DipLiveTests(Base):
    def test_off_does_nothing(self):
        self.assertEqual(self.tick(50000), "")
        self.assertEqual(fd._load()["buys"], [])

    def test_full_cycle(self):
        fd.set_enabled(True)
        self.assertEqual(self.tick(90000), "")                 # top 100k from the last 30 days, not -15% yet
        self.assertEqual(fd._load()["top"], 100000.0)
        self.assertEqual(self.tick(85000), "bought")
        self.assertIn("1/3", self.msgs[-1])
        self.assertEqual(self.tick(80000), "")
        self.assertEqual(self.tick(72250), "bought")
        self.assertEqual(self.tick(61412), "bought")
        self.assertEqual(self.tick(50000), "")                # 3 buys max
        self.assertEqual(len(fd._load()["buys"]), 3)
        avg = fd.avg_price(fd._load())
        self.assertEqual(self.tick(avg * 1.069), "")
        self.assertEqual(self.tick(avg * 1.07 + 1), "sold")
        d = fd._load()
        self.assertEqual(d["buys"], [])
        self.assertGreater(d["history"][-1]["pnl_usd"], 0)
        self.assertAlmostEqual(d["top"], avg * 1.07 + 1)
        self.assertIn("LUCRO", self.msgs[-1])

    def test_top_rises_while_waiting(self):
        fd.set_enabled(True)
        self.tick(90000)
        self.tick(110000)
        self.assertEqual(fd._load()["top"], 110000)
        self.assertEqual(self.tick(95000), "")                # 110k * 0.85 = 93.5k: not yet
        self.assertEqual(self.tick(93000), "bought")

    def test_failed_buy_waits_15_min(self):
        fd.set_enabled(True)
        def boom(usd, lev, p):
            raise RuntimeError("SOL insuficiente")
        self.assertEqual(self.tick(80000, buyer=boom), "buy_failed")
        self.assertEqual(self.tick(80000, now=1060), "")
        self.assertEqual(self.tick(80000, now=1000 + 16 * 60), "bought")

    def test_switched_off_still_sells_at_tp(self):
        fd.set_enabled(True)
        self.tick(80000)
        fd.set_enabled(False)
        self.assertEqual(self.tick(70000), "")                # no new buys when off
        self.assertEqual(self.tick(90000), "sold")

    def test_amount(self):
        self.assertEqual(fd.set_usd("75,5"), 75.5)
        with self.assertRaises(ValueError):
            fd.set_usd(5000)
        fd.set_enabled(True)
        self.tick(80000)
        self.assertEqual(fd._load()["buys"][0]["usd"], 75.5)

    def test_leveraged_cycle_and_liquidation_price(self):
        fd.set_lev(5)
        fd.set_enabled(True)
        self.assertEqual(self.tick(85000), "bought")
        self.assertIn("LONG 5x", self.msgs[-1])
        d = fd._load()
        self.assertEqual(fd.position_size(d), 500)
        self.assertAlmostEqual(fd.liq_price(d), 85000 * (1 - 0.9 / 5), delta=1)
        fd.set_lev(1)                                         # applies from the next cycle
        self.assertEqual(fd.cycle_lev(fd._load()), 5)
        self.assertEqual(self.tick(72250, now=1000 + 600), "bought")
        self.assertEqual(fd._load()["buys"][1]["lev"], 5)
        avg = fd.avg_price(fd._load())
        self.assertEqual(self.tick(avg * 1.071, now=1000 + 1200), "sold")
        self.assertGreater(fd._load()["history"][-1]["pnl_usd"], 5 * 200 * 0.06)
        self.assertEqual(fd.cycle_lev(fd._load()), 1)

    def test_liquidation_detected_on_chain(self):
        fd.set_lev(3)
        fd.set_enabled(True)
        self.tick(85000)
        self.assertEqual(self.tick(60000, now=1000 + 600, size_fn=lambda: 0.0), "liquidated")
        d = fd._load()
        self.assertEqual(d["buys"], [])
        self.assertEqual(d["history"][-1]["pnl_usd"], -100)
        self.assertIn("LIQUIDADO", self.msgs[-1])

    def test_leverage_limited_to_5(self):
        with self.assertRaises(ValueError):
            fd.set_lev(10)

    def test_status(self):
        self.assertIn("DESLIGADO", fd.status_text())


if __name__ == "__main__":
    unittest.main()
