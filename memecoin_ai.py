"""MEMECOIN CREATOR (owner, Nov 16) - SIMULATION ONLY for now.

What runs:
1. Trend radar: the Solana memecoins with REAL activity right now (DexScreener lists the bot already reads), words of
   their names/symbols weighted by 1 h volume and buys -> the themes that are attracting buyers.
2. Proposal: an ORIGINAL name, symbol, description and launch posts built from a trending theme (Claude API when
   ANTHROPIC_API_KEY is set in Render, else the bot's own generator); names/symbols that already exist on DexScreener
   are refused (no confusion with other projects); a logo is drawn by the bot (PNG, shapes + colours from the name).
3. Owner approval on Telegram (APROVAR / REJEITAR). Approving records a SIMULATED launch with the estimated cost -
   nothing is created on-chain. The real Pump.fun launch is a later step, only after the owner decides it.
Rules kept: no copy of existing coins or brands, no fake volume, no automatic selling, creator holds nothing (no dev
buy), max MAX_PER_DAY proposals per day, budget limits stored for the real step.
"""
import hashlib
import json
import logging
import os
import random
import re
import struct
import threading
import time
import zlib

import requests

import signals as sg

E = sg._f
MAX_PER_DAY = int(E("MEME_MAX_PER_DAY", 2))
AUTO_EVERY_S = E("MEME_AUTO_EVERY_H", 8) * 3600
LAUNCH_COST_SOL = E("MEME_LAUNCH_COST_SOL", 0.02)      # Pump.fun create + rent + fees (estimate)
BUDGET_PER_LAUNCH = E("MEME_BUDGET_USD", 5)
DAILY_BUDGET = E("MEME_DAILY_BUDGET_USD", 10)
RESERVE_USD = E("MEME_RESERVE_USD", 20)
STOP = {"the", "of", "and", "coin", "token", "sol", "solana", "on", "a", "to", "in", "for", "is", "inu", "official", "ai", "x"}
KEY = "memecoin_ai"
_LOCK = threading.RLock()
_MEM = {}


# ---------------------------------------------------------------- state
def _load():
    with _LOCK:
        if not _MEM:
            d = {"auto": False, "proposals": [], "launches": [], "last_auto": 0}
            try:
                import pg_store
                if pg_store.enabled():
                    got = pg_store.get(KEY)
                    if got:
                        d.update(got)
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
        logging.warning("meme: save failed (%s)", type(exc).__name__)


def set_auto(on):
    with _LOCK:
        _load()["auto"] = bool(on)
        _save()


# ---------------------------------------------------------------- trends
def _words(text):
    return [w for w in re.findall(r"[a-z]{3,12}", (text or "").lower()) if w not in STOP]


def trends(pairs=None, top=8):
    """[(theme word, score, example coins)] from coins with real activity now."""
    pairs = pairs if pairs is not None else sg.candidate_pairs()
    score, examples = {}, {}
    for p in pairs:
        b = p.get("baseToken") or {}
        vol = float((p.get("volume") or {}).get("h1") or 0)
        buys = float(((p.get("txns") or {}).get("h1") or {}).get("buys") or 0)
        if vol < 5_000 or buys < 30:
            continue
        w = vol ** 0.5 + buys
        for word in set(_words(b.get("name")) + _words(b.get("symbol"))):
            score[word] = score.get(word, 0) + w
            examples.setdefault(word, []).append(b.get("symbol") or "?")
    ranked = sorted(score.items(), key=lambda kv: -kv[1])[:top]
    return [(w, round(s), examples[w][:3]) for w, s in ranked]


# ---------------------------------------------------------------- proposal
PREFIX = ["Baby", "Lil", "Mega", "Turbo", "Moon", "Based", "Sir", "Captain", "Doctor", "Space", "Cosmic", "Pixel"]
SUFFIX = ["Club", "Squad", "World", "Gang", "Party", "Island", "Lab", "Republic", "Odyssey", "Express"]


def taken(name, symbol, fetch=None):
    """True when a Solana coin with the same name or symbol already exists (DexScreener search)."""
    data = (fetch or sg._get)(sg.DEX + "/latest/dex/search?q=" + requests.utils.quote(symbol)) or {}
    for p in data.get("pairs") or []:
        b = p.get("baseToken") or {}
        if p.get("chainId") == "solana" and (str(b.get("symbol", "")).upper() == symbol.upper() or
                                              str(b.get("name", "")).lower() == name.lower()):
            return True
    return False


def _own_generator(theme, rnd):
    t = theme.capitalize()
    name = rnd.choice([rnd.choice(PREFIX) + " " + t, t + " " + rnd.choice(SUFFIX), rnd.choice(PREFIX) + t + rnd.choice(SUFFIX)])
    letters = re.sub(r"[^A-Z]", "", name.upper())
    symbol = (letters[:2] + t.upper()[:3])[:6] if len(letters) >= 2 else t.upper()[:5]
    desc = (name + " is a community memecoin on Solana inspired by today's \"" + theme + "\" wave. No team allocation, "
            "no promises - just memes, fun and an open community. Not financial advice.")
    posts = ["Meet $" + symbol + " 🚀 a fresh community memecoin on Solana. Fair launch on Pump.fun, creator holds 0. "
             "Not financial advice.",
             "$" + symbol + " community is live - share your best " + theme + " memes! Contract: <CA> · DYOR."]
    return {"name": name, "symbol": symbol, "description": desc, "posts": posts}


def _claude(theme, examples):
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    prompt = ("Create ONE original memecoin concept for Solana based on the trending theme \"" + theme + "\". "
              "Do NOT reuse or imitate these existing coins: " + ", ".join(examples) + ", nor any brand, celebrity or "
              "trademark. Return only JSON: {\"name\": str (2-3 words), \"symbol\": str (3-6 capital letters), "
              "\"description\": str (English, 2 sentences, say it is a community meme with no promises of profit), "
              "\"posts\": [2 short English launch posts, no profit promises, mention creator holds 0]}")
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=40, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": os.getenv("MEME_AI_MODEL", "claude-haiku-5-5"), "max_tokens": 600,
              "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    text = "".join(c.get("text", "") for c in r.json().get("content") or [])
    m = re.search(r"\{.*\}", text, re.S)
    out = json.loads(m.group(0)) if m else None
    if out and out.get("name") and out.get("symbol"):
        out["symbol"] = re.sub(r"[^A-Z0-9]", "", str(out["symbol"]).upper())[:8]
        return out
    return None


def logo_png(name, size=256):
    """A simple original logo: background colour + rings + a centre disc, colours from the name (pure Python PNG)."""
    h = hashlib.sha256(name.encode()).digest()
    bg, ring, disc = h[0:3], h[3:6], h[6:9]
    c = size / 2
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            if d < size * 0.22:
                col = disc
            elif d < size * 0.42 and int(d / (size * 0.04)) % 2 == 0:
                col = ring
            elif d < size * 0.46:
                col = bytes(255 - v for v in disc)
            else:
                col = bg
            row += bytes(col)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(t, data):
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def propose(pairs=None, fetch=None, now=None, rnd=None):
    """A new original proposal (dict) or (None, why)."""
    now = now or time.time()
    d = _load()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    if sum(1 for p in d["proposals"] if p.get("day") == today) >= MAX_PER_DAY:
        return None, "já fiz " + str(MAX_PER_DAY) + " propostas hoje"
    tr = trends(pairs)
    if not tr:
        return None, "nenhuma tendência com volume real agora"
    rnd = rnd or random.Random()
    for theme, score, examples in tr[:4]:
        for _ in range(4):
            try:
                c = _claude(theme, examples)
            except Exception as exc:
                logging.warning("meme claude: %s", type(exc).__name__)
                c = None
            c = c or _own_generator(theme, rnd)
            try:
                if taken(c["name"], c["symbol"], fetch):
                    continue
            except Exception:
                pass
            pid = hashlib.sha1((c["name"] + str(now)).encode()).hexdigest()[:8]
            prop = dict(c, id=pid, theme=theme, theme_examples=examples, at=now, day=today, status="pending",
                        source="claude" if os.getenv("ANTHROPIC_API_KEY") else "gerador do bot")
            with _LOCK:
                d["proposals"] = (d["proposals"] or [])[-49:] + [prop]
            _save()
            return prop, ""
    return None, "todos os nomes gerados já existiam"


def decide(pid, approve, sol_usd=None):
    with _LOCK:
        d = _load()
        prop = next((p for p in d["proposals"] if p["id"] == pid), None)
        if not prop or prop["status"] != "pending":
            return None
        prop["status"] = "approved" if approve else "rejected"
        if approve:
            cost = LAUNCH_COST_SOL * float(sol_usd or 0)
            d["launches"] = (d["launches"] or [])[-99:] + [{"id": pid, "name": prop["name"], "symbol": prop["symbol"],
                                                            "at": time.time(), "cost_usd": round(cost, 2), "simulated": True}]
        _save()
        return prop


def proposal_text(p):
    return ("🧪 PROPOSTA DE MEMECOIN (SIMULAÇÃO)\n"
            "Nome: " + p["name"] + "\nSímbolo: $" + p["symbol"] + "\nTema em alta: \"" + p["theme"] + "\" (moedas com volume: " +
            ", ".join(p.get("theme_examples") or []) + ")\nTexto por: " + p.get("source", "?") + "\n\n" + p["description"] +
            "\n\n📣 Posts de lançamento:\n• " + "\n• ".join(p.get("posts") or []) +
            "\n\n💰 Custo estimado do lançamento real: ~" + format(LAUNCH_COST_SOL, "g") + " SOL · criador fica com 0 moedas · "
            "sem venda automática\n⚠️ APROVAR agora só registra uma SIMULAÇÃO: nada é criado na blockchain.")


def status_text(sol_usd=None):
    d = _load()
    L = ["🤖 CRIADOR DE MEMECOINS — " + ("🟢 propostas automáticas a cada " + format(AUTO_EVERY_S / 3600, "g") + " h" if d.get("auto")
                                          else "⏸️ propostas só quando você pedir") + " · SIMULAÇÃO",
         "⚙️ Máx. " + str(MAX_PER_DAY) + " por dia · orçamento por lançamento US$ " + format(BUDGET_PER_LAUNCH, "g") + " · por dia US$ " +
         format(DAILY_BUDGET, "g") + " · reserva US$ " + format(RESERVE_USD, "g") + " (para o lançamento real, ainda desligado)",
         "📊 Propostas: " + str(len(d["proposals"])) + " · aprovadas (simuladas): " + str(len(d["launches"])) +
         " · custo simulado US$ " + format(sum(x.get("cost_usd", 0) for x in d["launches"]), ".2f")]
    try:
        tr = trends()
        if tr:
            L.append("🔥 Tendências agora: " + ", ".join(w + " (" + "/".join(ex[:2]) + ")" for w, s, ex in tr[:6]))
    except Exception:
        pass
    for x in d["launches"][-3:]:
        L.append("• " + x["name"] + " ($" + x["symbol"] + ") — aprovada " + time.strftime("%d/%m %H:%M", time.gmtime(x["at"])) + " (simulada)")
    return "\n".join(L)
