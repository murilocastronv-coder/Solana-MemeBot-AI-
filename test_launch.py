import base64
import struct
import time
import unittest
from unittest.mock import patch

from solders.keypair import Keypair

import launch_sniper as ls


def s(text):
    raw = text.encode()
    return struct.pack("<I", len(raw)) + raw


def event_bytes(name="Pepe Rocket", symbol="PROCK", uri="https://ipfs.io/ipfs/abc"):
    mint, curve, user = (bytes(Keypair().pubkey()) for _ in range(3))
    return ls.CREATE_EVENT + s(name) + s(symbol) + s(uri) + mint + curve + user + b"\x00" * 40


def trade_bytes(mint, user, sol, is_buy=True):
    return (ls.TRADE_EVENT + bytes(mint) + struct.pack("<QQ", int(sol * 1e9), 1000) +
            bytes([1 if is_buy else 0]) + bytes(user) + b"\x00" * 24)


class DecodeTests(unittest.TestCase):
    def test_decode_create_from_logs(self):
        logs = ["Program log: Instruction: Create",
                "Program data: " + base64.b64encode(event_bytes()).decode()]
        events = ls.events_from_logs(logs)
        self.assertEqual((events[0]["name"], events[0]["symbol"]), ("Pepe Rocket", "PROCK"))

    def test_ignores_non_create(self):
        logs = ["Program log: Instruction: Buy", "Program data: " + base64.b64encode(event_bytes()).decode()]
        self.assertEqual(ls.events_from_logs(logs), [])

    def test_decode_trade(self):
        mint, user = Keypair().pubkey(), Keypair().pubkey()
        logs = ["Program log: Instruction: Sell", "Program data: " + base64.b64encode(trade_bytes(mint, user, 1.5, False)).decode()]
        t = ls.trades_from_logs(logs)[0]
        self.assertEqual((t["mint"], t["user"], t["sol"], t["is_buy"]), (str(mint), str(user), 1.5, False))


class ConfirmTests(unittest.TestCase):
    def setUp(self):
        ls.TRACK.clear()
        self.creator = str(Keypair().pubkey())
        self.mint = str(Keypair().pubkey())
        ls.track_create({"mint": self.mint, "creator": self.creator, "symbol": "PEPE"}, now=1000)

    def buy(self, user, sol, is_buy=True):
        ls.track_trade({"mint": self.mint, "user": user, "sol": sol, "is_buy": is_buy, "tokens": 1})

    def test_broad_demand_confirms(self):
        for i in range(40):
            self.buy("u" + str(i), 0.2)
        self.buy(self.creator, 5.0)            # creator buys do not count
        self.assertEqual(ls.confirm_reasons(ls.TRACK[self.mint]), [])

    def test_bundle_and_creator_sell_rejected(self):
        for i in range(39):
            self.buy("u" + str(i), 0.1)
        self.buy("whale", 6.0)
        self.assertIn("uma carteira domina as compras (bundle)", ls.confirm_reasons(ls.TRACK[self.mint]))
        self.buy(self.creator, 1.0, is_buy=False)
        self.assertIn("criador vendeu", ls.confirm_reasons(ls.TRACK[self.mint]))
        self.assertTrue(ls.creator_sold(self.mint))

    def test_decide_waits_then_buys(self):
        ls.TRACK[self.mint]["static"] = ""
        for i in range(40):
            self.buy("u" + str(i), 0.2)
        with patch.object(ls, "_buy_and_watch", return_value="sold") as b:
            self.assertIsNone(ls.decide_once(None, "1", now=1060))   # still inside the 3 min wait
            self.assertEqual(ls.decide_once(None, "1", now=1181), "sold")
        self.assertEqual(b.call_count, 1)

    def test_weak_token_expires(self):
        ls.TRACK[self.mint]["static"] = ""
        self.buy("u1", 0.1)
        with patch.object(ls, "_buy_and_watch") as b:
            ls.decide_once(None, "1", now=1181)
            self.assertFalse(ls.TRACK[self.mint]["decided"])          # keeps watching in the window
            ls.decide_once(None, "1", now=1301)
        self.assertTrue(ls.TRACK[self.mint]["decided"])
        b.assert_not_called()

    def test_money_leaving_is_rejected(self):
        for i in range(40):
            self.buy("u" + str(i), 0.2)
        for i in range(15):
            self.buy("s" + str(i), 0.4, is_buy=False)
        self.assertIn("dinheiro saindo (vendas comendo as compras)", ls.confirm_reasons(ls.TRACK[self.mint]))


class FilterTests(unittest.TestCase):
    def setUp(self):
        ls._creators.clear()

    def ev(self, **over):
        e = {"name": "Pepe", "symbol": "PEPE", "uri": "https://x/meta.json", "creator": "C1",
             "mint": "M", "seen_at": time.time()}
        e.update(over)
        return e

    def test_socials_and_serial_creator(self):
        with patch.object(ls, "has_socials", return_value=False):
            self.assertEqual(ls.reject_reason(self.ev()), "sem site/redes sociais")
        with patch.object(ls, "has_socials", return_value=True):
            self.assertEqual(ls.reject_reason(self.ev()), "")
            self.assertIn("várias moedas", ls.reject_reason(self.ev()))


class ExitTests(unittest.TestCase):
    def test_rules(self):
        d = ls.exit_decision
        self.assertEqual(d(1.05, 1.06, 3, dev_sold=True), "launch_dev_sold")
        self.assertEqual(d(0.84, 0.98, 3), "stop_loss")
        self.assertEqual(d(1.031, 1.031, 2), "launch_profit")   # small net profit: sell everything now
        self.assertEqual(d(1.20, 1.30, 10), "launch_profit")
        self.assertIsNone(d(1.02, 1.02, 5))                     # +2%: not yet
        self.assertEqual(d(0.87, 0.98, 5), "launch_drop")       # falling before profit
        self.assertEqual(d(1.02, 1.03, 26), "launch_flat")
        self.assertEqual(d(1.0, 1.05, 121), "launch_time")


class FlowTests(unittest.TestCase):
    def test_buy_then_sell_when_creator_sells(self):
        ls._last_buy = 0.0
        ls.TRACK.clear()
        ls.track_create({"mint": "M1", "creator": "C", "symbol": "PEPE"}, now=time.time())
        ls.TRACK["M1"]["creator_sold"] = True
        event = {"name": "Pepe", "symbol": "PEPE", "uri": "u", "creator": "C", "mint": "M1"}
        sold, bought = [], []
        pos = {"M1": {"token_amount_raw": "1000", "cost_usd": "8"}}
        with patch.object(ls.engine, "ready", return_value=True), \
             patch.object(ls.state, "can_open", return_value=(True, "Ready")), \
             patch.object(ls.state, "positions_by_mode", return_value=0), \
             patch.object(ls.engine, "get_address", return_value="W"), \
             patch.object(ls.engine, "_execute_buy", side_effect=lambda *a: bought.append(a[-2:]) or {"status": "settled_buy"}), \
             patch.object(ls.state, "_load", return_value={"positions": pos}), \
             patch("live_exit_monitor.quote_value_usd", return_value=8.4), \
             patch.object(ls.engine, "sell_now", side_effect=lambda m, r, s, c: sold.append(r) or "sold"), \
             patch.object(ls.time, "sleep", return_value=None):
            ls._buy_and_watch(event, lambda *a: None, "1")
        self.assertEqual(bought, [(ls.LAUNCH_FRACTION, "launch")])
        self.assertEqual(sold, ["launch_dev_sold"])


if __name__ == "__main__":
    unittest.main()
