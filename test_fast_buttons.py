"""Buttons must answer at once even while a background job holds a lock (e.g. waiting on the network)."""
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
import copy_trader as c
import live_trade_state as st
import telegram_bot as tb


class FastButtonTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        tb.CTX.interactive = True
        self.addCleanup(setattr, tb.CTX, "interactive", False)

    def _hold(self, lock, seconds=3.0):
        got, done = threading.Event(), threading.Event()

        def worker():
            with lock:
                got.set()
                done.wait(seconds)
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        got.wait(1)
        self.addCleanup(done.set)
        return done

    def _tap(self, action):
        sent = []
        with patch.object(tb, "authorized", return_value=True), patch.object(tb, "request_api"), \
                patch.object(tb, "send", side_effect=lambda chat, text, kb=None: sent.append(text)), \
                patch.object(tb, "run_async"):
            t = time.monotonic()
            tb.handle({"callback_query": {"id": "1", "data": action, "message": {"chat": {"id": 1, "type": "private"}}}})
            return time.monotonic() - t, sent

    def test_panels_answer_while_locks_are_busy(self):
        self._hold(c._LOCK)
        self._hold(st._LOCK)
        for action in ("copy_status", "copy_following", "copy_positions", "copy_wallets", "/start"):
            took, sent = self._tap(action)
            self.assertLess(took, 1.0, action)
            self.assertTrue(sent, action)

    def test_positions_answer_before_blockchain_check(self):
        with patch.object(c, "recover_real_orphans", side_effect=AssertionError("must run off the tap")):
            took, sent = self._tap("copy_positions")
        self.assertIn("posição", sent[0].lower())

    def test_buy_size_check_does_not_hold_the_lock(self):
        seen = []
        c.follow("7HtLL1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        c.set_mode(True)

        def typical(w, offline=False):
            seen.append(c._LOCK._is_owned())
            return None
        with patch.object(c, "new_signals", return_value=[("buy", "MINTX", 50.0)]), \
                patch.object(c, "typical_buy_usd", side_effect=typical), patch.object(c, "_open", return_value=None):
            c.cycle()
        self.assertEqual(seen, [False])


if __name__ == "__main__":
    unittest.main()
