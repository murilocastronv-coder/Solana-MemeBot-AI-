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
        self.assertEqual(fams, {"breakout", "ema_cross", "rsi_rev", "bb_rev", "squeeze"})
        self.assertGreaterEqual(len(br.entry_configs()) * len(br.exit_configs()), 400)

    def test_no_look_ahead(self):
        cs = walk(3000, 0.0001, seed=4)
        full, cut = br.Data(cs), br.Data(cs[:2000])
        for e in br.entry_configs():
            self.assertEqual(br.signals(full, e)[:2000], br.signals(cut, e), e)

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
        self.assertEqual(rep["tested"], len(br.entry_configs()) * len(br.exit_configs()))
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


if __name__ == "__main__":
    unittest.main()
