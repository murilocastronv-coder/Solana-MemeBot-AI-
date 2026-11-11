import unittest
from unittest.mock import patch

import wallet_holdings as wh

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def fake_rpc(accounts):
    """accounts: list of (program, mint, raw amount)."""
    def rpc(method, params):
        program = params[1]["programId"]
        return {"value": [{"account": {"data": {"parsed": {"info": {"mint": m, "tokenAmount": {"amount": str(a)}}}}}}
                          for p, m, a in accounts if p == program]}
    return rpc


TOKEN, T2022 = wh.TOKEN_PROGRAMS


class HoldingsTests(unittest.TestCase):
    def setUp(self):
        wh._cache.update(at=0.0, wallet=None, coins=None)

    def test_counts_both_token_programs_and_skips_stablecoins_and_dust(self):
        rpc = fake_rpc([(TOKEN, "A", 100), (T2022, "B", 50), (TOKEN, USDC, 10**9), (TOKEN, "DUST", 5),
                        (TOKEN, "RUG", 7), (TOKEN, "ZERO", 0)])
        values = {"A": 30.0, "B": 12.0, "DUST": 0.2, "RUG": None}
        coins = wh.coins_held("W", rpc, valuer=lambda m, a: values[m], fresh=True)
        self.assertEqual(set(coins), {"A", "B"})

    def test_blocks_fourth_coin_and_same_coin(self):
        rpc = fake_rpc([(TOKEN, "A", 1), (TOKEN, "B", 1), (T2022, "C", 1)])
        ok, why = wh.can_buy("D", "W", limit=3, rpc=rpc, valuer=lambda m, a: 20.0)
        self.assertFalse(ok)
        self.assertIn("já tem 3 moedas", why)
        rpc2 = fake_rpc([(TOKEN, "A", 1)])
        self.assertIn("já tem essa moeda", wh.can_buy("A", "W", limit=3, rpc=rpc2, valuer=lambda m, a: 20.0)[1])
        self.assertEqual(wh.can_buy("D", "W", limit=3, rpc=rpc2, valuer=lambda m, a: 20.0), (True, 1))

    def test_unreadable_wallet_blocks_buy(self):
        def broken(method, params):
            raise RuntimeError("down")
        ok, why = wh.can_buy("D", "W", limit=3, rpc=broken)
        self.assertFalse(ok)
        self.assertIn("não consegui ler", why)

    def test_status_line_uses_cache_only(self):
        import time
        self.assertIn("conferindo", wh.status_line("W"))
        wh._cache.update(at=time.monotonic(), wallet="W", coins={"AAAAxyz": 31.0})
        with patch.object(wh, "raw_balances", side_effect=AssertionError("no network from the button")):
            self.assertIn("1/12", wh.status_line("W"))
