import os
import tempfile
import time
import unittest
from unittest.mock import patch

import copy_trader as ct
import sniper as sn

MINT = "SnipeMintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NOW = 1_800_000_000


def pair(**kw):
    p = {"chainId": "solana", "baseToken": {"address": MINT, "symbol": "SNP"},
         "quoteToken": {"address": sn.SOL_MINT}, "liquidity": {"usd": 120_000},
         "pairCreatedAt": (NOW - 600) * 1000, "info": {"websites": [{"url": "https://x"}]},
         "txns": {"m5": {"buys": 40, "sells": 20}}, "volume": {"m5": 12_000},
         "priceChange": {"m5": 6, "h1": 20}, "fdv": 600_000}
    p.update(kw)
    return p


class FilterTests(unittest.TestCase):
    def test_good_pair_passes(self):
        self.assertEqual(sn.filter_reason(pair(), NOW), "")

    def test_liquidity_window_50k_250k(self):
        self.assertIn("< US$ 50 mil", sn.filter_reason(pair(liquidity={"usd": 49_000}), NOW))
        self.assertIn("> US$ 250 mil", sn.filter_reason(pair(liquidity={"usd": 251_000}), NOW))
        self.assertEqual(sn.filter_reason(pair(liquidity={"usd": 50_000}), NOW), "")

    def test_coin_must_be_older_than_one_minute(self):
        self.assertIn("menos de 1 min", sn.filter_reason(pair(pairCreatedAt=(NOW - 30) * 1000), NOW))
        self.assertEqual(sn.filter_reason(pair(pairCreatedAt=(NOW - 61) * 1000), NOW), "")

    def test_momentum_and_scam_filters(self):
        self.assertIn("sem site", sn.filter_reason(pair(info={}), NOW))
        self.assertIn("mais vendas", sn.filter_reason(pair(txns={"m5": {"buys": 20, "sells": 30}}), NOW))
        self.assertIn("topo", sn.filter_reason(pair(priceChange={"m5": 60, "h1": 90}), NOW))
        self.assertIn("caindo", sn.filter_reason(pair(priceChange={"m5": 5, "h1": -10}), NOW))


class ProtectionTests(unittest.TestCase):
    def test_token_2022_traps_are_rejected(self):
        def rpc(ext):
            return lambda m, p: {"value": {"data": {"parsed": {"type": "mint", "info": {"extensions": ext}}}}}
        fee = [{"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 500}}}]
        self.assertIn("taxa de 5.0%", sn.token_program_reason(MINT, rpc(fee)))
        self.assertIn("tirar as moedas", sn.token_program_reason(MINT, rpc([{"extension": "permanentDelegate"}])))
        self.assertEqual(sn.token_program_reason(MINT, rpc([{"extension": "metadataPointer"}])), "")

    def test_rugcheck_danger_and_unlocked_liquidity(self):
        self.assertIn("RugCheck", sn.rugcheck_reason(MINT, lambda u: {"risks": [{"name": "Freeze", "level": "danger"}]}))
        self.assertIn("rug", sn.rugcheck_reason(MINT, lambda u: {"risks": [], "lpLockedPct": 10}))
        self.assertEqual(sn.rugcheck_reason(MINT, lambda u: {"risks": [{"name": "x", "level": "warn"}], "lpLockedPct": 100}), "")

    def test_rugcheck_unexpected_answer_blocks_buy(self):
        self.assertIn("indisponível", sn.rugcheck_reason(MINT, lambda u: {"message": "rate limited"}))

    def test_rugcheck_down_blocks_buy(self):
        def down(u):
            raise ConnectionError()
        self.assertIn("indisponível", sn.rugcheck_reason(MINT, down))

    def test_honeypot_from_sell_test_blocks(self):
        with patch.object(ct, "safety_reason", return_value="teste de venda devolveu 40%"), \
                patch.object(sn, "token_program_reason") as other:
            self.assertIn("devolveu 40%", sn.safety_reason(MINT))
        other.assert_not_called()


class CycleTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        q = patch.object(ct, "_sol_usd", return_value=100.0)
        q.start()
        self.addCleanup(q.stop)

    def _get(self, pairs):
        return lambda path: [{"chainId": "solana", "tokenAddress": MINT}] if "/tokens/" not in path else pairs

    def test_sim_buys_safe_coin_once_and_sells_on_stop(self):
        sn.set_mode(True, False)
        sent = []
        with patch.object(sn, "safety_reason", return_value=""), \
                patch.object(ct, "_order", side_effect=lambda i, o, a: {"outAmount": "1000" if i == sn.SOL_MINT
                                                                         else str(int(0.2e9))}):   # flat price
            sn.cycle(lambda c, t: sent.append(t), 1, get=self._get([pair()]), now=NOW)
            sn.cycle(lambda c, t: sent.append(t), 1, get=self._get([pair()]), now=NOW + 60)   # not twice
        self.assertEqual(len(sn._load()["positions"]), 1)
        self.assertEqual(sum("COMPROU" in t for t in sent), 1)
        with patch.object(ct, "_order", return_value={"outAmount": str(int(0.15e9))}):     # US$ 15 of 20: -25%
            sn.sim_exits(lambda c, t: sent.append(t), 1)
        self.assertEqual(sn._load()["positions"], {})
        self.assertIn("PREJUÍZO", sent[-1])

    def test_unsafe_coin_is_not_bought(self):
        sn.set_mode(True, False)
        with patch.object(sn, "safety_reason", return_value="criador pode congelar carteiras"), \
                patch.object(sn, "_open_sim") as buy:
            sn.cycle(None, None, get=self._get([pair()]), now=NOW)
        buy.assert_not_called()
        self.assertEqual(sn.LAST_SCAN["rejected"], {"criador pode congelar carteiras": 1})

    def test_real_uses_live_engine_sniper_mode(self):
        sn.set_mode(True, True)
        import live_engine
        import live_trade_state as st
        import wallet_holdings
        with patch.object(sn, "safety_reason", return_value=""), patch.object(live_engine, "ready", return_value=True), \
                patch.object(st, "can_open", return_value=(True, "")), \
                patch.object(wallet_holdings, "can_buy", return_value=(True, "")), \
                patch.object(live_engine, "get_address", return_value="W"), \
                patch.object(live_engine, "_execute_buy") as buy:
            sn.cycle(None, 1, get=self._get([pair()]), now=NOW)
        self.assertEqual(buy.call_args.args[7], "sniper")
        self.assertEqual(buy.call_args.kwargs["usd"], sn.ORDER_USD)


if __name__ == "__main__":
    unittest.main()
