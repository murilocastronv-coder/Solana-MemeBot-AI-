import unittest
from unittest import mock

import sniper_demo as sd
import trade_panel as tp

NOW = 1_800_000_000


def pair(mint="AbcPump1111111111111111111111111111111pump", liq=50_000, age_h=5, vol5=9000, buys=60, sells=20, up5=12, price=0.001,
         up1h=30, up6h=40):
    return {"baseToken": {"address": mint, "symbol": "PEPE2"}, "priceUsd": str(price), "liquidity": {"usd": liq},
            "marketCap": 400_000, "volume": {"m5": vol5}, "txns": {"m5": {"buys": buys, "sells": sells}},
            "priceChange": {"m5": up5, "h1": up1h, "h6": up6h}, "pairCreatedAt": (NOW - age_h * 3600) * 1000}


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
        self.assertIn("repique", ok(up1h=-6))                     # v2: no bounce inside a falling hour (3 of the 4 losses)
        self.assertIn("volume", ok(vol5=4000))                    # v5: at least US$ 5 mil in 5 min
        self.assertIn("7 dias", ok(age_h=505))                    # v2: DIGS was 21 days old

    def test_demo_200_buys_then_sells_with_details(self):
        sd.reset(1, now=NOW)
        d = sd.state(1)
        d["on"] = True
        sd.save(1, d)
        msgs = []
        send = lambda c, t, *a: msgs.append(t)
        m = "AbcPump1111111111111111111111111111111pump"
        self.assertEqual(sd.buys(1, [pair()], send, now=NOW - 60, safety=lambda mint: ""), [])     # seen: waits 1 min
        self.assertEqual(sd.buys(1, [pair()], send, now=NOW, safety=lambda mint: ""), ["PEPE2"])   # still up: buys
        self.assertEqual(sd.state(1)["cash"], 180.0)
        self.assertEqual(sd.buys(1, [pair()], send, now=NOW + 60, safety=lambda mint: ""), [])     # one entry per coin
        sd.exits(1, {m: 0.00105}, send, now=NOW + 300)                                            # +5%: holds
        self.assertIn(m, sd.state(1)["pos"])
        sd.exits(1, {m: 0.00126}, send, now=NOW + 600)                                            # +26%: take profit
        d = sd.state(1)
        self.assertEqual(d["pos"], {})
        self.assertGreater(d["hist"][0]["pnl"], 0)
        self.assertIn("vol5", d["hist"][0]["f"])                                                  # what it bought with, to improve
        self.assertTrue(any("SNIPER DEMO COMPROU PEPE2" in x and "dexscreener.com/solana/" + m in x for x in msgs))
        self.assertTrue(any("SNIPER DEMO VENDEU PEPE2" in x and "alvo" in x for x in msgs))
        self.assertIn("US$ 200", sd.text(1, now=NOW))

    def test_scam_and_time_stop(self):
        sd.reset(1, now=NOW)
        sd.buys(1, [pair()], None, now=NOW - 60, safety=lambda mint: "mint authority")
        self.assertEqual(sd.buys(1, [pair()], None, now=NOW, safety=lambda mint: "mint authority"), [])
        sd.reset(1, now=NOW)
        sd.buys(1, [pair()], None, now=NOW - 60, safety=lambda mint: "")
        sd.buys(1, [pair()], None, now=NOW, safety=lambda mint: "")
        sd.exits(1, {"AbcPump1111111111111111111111111111111pump": 0.00101}, None, now=NOW + 46 * 60)
        self.assertEqual(sd.state(1)["hist"][0]["why"], "tempo de 45 min")

    def test_confirmation_and_trailing(self):
        sd.reset(1, now=NOW)
        sd.buys(1, [pair()], None, now=NOW - 60, safety=lambda mint: "")
        self.assertEqual(sd.buys(1, [pair(price=0.00095)], None, now=NOW, safety=lambda mint: ""), [])   # fell: no buy
        sd.reset(1, now=NOW)
        sd.buys(1, [pair()], None, now=NOW - 60, safety=lambda mint: "")
        sd.buys(1, [pair()], None, now=NOW, safety=lambda mint: "")
        m = "AbcPump1111111111111111111111111111111pump"
        sd.exits(1, {m: 0.00120}, None, now=NOW + 120)                 # +20%: trailing armed
        sd.exits(1, {m: 0.00110}, None, now=NOW + 200)                 # 8% below the top: still holds (10% trailing)
        self.assertIn(m, sd.state(1)["pos"])
        sd.exits(1, {m: 0.00107}, None, now=NOW + 240)                 # 11% below the top: sells with profit
        h = sd.state(1)["hist"][0]
        self.assertIn("stop móvel", h["why"])
        self.assertEqual(h["rule"], sd.RULE)


class V3Test(unittest.TestCase):
    def test_profit_floor_after_10(self):
        self.assertIsNone(sd.exit_reason(1.09, 60, peak=1.09))
        self.assertIn("piso", sd.exit_reason(1.04, 60, peak=1.11))         # Tomás: +10% then back to +0.2%
        self.assertIn("stop móvel", sd.exit_reason(1.10, 60, peak=1.23))   # 10% trailing above the floor
        self.assertIsNone(sd.exit_reason(1.12, 60, peak=1.23))             # 9% below the top: holds
        self.assertEqual((sd.TRAIL_ARM, sd.TRAIL_DROP), (0.10, 0.10))

    def test_refusal_funnel(self):
        sd.REASONS.clear()
        f = sd.features(pair(up1h=-10), NOW)
        sd.count(1, sd.entry_reason(f), f, NOW)
        sd.count(1, sd.entry_reason(f), f, NOW)                           # same coin counted once
        g = sd.features(pair(mint="Zzz111111111111111111111111111111111pump", vol5=3000), NOW)
        sd.count(1, sd.entry_reason(g), g, NOW)
        t = sd.funnel_text(1)
        self.assertIn("2 moedas", t)
        self.assertIn("a hora não está subindo: 1", t)
        self.assertIn("pouco volume em 5 min: 1", t)
