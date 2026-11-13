import base64
import os
import tempfile
import unittest
from unittest.mock import patch

import dashboard
import telegram_bot as tb


def basic(pw):
    return {"Authorization": "Basic " + base64.b64encode(("x:" + pw).encode()).decode()}


class PanelTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json"),
                                    "DASHBOARD_PASSWORD": "senha-forte-123"})
        p.start()
        self.addCleanup(p.stop)
        self.client = tb.app.test_client()

    def test_needs_the_password(self):
        self.assertEqual(self.client.get("/painel").status_code, 401)
        self.assertEqual(self.client.get("/painel", headers=basic("errada-123456")).status_code, 401)

    def test_shows_the_panel_with_notices(self):
        dashboard.record("🟢 🚀 SNIPER COMPROU <b>XYZ</b>")
        with patch("copy_trader.wallet_usd", return_value=100.0), patch("live_engine.get_address", return_value="W" * 44):
            r = self.client.get("/painel", headers=basic("senha-forte-123"))
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("Painel do bot", body)
        self.assertIn("SNIPER COMPROU &lt;b&gt;XYZ&lt;/b&gt;", body)      # escaped
        self.assertIn("Posições abertas", body)

    def test_off_without_a_password(self):
        with patch.dict(os.environ, {"DASHBOARD_PASSWORD": ""}):
            self.assertEqual(self.client.get("/painel").status_code, 503)


class SafeSenderTests(unittest.TestCase):
    def test_exit_cycle_send_never_raises(self):
        import live_engine

        def boom(chat, text, *a, **k):
            raise RuntimeError("Telegram API returned HTTP 502")
        self.assertIsNone(live_engine._safe_sender(boom)(1, "🚀 STOP MÓVEL ATIVADO"))


if __name__ == "__main__":
    unittest.main()
