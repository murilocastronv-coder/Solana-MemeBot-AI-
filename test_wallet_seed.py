import unittest
from unittest.mock import patch
import wallet_seed

PHRASE = "abandon " * 11 + "about"

class WalletSeedTests(unittest.TestCase):
    def test_phantom_derivation(self):
        self.assertEqual(wallet_seed.address_from_phrase(PHRASE), "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk")
    def test_invalid_phrase(self):
        with self.assertRaises(ValueError):
            wallet_seed.address_from_phrase("abandon " * 12)
    def test_detects_phrase_in_chat(self):
        self.assertTrue(wallet_seed.looks_like_seed_phrase(PHRASE))
        self.assertFalse(wallet_seed.looks_like_seed_phrase("/carteira"))
    def test_env(self):
        with patch.dict("os.environ", {"SOLANA_BOT_SEED_PHRASE": "  " + PHRASE.upper() + " ", "SOLANA_BOT_ACCOUNT_INDEX": ""}):
            self.assertEqual(wallet_seed.connected_address(), "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk")
        with patch.dict("os.environ", {"SOLANA_BOT_SEED_PHRASE": ""}):
            self.assertIsNone(wallet_seed.connected_address())

if __name__ == "__main__":
    unittest.main()
