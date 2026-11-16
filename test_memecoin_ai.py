import random
import unittest

import memecoin_ai as mc


def pair(name, sym, vol, buys):
    return {"baseToken": {"name": name, "symbol": sym}, "volume": {"h1": vol}, "txns": {"h1": {"buys": buys}}}


class MemeTest(unittest.TestCase):
    def setUp(self):
        mc._MEM.clear()

    def test_trends_proposal_and_decision(self):
        pairs = [pair("Space Frog", "SFROG", 90000, 400), pair("Frog King", "FKING", 50000, 200),
                 pair("Dead Cat", "DCAT", 1000, 5)]
        tr = mc.trends(pairs)
        self.assertEqual(tr[0][0], "frog")
        self.assertNotIn("cat", [w for w, s, e in tr])                 # no real activity
        seen = []
        fetch = lambda url: seen.append(url) or {"pairs": []}
        p, why = mc.propose(pairs, fetch, now=1_800_000_000, rnd=random.Random(1))
        self.assertTrue(p and p["status"] == "pending")
        self.assertIn("Frog", p["name"])
        self.assertTrue(seen)                                           # name checked against existing coins
        self.assertEqual(mc.decide(p["id"], True, 150.0)["status"], "approved")
        self.assertTrue(mc._load()["launches"][0]["simulated"])
        self.assertIsNone(mc.decide(p["id"], True, 150.0))             # decided once
        png = mc.logo_png(p["name"], size=32)
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_existing_name_is_refused_and_daily_limit(self):
        pairs = [pair("Space Frog", "SFROG", 90000, 400)]
        taken_all = lambda url: {"pairs": [{"chainId": "solana", "baseToken": {"symbol": url.split("q=")[-1], "name": "x"}}]}
        p, why = mc.propose(pairs, taken_all, now=1_800_000_000, rnd=random.Random(2))
        self.assertIsNone(p)
        for i in range(mc.MAX_PER_DAY):
            p, _ = mc.propose(pairs, lambda u: {"pairs": []}, now=1_800_000_000 + i, rnd=random.Random(i))
        p, why = mc.propose(pairs, lambda u: {"pairs": []}, now=1_800_000_100, rnd=random.Random(9))
        self.assertIsNone(p)
        self.assertIn("hoje", why)



class DevBuyTest(unittest.TestCase):
    def test_creator_buy_stays_inside_budget(self):
        self.assertAlmostEqual(mc.dev_buy_sol(10), 30 * 1e8 / (1.073e9 - 1e8), places=6)   # 10% would cost ~3.08 SOL
        line = mc.dev_buy_line(sol_usd=150.0)                       # opção 2: small buy, creation + buy = US$ 5
        self.assertIn("US$ 5.00", line)
        self.assertIn("sem venda automática", line)


class FakeSigner:
    def __init__(self):
        from solders.keypair import Keypair
        self.kp = Keypair()

    def pubkey(self):
        return self.kp.pubkey()


class RealLaunchTest(unittest.TestCase):
    def setUp(self):
        mc._MEM.clear()

    def test_pump_accounts_and_instructions(self):
        import struct
        import pump_launch as pl
        user, mint = str(FakeSigner().pubkey()), str(FakeSigner().pubkey())
        a = pl.accounts(mint, user)
        self.assertEqual(a["fee_config"], "8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt")   # address given in the docs
        ix = pl.ix_create_v2(mint, user, "Frog Lab", "FLAB", "https://x/y.json")
        self.assertEqual(bytes(ix.data)[:8], pl.DISC_CREATE_V2)
        self.assertEqual(len(ix.accounts), 16)
        self.assertTrue(ix.accounts[0].is_signer and ix.accounts[5].is_signer)
        self.assertEqual(bytes(ix.data)[-1], 0)                       # no mayhem mode
        b = pl.ix_buy_quote_in_v3(mint, user, 15_000_000, 1, buyback=pl.BUYBACK_RECIPIENTS[0])
        self.assertEqual(len(b.accounts), 17)
        self.assertEqual(struct.unpack("<QQ", bytes(b.data)[8:24]), (15_000_000, 1))
        self.assertEqual(str(b.accounts[3].pubkey), pl.TOKEN22)
        # first buy of 0.0157 SOL on a fresh curve: ~0.056% of the supply
        self.assertAlmostEqual(pl.tokens_for(0.0157) / 1e9 * 100, 0.0561, places=3)
        self.assertLess(pl.min_out(15_700_000), pl.tokens_for(0.0157) * 1e6)

    def test_limits(self):
        why, buy = mc.limits_ok(140.0, int(1e9), now=1_800_000_000)
        self.assertEqual(why, "")
        self.assertAlmostEqual((buy + mc.LAUNCH_COST_SOL) * 140, mc.BUDGET_PER_LAUNCH, places=6)   # inside US$ 5
        why, _ = mc.limits_ok(140.0, int(0.03e9), now=1_800_000_000)
        self.assertIn("US$ 3", why)                                    # the US$ 3 in SOL always stay
        day = __import__("time").strftime("%Y-%m-%d", __import__("time").gmtime(1_800_000_000))
        mc._load()["launches"] = [{"simulated": False, "day": day, "cost_usd": 5}, {"simulated": False, "day": day, "cost_usd": 5}]
        why, _ = mc.limits_ok(140.0, int(1e9), now=1_800_000_000)
        self.assertIn("2 lançamentos", why)

    def test_launch_only_after_approval_in_real_mode(self):
        import liq_bot as lb
        mc.set_real(True)
        mc._load()["proposals"] = [{"id": "ab", "name": "Frog Lab", "symbol": "FLAB", "description": "d", "status": "pending"}]
        signer = FakeSigner()
        calls = []

        def launcher(rpc, s, name, symbol, desc, png, lamports, simulate):
            calls.append((name, symbol, lamports))
            return {"mint": "MINT1", "sig": "SIG1", "uri": "u"}, ""
        orig = lb.sol_lamports
        lb.sol_lamports = lambda rpc, me: int(1e9)
        try:
            self.assertEqual(mc.launch_real("ab", 140.0, None, signer, launcher)[0], None)   # not approved yet
            self.assertFalse(calls)
            self.assertEqual(mc.decide("ab", True, 140.0)["status"], "launching")
            rec, why = mc.launch_real("ab", 140.0, None, signer, launcher)
        finally:
            lb.sol_lamports = orig
        self.assertEqual(why, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(rec["mint"], "MINT1")
        self.assertFalse(rec["simulated"])
        self.assertLessEqual(rec["cost_usd"], mc.BUDGET_PER_LAUNCH)
        self.assertIn("pump.fun/coin/MINT1", mc.launched_text(rec))
        self.assertEqual(mc.launch_real("ab", 140.0, None, signer, launcher)[0], None)       # never twice


if __name__ == "__main__":
    unittest.main()
