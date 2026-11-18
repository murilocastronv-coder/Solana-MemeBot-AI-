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

FEEDS = tuple(u.strip() for u in os.getenv("AIRDROP_FEEDS", "https://airdrops.io/feed/").split(",") if u.strip())
EVERY_S = int(os.getenv("AIRDROP_EVERY_S", "30"))      # owner (Nov 18): look every 30 s (conditional GET, light on the site)
_FEED_CACHE = {}                                          # url -> (etag, last-modified, body)
MAX_PER_RUN = 5
STATUS_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d{5,25})")
HANDLE_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?\"'\s<]|$)")
SCAM_RE = re.compile(r"seed phrase|frase secreta|private key|chave privada|connect (?:your )?wallet to claim|"
                     r"validate (?:your )?wallet|sync (?:your )?wallet|wallet ?connect.*claim|double your", re.I)
NOT_HANDLES = {"intent", "share", "home", "search", "i", "hashtag", "explore"}
WAIT = {}                                      # chat -> time: waiting for the wallet address
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
    return "solana" in blob or " sol " in blob or "spl token" in blob


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
    t = re.sub(r"(?i)\s*airdrop.*$", "", item["title"]).strip(" -–|:")
    return (t or item["title"])[:40]


def _fetch(url):
    """The feed, asking 'changed since?' first (ETag / Last-Modified): a 304 reuses the copy already read."""
    etag, mod, body = _FEED_CACHE.get(url, (None, None, None))
    h = {"User-Agent": "Mozilla/5.0 AirdropAlert/1.0"}
    if etag:
        h["If-None-Match"] = etag
    if mod:
        h["If-Modified-Since"] = mod
    r = requests.get(url, timeout=20, headers=h)
    if r.status_code == 304 and body is not None:
        return body
    r.raise_for_status()
    _FEED_CACHE[url] = (r.headers.get("ETag"), r.headers.get("Last-Modified"), r.text)
    return r.text


def find(get=None):
    """Solana airdrops from the feeds, newest first, without the scams."""
    out = []
    for url in FEEDS:
        try:
            body = get(url) if get else _fetch(url)
        except Exception as exc:
            logging.info("airdrop feed %s: %s", url, type(exc).__name__)
            continue
        for it in parse_feed(body or ""):
            if is_solana(it) and not is_scam(it):
                it["handle"], it["tweet"] = x_post(it["text"])
                it["name"] = project_name(it)
                out.append(it)
    return out


# ---------------------------------------------------------------- the reply and the X link
def message(name, wallet, key, used=()):
    """A different reply for each airdrop (the template is chosen from the airdrop's link, never the last one used)."""
    n = int(hashlib.sha256(str(key).encode()).hexdigest(), 16)
    order = [TEMPLATES[(n + i) % len(TEMPLATES)] for i in range(len(TEMPLATES))]
    pick = next((t for t in order if t not in used), order[0])
    return pick.format(p=name, w=wallet), pick


def x_link(text, tweet=None):
    q = {"text": text}
    if tweet:
        q["in_reply_to"] = tweet
    return "https://twitter.com/intent/tweet?" + urllib.parse.urlencode(q)


def card(item, wallet, used=()):
    msg, tpl = message(item.get("name") or "o projeto", wallet, item.get("link") or item.get("tweet"), used)
    if item.get("handle") and not item.get("tweet"):
        msg = "@" + item["handle"] + " " + msg
    lines = ["🪂 AIRDROP: " + str(item.get("name") or item.get("title")),
             ("📝 " + item["title"] if item.get("title") and item.get("title") != item.get("name") else ""),
             ("🔗 " + item["link"] if item.get("link") else ""),
             "✍️ Sua resposta (diferente em cada airdrop):\n" + msg,
             ("Toque em 🐦 Abrir no X: abre a resposta no post do projeto, já escrita. Confira e publique você mesmo." if item.get("tweet")
              else "Toque em 🐦 Abrir no X: abre um post já escrito marcando o projeto. Confira e publique você mesmo."),
             "⚠️ Nunca conecte a carteira em site de 'resgate' nem passe frase secreta: airdrop de verdade não pede isso."]
    kb = [[{"text": "🐦 Abrir no X", "url": x_link(msg, item.get("tweet"))}]]
    if item.get("link"):
        kb.append([{"text": "📄 Ver o airdrop", "url": item["link"]}])
    return "\n".join(x for x in lines if x), kb, tpl


# ---------------------------------------------------------------- per account
def run_for(chat, send, items, now=None, force=False):
    now = now or time.time()
    a = state(chat)
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
    return sent


def cycle(send, chats, get=None, now=None):
    """Every EVERY_S: new airdrops to every account with alerts on."""
    now = now or time.time()
    want = [c for c in chats if state(c)["on"] and state(c)["wallet"] and now - float(state(c)["last_run"] or 0) >= EVERY_S]
    if not want:
        return
    items = find(get)
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
