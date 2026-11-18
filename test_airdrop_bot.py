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


class AirdropOnlyTest(unittest.TestCase):
    def test_each_source_read_at_most_every_10_min(self):
        class R:
            status_code, text, headers = 200, FEED, {}

            def raise_for_status(self):
                pass
        ab._FEED_CACHE.clear()
        ab._LAST_READ.clear()
        with mock.patch.object(ab.requests, "get", return_value=R()) as g:
            ab._fetch("v")
            ab._fetch("v")
        self.assertEqual(g.call_count, 1)

    def test_trading_buttons_removed_selling_kept(self):
        self.assertTrue(tp.AIRDROP_ONLY)
        for a in ("tbuy", "tb:0.1:X", "trob", "trb_on", "tlo:X", "tdo:X", "tset", "tcopy", "tscan"):
            self.assertTrue(tp.cb_blocked(a), a)
        for a in ("tad", "tad_on", "thome", "tpl", "ts:100:X", "tsx:100:X", "towner"):
            self.assertFalse(tp.cb_blocked(a), a)

    def test_home_is_the_airdrop_hunter(self):
        tp._MEM.clear()
        with mock.patch.object(tp, "is_owner", return_value=True), mock.patch.object(tp, "_save"), \
                mock.patch.object(tp, "positions", return_value=[]):
            self.assertIn("CAÇADOR DE AIRDROPS", tp.home_text(1))
            data = {b.get("callback_data") for row in tp.home_keyboard(1) for b in row}
        self.assertIn("tad_now", data)
        self.assertNotIn("tbuy", data)

    def test_feed_not_downloaded_again_when_unchanged(self):
        calls = []

        class R:
            def __init__(self, code, text="", headers=None):
                self.status_code, self.text, self.headers = code, text, headers or {}

            def raise_for_status(self):
                pass
        replies = [R(200, FEED, {"ETag": "abc"}), R(304)]
        with mock.patch.object(ab.requests, "get", side_effect=lambda url, **k: calls.append(k["headers"]) or replies.pop(0)), \
                mock.patch.object(ab, "FEED_EVERY_S", 0):
            ab._FEED_CACHE.clear()
            self.assertEqual(ab._fetch("u"), FEED)
            self.assertEqual(ab._fetch("u"), FEED)
        self.assertEqual(calls[1].get("If-None-Match"), "abc")


ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Kamino airdrop season 3 on Solana is live</title><link href="https://www.reddit.com/r/solana/comments/abc/"/>
<content>Official: https://x.com/KaminoFinance/status/1777777777777777777</content></entry>
<entry><title>Free SOL airdrop - connect your wallet to claim</title><link href="https://www.reddit.com/r/solana/comments/def/"/>
<content>scam</content></entry></feed>"""
NEWS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Kamino airdrop season 3 on Solana is live - CoinDesk</title><link>https://news.example/kamino</link><description>solana airdrop</description></item>
<item><title>Bitcoin hits new high - Reuters</title><link>https://news.example/btc</link><description>no</description></item>
</channel></rss>"""


class MoreSourcesTest(unittest.TestCase):
    def test_atom_news_dedupe_and_scam(self):
        with mock.patch.object(ab, "FEEDS", ("atom", "news")):
            items = ab.find(get=lambda url: ATOM if url == "atom" else NEWS)
        self.assertEqual(len(items), 1)                    # same title from 2 sources: once; scam and BTC news dropped
        self.assertEqual(items[0]["tweet"], "1777777777777777777")
        self.assertEqual(items[0]["name"], "Kamino")
        self.assertEqual(ab.project_name({"title": "Kamino airdrop season 3 on Solana is live - CoinDesk"}), "Kamino")

    def test_refusing_source_is_skipped_for_a_while(self):
        class R:
            status_code, text, headers = 403, "", {}
        ab._FEED_FAIL.clear()
        ab._FEED_CACHE.clear()
        ab._LAST_READ.clear()
        with mock.patch.object(ab.requests, "get", return_value=R()) as g, mock.patch.object(ab, "FEEDS", ("u",)):
            self.assertEqual(ab.find(), [])
            self.assertEqual(ab.find(), [])
        self.assertEqual(g.call_count, 1)


class LanguagesTest(unittest.TestCase):
    def test_pt_fr_en_sources_and_messages(self):
        langs = {"pt": "hl=pt-BR", "fr": "hl=fr&", "en": "hl=en-US"}
        for code, frag in langs.items():
            self.assertTrue(any(frag in u for u in ab.DEFAULT_FEEDS), code)
        txt = " ".join(ab.TEMPLATES)
        for word in ("carteira", "wallet Solana :", "My Solana address"):
            self.assertIn(word, txt)
        msgs = {ab.message("Foo", W, "k" + str(i))[0] for i in range(60)}
        self.assertTrue(any("Mon wallet" in m or "Adresse SOL" in m or "Je " in m or "Partant" in m for m in msgs))
