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

    def test_trailing_stop_card(self):
        dashboard.record("🚀 STOP MÓVEL ATIVADO — ABC +45%\nAgora vendo se cair 15% abaixo do topo")
        with patch("copy_trader.wallet_usd", return_value=100.0):
            body = self.client.get("/painel", headers=basic("senha-forte-123")).get_data(as_text=True)
        self.assertIn("id='trail' data-latest='", body)
        self.assertNotIn("data-latest='0'", body)
        self.assertIn("ABC +45%", body)
        self.assertIn("Notification", body)

    def test_off_without_a_password(self):
        with patch.dict(os.environ, {"DASHBOARD_PASSWORD": ""}):
            self.assertEqual(self.client.get("/painel").status_code, 503)


class ControlTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json"),
                                    "DASHBOARD_PASSWORD": "senha-forte-123"})
        p.start()
        self.addCleanup(p.stop)
        self.client = tb.app.test_client()

    def post(self, acao, csrf=None, headers=None):
        h = basic("senha-forte-123")
        h.update(headers or {})
        return self.client.post("/painel/acao", data={"acao": acao, "csrf": csrf or dashboard.csrf_token()}, headers=h)

    def test_turn_sniper_and_copy_on_and_off(self):
        import copy_trader as ct
        import rocket_bot
        with patch("live_engine.start"), patch.object(tb, "active_chat", return_value=None):
            self.assertEqual(self.post("rk_off").status_code, 303)
            self.assertFalse(rocket_bot.enabled())
            self.assertEqual(self.post("rk_on").status_code, 303)
            self.assertTrue(rocket_bot.enabled())
            self.assertEqual(self.post("copy_real").status_code, 303)
            self.assertTrue(ct.real_enabled())
            self.assertEqual(self.post("copy_off").status_code, 303)
            self.assertFalse(ct.enabled())

    def test_protected(self):
        self.assertEqual(self.client.post("/painel/acao", data={"acao": "rk_on"}).status_code, 401)
        self.assertEqual(self.post("rk_on", csrf="x").status_code, 403)
        self.assertEqual(self.post("rk_on", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.post("sell_everything").status_code, 400)


class SafeSenderTests(unittest.TestCase):
    def test_exit_cycle_send_never_raises(self):
        import live_engine

        def boom(chat, text, *a, **k):
            raise RuntimeError("Telegram API returned HTTP 502")
        self.assertIsNone(live_engine._safe_sender(boom)(1, "🚀 STOP MÓVEL ATIVADO"))


if __name__ == "__main__":
    unittest.main()
