import os
import tempfile
import time
import unittest
from unittest.mock import patch

import consensus as cn
import copy_trader as ct

MINT = "ConsMintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
WALLETS = ["W" + str(i) * 43 for i in range(1, 6)]


class ConsensusTests(unittest.TestCase):
    def setUp(self):
        r = patch.object(cn, "dex_pairs", return_value=[])
        r.start()
        self.addCleanup(r.stop)
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        q = patch.object(ct, "_sol_usd", return_value=100.0)
        q.start()
        self.addCleanup(q.stop)
        d = ct._load()
        d["score_v2"] = True
        for w in WALLETS:
            d["wallets"][w] = {"last_sig": "s", "copies": 0, "wins": 0, "pnl_usd": 0.0, "paused": False, "holds": [],
                               "followed_at": time.time()}
        ct._save(d)

    def test_buys_only_when_3_wallets_buy_the_same_coin_in_an_hour(self):
        cn.set_mode(True, False)
        now = 1_800_000_000
        cn.record_buy(MINT, WALLETS[0], now)
        cn.record_buy(MINT, WALLETS[1], now + 60)
        cn.record_buy(MINT, WALLETS[1], now + 120)              # same wallet twice counts once
        self.assertEqual(cn.candidates(now + 130), [])
        cn.record_buy(MINT, WALLETS[2], now + 600)
        self.assertEqual(len(cn.candidates(now + 610)[0][1]), 3)
        sent = []
        with patch.object(cn, "safety_reason", return_value=""), \
                patch.object(ct, "_order", side_effect=lambda i, o, a: {"outAmount": "1000" if i == cn.SOL_MINT
                                                                         else str(int(0.32e9))}):
            cn.cycle(lambda c, t: sent.append(t), 1, now=now + 610)
            cn.cycle(lambda c, t: sent.append(t), 1, now=now + 700)      # not twice
        self.assertEqual(sum("COMPROU" in t for t in sent), 1)

    def test_old_buys_dont_count(self):
        now = 1_800_000_000
        cn.record_buy(MINT, WALLETS[0], now)
        cn.record_buy(MINT, WALLETS[1], now + 10)
        cn.record_buy(MINT, WALLETS[2], now + 3700)               # first one is > 1 h old
        self.assertEqual(cn.candidates(now + 3700), [])

    def test_majority_option(self):
        with patch.object(cn, "MIN_SHARE", 0.5):
            self.assertEqual(cn.needed(5), 3)
            self.assertEqual(cn.needed(20), 10)

    def test_unsafe_coin_not_bought(self):
        cn.set_mode(True, False)
        for w in WALLETS[:3]:
            cn.record_buy(MINT, w)
        with patch.object(cn, "safety_reason", return_value="teste de venda devolveu 40%"), \
                patch.object(cn, "_open_sim") as buy:
            cn.cycle(None, None)
        buy.assert_not_called()

    def test_exit_stop_40_and_trailing_from_25(self):
        from live_exit_monitor import position_signal
        pos = {"mode": "consensus", "opened_at": time.time()}
        self.assertEqual(position_signal(pos, 100, 61)["action"], "hold")          # -39%
        self.assertEqual(position_signal(pos, 100, 59)["reason"], "stop_loss")     # -41%
        self.assertEqual(position_signal(pos, 100, 124)["action"], "hold")         # +24%: no trailing yet
        armed = dict(pos, peak_ratio=1.40)                                          # went to +40%
        self.assertEqual(position_signal(armed, 100, 130)["action"], "hold")       # 7% below the top
        self.assertEqual(position_signal(armed, 100, 125)["reason"], "take_profit")  # 11% below the top

    def test_copy_cycle_records_buys_for_consensus_even_with_copy_off(self):
        cn.set_mode(True, False)
        with patch.object(ct, "new_signals", return_value=[("buy", MINT, 200.0)]), \
                patch.object(ct, "users_active", return_value=False), patch.object(ct, "_open") as copy_buy:
            ct.cycle()
        self.assertEqual(len(cn._load()["buys"][MINT]), len(WALLETS))
        copy_buy.assert_not_called()                              # owner's copy trade is off

    def test_real_uses_live_engine_consensus_mode(self):
        cn.set_mode(True, True)
        for w in WALLETS[:3]:
            cn.record_buy(MINT, w)
        import live_engine
        import live_trade_state as st
        import wallet_holdings
        with patch.object(cn, "safety_reason", return_value=""), patch.object(live_engine, "ready", return_value=True), \
                patch.object(st, "can_open", return_value=(True, "")), \
                patch.object(wallet_holdings, "can_buy", return_value=(True, "")), \
                patch.object(live_engine, "get_address", return_value="W"), \
                patch("live_order_bridge.MAX_ORDER_USD", 38.0), \
                patch.object(live_engine, "_execute_buy") as buy:
            cn.cycle(None, 1)
        self.assertEqual(buy.call_args.args[7], "consensus")
        self.assertEqual(buy.call_args.kwargs["usd"], 32)


class MarketConsensusTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        q = patch.object(ct, "_sol_usd", return_value=100.0)
        q.start()
        self.addCleanup(q.stop)
        r = patch.object(cn, "dex_pairs", return_value=[])
        r.start()
        self.addCleanup(r.stop)

    def trades(self, buyers, sellers, now):
        rows = [(MINT, "Buyer" + str(i).zfill(39), now - 60, "b") for i in range(buyers)]
        rows += [(MINT, "Seller" + str(i).zfill(38), now - 60, "s") for i in range(sellers)]
        cn.record_market_buys(rows, now)

    def test_most_market_wallets_buying_triggers_without_any_copied_or_proven_wallet(self):
        now = time.time()
        self.trades(20, 5, now)                                   # 80% buying, none copied, none analysed
        found = cn.market_candidates(now)
        self.assertEqual((found[0][0], len(found[0][1]), len(found[0][2])), (MINT, 20, 5))

    def test_not_the_majority_buying_is_not_enough(self):
        now = time.time()
        self.trades(20, 15, now)                                  # 57% buying
        self.assertEqual(cn.market_candidates(now), [])

    def test_few_wallets_is_not_enough(self):
        now = time.time()
        self.trades(10, 0, now)
        self.assertEqual(cn.market_candidates(now), [])

    def test_dexscreener_majority_buying(self):
        def pair(b, s, m5b=10, m5s=5, liq=50_000):
            return {"baseToken": {"address": MINT}, "liquidity": {"usd": liq},
                    "txns": {"h1": {"buys": b, "sells": s}, "m5": {"buys": m5b, "sells": m5s}}}
        self.assertEqual(cn.dex_candidates([pair(300, 100)]), [(MINT, 300, 100)])   # 75% buys
        self.assertEqual(cn.dex_candidates([pair(300, 250)]), [])                   # 55%: not the majority
        self.assertEqual(cn.dex_candidates([pair(100, 10)]), [])                    # too few buys
        self.assertEqual(cn.dex_candidates([pair(300, 100, m5b=2, m5s=9)]), [])     # now selling
        self.assertEqual(cn.dex_candidates([pair(300, 100, liq=5_000)]), [])        # liquidity < 10k

    def test_cycle_buys_market_consensus_coin(self):
        now = time.time()
        cn.set_mode(True, False)
        self.trades(20, 4, now)
        sent = []
        with patch.object(cn, "safety_reason", return_value=""), \
                patch.object(ct, "_order", side_effect=lambda i, o, a: {"outAmount": "1000" if i == cn.SOL_MINT
                                                                         else str(int(0.32e9))}):
            cn.cycle(lambda c, t: sent.append(t), 1, now=now)
        self.assertIn("mercado: 20 carteiras comprando x 4 vendendo (83% compram)", sent[0])

    def test_search_records_buyers_and_sellers(self):
        import dex_wallets
        w = "Wbuyer" + "x" * 38
        tx = {"meta": {"err": None, "preBalances": [10_000_000_000], "postBalances": [9_000_000_000],
                       "preTokenBalances": [], "postTokenBalances": [
                           {"owner": w, "mint": MINT, "uiTokenAmount": {"amount": "500", "decimals": 6}}]},
              "transaction": {"message": {"accountKeys": [w]}}}
        self.assertEqual(dex_wallets.buyers_of(tx, MINT), [w])
        self.assertEqual(dex_wallets.sellers_of(tx, MINT), [])
        sell = {"meta": {"err": None, "preBalances": [9_000_000_000], "postBalances": [10_000_000_000],
                         "preTokenBalances": [{"owner": w, "mint": MINT, "uiTokenAmount": {"amount": "500", "decimals": 6}}],
                         "postTokenBalances": []},
                "transaction": {"message": {"accountKeys": [w]}}}
        self.assertEqual(dex_wallets.sellers_of(sell, MINT), [w])


if __name__ == "__main__":
    unittest.main()
