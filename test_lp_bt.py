import unittest

import lp_bt as lp


class LpTest(unittest.TestCase):
    def test_math_and_simulation(self):
        pa, pb = 90.0, 110.0
        self.assertAlmostEqual(lp._per_l(100.0, pa, pb) * (60 / lp._per_l(100.0, pa, pb)), 60.0)
        flat = [(1_700_000_000 + 3600 * i, 100.0) for i in range(24 * 30)]
        s = lp.simulate(flat, 0.10, 0.30)
        self.assertAlmostEqual(s["end"], 60.0, places=6)
        self.assertGreater(s["fees"], 0)
        self.assertEqual(s["rebal"], 0)
        crash = flat[:10] + [(1_700_000_000 + 3600 * (10 + i), 60.0) for i in range(100)]
        c = lp.simulate(crash, 0.10, 0.30)
        self.assertLess(c["net"], 0)
        self.assertGreaterEqual(c["rebal"], 1)
        self.assertGreater(lp.efficiency(0.05), lp.efficiency(0.20))
        txt = lp.report(flat, 0.30, "teste")
        self.assertIn("Melhor faixa", txt)

    def test_pool_apr(self):
        data = {"pairs": [{"chainId": "solana", "dexId": "orca", "baseToken": {"address": lp.SOL}, "quoteToken": {"address": lp.USDC},
                           "liquidity": {"usd": 10_000_000}, "volume": {"h24": 50_000_000}}]}
        apr, desc = lp.pool_fee_apr(get=lambda u: data)
        self.assertAlmostEqual(apr, 50e6 * lp.FEE / 10e6 * 365)


if __name__ == "__main__":
    unittest.main()
