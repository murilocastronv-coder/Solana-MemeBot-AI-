import unittest
from unittest import mock

import superteam_bot as sb
import trade_panel as tp

NOW = 1_800_000_000
LISTINGS = {"listings": [
    {"slug": "write-thread", "title": "Write a thread about Kamino", "status": "OPEN", "deadline": "2027-01-30T12:00:00Z",
     "rewardAmount": 300, "token": "USDC", "type": "bounty", "sponsor": {"name": "Kamino"}, "skills": [{"skills": "Content"}]},
    {"slug": "build-app", "title": "Build a Solana Blinks app", "status": "OPEN", "deadline": "2027-01-30T12:00:00Z",
     "rewardAmount": 2000, "token": "USDC", "type": "project", "sponsor": {"name": "Dialect"}, "skills": [{"skills": "Frontend"}]},
    {"slug": "old", "title": "Closed one", "status": "OPEN", "deadline": "2020-01-01T00:00:00Z", "rewardAmount": 100},
    {"slug": "tiny", "title": "Translate a page", "status": "OPEN", "rewardAmount": 20, "token": "USDC", "skills": ["Writing"]},
]}


class SuperteamTest(unittest.TestCase):
    def setUp(self):
        tp._MEM.clear()
        self.p = [mock.patch.object(tp, "is_owner", return_value=True), mock.patch.object(tp, "_save")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_parse_open_tasks_only(self):
        items = sb.fetch(get=lambda url: LISTINGS, now=NOW)
        self.assertEqual([i["slug"] for i in items], ["write-thread", "build-app", "tiny"])
        self.assertTrue(items[1]["dev"])
        self.assertFalse(items[0]["dev"])
        self.assertEqual(items[0]["link"], "https://earn.superteam.fun/listing/write-thread")

    def test_first_endpoint_failing_uses_the_next(self):
        calls = []

        def get(url):
            calls.append(url)
            if len(calls) == 1:
                raise RuntimeError("HTTP 404")
            return LISTINGS["listings"]
        self.assertEqual(len(sb.fetch(get=get, now=NOW)), 3)

    def test_filters_and_no_repeats(self):
        items = sb.fetch(get=lambda url: LISTINGS, now=NOW)
        a = sb.state(1)
        a.update(nocode=True, min=50)
        sb.set_state(1, a)
        sent = []
        self.assertEqual(sb.run_for(1, lambda c, t, kb=None: sent.append((t, kb)), items), 1)   # no code and >= 50
        self.assertIn("300 USDC", sent[0][0])
        self.assertEqual(sent[0][1][0][0]["url"], "https://earn.superteam.fun/listing/write-thread")
        self.assertEqual(sb.run_for(1, lambda *a, **k: None, items), 0)

    def test_buttons(self):
        sent = []
        sb.handle("sup_code", lambda c, t, kb=None: sent.append(t), 1, [], lambda fn: fn())
        self.assertTrue(sb.state(1)["nocode"])
        sb.handle("sup_min", lambda *a, **k: None, 1, [], lambda fn: fn())
        self.assertEqual(sb.state(1)["min"], 50)
        self.assertIn("SEM programação", sent[-1])


if __name__ == "__main__":
    unittest.main()
