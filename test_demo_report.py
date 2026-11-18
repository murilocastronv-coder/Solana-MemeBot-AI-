import unittest
from unittest import mock

import big_bot as bb
import demo_report as dr
import sniper_demo as sd
import trade_panel as tp

NOW = 1_800_000_000


class DemoReportTest(unittest.TestCase):
    def setUp(self):
        tp._MEM.clear()
        self.p = [mock.patch.object(tp, "is_owner", return_value=True), mock.patch.object(tp, "_save")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_curve_one_point_per_hour_and_per_day(self):
        d = {"started": NOW, "curve": [[NOW, 200]]}
        self.assertFalse(dr.snapshot(d, 201, now=NOW + 600))
        self.assertTrue(dr.snapshot(d, 205, now=NOW + 3700))
        dr.snapshot(d, 230, now=NOW + 86400 + 10)
        self.assertEqual(dr.per_day(d, 200, now=NOW + 86400 + 20), [(0, 205), (1, 230)])
        txt = dr.wallet_text("X", dict(d, start_usd=200, on=True, hist=[{"pnl": 5}]), 230, now=NOW + 86400 + 20)
        self.assertIn("+30.00 USD (+15.0%)", txt)
        self.assertIn("dia 1: 230.00", txt)

    def test_results_text_and_chart_for_both_wallets(self):
        bb.demo_reset(1, now=NOW)
        sd.reset(1, now=NOW)
        b = bb.demo(1)
        dr.snapshot(b, 212, now=NOW + 7200)
        bb.set_demo(1, b)
        text, png = dr.results(1, now=NOW + 7200)
        self.assertIn("ROBÔ MOEDAS GRANDES", text)
        self.assertNotIn("SNIPER", text)                       # owner (Nov 18): sniper removed
        self.assertTrue(png.startswith(b"\x89PNG"))
