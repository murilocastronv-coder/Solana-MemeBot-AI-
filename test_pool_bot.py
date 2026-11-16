import unittest

import pool_bot as pb

SOL = "So11111111111111111111111111111111111111112"


class PoolBotTest(unittest.TestCase):
    def setUp(self):
        pb._MEM.clear()
        pb._load()["enabled"] = True

    def test_rotates_and_respects_rules(self):
        api = [{"address": "P1", "name": "AAA-SOL", "mint_x": "AAA", "mint_y": SOL, "liquidity": "1000000",
                "trade_volume_24h": "5000000", "fees_24h": "200000"},                       # 20%/day
               {"address": "P2", "name": "BBB-SOL", "mint_x": "BBB", "mint_y": SOL, "liquidity": "1000000",
                "trade_volume_24h": "5000000", "fees_24h": "900000"},                       # 90%/day: above the max
               {"address": "P3", "name": "CCC-SOL", "mint_x": "CCC", "mint_y": SOL, "liquidity": "1000",
                "trade_volume_24h": "5000000", "fees_24h": "900"}]                          # tiny pool
        price = {"AAA": 1.0}
        fetch = lambda url: [{"baseToken": {"address": url.split("/")[-1]}, "priceUsd": str(price.get(url.split("/")[-1], 1.0)),
                              "liquidity": {"usd": 1e6}, "pairCreatedAt": 1}]
        t0 = 1_800_000_000
        pos = pb.cycle(now=t0, get=lambda u: api, fetch=fetch, safety=lambda m: "")
        self.assertEqual(pos["address"], "P1")
        pb.cycle(now=t0 + 86400, get=lambda u: api, fetch=fetch, safety=lambda m: "")
        self.assertGreater(pb._load()["pos"]["fees"], 10.0)                                 # ~20% of 60
        price["AAA"] = 0.8                                                                  # coin -20%: stop
        pb.cycle(now=t0 + 90000, get=lambda u: api, fetch=fetch, safety=lambda m: "")
        closed = pb._load()["closed"]
        self.assertEqual(len(closed), 1)
        self.assertIn("caiu", closed[0]["why"])
        self.assertLess(closed[0]["il"], 0)
        self.assertIn("ROBÔ DE POOLS", pb.status_text())

    def test_scam_is_refused(self):
        api = [{"address": "P1", "name": "AAA-SOL", "mint_x": "AAA", "mint_y": SOL, "liquidity": "1000000",
                "trade_volume_24h": "5000000", "fees_24h": "200000"}]
        fetch = lambda url: [{"baseToken": {"address": "AAA"}, "priceUsd": "1", "liquidity": {"usd": 1e6}, "pairCreatedAt": 1}]
        self.assertIsNone(pb.cycle(now=1_800_000_000, get=lambda u: api, fetch=fetch, safety=lambda m: "mint authority ativa"))

    def tearDown(self):
        pb._MEM.clear()


if __name__ == "__main__":
    unittest.main()


class CooldownTest(unittest.TestCase):
    def test_does_not_reenter_the_pool_it_just_left(self):
        pb._MEM.clear()
        pb._load().update(enabled=True, exited={"P1": 1_800_000_000})
        api = [{"address": "P1", "name": "AAA-SOL", "mint_x": "AAA", "mint_y": SOL, "liquidity": "1000000",
                "trade_volume_24h": "5000000", "fees_24h": "200000"}]
        fetch = lambda url: [{"baseToken": {"address": "AAA"}, "priceUsd": "1", "liquidity": {"usd": 1e6}, "pairCreatedAt": 1}]
        self.assertIsNone(pb.cycle(now=1_800_000_100, get=lambda u: api, fetch=fetch, safety=lambda m: ""))
        pb._MEM.clear()


class GeckoPoolsTest(unittest.TestCase):
    def test_fee_from_chain_and_yield(self):
        import base64
        import struct
        page = {"data": [{"attributes": {"address": "So11111111111111111111111111111111111111112", "name": "AAA / SOL",
                                         "reserve_in_usd": "1000000", "volume_usd": {"h24": "40000000"}},
                          "relationships": {"base_token": {"data": {"id": "solana_AAA"}}, "quote_token": {"data": {"id": "solana_" + SOL}}}}]}
        d = bytearray(84)
        struct.pack_into("<H", d, 8, 10000)          # base factor
        struct.pack_into("<H", d, 32, 500)           # protocol share 5%
        d[34] = 0
        struct.pack_into("<H", d, 80, 25)            # bin step -> base fee 0.25%
        calls = []
        fetch = lambda url: calls.append(url) or (page if "page=1&" in url else {"data": []})
        rpc = lambda m, a: {"value": [{"data": [base64.b64encode(bytes(d)).decode(), "base64"]}]}
        rows = pb._gecko_pools(fetch=fetch, rpc=rpc)
        self.assertAlmostEqual(rows[0]["fees_24h"], 40e6 * 0.0025 * 0.95)
        self.assertEqual(rows[0]["mint_y"], SOL)
