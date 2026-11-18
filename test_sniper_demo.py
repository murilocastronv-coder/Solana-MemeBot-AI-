import unittest
from unittest import mock

import sniper_demo as sd
import trade_panel as tp

NOW = 1_800_000_000


def pair(mint="AbcPump1111111111111111111111111111111pump", liq=50_000, age_h=5, vol5=8000, buys=60, sells=20, up5=12, price=0.001):
    return {"baseToken": {"address": mint, "symbol": "PEPE2"}, "priceUsd": str(price), "liquidity": {"usd": liq},
            "marketCap": 400_000, "volume": {"m5": vol5}, "txns": {"m5": {"buys": buys, "sells": sells}},
            "priceChange": {"m5": up5, "h1": 30}, "pairCreatedAt": (NOW - age_h * 3600) * 1000}


class SniperDemoTest(unittest.TestCase):
    def setUp(self):
        tp._MEM.clear()
        self.p = [mock.patch.object(tp, "is_owner", return_value=True), mock.patch.object(tp, "_save")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_owner_filters(self):
        ok = lambda **k: sd.entry_reason(sd.features(pair(**k), NOW))
        self.assertEqual(ok(), "")
        self.assertIn("pump.fun", ok(mint="So1aNa111111111111111111111111111111111111"))
        self.assertIn("liquidez", ok(liq=5_000))
        self.assertIn("liquidez", ok(liq=250_000))
        self.assertIn("2 h", ok(age_h=1.5))
        self.assertIn("compradores", ok(buys=20, sells=20))
        self.assertIn("5 min", ok(up5=60))

    def test_demo_200_buys_then_sells_with_details(self):
        sd.reset(1, now=NOW)
        d = sd.state(1)
        d["on"] = True
        sd.save(1, d)
        msgs = []
        send = lambda c, t, *a: msgs.append(t)
        m = "AbcPump1111111111111111111111111111111pump"
        self.assertEqual(sd.buys(1, [pair()], send, now=NOW, safety=lambda mint: ""), ["PEPE2"])
        self.assertEqual(sd.state(1)["cash"], 180.0)
        self.assertEqual(sd.buys(1, [pair()], send, now=NOW + 60, safety=lambda mint: ""), [])     # one entry per coin
        sd.exits(1, {m: 0.00105}, send, now=NOW + 300)                                            # +5%: holds
        self.assertIn(m, sd.state(1)["pos"])
        sd.exits(1, {m: 0.00121}, send, now=NOW + 600)                                            # +21%: take profit
        d = sd.state(1)
        self.assertEqual(d["pos"], {})
        self.assertGreater(d["hist"][0]["pnl"], 0)
        self.assertIn("vol5", d["hist"][0]["f"])                                                  # what it bought with, to improve
        self.assertTrue(any("SNIPER DEMO COMPROU PEPE2" in x and "dexscreener.com/solana/" + m in x for x in msgs))
        self.assertTrue(any("SNIPER DEMO VENDEU PEPE2" in x and "alvo" in x for x in msgs))
        self.assertIn("US$ 200", sd.text(1, now=NOW))

    def test_scam_and_time_stop(self):
        sd.reset(1, now=NOW)
        self.assertEqual(sd.buys(1, [pair()], None, now=NOW, safety=lambda mint: "mint authority"), [])
        sd.reset(1, now=NOW)
        sd.buys(1, [pair()], None, now=NOW, safety=lambda mint: "")
        sd.exits(1, {"AbcPump1111111111111111111111111111111pump": 0.00101}, None, now=NOW + 31 * 60)
        self.assertEqual(sd.state(1)["hist"][0]["why"], "tempo de 30 min")
