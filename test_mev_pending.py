import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jito_arbitrage as j


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "pending.json"
        p = patch.dict(os.environ, {"MEV_PENDING_FILE": str(self.path)})
        p.start(); self.addCleanup(p.stop)

    def add(self, mint, bundle_id="b"):
        j._save_pending({"status": "pending", "bundle_id": bundle_id + mint, "mint": mint}, mint + ":1")

    def test_up_to_five_in_flight(self):
        for i in range(4):
            self.add("M" + str(i))
        self.assertTrue(j._can_submit("NEW")[0])
        self.add("M4")
        ok, why = j._can_submit("NEW")
        self.assertFalse(ok)
        self.assertIn("5 ordens", why)

    def test_same_mint_and_uncertain_block(self):
        self.add("M1")
        self.assertFalse(j._can_submit("M1")[0])
        j._save_pending({"status": "submitting", "mint": "M2"}, "M2:1")
        ok, why = j._can_submit("OTHER")
        self.assertFalse(ok)
        self.assertIn("incerto", why)

    def test_legacy_single_record_is_read(self):
        self.path.write_text(json.dumps({"status": "pending", "bundle_id": "X", "mint": "M"}))
        self.assertEqual(j.pending_summary()["in_flight"], 1)

    def test_reconcile_resolves_each_bundle(self):
        self.add("A"); self.add("B")
        events = []
        with patch.object(j, "_resolve_one", side_effect=lambda r: {"status": "not_landed", "bundle_id": r["bundle_id"]} if r["mint"] == "A" else None), \
             patch("live_trade_state.record_mev_event", side_effect=events.append):
            j.reconcile_pending_bundle()
        self.assertEqual([e["bundle_id"] for e in events], ["bA"])
        self.assertEqual(j.pending_summary()["mints"], {"B"})


class WhyTextTests(unittest.TestCase):
    def test_explains_last_hour(self):
        j._RECENT.clear()
        for _ in range(5):
            j._note({"status": "no_edge"})
        j._note({"status": "skipped", "error": "HTTP_400 (sem rota nessa DEX)"})
        j._note({"status": "live_failed", "error": "simulação: compra: 6001"})
        text = j.why_text()
        self.assertIn("7 análises", text)
        self.assertIn("com lucro: 1", text)
        self.assertIn("Achou lucro mas não enviou: 1× simulação", text)
        self.assertIn("Puladas: 1× HTTP_400", text)
        j._RECENT.clear()
        for _ in range(3):
            j._note({"status": "no_edge"})
        self.assertIn("nenhuma diferença de preço deu lucro", j.why_text())


if __name__ == "__main__":
    unittest.main()
