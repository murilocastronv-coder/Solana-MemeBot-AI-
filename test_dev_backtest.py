import pathlib
import tempfile
import threading
import time
import unittest
from unittest import mock

import dev_backtest as dbt
import dev_bot as db

DAY = 86400
T0 = 1_790_000_000


def trade(i, res, dev_win=True, start=T0):
    o = start + i * DAY / 2
    return {"opened": o, "closed": o + 600, "result_pct": res, "pnl_usd": 10 if dev_win else -10,
            "mint": "M" + str(i) + "pump", "cost_usd": 50}


class SimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(db, "_path", return_value=pathlib.Path(self.tmp.name) / "d.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_only_qualified_devs_and_no_future_data(self):
        good = [trade(i, 0.5) for i in range(60)]
        bad = [trade(i, -0.5, dev_win=False) for i in range(60)]
        s, trades, rej = dbt.simulate({"G": good, "B": bad}, T0, T0 + 40 * DAY, 0.8, capital=500, usd=15, max_open=2)
        self.assertTrue(trades)
        self.assertTrue(all(t["dev"] == "G" for t in trades))
        self.assertTrue(all(t["opened"] >= good[20]["opened"] for t in trades))   # needs 20 closed trades first
        self.assertIn("DEV com menos de 20 operações antes da data", rej)
        self.assertGreater(s["pnl"], 0)
        self.assertAlmostEqual(trades[0]["pnl_usd"], 15 * (0.5 - dbt.DELAY_COST - 2 * db.COST) - 2 * db.NET_FEE_USD, places=3)

    def test_report_text_has_all_strategies_and_gaps(self):
        d = db._load()
        d["devs"] = {"G": {"hist": [trade(i, 0.5) for i in range(80)]}}
        db._save(d)
        end = T0 + 150 * DAY
        rep = dbt.report(T0, end, "5 meses")
        txt = dbt.text(rep)
        for k in ("A: DEV com acerto ≥ 70%", "B: DEV com acerto ≥ 80%", "C: DEV com acerto ≥ 90%", "D: DEV 80% + Smart Money",
                  "NÃO CALCULADO", "Períodos SEM dados", "Rejeitadas"):
            self.assertIn(k, txt)
        self.assertEqual(rep["days"], 150)
        self.assertIn("estrategia", dbt.to_csv(rep["rows"]))

    def test_job_runs_and_saves_history(self):
        d = db._load()
        d["devs"] = {"G": {"hist": [trade(i, 0.5) for i in range(80)]}}
        db._save(d)
        sent = []
        self.assertEqual(dbt.start(start_ts=T0, end_ts=T0 + 150 * DAY, label="5 meses", send=lambda c, m: sent.append(m), chat=1), "")
        for _ in range(200):
            if dbt.job().get("status") != "rodando":
                break
            time.sleep(0.02)
        self.assertEqual(dbt.job()["status"], "pronto")
        self.assertEqual(len(dbt.runs()), 1)
        self.assertIn("BACKTEST DEV", sent[-1])

    def test_restart_marks_interrupted(self):
        dbt._set_job(status="rodando", stage="x")
        dbt.mark_interrupted()
        self.assertEqual(dbt.job()["status"], "interrompido")


if __name__ == "__main__":
    unittest.main()
