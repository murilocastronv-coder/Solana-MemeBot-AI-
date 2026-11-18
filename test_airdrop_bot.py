import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

import airdrop_bot as ab
import trade_panel as tp

W = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
FEED = """<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<item><title>Fooswap Airdrop</title><link>https://airdrops.io/fooswap/</link><category>Solana</category>
<description>Follow https://x.com/fooswap and reply https://x.com/fooswap/status/1888888888888888888</description></item>
<item><title>Barchain Airdrop</title><link>https://airdrops.io/bar/</link><category>Ethereum</category><description>eth only</description></item>
<item><title>Scamdrop Airdrop</title><link>https://airdrops.io/scam/</link><category>Solana</category>
<description>Connect your wallet to claim now!</description></item>
<item><title>Bazfi Airdrop</title><link>https://airdrops.io/baz/</link><category>Solana</category><description>follow https://twitter.com/bazfi</description></item>
</channel></rss>"""


class AirdropTest(unittest.TestCase):
    def setUp(self):
        tp._MEM.clear()
        self.p = [mock.patch.object(tp, "is_owner", return_value=True), mock.patch.object(tp, "_save")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_find_keeps_solana_and_drops_scams(self):
        items = ab.find(get=lambda url: FEED)
        self.assertEqual([i["name"] for i in items], ["Fooswap", "Bazfi"])
        self.assertEqual((items[0]["handle"], items[0]["tweet"]), ("fooswap", "1888888888888888888"))
        self.assertEqual((items[1]["handle"], items[1]["tweet"]), ("bazfi", None))

    def test_each_airdrop_gets_a_different_message_and_x_reply_link(self):
        items = ab.find(get=lambda url: FEED)
        tp._load()["airdrop"] = {"wallet": W, "on": True}
        sent = []
        n = ab.run_for(1, lambda c, t, kb=None: sent.append((t, kb)), items)
        self.assertEqual(n, 2)
        msgs = [t.split("já escrita com", 1)[0] for t, _ in sent]
        self.assertNotEqual(msgs[0], msgs[1])
        url = sent[0][1][0][0]["url"]
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q["in_reply_to"], ["1888888888888888888"])
        self.assertIn(W, q["text"][0])
        self.assertTrue(parse_qs(urlparse(sent[1][1][0][0]["url"]).query)["text"][0].startswith("@bazfi "))
        self.assertEqual(ab.run_for(1, lambda *a, **k: None, items), 0)          # already sent: not again

    def test_pasted_x_post_and_wallet_flow(self):
        sent = []
        send = lambda c, t, kb=None: sent.append((t, kb))
        run = lambda fn: fn()
        link = "https://x.com/someproj/status/1999999999999999999"
        self.assertTrue(tp.handle(link, link, send, 1, run_async=run))         # no wallet yet: asks for it
        self.assertIn(1, ab.WAIT)
        self.assertTrue(tp.handle("nada", "nada", send, 1, run_async=run))
        self.assertIn("inválido", sent[-1][0])
        self.assertTrue(tp.handle(W, W, send, 1, run_async=run))
        self.assertEqual(ab.state(1)["wallet"], W)
        self.assertTrue(tp.handle(link, link, send, 1, run_async=run))
        q = parse_qs(urlparse(sent[-1][1][0][0]["url"]).query)
        self.assertEqual(q["in_reply_to"], ["1999999999999999999"])

    def test_nothing_is_posted_automatically(self):
        src = open(ab.__file__, encoding="utf-8").read()
        self.assertNotIn("api.twitter.com", src)
        self.assertNotIn("api.x.com", src)


if __name__ == "__main__":
    unittest.main()
