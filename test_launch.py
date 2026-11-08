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


class DecodeTests(unittest.TestCase):
    def test_decode_from_logs(self):
        logs = ["Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
                "Program log: Instruction: Create",
                "Program data: " + base64.b64encode(event_bytes()).decode()]
        events = ls.events_from_logs(logs)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["name"], events[0]["symbol"]), ("Pepe Rocket", "PROCK"))
        from solders.pubkey import Pubkey
        Pubkey.from_string(events[0]["mint"])  # valid address

    def test_ignores_buys(self):
        logs = ["Program log: Instruction: Buy", "Program data: " + base64.b64encode(event_bytes()).decode()]
        self.assertEqual(ls.events_from_logs(logs), [])
        self.assertIsNone(ls.decode_create_event(b"\x00" * 200))


class FilterTests(unittest.TestCase):
    def setUp(self):
        ls._creators.clear()

    def ev(self, **over):
        e = {"name": "Pepe", "symbol": "PEPE", "uri": "https://x/meta.json", "creator": "C1",
             "mint": "M", "seen_at": time.time()}
        e.update(over)
        return e

    def test_socials_required(self):
        with patch.object(ls, "has_socials", return_value=False):
            self.assertEqual(ls.reject_reason(self.ev()), "sem site/redes sociais")
        with patch.object(ls, "has_socials", return_value=True):
            self.assertEqual(ls.reject_reason(self.ev(creator="C2")), "")

    def test_serial_creator_and_late(self):
        with patch.object(ls, "has_socials", return_value=True):
            for _ in range(2):
                self.assertEqual(ls.reject_reason(self.ev()), "")
            self.assertIn("várias moedas", ls.reject_reason(self.ev()))
            self.assertEqual(ls.reject_reason(self.ev(creator="Z", seen_at=time.time() - 30)), "vista tarde demais")

    def test_metadata_links(self):
        class R:
            def __init__(self, d): self.d = d
            def json(self): return self.d
        with patch.object(ls.requests, "get", return_value=R({"twitter": "https://x.com/pepe"})):
            self.assertTrue(ls.has_socials("https://x/meta.json"))
        with patch.object(ls.requests, "get", return_value=R({"description": "moon"})):
            self.assertFalse(ls.has_socials("https://x/meta.json"))


class ExitTests(unittest.TestCase):
    def test_rules(self):
        d = ls.exit_decision
        self.assertEqual(d(0.79, 0.98, 3), "stop_loss")
        self.assertEqual(d(0.89, 0.98, 3), "launch_drop")       # started falling
        self.assertEqual(d(1.011, 1.011, 2), "launch_profit")   # profit after fees: sell now
        self.assertEqual(d(1.30, 1.42, 10), "launch_profit")
        self.assertEqual(d(1.005, 1.006, 16), "launch_flat")    # did not go up fast
        self.assertIsNone(d(1.005, 1.006, 5))                   # tiny gain, still early: hold
        self.assertEqual(d(1.005, 1.006, 60), "launch_flat")


class FlowTests(unittest.TestCase):
    def _flow(self, value):
        ls._last_buy = 0.0
        event = {"name": "Pepe", "symbol": "PEPE", "uri": "u", "creator": "C9", "mint": "M1", "seen_at": time.time()}
        sold, bought = [], []
        pos = {"M1": {"token_amount_raw": "1000", "cost_usd": "8"}}
        clock = iter(range(0, 1000, 5))
        with patch.object(ls, "reject_reason", return_value=""), \
             patch.object(ls.engine, "ready", return_value=True), \
             patch.object(ls.state, "can_open", return_value=(True, "Ready")), \
             patch.object(ls.state, "positions_by_mode", return_value=0), \
             patch.object(ls.engine, "get_address", return_value="W"), \
             patch.object(ls.engine, "_execute_buy", side_effect=lambda *a: bought.append(a[-2:]) or {"status": "settled_buy"}), \
             patch.object(ls.state, "_load", return_value={"positions": pos}), \
             patch("live_exit_monitor.quote_value_usd", return_value=value), \
             patch.object(ls.engine, "sell_now", side_effect=lambda m, r, s, c: sold.append(r) or "sold"), \
             patch.object(ls.time, "sleep", return_value=None), \
             patch.object(ls.time, "time", side_effect=lambda: 1_000_000 + next(clock)):
            ls._handle(event, lambda *a: None, "1")
        self.assertEqual(bought, [(ls.LAUNCH_FRACTION, "launch")])
        return sold

    def test_sells_immediately_in_profit(self):
        self.assertEqual(self._flow(8.1), ["launch_profit"])

    def test_sells_when_flat(self):
        self.assertEqual(self._flow(8.04), ["launch_flat"])


if __name__ == "__main__":
    unittest.main()
