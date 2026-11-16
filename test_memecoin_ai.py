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


if __name__ == "__main__":
    unittest.main()
