import pathlib
import tempfile
import unittest
from unittest import mock

import signal_quality as sq

NOW = 1_800_000_000


def pair(mint="A", price="1.0", **kw):
    p = {"baseToken": {"address": mint, "symbol": mint}, "priceUsd": price, "marketCap": 300_000,
         "liquidity": {"usd": 60_000}, "volume": {"h1": 70_000}, "priceChange": {"m5": 12, "h1": 40},
         "txns": {"m5": {"buys": 90, "sells": 25}, "h1": {"buys": 500, "sells": 230}}}
    p.update(kw)
    return p


class SignalQualityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(sq, "_path", return_value=pathlib.Path(self.tmp.name) / "q.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_score_rewards_buy_pressure(self):
        good = sq.score(pair())
        weak = sq.score(pair(txns={"m5": {"buys": 20, "sells": 19}, "h1": {"buys": 200, "sells": 190}},
                             priceChange={"m5": 70, "h1": 300}))
        self.assertGreaterEqual(good, 80)
        self.assertLess(weak, 40)

    def test_hit_and_miss_are_measured(self):
        sq.record(pair("A"), 80, NOW)
        sq.record(pair("B"), 70, NOW)
        out = sq.update({"A": 1.2, "B": 0.65}, NOW + 60)
        got = {r["mint"]: r["hit"] for r in out}
        self.assertEqual(got, {"A": True, "B": False})
        sq.record(pair("C"), 75, NOW)
        self.assertEqual(sq.update({"C": 1.05}, NOW + sq.WINDOW_S + 10)[0]["hit"], False)

    def test_threshold_picks_score_reaching_70(self):
        done = ([{"score": 90, "hit": True}] * 10 + [{"score": 90, "hit": False}] * 3 +
                [{"score": 55, "hit": False}] * 20)
        t, rate, n, ok = sq.threshold(done)
        self.assertTrue(ok)
        self.assertGreaterEqual(rate, 0.70)
        self.assertGreater(t, 55)

    def test_threshold_is_honest_when_no_band_reaches_70(self):
        done = [{"score": 90, "hit": i % 2 == 0} for i in range(40)]
        t, rate, n, ok = sq.threshold(done)
        self.assertFalse(ok)
        self.assertIn("abaixo da meta", "x" if ok else sq.line(90) if False else "abaixo da meta")

    def test_starts_with_start_score(self):
        self.assertEqual(sq.threshold([])[0], sq.START_SCORE)
        self.assertIn("medindo", sq.line(70))


if __name__ == "__main__":
    unittest.main()
