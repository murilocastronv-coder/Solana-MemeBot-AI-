import unittest

import funding_bt as fb


class FundingTest(unittest.TestCase):
    def test_rate_and_simulation(self):
        # fundingRate 1e9 precision, price TWAP 1e6: 0.0001 per hour of a 100 USD coin
        self.assertAlmostEqual(fb._rate({"fundingRate": str(0.01 * 1e9), "oraclePriceTwap": str(100 * 1e6)}), 0.0001)
        rows = [(1_700_000_000 + 3600 * i, 0.0001) for i in range(24 * 30)]           # 30 days, +0.01%/h
        s = fb.simulate(rows, 30.0, "always")
        self.assertAlmostEqual(s["funding"], 30 * 0.0001 * 720, places=6)
        self.assertAlmostEqual(s["fees"], 30 * fb.COST_SIDE * 4, places=6)
        neg = [(t, -abs(r)) for t, r in rows]
        self.assertLess(fb.simulate(neg, 30.0, "always")["net"], 0)
        self.assertGreater(fb.simulate(neg, 30.0, "switch")["net"], fb.simulate(neg, 30.0, "always")["net"])
        txt = fb.report({"SOL-PERP": (rows, "teste"), "BTC-PERP": ([], "sem dados")})
        self.assertIn("Melhor: SOL-PERP", txt)

    def test_api_parse(self):
        data = {"fundingRates": [{"ts": "1700000000", "fundingRate": "1000000", "oraclePriceTwap": "100000000"}]}
        self.assertEqual(fb.from_api("SOL-PERP", get=lambda u: data), [(1700000000, 1e-5)])


if __name__ == "__main__":
    unittest.main()


class HyperliquidTest(unittest.TestCase):
    def test_paginates_and_falls_back(self):
        calls = []

        def post(body):
            calls.append(body["startTime"])
            if len(calls) == 1:
                return [{"time": 1_700_000_000_000 + 3_600_000 * i, "fundingRate": "0.0001"} for i in range(500)]
            if len(calls) == 2:
                return [{"time": 1_700_000_000_000 + 3_600_000 * (500 + i), "fundingRate": "-0.00005"} for i in range(10)]
            return []

        def boom(*a):
            raise ConnectionError()
        rows, src = fb.history("SOL-PERP", get=boom, get_text=boom, post=post)
        self.assertEqual(len(rows), 510)
        self.assertIn("Hyperliquid", src)
        self.assertEqual(len(calls), 2)
