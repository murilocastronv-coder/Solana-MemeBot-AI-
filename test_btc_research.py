import os
import random
import tempfile
import time
import unittest
from unittest.mock import patch

import btc_research as br

T0 = 1_700_000_100 // 900 * 900


def walk(n, drift=0.0, vol=0.003, seed=1):
    rng, p, out = random.Random(seed), 60000.0, []
    for i in range(n):
        o = p
        p *= 1 + drift + rng.gauss(0, vol)
        out.append({"t": T0 + i * 900, "o": o, "h": max(o, p) * (1 + abs(rng.gauss(0, vol / 3))),
                    "l": min(o, p) * (1 - abs(rng.gauss(0, vol / 3))), "c": p, "v": 1.0})
    return out


class SignalTests(unittest.TestCase):
    def test_every_family_has_variants(self):
        fams = {e["family"] for e in br.entry_configs()}
        self.assertEqual(fams, set(br.FAMILY_NAMES))
        self.assertGreaterEqual(len(br.entry_configs()) * len(br.exit_configs()) * len(br.TFS), 2000)

    def test_every_variant_fires_on_a_random_walk(self):
        data = br.Data(walk(3000, seed=5))
        new = {"macd", "supertrend", "keltner", "psar", "ichimoku", "heikin", "adx", "williams"}
        for e in br.entry_configs():
            if e["family"] in new:
                self.assertTrue(any(br.signals(data, e)), e)

    def test_resample_builds_complete_candles(self):
        cs = walk(16 * 10 + 5, seed=3)
        h4 = br.resample(cs, 14400)
        self.assertTrue(all(c["t"] % 14400 == 0 for c in h4))
        g = [c for c in cs if c["t"] // 14400 * 14400 == h4[1]["t"]]
        self.assertEqual((h4[1]["o"], h4[1]["c"], h4[1]["h"]), (g[0]["o"], g[-1]["c"], max(c["h"] for c in g)))

    def test_no_look_ahead(self):
        cs = walk(3000, 0.0001, seed=4)
        full, cut = br.Data(cs), br.Data(cs[:2000])
        for e in br.entry_configs():
            self.assertEqual(br.signals(full, e)[:2000], br.signals(cut, e), e)
        h1 = br.resample(cs, 3600)
        full, cut = br.Data(h1, 3600), br.Data(h1[:500], 3600)
        for e in br.entry_configs():
            self.assertEqual(br.signals(full, e)[:500], br.signals(cut, e), e)

    def test_stop_before_target_in_same_candle(self):
        cs = walk(400, seed=2)
        for c in cs[300:]:
            c.update(o=60000, c=60000, h=60000, l=60000)
        cs[311].update(h=70000, l=50000)          # both stop and target touched
        data = br.Data(cs)
        sig = [None] * len(cs)
        sig[309] = "long"
        r = br.backtest(data, sig, {"sl": 1.0, "rr": 1.0, "max_bars": 96}, 2, 300, len(cs))
        self.assertEqual(r["trades"], 1)
        self.assertLess(r["final"], br.START)


class SearchTests(unittest.TestCase):
    def test_full_year_search_runs_and_reports(self):
        cs = walk(365 * 96 + 400, 0.00001, seed=9)            # random walk: no real edge
        t = time.time()
        rep = br.search(cs)
        took = time.time() - t
        self.assertEqual(rep["tested"], len(br.entry_configs()) * len(br.exit_configs()) * len(br.TFS))
        self.assertEqual(set(rep["per_tf"]), {"15m", "1h", "4h"})
        text = br.report_text(rep)
        self.assertIn("PESQUISA BTC", text)
        if rep["winner"]:
            self.assertIn(rep["winner"]["passed"], (True, False))
        d = tempfile.mkdtemp()
        br.save(rep, os.path.join(d, "r.json"))
        self.assertLess(took, 600)
        print("\\nfull search took %.0f s, qualified %d/%d" % (took, rep["qualified"], rep["tested"]))


class TelegramTests(unittest.TestCase):
    def test_command_starts_research_not_removed_message(self):
        import telegram_bot as tb
        out = []
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "1"}), patch.object(tb, "CHAT", "1"), \
                patch.object(tb, "send", side_effect=lambda chat, text, kb=None: out.append(text)), \
                patch.object(tb, "request_api", return_value={}), patch.object(tb, "run_async") as ra:
            tb.handle({"message": {"chat": {"id": 1, "type": "private"}, "text": "/pesquisa_btc"}})
        self.assertIn("Pesquisa BTC", out[0])
        self.assertIs(ra.call_args[0][0], tb.btc_research_job)


class BtcModeTests(unittest.TestCase):
    def test_owner_menu_is_only_btc(self):
        import telegram_bot as tb
        with patch.object(tb, "OWNER_MENU", "btc"):
            data = [b["callback_data"] for row in tb.keyboard() for b in row]
        self.assertEqual(data, ["btc_research", "btc_last", "btc_dip", "bl_status", "bl_on", "bl_off", "bl_lev2", "bl_lev3", "bl_lev4", "bl_lev5", "bl_usd20", "bl_usd50", "bl_usd100", "bl_usd200", "bl_usdask", "wallet_deposit", "wd_start", "wallet_balance"])

    def test_btc_button_starts_research(self):
        import telegram_bot as tb
        out = []
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "1"}), patch.object(tb, "CHAT", "1"), \
                patch.object(tb, "send", side_effect=lambda chat, text, kb=None: out.append(text)), \
                patch.object(tb, "request_api", return_value={}), patch.object(tb, "run_async") as ra:
            tb.handle({"callback_query": {"id": "1", "data": "btc_research",
                                          "message": {"chat": {"id": 1, "type": "private"}}}})
        self.assertIs(ra.call_args[0][0], tb.btc_research_job)

    def test_restart_notice_has_no_copy_trade_text(self):
        import telegram_bot as tb
        out = []
        with patch.object(tb, "OWNER_MENU", "btc"), patch.object(tb, "active_chat", return_value="1"), \
                patch.object(tb, "send", side_effect=lambda chat, text, kb=None: out.append(text)), \
                patch("live_trade_state.status", return_value={"positions": 1, "pending": 0}):
            tb.startup_notice()
        self.assertEqual(len(out), 1)
        self.assertIn("MODO BTC", out[0])
        self.assertNotIn("COPY", out[0])

    def test_stop_all_buying(self):
        import telegram_bot as tb
        with patch("copy_trader.set_mode") as cm, patch("rocket_bot.set_enabled") as re_, \
                patch("rocket_bot.set_signals") as rs, patch("signals.set_enabled") as se, patch("signals.set_auto") as sa:
            tb.stop_all_buying()
        cm.assert_called_with(False); re_.assert_called_with(False); rs.assert_called_with(False)
        se.assert_called_with(False); sa.assert_called_with(False)


if __name__ == "__main__":
    unittest.main()
