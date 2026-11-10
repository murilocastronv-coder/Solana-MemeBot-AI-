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
         "pairCreatedAt": (NOW - 7200) * 1000, "info": {"websites": [{"url": "https://x"}]},
         "txns": {"m5": {"buys": 40, "sells": 20}}, "volume": {"m5": 12_000},
         "priceChange": {"m5": 6, "h1": 20}, "fdv": 600_000}
    p.update(kw)
    return p


class FilterTests(unittest.TestCase):
    def test_good_pair_passes(self):
        self.assertEqual(sn.filter_reason(pair(), NOW), "")

    def test_liquidity_window_10k_250k(self):
        self.assertIn("< US$ 10 mil", sn.filter_reason(pair(liquidity={"usd": 9_999}), NOW))
        self.assertEqual(sn.filter_reason(pair(liquidity={"usd": 10_000}, fdv=100_000), NOW), "")
        self.assertIn("> US$ 250 mil", sn.filter_reason(pair(liquidity={"usd": 251_000}), NOW))
        self.assertEqual(sn.filter_reason(pair(liquidity={"usd": 50_000}), NOW), "")       # still fine

    def test_coin_must_be_at_least_one_hour_old(self):
        self.assertIn("menos de 1 h", sn.filter_reason(pair(pairCreatedAt=(NOW - 3599) * 1000), NOW))
        self.assertEqual(sn.filter_reason(pair(pairCreatedAt=(NOW - 3601) * 1000), NOW), "")

    def test_coin_at_most_48_hours_old(self):
        self.assertEqual(sn.filter_reason(pair(pairCreatedAt=(NOW - 47 * 3600) * 1000), NOW), "")
        self.assertIn("mais de 48 h", sn.filter_reason(pair(pairCreatedAt=(NOW - 49 * 3600) * 1000), NOW))

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


class PumpFunTests(unittest.TestCase):
    def test_pump_sources_are_read(self):
        def fetch(url):
            if "pump.fun" in url:
                return [{"mint": "PumpMint1", "twitter": "https://x.com/a"}, {"mint": "PumpMint2"}]
            return {"data": [{"relationships": {"base_token": {"data": {"id": "solana_GeckoMint"}}}}]}
        mints, socials = sn.pump_mints(fetch)
        self.assertEqual(set(mints), {"PumpMint1", "PumpMint2", "GeckoMint"})
        self.assertEqual(socials, {"PumpMint1": True, "PumpMint2": False})

    def test_pump_sources_down_keep_dexscreener(self):
        def down(url):
            raise ConnectionError()
        self.assertEqual(sn.pump_mints(down), ([], {}))

    def test_pump_coin_goes_through_same_filters_and_socials_from_pump(self):
        p = pair(dexId="pumpswap", info={})
        get = lambda path: [] if "/tokens/" not in path else [p]
        found = sn.candidate_pairs(get, pump=([MINT], {MINT: True}))
        self.assertEqual(len(found), 1)
        self.assertEqual(sn.filter_reason(found[0], NOW), "")                 # socials registered on pump.fun
        self.assertIn("sem site", sn.filter_reason(sn.candidate_pairs(get, pump=([MINT], {}))[0], NOW))

    def test_launch_curve_skips_lp_lock_but_not_danger(self):
        unlocked = lambda u: {"risks": [], "lpLockedPct": 0}
        self.assertIn("rug", sn.rugcheck_reason(MINT, unlocked))               # normal pool: LP must be locked
        self.assertEqual(sn.rugcheck_reason(MINT, unlocked, curve=True), "")   # pump.fun curve: can't be pulled
        danger = lambda u: {"risks": [{"name": "Mutable metadata", "level": "danger"}], "lpLockedPct": 0}
        self.assertIn("RugCheck", sn.rugcheck_reason(MINT, danger, curve=True))

    def test_cycle_marks_curve_coins(self):
        sn_set = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(tempfile.mkdtemp(), "w.json")})
        sn_set.start()
        self.addCleanup(sn_set.stop)
        sn.set_mode(True, False)
        get = lambda path: [] if "/tokens/" not in path else [pair(dexId="pumpfun")]
        with patch.object(sn, "safety_reason", return_value="x") as safety:
            sn.cycle(None, None, get=get, now=NOW, pump=([MINT], {}))
        self.assertTrue(safety.call_args.kwargs["curve"])


class StopLossTests(unittest.TestCase):
    def test_sniper_stop_is_40_percent(self):
        from live_exit_monitor import position_signal
        pos = {"mode": "sniper", "opened_at": time.time()}
        self.assertEqual(position_signal(pos, 100, 61)["action"], "hold")      # -39%: holds
        sig = position_signal(pos, 100, 59)                                    # -41%: sells
        self.assertEqual((sig["action"], sig["reason"]), ("sell", "stop_loss"))


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
                                                                         else str(int(0.38e9))}):  # flat price (US$ 38)
            sn.cycle(lambda c, t: sent.append(t), 1, get=self._get([pair()]), now=NOW)
            sn.cycle(lambda c, t: sent.append(t), 1, get=self._get([pair()]), now=NOW + 60)   # not twice
        self.assertEqual(len(sn._load()["positions"]), 1)
        self.assertEqual(sum("COMPROU" in t for t in sent), 1)
        with patch.object(ct, "_order", return_value={"outAmount": str(int(0.2e9))}):      # US$ 20 of 38: -47%
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
                patch.object(ct, "wallet_usd", return_value=100.0), \
                patch("live_order_bridge.MAX_ORDER_USD", 38.0), \
                patch.object(live_engine, "_execute_buy") as buy:
            sn.cycle(None, 1, get=self._get([pair()]), now=NOW)
        self.assertEqual(buy.call_args.args[7], "sniper")
        self.assertEqual(buy.call_args.kwargs["usd"], 38)

    def test_real_never_buys_less_than_38(self):
        sn.set_mode(True, True)
        import live_engine
        import live_trade_state as st
        import wallet_holdings
        sent = []
        common = [patch.object(sn, "safety_reason", return_value=""), patch.object(live_engine, "ready", return_value=True),
                  patch.object(st, "can_open", return_value=(True, "")),
                  patch.object(wallet_holdings, "can_buy", return_value=(True, "")),
                  patch.object(live_engine, "get_address", return_value="W")]
        for c in common:
            c.start()
            self.addCleanup(c.stop)
        with patch.object(ct, "wallet_usd", return_value=30.0), patch("live_order_bridge.MAX_ORDER_USD", 38.0), \
                patch.object(live_engine, "_execute_buy") as buy:
            sn.cycle(lambda c, t: sent.append(t), 1, get=self._get([pair()]), now=NOW)
        buy.assert_not_called()
        self.assertIn("Deposite SOL", sent[-1])
        with patch.object(ct, "wallet_usd", return_value=100.0), patch("live_order_bridge.MAX_ORDER_USD", 32.0), \
                patch.object(live_engine, "_execute_buy") as buy:
            sn.cycle(None, 1, get=self._get([pair()]), now=NOW + 1)
        buy.assert_not_called()                              # cap below 38: refuses instead of buying 32

    def test_only_3_sniper_orders_at_a_time(self):
        self.assertEqual((sn.ORDER_USD, sn.MAX_POSITIONS), (38, 3))


if __name__ == "__main__":
    unittest.main()
