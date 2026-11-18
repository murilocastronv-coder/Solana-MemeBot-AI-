"""Vídeos automáticos (owner, Nov 18): 5 short videos a day in Portuguese about what is trending in Brazil right now,
posted to YouTube (Shorts) and Instagram (Reels). Free tools only:
  - topics: Google Trends "trending now" RSS for Brazil (with the related news title / snippet / source);
  - sensitive topics (deaths, crimes, tragedies, politics...) are skipped;
  - script: written from the trend and its news (no AI), with the source named;
  - voice: edge-tts (pt-BR neural voice), falling back to gTTS;
  - video: 1080x1920 slides (Pillow) + narration, encoded by ffmpeg (imageio-ffmpeg) - no copyrighted images;
  - YouTube: Data API v3 resumable upload, owner connects once with Google's device code flow (YT_CLIENT_ID /
    YT_CLIENT_SECRET on Render). Videos from unverified API projects stay PRIVATE until Google's audit.
  - Instagram: Graph API Reels (IG_USER_ID / IG_ACCESS_TOKEN on Render); the video is served at PUBLIC_URL/videos/...
"""
import asyncio
import html
import json
import logging
import os
import re
import secrets
import subprocess
import textwrap
import threading
import time
import xml.etree.ElementTree as ET

import requests

KEY = "video_bot"
TRENDS_URL = os.getenv("VIDEO_TRENDS_URL", "https://trends.google.com/trending/rss?geo=BR")
PER_DAY = int(os.getenv("VIDEO_PER_DAY", "5"))
SLOTS_UTC = tuple(int(h) for h in os.getenv("VIDEO_SLOTS_UTC", "12,15,18,21,0").split(","))   # 9, 12, 15, 18, 21 h Brasília
VOICE = os.getenv("VIDEO_VOICE", "pt-BR-AntonioNeural")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://solana-memebot-telegram.onrender.com").rstrip("/")
W, H = 1080, 1920
BLOCK = re.compile(r"mort|morre|óbito|assassin|homic|tiroteio|tiro|baleado|acidente|tragédia|tragedia|estupr|abus|suicid|"
                   r"sequestr|ataque|atentado|guerra|crime|preso|presa|prisão|polícia|policia|velório|enterro|luto|câncer|"
                   r"doença|hospital|eleiç|eleic|bolsonaro|lula|stf|senado|deputad|partido|terror|violên|violenc|faleceu|vítima|"
                   r"vitima|incêndio|incendio|enchente|desabamento|desaparec|aposta|bet|tigrinho|cassino", re.I)
_LOCK = threading.RLock()
_MEM = {}
YT_SCOPE = "https://www.googleapis.com/auth/youtube"


# ---------------------------------------------------------------- state (pg_store)
def _load():
    with _LOCK:
        if not _MEM:
            d = {"on": False, "done": [], "log": [], "yt_refresh": "", "used_topics": []}
            try:
                import pg_store
                if pg_store.enabled():
                    d.update(pg_store.get(KEY) or {})
            except Exception:
                pass
            _MEM.update(d)
        return _MEM


def _save():
    try:
        import pg_store
        if pg_store.enabled():
            pg_store.put(KEY, json.loads(json.dumps(_MEM)))
    except Exception as exc:
        logging.warning("video save: %s", type(exc).__name__)


def note(text):
    with _LOCK:
        d = _load()
        d["log"] = (d.get("log") or [])[-29:] + [time.strftime("%d/%m %H:%M", time.gmtime()) + " " + text]
        _save()
    logging.info("video: %s", text)


# ---------------------------------------------------------------- topics
def _clean(t):
    t = html.unescape(re.sub(r"<[^>]+>", " ", str(t or "")))
    return re.sub(r"\s+", " ", t).strip()


def parse_trends(xml_text):
    """Google Trends RSS -> [{topic, traffic, news: [{title, snippet, source, url}]}]."""
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    ht = "{https://trends.google.com/trending/rss}"
    for it in root.iter("item"):
        topic = _clean(it.findtext("title"))
        news = []
        for n in it.iter(ht + "news_item"):
            news.append({"title": _clean(n.findtext(ht + "news_item_title")), "snippet": _clean(n.findtext(ht + "news_item_snippet")),
                         "source": _clean(n.findtext(ht + "news_item_source")), "url": _clean(n.findtext(ht + "news_item_url"))})
        if topic:
            out.append({"topic": topic, "traffic": _clean(it.findtext(ht + "approx_traffic")), "news": [n for n in news if n["title"]]})
    return out


def safe(trend):
    blob = trend["topic"] + " " + " ".join(n["title"] + " " + n["snippet"] for n in trend["news"])
    return bool(trend["news"]) and not BLOCK.search(blob)


def pick_topic(trends, used):
    for t in trends:
        if safe(t) and t["topic"].lower() not in used:
            return t
    return None


def script(trend):
    """Short narration (about 30-45 s) from the trend and its news; the source is always named."""
    topic = trend["topic"]
    n1 = trend["news"][0]
    parts = [topic + ": por que todo mundo está falando disso hoje?",
             topic + " está entre os assuntos mais buscados do Brasil agora" +
             (", com mais de " + trend["traffic"].replace("+", "") + " buscas." if trend["traffic"] else ".")]
    parts.append("Segundo " + (n1["source"] or "a imprensa") + ": " + n1["title"].rstrip(".") + ".")
    if n1["snippet"] and n1["snippet"][:40] not in n1["title"]:
        parts.append(textwrap.shorten(n1["snippet"], 220, placeholder="..."))
    if len(trend["news"]) > 1 and trend["news"][1]["title"] != n1["title"]:
        n2 = trend["news"][1]
        parts.append("E " + (n2["source"] or "outro site") + " destaca: " + n2["title"].rstrip(".") + ".")
    parts.append("E você, o que acha? Comenta aqui e se inscreve para ver os assuntos do dia.")
    tag = re.sub(r"[^0-9A-Za-zÀ-ÿ]", "", topic)[:30]
    title = textwrap.shorten(topic + ": entenda por que está em alta", 90, placeholder="...") + " #Shorts"
    sources = "\n".join("Fonte: " + n["source"] + " - " + n["url"] for n in trend["news"][:3] if n["url"])
    desc = (topic + " está entre os assuntos mais buscados do Brasil hoje. Resumo rápido do que aconteceu.\n\n" + sources +
            "\n\n#" + tag + " #EmAlta #Notícias #Brasil #Shorts")
    return {"topic": topic, "parts": parts, "title": title, "description": desc[:4900],
            "tags": [topic[:30], "em alta", "notícias", "brasil", "shorts"], "caption": desc[:2100]}


# ---------------------------------------------------------------- video
def _font(size, bold=True):
    from PIL import ImageFont
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


def slide(text, path, idx, total, topic):
    """One 1080x1920 slide: gradient, 'EM ALTA NO BRASIL', the sentence in big letters, progress bar."""
    from PIL import Image, ImageDraw
    hue = sum(map(ord, topic)) % 5
    top, bottom = [((20, 24, 60), (120, 20, 90)), ((10, 50, 70), (10, 120, 110)), ((60, 10, 20), (160, 60, 10)),
                   ((15, 15, 15), (60, 60, 90)), ((30, 10, 70), (10, 90, 160))][hue]
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        f = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * f) for i in range(3)))
    d.text((W // 2, 170), "EM ALTA NO BRASIL", font=_font(64), fill=(255, 210, 60), anchor="mm")
    d.text((W // 2, 250), textwrap.shorten(topic.upper(), 28, placeholder="..."), font=_font(48), fill=(255, 255, 255), anchor="mm")
    size = 78 if len(text) < 90 else 64 if len(text) < 170 else 54
    lines = textwrap.wrap(text, width=int(1450 / size))
    f = _font(size)
    y = H // 2 - len(lines) * (size + 18) // 2
    for line in lines:
        d.text((W // 2, y), line, font=f, fill=(255, 255, 255), anchor="mm", stroke_width=3, stroke_fill=(0, 0, 0))
        y += size + 18
    d.rectangle([80, H - 160, 80 + int((W - 160) * (idx + 1) / total), H - 140], fill=(255, 210, 60))
    d.text((W // 2, H - 90), "Se inscreva para os assuntos do dia", font=_font(40, False), fill=(230, 230, 230), anchor="mm")
    img.save(path)


def tts(text, path):
    """Narration mp3: edge-tts neural voice, else gTTS."""
    try:
        import edge_tts
        asyncio.run(edge_tts.Communicate(text, VOICE, rate="+8%").save(path))
        if os.path.getsize(path) > 1000:
            return path
    except Exception as exc:
        logging.info("edge-tts: %s", type(exc).__name__)
    from gtts import gTTS
    gTTS(text, lang="pt", tld="com.br").save(path)
    return path


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def duration(path):
    out = subprocess.run([_ffmpeg(), "-i", path], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", out)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def render(sc, folder, voice=None):
    """The mp4 (vertical, < 60 s) for a script. Light on memory: still images + one audio track, 1 thread."""
    voice = voice or tts
    os.makedirs(folder, exist_ok=True)
    tag = secrets.token_hex(8)
    lst, auds = [], []
    total = len(sc["parts"])
    for i, part in enumerate(sc["parts"]):
        a = os.path.join(folder, tag + "_%d.mp3" % i)
        voice(part, a)
        dur = max(1.5, duration(a)) + 0.25
        img = os.path.join(folder, tag + "_%d.png" % i)
        slide(part, img, i, total, sc["topic"])
        lst.append((img, dur))
        auds.append(a)
    if sum(d for _, d in lst) > 58:                      # Shorts: under 60 s
        raise ValueError("narração longa demais")
    concat = os.path.join(folder, tag + "_v.txt")
    with open(concat, "w") as f:
        for img, dur in lst:
            f.write("file '%s'\nduration %.2f\n" % (img, dur))
        f.write("file '%s'\n" % lst[-1][0])
    alist = os.path.join(folder, tag + "_a.txt")
    with open(alist, "w") as f:
        for a, (_, dur) in zip(auds, lst):
            f.write("file '%s'\n" % a)
    audio = os.path.join(folder, tag + "_a.m4a")
    subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", alist, "-c:a", "aac", "-b:a", "128k", audio],
                   check=True, timeout=120)
    out = os.path.join(folder, tag + ".mp4")
    subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", concat, "-i", audio,
                    "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-threads", "1",
                    "-c:a", "copy", "-shortest", "-movflags", "+faststart", out], check=True, timeout=300)
    for p in [x for x, _ in lst] + auds + [concat, alist, audio]:
        try:
            os.remove(p)
        except OSError:
            pass
    return out


def folder():
    import user_wallets
    return str(user_wallets._base_dir() / "videos")


def cleanup(now=None):
    now = now or time.time()
    try:
        for name in os.listdir(folder()):
            p = os.path.join(folder(), name)
            if now - os.path.getmtime(p) > 2 * 86400:
                os.remove(p)
    except OSError:
        pass


# ---------------------------------------------------------------- YouTube
def yt_configured():
    return bool(os.getenv("YT_CLIENT_ID") and os.getenv("YT_CLIENT_SECRET"))


def yt_device_start():
    """Google device code: the owner opens the URL and types the code; returns (url, code, device_code, interval)."""
    r = requests.post("https://oauth2.googleapis.com/device/code", data={"client_id": os.environ["YT_CLIENT_ID"], "scope": YT_SCOPE},
                      timeout=20)
    r.raise_for_status()
    d = r.json()
    return d["verification_url"], d["user_code"], d["device_code"], int(d.get("interval", 5)), int(d.get("expires_in", 1800))


def yt_device_wait(device_code, interval, expires_in, sleep=time.sleep):
    deadline = time.time() + expires_in
    while time.time() < deadline:
        sleep(interval)
        r = requests.post("https://oauth2.googleapis.com/token", timeout=20, data={
            "client_id": os.environ["YT_CLIENT_ID"], "client_secret": os.environ["YT_CLIENT_SECRET"],
            "device_code": device_code, "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        d = r.json()
        if d.get("refresh_token"):
            with _LOCK:
                _load()["yt_refresh"] = d["refresh_token"]
                _save()
            return True
        if d.get("error") not in ("authorization_pending", "slow_down"):
            return False
    return False


def yt_token():
    r = requests.post("https://oauth2.googleapis.com/token", timeout=20, data={
        "client_id": os.environ["YT_CLIENT_ID"], "client_secret": os.environ["YT_CLIENT_SECRET"],
        "refresh_token": _load()["yt_refresh"], "grant_type": "refresh_token"})
    r.raise_for_status()
    return r.json()["access_token"]


def yt_upload(path, sc):
    """Resumable upload; returns the video id."""
    tok = yt_token()
    meta = {"snippet": {"title": sc["title"][:100], "description": sc["description"], "tags": sc["tags"], "categoryId": "25",
                        "defaultLanguage": "pt-BR", "defaultAudioLanguage": "pt-BR"},
            "status": {"privacyStatus": os.getenv("VIDEO_YT_PRIVACY", "public"), "selfDeclaredMadeForKids": False}}
    r = requests.post("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
                      headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json; charset=UTF-8",
                               "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(os.path.getsize(path))},
                      data=json.dumps(meta), timeout=30)
    r.raise_for_status()
    with open(path, "rb") as f:
        up = requests.put(r.headers["Location"], data=f, headers={"Content-Type": "video/mp4"}, timeout=600)
    up.raise_for_status()
    return up.json()["id"]


# ---------------------------------------------------------------- Instagram
def ig_configured():
    return bool(os.getenv("IG_USER_ID") and os.getenv("IG_ACCESS_TOKEN"))


def ig_upload(path, sc, sleep=time.sleep):
    """Reels via the Graph API: Meta downloads the file from PUBLIC_URL/videos/<name>."""
    g = "https://graph.facebook.com/v21.0/"
    tok, uid = os.environ["IG_ACCESS_TOKEN"], os.environ["IG_USER_ID"]
    url = PUBLIC_URL + "/videos/" + os.path.basename(path)
    r = requests.post(g + uid + "/media", timeout=30, data={"media_type": "REELS", "video_url": url, "caption": sc["caption"],
                                                             "share_to_feed": "true", "access_token": tok})
    r.raise_for_status()
    cid = r.json()["id"]
    for _ in range(60):
        sleep(10)
        st = requests.get(g + cid, params={"fields": "status_code", "access_token": tok}, timeout=20).json().get("status_code")
        if st == "FINISHED":
            break
        if st == "ERROR":
            raise RuntimeError("Instagram recusou o vídeo")
    p = requests.post(g + uid + "/media_publish", data={"creation_id": cid, "access_token": tok}, timeout=30)
    p.raise_for_status()
    return p.json()["id"]


# ---------------------------------------------------------------- one video, schedule
def make_and_post(send=None, chat=None, get=None, render_fn=None, yt=None, ig=None):
    """Picks a trending topic, renders the video, posts it. Returns a summary line."""
    d = _load()
    body = get(TRENDS_URL) if get else requests.get(TRENDS_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).text
    trend = pick_topic(parse_trends(body), set(d.get("used_topics") or []))
    if not trend:
        note("nenhum assunto em alta seguro agora")
        return "Nenhum assunto em alta seguro agora (os sensíveis eu pulo). Tento no próximo horário."
    sc = script(trend)
    path = (render_fn or render)(sc, folder())
    links = []
    if (yt or yt_upload) and (yt or (yt_configured() and d.get("yt_refresh"))):
        try:
            vid = (yt or yt_upload)(path, sc)
            links.append("YouTube: https://youtube.com/shorts/" + vid)
        except Exception as exc:
            links.append("YouTube falhou: " + type(exc).__name__ + " " + str(exc)[:80])
    if ig or ig_configured():
        try:
            links.append("Instagram: post " + str((ig or ig_upload)(path, sc)))
        except Exception as exc:
            links.append("Instagram falhou: " + type(exc).__name__ + " " + str(exc)[:80])
    with _LOCK:
        d["used_topics"] = (list(d.get("used_topics") or []) + [trend["topic"].lower()])[-200:]
        _save()
    line = "🎬 " + sc["topic"] + " · " + (" · ".join(links) if links else "vídeo pronto (YouTube/Instagram ainda não conectados)")
    note(line)
    if send and chat:
        send(chat, line + ("\n📁 " + PUBLIC_URL + "/videos/" + os.path.basename(path)))
    return line


def due_slot(now=None):
    """The 'YYYY-MM-DD HH' slot that should run now (first 50 min after each slot hour), or None."""
    now = now or time.time()
    t = time.gmtime(now)
    if t.tm_hour in SLOTS_UTC[:PER_DAY] and t.tm_min < 50:
        return time.strftime("%Y-%m-%d %H", t)
    return None


def tick(send=None, chat=None, now=None, make=None):
    d = _load()
    if not d.get("on"):
        return None
    slot = due_slot(now)
    if not slot or slot in (d.get("done") or []):
        return None
    with _LOCK:
        d["done"] = (list(d.get("done") or []) + [slot])[-50:]
        _save()
    try:
        return (make or make_and_post)(send, chat)
    except Exception as exc:
        note("erro: " + type(exc).__name__ + " " + str(exc)[:100])
        return None
    finally:
        cleanup(now)


def status_text():
    d = _load()
    L = ["🎬 VÍDEOS AUTOMÁTICOS — " + ("🟢 LIGADO" if d.get("on") else "🔴 DESLIGADO"),
         "Assuntos em alta no Brasil agora (Google Trends) · " + str(PER_DAY) + " vídeos por dia, às " +
         ", ".join(str((h - 3) % 24) + "h" for h in SLOTS_UTC[:PER_DAY]) + " (Brasília) · em português · narração e legenda",
         "Pulo assuntos sensíveis (mortes, crimes, tragédias, política, apostas).",
         "▶️ YouTube: " + ("✅ conectado" if d.get("yt_refresh") else ("toque em 🔗 Conectar YouTube" if yt_configured()
                                                                    else "falta YT_CLIENT_ID e YT_CLIENT_SECRET no Render")),
         "📸 Instagram: " + ("✅ configurado" if ig_configured() else "falta IG_USER_ID e IG_ACCESS_TOKEN no Render")]
    if d.get("log"):
        L.append("Últimos:\n" + "\n".join(d["log"][-5:]))
    return "\n".join(L)


def keyboard():
    d = _load()
    return [[{"text": "⏸️ Desligar" if d.get("on") else "▶️ Ligar", "callback_data": "vid_off" if d.get("on") else "vid_on"},
             {"text": "🎬 Fazer 1 agora", "callback_data": "vid_now"}],
            [{"text": "🔗 Conectar YouTube", "callback_data": "vid_yt"}, {"text": "🔄 Atualizar", "callback_data": "vid"}]]


def handle(action, send, chat, tail, run_async):
    d = _load()
    if action in ("vid_on", "vid_off"):
        with _LOCK:
            d["on"] = action == "vid_on"
            _save()
    elif action == "vid_now":
        send(chat, "🎬 Fazendo um vídeo agora (leva 1 a 3 minutos)…")
        run_async(lambda: send(chat, make_and_post(send, chat), tail))
        return True
    elif action == "vid_yt":
        if not yt_configured():
            send(chat, "Para conectar o YouTube, coloque no Render as variáveis YT_CLIENT_ID e YT_CLIENT_SECRET "
                 "(do Google Cloud, tipo 'TV e dispositivos de entrada limitada'). Depois toque de novo aqui.", tail)
            return True

        def go():
            try:
                url, code, dev, interval, exp = yt_device_start()
            except Exception as exc:
                send(chat, "⛔ Google recusou: " + type(exc).__name__ + " " + str(exc)[:120], tail)
                return
            send(chat, "🔗 Abra " + url + " no celular ou PC, entre na conta do seu canal e digite o código:\n" + code +
                 "\nTem " + str(exp // 60) + " minutos. Eu aviso quando conectar.")
            ok = yt_device_wait(dev, interval, exp)
            send(chat, "✅ YouTube conectado! Os vídeos vão para o seu canal." if ok else "⛔ Não conectou (código expirou ou foi recusado).", tail)
        run_async(go)
        return True
    send(chat, status_text(), keyboard() + tail)
    return True
