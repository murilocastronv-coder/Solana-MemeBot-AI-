import os
import tempfile
import unittest
from unittest.mock import patch

ADDR = "DNMUwKFS1hsrEHoWchxHAptbb9QcPDYo2rLeoShugfni"
OTHER = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


class OwnerWalletPickTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json"), "COPY_STATE_FILE": os.path.join(d, "c.json")})
        p.start()
        self.addCleanup(p.stop)
        import copy_trader
        self.ct = copy_trader

    def test_add_list_remove(self):
        ct = self.ct
        with patch.object(ct, "_path", return_value=__import__("pathlib").Path(tempfile.mkdtemp()) / "c.json"):
            ct.add_owner_wallet(OTHER)
            self.assertTrue(ct.mirror(OTHER))
            text = ct.picks_text()
            self.assertIn("`" + OTHER + "`", text)
            self.assertIn("sua escolha", text)
            self.assertTrue(ct.unfollow(OTHER))
            self.assertNotIn(OTHER, ct.picks_text())

    def test_invalid_address(self):
        with self.assertRaises(Exception):
            self.ct.add_owner_wallet("not-a-wallet")

    def test_telegram_accepts_plain_address_after_button(self):
        import telegram_bot as tb
        self.assertTrue(tb.SOL_ADDRESS.fullmatch(ADDR))
        self.assertFalse(tb.SOL_ADDRESS.fullmatch("/start"))


if __name__ == "__main__":
    unittest.main()


class ReferenceAndCounterTest(unittest.TestCase):
    def test_reference_comes_back_unless_owner_removed_and_counter(self):
        import pathlib
        import copy_trader as ct
        import wallet_twin
        p = pathlib.Path(tempfile.mkdtemp()) / "c.json"
        with patch.object(ct, "_path", return_value=p), patch.object(wallet_twin, "REFERENCE", ADDR), \
                patch.object(wallet_twin, "ENABLED", True):
            self.assertTrue(ct.ensure_reference())
            self.assertIn(ADDR, ct._load()["wallets"])
            self.assertTrue(ct.mirror(ADDR))
            ct.unfollow(ADDR)
            ct.mark_owner_removed(ADDR)
            self.assertFalse(ct.ensure_reference())          # the owner removed it: stays out
            with patch.object(ct, "_preaudit_and_follow", side_effect=["rejected", "added", "full"]):
                for _ in range(3):
                    ct.preaudit_and_follow({"wallet": "x"}, None, 200.0)
            self.assertIn("analisadas até agora: 2", ct.search_line())
            self.assertIn("aprovadas 1", ct.search_line())
