import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import paris_bot as pb

HEAD = "Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,PSH,PSD,PSA,MaxH,MaxD,MaxA,AvgH,AvgD,AvgA,P>2.5,P<2.5,Max>2.5,Max<2.5,Avg>2.5,Avg<2.5\n"


def row(date, home, away, hg, ag, ps, mx, av, pou, mou, aou):
    return ",".join(["E0", date, "15:00", home, away, str(hg), str(ag)] + [str(x) for x in ps + mx + av + pou + mou + aou]) + "\n"


class Strategy(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.object(pb, "_path", return_value=Path(d) / "paris.json")
        p.start()
        self.addCleanup(p.stop)
        self.msgs = []

    def send(self, chat, text, kb=None):
        self.msgs.append(text)

    def test_fair_probabilities_remove_the_margin(self):
        p = pb.fair_probs([2.0, 3.6, 4.0])
        self.assertAlmostEqual(sum(p), 1.0)
        self.assertGreater(p[0], p[1])
        self.assertIsNone(pb.fair_probs([2.0, None, 4.0]))

    def test_value_bet_only_when_the_price_beats_the_fair_odds(self):
        r = pb.parse_csv(HEAD + row("01/11/2026", "A", "B", 2, 0, [2.0, 3.6, 4.0], [2.25, 3.6, 4.0], [2.05, 3.5, 3.9],
                                     [1.9, 2.0], [1.92, 2.02], [1.85, 1.95]))[0]
        c = pb.candidates(r, "max")
        self.assertEqual([x[0] for x in c], ["H"])                        # home 2.25 vs fair ~2.08: +8%
        self.assertGreater(c[0][3], pb.EDGE)
        self.assertEqual(pb.candidates(r, "avg"), [])                      # the average price has no value

    def test_backtest_counts_wins_losses_and_bank(self):
        csvtxt = HEAD + row("01/11/2026", "A", "B", 2, 0, [2.0, 3.6, 4.0], [2.25, 3.6, 4.0], [2.0, 3.5, 3.9], [1.9, 2.0], [1.9, 2.0], [1.85, 1.95]) \
                      + row("08/11/2026", "C", "D", 0, 1, [2.0, 3.6, 4.0], [2.25, 3.6, 4.0], [2.0, 3.5, 3.9], [1.9, 2.0], [1.9, 2.0], [1.85, 1.95])
        rows = pb.parse_csv(csvtxt)
        for r in rows:
            r["_date"], r["_div"] = pb.parse_date(r), "E0"
        res = pb.backtest(rows, 100.0, "max")
        self.assertEqual((res["bets"], res["wins"]), (2, 1))
        st = round(100 * pb.stake_frac(pb.fair_probs([2.0, 3.6, 4.0])[0], 2.25), 2)
        self.assertAlmostEqual(res["bank"], round(100 + st * 1.25 - round((100 + st * 1.25) * pb.stake_frac(
            pb.fair_probs([2.0, 3.6, 4.0])[0], 2.25), 2), 2), places=1)
        self.assertLessEqual(st, 100 * pb.MAX_STAKE)
        self.assertIn("BACKTEST", pb.backtest_text(res, pb.backtest(rows, 100.0, "avg")))

    def test_history_downloads_each_league_and_keeps_the_last_year(self):
        now = datetime(2026, 11, 24, tzinfo=timezone.utc)
        old = row("01/01/2025", "X", "Y", 1, 1, [2, 3.5, 4], [2.2, 3.5, 4], [2, 3.4, 3.9], [1.9, 2], [1.9, 2], [1.8, 1.9])
        new = row("01/11/2026", "A", "B", 1, 0, [2, 3.5, 4], [2.2, 3.5, 4], [2, 3.4, 3.9], [1.9, 2], [1.9, 2], [1.8, 1.9])
        asked = []

        def get(url):
            asked.append(url)
            return HEAD + old + new
        rows = pb.history(365, get, now)
        self.assertTrue(rows)
        self.assertTrue(all(r["_date"].year == 2026 for r in rows))
        self.assertTrue(any("/2526/" in u for u in asked) and any("/2627/" in u for u in asked))

    def test_signals_are_sent_once(self):
        now = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
        fx = HEAD + row("25/11/2026", "A", "B", "", "", [2.0, 3.6, 4.0], [2.25, 3.6, 4.0], [2.0, 3.5, 3.9],
                        [1.9, 2.0], [1.9, 2.0], [1.85, 1.95])
        d = pb.load()
        d["on"] = True
        pb.save(d)
        n = pb.cycle(self.send, "1", now=now.timestamp(), get=lambda u: fx)
        self.assertEqual(n, 1)
        self.assertIn("vitória do mandante", self.msgs[-1])
        self.assertIn("Odd MÍNIMA", self.msgs[-1])
        self.assertEqual(pb.cycle(self.send, "1", now=now.timestamp(), get=lambda u: fx, force=True), 0)   # not again

    def test_buttons(self):
        cbs = {b["callback_data"] for row_ in pb.keyboard() for b in row_}
        self.assertTrue({"pb_on", "pb_now", "pb_bt", "pb_bank"} <= cbs)
        pb.handle("pb_bank", self.send, "1", lambda fn: fn())
        self.assertEqual(pb.load()["bank"], 200.0)
        self.assertIn("PARIS SPORTIFS", self.msgs[-1])


class SolRobotRetired(unittest.TestCase):
    def test_open_orders_are_closed_and_accounts_off(self):
        import btc_fullmargin as bfm
        d = tempfile.mkdtemp()
        with patch.object(bfm, "_path", side_effect=lambda mode="demo": Path(d) / (mode + ".json")):
            a = bfm._load("demo")
            a.update(on=True, pos={"side": "long", "entry": 70.0, "size_usd": 400.0, "collateral_usd": 10.0, "n": 1,
                                   "opened": 0, "asset": "SOL"})
            bfm._save(a)
            msgs = []
            self.assertTrue(bfm.retire(lambda c, t, k=None: msgs.append(t), "1", price=71.0))
            a = bfm._load("demo")
            self.assertFalse(a["on"])
            self.assertIsNone(a["pos"])
            self.assertIn("ROBÔ SOL REMOVIDO", msgs[-1])


if __name__ == "__main__":
    unittest.main()
