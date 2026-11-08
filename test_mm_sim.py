import os
import tempfile
import unittest
from unittest.mock import patch

import mm_sim as m


class MarketMakingSimTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start(); self.addCleanup(p.stop)
        m._prices.clear(); m._reprices.clear()
        self.t = 1_700_000_000

    def tick(self, mid, dt=10):
        self.t += dt
        return m.tick(mid=mid, now=self.t)

    def test_sizes_book_from_wallet_value(self):
        self.assertEqual(m.start(mid=150, wallet_value=500), 50)       # capped at US$ 50
        d = m._load()
        self.assertAlmostEqual(d["usdc"], 20); self.assertAlmostEqual(d["sol"] * 150, 20)
        self.assertAlmostEqual(d["fee_budget_usd"], 5); self.assertAlmostEqual(d["reserve_usd"], 5)
        self.assertEqual(m.start(mid=150, wallet_value=40), 40)        # smaller wallet -> smaller book
        with self.assertRaises(RuntimeError):
            m.start(mid=150, wallet_value=10)

    def test_four_orders_around_mid(self):
        m.start(mid=150, wallet_value=50)
        self.tick(150)
        orders = m._load()["orders"]
        self.assertEqual(sorted(o["side"] for o in orders), ["buy", "buy", "sell", "sell"])
        best_bid = max(o["price"] for o in orders if o["side"] == "buy")
        best_ask = min(o["price"] for o in orders if o["side"] == "sell")
        self.assertAlmostEqual(best_bid, 150 * (1 - 0.002), places=2)  # 0.4% total spread when calm
        self.assertAlmostEqual(best_ask, 150 * (1 + 0.002), places=2)
        self.assertTrue(all(o["usd"] >= m.MIN_ORDER_USD for o in orders))

    def test_fill_only_when_price_trades_through_and_partial_on_touch(self):
        m.start(mid=150, wallet_value=50)
        self.tick(150)
        bid = max(o["price"] for o in m._load()["orders"] if o["side"] == "buy")
        self.assertEqual(self.tick(149.9, dt=1), [])                    # above the bid: no fill
        touch = self.tick(bid, dt=1)
        self.assertTrue(touch and touch[0]["partial"])
        through = self.tick(bid * 0.999, dt=1)
        self.assertTrue(through and not through[0]["partial"])

    def test_reprice_costs_network_fees(self):
        m.start(mid=150, wallet_value=50)
        self.tick(150)
        fees_after_first = m._load()["fees_usd"]
        self.assertAlmostEqual(fees_after_first, 4 * m.TX_FEE_USD)      # 4 places
        self.tick(150.25)                                               # moved 0.17%, no fill -> cancel 4 + place 4
        self.assertAlmostEqual(m._load()["fees_usd"], fees_after_first + 8 * m.TX_FEE_USD)

    def test_daily_loss_limit_pauses(self):
        m.start(mid=150, wallet_value=50)
        self.tick(150)
        for price in (149.5, 149, 148.5, 148, 147.6, 147.2, 146.8, 146.5, 146.2, 146):
            self.tick(price, dt=11)
        d = m._load()
        self.assertTrue(d["paused"])
        self.assertEqual(d["orders"], [])

    def test_sharp_drop_and_volatility_pause(self):
        m.start(mid=150, wallet_value=50)
        self.tick(150)
        self.tick(160, dt=2); self.tick(145, dt=2); self.tick(162, dt=2); self.tick(144, dt=2); self.tick(161, dt=2)
        self.assertIn(m._load()["paused"], ("volatilidade alta", "queda brusca",
                                           "limite de perda diária (US$ 1.00) atingido"))

    def test_restart_keeps_orders_without_duplicates(self):
        m.start(mid=150, wallet_value=50)
        self.tick(150)
        before = m._load()["orders"]
        m._prices.clear(); m._reprices.clear()                          # process restart
        self.tick(150.01, dt=3)                                         # not due: orders untouched
        self.assertEqual(m._load()["orders"], before)
        self.assertEqual(len(m._load()["orders"]), 4)

    def test_pnl_counts_inventory_not_just_spread(self):
        m.start(mid=150, wallet_value=50)
        self.tick(150)
        for price in (149.6, 149.0, 148.6):
            self.tick(price, dt=11)
        text = m.status_text(mid=148.6)
        self.assertIn("P/L líquido", text)
        self.assertIn("-", text.split("P/L líquido")[1].split("USD")[0])  # buying into a fall shows a loss


if __name__ == "__main__":
    unittest.main()
