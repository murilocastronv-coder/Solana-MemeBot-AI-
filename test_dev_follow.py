import pathlib
import tempfile
import unittest
from unittest import mock

import dev_bot as db
import dev_follow as df

T0 = 1_800_000_000


def candles(prices, start=T0, step=60):
    # (ts, open, high, low, close)
    return [(start + i * step, p, p * 1.01, p * 0.99, p) for i, p in enumerate(prices)]


class SimulateTest(unittest.TestCase):
    def test_mirror_uses_price_after_delay_and_dev_sells(self):
        c = candles([1.0, 2.0, 2.0, 3.0, 3.0, 4.0])
        h = {"opened": T0, "closed": T0 + 300, "sells": [[T0 + 180, 0.5], [T0 + 300, 1.0]]}
        r = df.simulate(h, c, 60)
        # enters at 2.0 (1 min later), half at 3.0 (+50%), half at 4.0 (+100%) -> +75% minus costs
        self.assertAlmostEqual(r["mirror"][0], 0.75 - 2 * db.COST, places=3)

    def test_stop_hits_before_dev_sells(self):
        c = candles([1.0, 1.0, 0.4, 0.4, 2.0])
        h = {"opened": T0, "closed": T0 + 240}
        r = df.simulate(h, c, 60)
        self.assertAlmostEqual(r["sl30"][0], -0.30 - 2 * db.COST, places=3)
        self.assertGreater(r["mirror"][0], 0.9)

    def test_runup_since_dev_is_skipped(self):
        c = candles([1.0, 1.5, 1.5])
        self.assertEqual(df.simulate({"opened": T0, "closed": T0 + 120, "entry_usd": 1.0}, c, 60), {"skip": "runup"})

    def test_no_price_before_pool_existed(self):
        self.assertIsNone(df.simulate({"opened": T0 - 99999, "closed": T0 + 60}, candles([1, 1]), 60))


class FillAndRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(db, "_path", return_value=pathlib.Path(self.tmp.name) / "d.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def _devs(self):
        d = db._load()
        good = [{"opened": T0 + i * 3600, "closed": T0 + i * 3600 + 300, "mint": "G" + str(i) + "pump", "result_pct": 0.5,
                 "pnl_usd": 5} for i in range(50)]
        bad = [{"opened": T0 + i * 3600 + 60, "closed": T0 + i * 3600 + 360, "mint": "B" + str(i) + "pump", "result_pct": 0.5,
                "pnl_usd": 5} for i in range(50)]
        d["devs"] = {"GOOD": {"hist": good, "tier": "MONITORADO", "rated": 1}, "BAD": {"hist": bad, "tier": "MONITORADO", "rated": 1}}
        db._save(d)

    def fetch(self, url):
        if "/tokens/" in url:
            mint = url.split("/tokens/")[1].split("/")[0]
            return {"data": [{"attributes": {"address": "P" + mint}}]}
        pool = url.split("/pools/")[1].split("/")[0]
        before = int(url.split("before_timestamp=")[1].split("&")[0])
        i = int(pool[2:].split("pump")[0])
        start = T0 + i * 3600 + (60 if pool[1] == "B" else 0)
        prices = [1.0, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0] if pool[1] == "G" else [1.0, 1.0, 0.8, 0.6, 0.5, 0.4, 0.3]
        rows = [[start + k * 60, p, p, p, p, 0] for k, p in enumerate(prices) if start + k * 60 < before]
        return {"data": {"attributes": {"ohlcv_list": rows}}}

    def test_good_dev_wins_out_of_sample_and_can_be_applied(self):
        self._devs()
        self.assertEqual(df.fill(fetch=self.fetch), 100)
        info, res = df.run()
        self.assertEqual(info["replayed"], 100)
        b = df.best(res)
        self.assertTrue(b[0][1].startswith("f_"))         # picks the dev whose copies made money
        self.assertTrue(df.winner_ok(b[1]))
        txt = df.report()
        self.assertIn("Deu lucro fora da amostra", txt)
        r = df.apply_winner()
        self.assertTrue(r)
        d = db._load()
        db._RULE["r"] = d["follow_rule"]
        try:
            self.assertTrue(db.approved(d["devs"]["GOOD"]))
            self.assertFalse(db.approved(d["devs"]["BAD"]))
        finally:
            db._RULE["r"] = None

    def test_no_edge_says_so(self):
        d = db._load()
        d["devs"] = {"X": {"hist": [{"opened": T0 + i * 3600, "closed": T0 + i * 3600 + 300, "mint": "B" + str(i) + "pump",
                                     "result_pct": 0.1, "pnl_usd": 1, "f": {n: [-0.1, T0 + i * 3600 + 300] for n, _, _ in df.EXITS}}
                                    for i in range(40)]}}
        db._save(d)
        txt = df.report()
        self.assertIn("Nenhuma combinação deu lucro", txt)
        self.assertIsNone(df.apply_winner())

    def test_rule_stop_in_live_exits(self):
        db._RULE["r"] = {"rule": "f_mean", "rule_name": "x", "exit": "sl30", "sl": 0.3, "tp": 0}
        try:
            d = db._load()
            d["positions"]["M"] = {"symbol": "M", "dev": "D", "entry": 1.0, "last": 1.0, "peak": 1.0, "qty": 10, "cost": 10,
                                   "at": T0, "tp1": False, "reason": "t", "realized": 0.0, "real": False}
            db._save(d)
            out = db.exits({"M": 0.65}, T0 + 60)
            self.assertIn("stop −30%", out[0][1])
        finally:
            db._RULE["r"] = None


if __name__ == "__main__":
    unittest.main()
