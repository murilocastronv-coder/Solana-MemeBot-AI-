"""Airdrops (owner, Nov 18): the bot finds Solana airdrops several times a day and sends each one to the account in
Telegram with a DIFFERENT reply already written with the account's wallet and an "Abrir no X" button. The person
reads it and posts it themselves on X (nothing is posted automatically: automated replies to posts found by search are
against X's rules and get accounts banned). Pasting the link of an X post also prepares the reply.

Sources: airdrop RSS feeds (AIRDROP_FEEDS), filtered to Solana; obvious scams ("connect wallet to claim", seed
phrase...) are dropped. Each account's wallet / alerts / seen items live in its trade settings ("airdrop")."""
import hashlib
import logging
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests

# Nov 18: airdrops.io refuses the server (HTTP error), so news and Reddit feeds that accept servers are read too
def _gnews(q, lang="en"):
    loc = {"en": "hl=en-US&gl=US&ceid=US:en", "pt": "hl=pt-BR&gl=BR&ceid=BR:pt-419", "fr": "hl=fr&gl=FR&ceid=FR:fr"}[lang]
    return "https://news.google.com/rss/search?q=" + q.replace(" ", "+") + "+when:7d&" + loc


# Nov 18 (owner: "everywhere that has Solana airdrops"): news searches, Medium and airdrop sites. Reddit (404 / 429) and
# airdrops.io (403) refuse the server on Render, so they were taken out (they can be added back with AIRDROP_FEEDS). A source that
# refuses the server is skipped for 30 min; the same story from two sources is sent once.
DEFAULT_FEEDS = (
    _gnews("solana airdrop"), _gnews("solana airdrop claim"), _gnews("solana airdrop eligibility"),
    _gnews("SPL token airdrop"), _gnews("solana points airdrop"), _gnews("solana ecosystem airdrop"),
    _gnews("airdrop solana", "pt"), _gnews("airdrop na solana", "pt"),
    _gnews("airdrop solana", "fr"), _gnews("largage aérien solana", "fr"),        # owner (Nov 18): PT, FR and EN
    _gnews("solana airdrop", "pt"), _gnews("solana airdrop", "fr"), _gnews("jupiter airdrop"), _gnews("pump.fun airdrop"),
    _gnews("solana testnet airdrop campaign"), _gnews("solana memecoin airdrop"),
    "https://medium.com/feed/tag/solana-airdrop",
    "https://medium.com/feed/tag/airdrop",
    "https://airdropalert.com/feed/",
)
FEEDS = tuple(u.strip() for u in os.getenv("AIRDROP_FEEDS", ",".join(DEFAULT_FEEDS)).split(",") if u.strip())
FEED_EVERY_S = int(os.getenv("AIRDROP_FEED_EVERY_S", "600"))     # each source read at most every 10 min (no blocking)
_LAST_READ = {}                                                   # url -> time it was last downloaded
_FEED_FAIL = {}                                           # url -> time of the last refusal (retried after 30 min)
EVERY_S = int(os.getenv("AIRDROP_EVERY_S", "30"))      # owner (Nov 18): look every 30 s (conditional GET, light on the site)
_FEED_CACHE = {}                                          # url -> (etag, last-modified, body)
MAX_PER_RUN = 5
STATUS_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d{5,25})")
HANDLE_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?\"'\s<]|$)")
SCAM_RE = re.compile(r"seed phrase|frase secreta|private key|chave privada|connect (?:your )?wallet to claim|"
                     r"validate (?:your )?wallet|sync (?:your )?wallet|wallet ?connect.*claim|double your", re.I)
NOT_HANDLES = {"intent", "share", "home", "search", "i", "hashtag", "explore"}
WAIT = {}                                      # chat -> time: waiting for the wallet address
_LOGGED = [0.0]
TEMPLATES = (
    "Participando! 🚀 Minha carteira Solana: {w}",
    "Bora {p}! 🔥 Carteira SOL: {w}",
    "Projeto forte na Solana 💜 Minha wallet: {w}",
    "Quero participar do airdrop {p} 🙌 {w}",
    "Acompanhando {p} desde já ✅ SOL: {w}",
    "In! 🪂 Solana wallet: {w}",
    "Count me in for {p} 🚀 SOL wallet: {w}",
    "Let's go {p}! 💎 My Solana address: {w}",
    "Excited for {p} on Solana ⚡ {w}",
    "Joined ✅ Wallet (Solana): {w}",
    "Je participe ! 🚀 Mon wallet Solana : {w}",
    "Allez {p} ! 🔥 Adresse SOL : {w}",
    "Super projet sur Solana 💜 Mon wallet : {w}",
    "Partant pour l'airdrop {p} 🙌 {w}",
    "Je suis {p} de près ✅ SOL : {w}",
)


# ---------------------------------------------------------------- settings
def state(chat):
    import trade_panel as tp
    try:
        a = dict(tp.settings(chat).get("airdrop") or {})
    except Exception:                                  # no account yet (e.g. owner not registered)
        a = {}
    a.setdefault("wallet", "")
    a.setdefault("on", False)
    a.setdefault("seen", [])
    a.setdefault("last_run", 0)
    return a


def set_state(chat, a):
    import trade_panel as tp
    a["seen"] = list(a.get("seen") or [])[-300:]
    tp.set_setting(chat, "airdrop", a)


def valid_wallet(text):
    from solders.pubkey import Pubkey
    try:
        return str(Pubkey.from_string(str(text).strip()))
    except Exception:
        return None


# ---------------------------------------------------------------- finding airdrops
def parse_feed(xml_text):
    """RSS items -> [{title, link, text, cats}]."""
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    atom = "{http://www.w3.org/2005/Atom}"
    for e in root.iter(atom + "entry"):                 # Atom feeds (Reddit)
        title = (e.findtext(atom + "title") or "").strip()
        link_el = e.find(atom + "link")
        link = (link_el.get("href") if link_el is not None else "") or ""
        text = e.findtext(atom + "content") or ""
        if title and link:
            out.append({"title": title, "link": link, "text": text, "cats": []})
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        text = " ".join(x for x in ((it.findtext("description") or ""), (it.findtext("content:encoded", namespaces=ns) or "")) if x)
        cats = [c.text or "" for c in it.findall("category")]
        if title and link:
            out.append({"title": title, "link": link, "text": text, "cats": cats})
    return out


def is_solana(item):
    blob = (item["title"] + " " + item["text"] + " " + " ".join(item["cats"])).lower()
    sol = "solana" in blob or " sol " in blob or "spl token" in blob
    return sol and ("airdrop" in blob or "airdrops.io" in item["link"])


def is_scam(item):
    return bool(SCAM_RE.search(item["title"] + " " + item["text"]))


def x_post(text):
    """(handle, tweet id) of the first X post linked in the text, else (handle or None, None)."""
    m = STATUS_RE.search(text or "")
    if m:
        return m.group(1), m.group(2)
    for h in HANDLE_RE.findall(text or ""):
        if h.lower() not in NOT_HANDLES:
            return h, None
    return None, None


def project_name(item):
    title = re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", item["title"])          # "... - CoinDesk" (news source)
    t = re.sub(r"(?i)\s*airdrop.*$", "", title).strip(" -–|:")
    t = " ".join((t or title).split()[:4])
    return t[:40]


def _fetch(url):
    """The feed, asking 'changed since?' first (ETag / Last-Modified): a 304 reuses the copy already read."""
    etag, mod, body = _FEED_CACHE.get(url, (None, None, None))
    if body is not None and time.time() - _LAST_READ.get(url, 0) < FEED_EVERY_S:
        return body                                       # read recently: use the copy we have
    _LAST_READ[url] = time.time()
    h = {"User-Agent": "Mozilla/5.0 AirdropAlert/1.0"}
    if etag:
        h["If-None-Match"] = etag
    if mod:
        h["If-Modified-Since"] = mod
    r = requests.get(url, timeout=20, headers=h)
    if r.status_code == 304 and body is not None:
        return body
    if r.status_code >= 400:
        _FEED_FAIL[url] = time.time()
        raise RuntimeError("HTTP " + str(r.status_code))
    _FEED_CACHE[url] = (r.headers.get("ETag"), r.headers.get("Last-Modified"), r.text)
    return r.text


def find(get=None):
    """Solana airdrops from the feeds, newest first, without the scams."""
    out, titles = [], set()
    for url in FEEDS:
        if not get and time.time() - _FEED_FAIL.get(url, 0) < 1800:
            continue                                      # refused us recently: try again in 30 min
        try:
            body = get(url) if get else _fetch(url)
        except Exception as exc:
            _FEED_FAIL[url] = time.time()
            logging.info("airdrop feed %s: %s %s", url.split("?")[0], type(exc).__name__, str(exc)[:60])
            continue
        for it in parse_feed(body or ""):
            key = re.sub(r"\W+", " ", re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", it["title"]).lower()).strip()[:80]
            if key in titles:
                continue
            titles.add(key)
            if is_solana(it) and not is_scam(it) and not CJK_RE.search(it["title"]):
                it["handle"], it["tweet"] = x_post(it["text"])
                it["name"] = project_name(it)
                out.append(it)
    return out


# ---------------------------------------------------------------- the reply and the X link
GENERIC_NAMES = {"", "solana", "sol", "crypto", "cripto", "the", "new", "o projeto", "token", "airdrop"}
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def good_name(name):
    """A project name worth writing in the reply (Nov 18: 'Solana on Solana', a Chinese headline)."""
    n = str(name or "").strip()
    return bool(n) and n.lower().lstrip("@") not in GENERIC_NAMES and not CJK_RE.search(n) and len(n.split()) <= 3


def message(name, wallet, key, used=()):
    """A different reply for each airdrop (the template is chosen from the airdrop's link, never the last one used).
    Without a good project name, only the templates that do not name the project are used."""
    n = int(hashlib.sha256(str(key).encode()).hexdigest(), 16)
    pool = TEMPLATES if good_name(name) else tuple(t for t in TEMPLATES if "{p}" not in t)
    order = [pool[(n + i) % len(pool)] for i in range(len(pool))]
    pick = next((t for t in order if t not in used), order[0])
    return pick.format(p=name, w=wallet), pick


def x_link(text, tweet=None):
    q = {"text": text}
    if tweet:
        q["in_reply_to"] = tweet
    return "https://twitter.com/intent/tweet?" + urllib.parse.urlencode(q)


def x_search(name):
    q = (name if good_name(name) else "solana") + " airdrop"
    return "https://x.com/search?" + urllib.parse.urlencode({"q": q, "f": "live"})


def card(item, wallet, used=()):
    """The airdrop with its reply. 'Abrir no X' only when the project's X post is known: the reply goes in the
    COMMENTS of that post (owner, Nov 18: never a loose post on the profile). Otherwise a button searches the post on X
    and the person pastes its link here to get the comment ready."""
    msg, tpl = message(item.get("name") or "", wallet, item.get("link") or item.get("tweet"), used)
    tweet = item.get("tweet")
    lines = ["🪂 AIRDROP: " + str(item.get("name") if good_name(item.get("name")) else item.get("title") or item.get("name")),
             ("📝 " + item["title"] if item.get("title") and item.get("title") != item.get("name") else ""),
             ("🔗 " + item["link"] if item.get("link") else ""),
             "✍️ Seu comentário (diferente em cada airdrop):\n" + msg]
    if tweet:
        lines.append("Toque em 💬 Comentar no X: abre o post do projeto com o comentário já escrito. Confira e publique.")
        kb = [[{"text": "💬 Comentar no X", "url": x_link(msg, tweet)}]]
    else:
        lines.append("Esse aviso não trouxe o post do projeto. Toque em 🔎 Achar o post no X, abra o post OFICIAL do airdrop, "
                     "copie o link dele e cole aqui no bot: eu preparo o comentário certo nesse post.")
        kb = [[{"text": "🔎 Achar o post no X", "url": x_search(item.get("name"))}]]
    lines.append("⚠️ Nunca conecte a carteira em site de 'resgate' nem passe frase secreta: airdrop de verdade não pede isso.")
    if item.get("link"):
        kb.append([{"text": "📄 Ver o airdrop", "url": item["link"]}])
    return "\n".join(x for x in lines if x), kb, tpl


# ---------------------------------------------------------------- per account
SEEN_V = 2          # Nov 18: the airdrops sent before 'Comentar no X' are sent again ONCE with the right buttons


def run_for(chat, send, items, now=None, force=False):
    now = now or time.time()
    a = state(chat)
    if a.get("seen_v") != SEEN_V:
        a["seen"], a["seen_v"] = [], SEEN_V
    if not a["wallet"] or (not a["on"] and not force):
        return 0
    used, sent = [], 0
    for it in items:
        key = it["link"]
        if key in a["seen"]:
            continue
        text, kb, tpl = card(it, a["wallet"], used[-3:])
        used.append(tpl)
        send(chat, text, kb)
        a["seen"].append(key)
        sent += 1
        if sent >= MAX_PER_RUN:
            break
    a["last_run"] = now
    set_state(chat, a)
    if sent:
        logging.info("airdrop: sent %s new airdrop(s) to %s", sent, chat)
    return sent


def cycle(send, chats, get=None, now=None):
    """Every EVERY_S: new airdrops to every account with alerts on."""
    now = now or time.time()
    want = [c for c in chats if state(c)["on"] and state(c)["wallet"] and now - float(state(c)["last_run"] or 0) >= EVERY_S]
    if not want:
        return
    items = find(get)
    if items and now - _LOGGED[0] > 1800:
        _LOGGED[0] = now
        logging.info("airdrop: %s Solana airdrops in the sources right now", len(items))
    for c in want:
        try:
            run_for(c, send, items, now)
        except Exception as exc:
            logging.info("airdrop %s: %s", c, type(exc).__name__)


def from_post_link(chat, text):
    """The account pasted an X post: its reply with a different message and the 'Abrir no X' button."""
    handle, tweet = x_post(text)
    if not tweet:
        return None
    a = state(chat)
    if not a["wallet"]:
        return "wallet"
    item = {"name": "@" + handle, "tweet": tweet, "handle": handle, "link": "https://x.com/" + handle + "/status/" + tweet}
    out = card(item, a["wallet"], list(a.get("last_tpls") or []))
    a["last_tpls"] = (list(a.get("last_tpls") or []) + [out[2]])[-3:]
    set_state(chat, a)
    return out


def screen(chat):
    a = state(chat)
    L = ["🪂 AIRDROPS DA SOLANA",
         "👛 Carteira nas respostas: " + (a["wallet"] or "nenhuma ainda (toque em ✏️ Minha carteira)"),
         "🔔 Avisos automáticos: " + ("🟢 ligados (procuro a cada " + format(EVERY_S / 3600, "g") + " h, até " + str(MAX_PER_RUN) +
                                     " por vez)" if a["on"] else "🔴 desligados"),
         "Como funciona: eu acho os airdrops da Solana e te mando cada um com uma resposta diferente já escrita com a sua carteira. "
         "Você toca em 🐦 Abrir no X, confere e publica. Também pode colar aqui o link de um post do X que eu preparo a resposta.",
         "Use o endereço PÚBLICO de uma carteira sua (ex.: Phantom). Nunca mande chave ou frase secreta.",
         "⚠️ Airdrop que pede para conectar a carteira num site ou para 'validar' a carteira é golpe."]
    kb = [[{"text": "🔴 Desligar avisos" if a["on"] else "🟢 Ligar avisos", "callback_data": "tad_off" if a["on"] else "tad_on"},
           {"text": "✏️ Minha carteira", "callback_data": "tad_w"}],
          [{"text": "🔎 Buscar agora", "callback_data": "tad_now"}]]
    return "\n".join(L), kb
