import os
import subprocess
import tempfile
import unittest
from unittest import mock

import video_bot as vb

RSS = """<?xml version="1.0"?><rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>Morte de cantor</title><ht:approx_traffic>500+</ht:approx_traffic>
<ht:news_item><ht:news_item_title>Cantor morre aos 50</ht:news_item_title><ht:news_item_source>G1</ht:news_item_source></ht:news_item></item>
<item><title>Flamengo</title><ht:approx_traffic>200.000+</ht:approx_traffic>
<ht:news_item><ht:news_item_title>Flamengo vence o clássico e assume a liderança</ht:news_item_title>
<ht:news_item_snippet>Time fez 3 a 1 no Maracanã.</ht:news_item_snippet><ht:news_item_url>https://ge.globo.com/x</ht:news_item_url>
<ht:news_item_source>ge</ht:news_item_source></ht:news_item></item>
<item><title>iPhone 18</title><ht:approx_traffic>50.000+</ht:approx_traffic>
<ht:news_item><ht:news_item_title>Apple mostra o novo iPhone</ht:news_item_title><ht:news_item_source>Tecmundo</ht:news_item_source></ht:news_item></item>
</channel></rss>"""


def fake_voice(text, path):
    subprocess.run([vb._ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "2",
                    "-c:a", "libmp3lame", path], check=True)
    return path


class VideoBotTest(unittest.TestCase):
    def setUp(self):
        vb._MEM.clear()
        vb._MEM.update({"on": False, "done": [], "log": [], "yt_refresh": "", "used_topics": []})
        self.p = mock.patch.object(vb, "_save")
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_trending_topics_skip_sensitive_and_used(self):
        trends = vb.parse_trends(RSS)
        self.assertEqual([t["topic"] for t in trends], ["Morte de cantor", "Flamengo", "iPhone 18"])
        self.assertEqual(vb.pick_topic(trends, set())["topic"], "Flamengo")
        self.assertEqual(vb.pick_topic(trends, {"flamengo"})["topic"], "iPhone 18")

    def test_script_names_the_source(self):
        sc = vb.script(vb.parse_trends(RSS)[1])
        self.assertIn("Segundo ge:", " ".join(sc["parts"]))
        self.assertIn("#Shorts", sc["title"])
        self.assertIn("https://ge.globo.com/x", sc["description"])

    def test_renders_a_vertical_mp4(self):
        sc = vb.script(vb.parse_trends(RSS)[1])
        d = tempfile.mkdtemp()
        out = vb.render(sc, d, voice=fake_voice)
        self.assertTrue(out.endswith(".mp4") and os.path.getsize(out) > 10_000)
        info = subprocess.run([vb._ffmpeg(), "-i", out], capture_output=True, text=True).stderr
        self.assertIn("1080x1920", info)
        self.assertLess(vb.duration(out), 60)
        self.assertEqual([f for f in os.listdir(d)], [os.path.basename(out)])     # temporary files removed

    def test_five_slots_once_each(self):
        made = []
        vb._MEM["on"] = True
        t = 1_800_000_000 - 1_800_000_000 % 86400 + 12 * 3600 + 60        # 12:01 UTC
        with mock.patch.object(vb, "cleanup"):
            vb.tick(now=t, make=lambda s, c: made.append(1))
            vb.tick(now=t + 600, make=lambda s, c: made.append(1))         # same slot: not again
            vb.tick(now=t + 3 * 3600, make=lambda s, c: made.append(1))    # 15:01: next slot
            vb.tick(now=t + 3600, make=lambda s, c: made.append(1))        # 13:01: no slot
        self.assertEqual(len(made), 2)
        self.assertEqual(len(vb.SLOTS_UTC[:vb.PER_DAY]), 5)

    def test_make_and_post_uploads(self):
        posted = []
        with mock.patch.object(vb, "folder", return_value=tempfile.mkdtemp()):
            line = vb.make_and_post(get=lambda u: RSS, render_fn=lambda sc, f: vb.render(sc, f, voice=fake_voice),
                                    yt=lambda p, sc: posted.append("yt") or "abc123", ig=lambda p, sc: posted.append("ig") or "9")
        self.assertEqual(posted, ["yt", "ig"])
        self.assertIn("youtube.com/shorts/abc123", line)
        self.assertIn("flamengo", vb._MEM["used_topics"])


if __name__ == "__main__":
    unittest.main()


class AutomaticTest(unittest.TestCase):
    def test_on_by_default(self):
        vb._MEM.clear()
        with mock.patch("pg_store.enabled", return_value=False):
            self.assertTrue(vb._load()["on"])
