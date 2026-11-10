import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
import copy_trader as ct
import telegram_bot as tb

W = "GgfpQyAbCdEfGhJkLmNpQrStUvWxYz123456789jaWH"
MINT = "Bv1a7cEj5aRnJQhMw4kRQqAFesMSd5LwcDnUzq52BMmr"


class CopyAddressTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)

    def test_full_address_is_tap_to_copy_but_links_are_not_touched(self):
        text = "📦 posição\n  👛 Carteira copiada:\n  " + W + "\n  solscan.io/token/" + MINT
        ents = tb.address_entities(text)
        self.assertEqual(len(ents), 1)
        e = ents[0]
        utf16 = text.encode("utf-16-le")
        self.assertEqual(utf16[e["offset"] * 2:(e["offset"] + e["length"]) * 2].decode("utf-16-le"), W)

    def test_positions_show_full_wallet_with_copy_and_view_buttons(self):
        d = ct._load()
        d["positions"][MINT] = {"wallet": W, "real": True, "opened_at": time.time()}
        ct._save(d)
        self.assertIn("\n  " + W + "\n", ct.positions_text())
        sent = []
        with patch.object(tb, "authorized", return_value=True), patch.object(tb, "request_api"), \
                patch.object(tb, "run_async"), patch.object(tb, "send", side_effect=lambda c, t, kb=None: sent.append(kb)):
            tb.CTX.interactive = True
            tb.handle({"callback_query": {"id": "1", "data": "copy_positions", "message": {"chat": {"id": 1, "type": "private"}}}})
        first = sent[-1][0]
        self.assertEqual(first[0]["copy_text"], {"text": W})
        self.assertEqual(first[1]["url"], "https://solscan.io/account/" + W)


if __name__ == "__main__":
    unittest.main()
