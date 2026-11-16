"""MEMECOIN CREATOR (owner, Nov 16). Simulation by default; REAL mode (owner, "opção 2") launches on Pump.fun only
when the owner presses APROVAR on that proposal (pump_launch.py), inside the limits below.

What runs:
1. Trend radar: the Solana memecoins with REAL activity right now (DexScreener lists the bot already reads), words of
   their names/symbols weighted by 1 h volume and buys -> the themes that are attracting buyers.
2. Proposal: an ORIGINAL name, symbol, description and launch posts built from a trending theme (Claude API when
   ANTHROPIC_API_KEY is set in Render, else the bot's own generator); names/symbols that already exist on DexScreener
   are refused (no confusion with other projects); a logo is drawn by the bot (PNG, shapes + colours from the name).
3. Owner approval on Telegram (APROVAR / REJEITAR). Approving records a SIMULATED launch with the estimated cost -
   nothing is created on-chain. The real Pump.fun launch is a later step, only after the owner decides it.
Rules kept: no copy of existing coins or brands, no fake volume, no automatic selling, the creator buys DEV_BUY_PCT% at launch and it
is disclosed in the texts, max MAX_PER_DAY proposals per day, budget limits stored for the real step.
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
DEV_BUY_PCT = E("MEME_DEV_BUY_PCT", 0.1)               # owner (Nov 16, "opção 2"): small public creator buy, inside US$ 5
KEEP_SOL_USD = E("MEME_KEEP_SOL_USD", 3)               # owner: always leave US$ 3 in SOL in the wallet
# Pump.fun bonding curve at creation: 30 SOL virtual / 1,073,000,000 virtual tokens, 1,000,000,000 supply
CURVE_SOL, CURVE_TOKENS, SUPPLY = 30.0, 1_073_000_000.0, 1_000_000_000.0


def dev_buy_sol(pct=None):
    """SOL needed to buy pct% of the supply right at creation (constant-product curve, before fees)."""
    t = SUPPLY * (DEV_BUY_PCT if pct is None else pct) / 100
    return CURVE_SOL * t / (CURVE_TOKENS - t) if t < CURVE_TOKENS else float("inf")
STOP = {"the", "of", "and", "coin", "token", "sol", "solana", "on", "a", "to", "in", "for", "is", "inu", "official", "ai", "x"}
KEY = "memecoin_ai"
_LOCK = threading.RLock()
_MEM = {}


# ---------------------------------------------------------------- state
def _load():
    with _LOCK:
        if not _MEM:
            d = {"auto": False, "real": False, "proposals": [], "launches": [], "last_auto": 0}
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


def set_real(on):
    with _LOCK:
        _load()["real"] = bool(on)
        _save()


def real():
    return bool(_load().get("real"))


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
    desc = (name + " is a community memecoin on Solana inspired by today's \"" + theme + "\" wave. The creator bought about " + disclosed_pct() + "% of the supply at launch, "
            "no promises - just memes, fun and an open community. Not financial advice.")
    posts = ["Meet $" + symbol + " 🚀 a fresh community memecoin on Solana. Launched on Pump.fun; the creator bought about " + disclosed_pct() + "% at launch. "
             "Not financial advice.",
             "$" + symbol + " community is live - share your best " + theme + " memes! Contract: <CA> · DYOR."]
    return {"name": name, "symbol": symbol, "description": desc, "posts": posts}


def _style_hint():
    try:
        names = __import__("creator_patterns").report().get("win_names") or []
    except Exception:
        names = []
    if not names:
        return ""
    return ("For STYLE only (length, tone, simplicity), these recent honest launches worked: " + ", ".join(names[:10]) +
            ". Never reuse their names, words in the same order, symbols or images. ")


def _claude(theme, examples):
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    prompt = ("Create ONE original memecoin concept for Solana based on the trending theme \"" + theme + "\". "
              "Do NOT reuse or imitate these existing coins: " + ", ".join(examples) + ", nor any brand, celebrity or "
              "trademark. " + _style_hint() + "Return only JSON: {\"name\": str (2-3 words), \"symbol\": str (3-6 capital letters), "
              "\"description\": str (English, 2 sentences, say it is a community meme with no promises of profit), "
              "\"posts\": [2 short English launch posts, no profit promises, disclose that the creator bought about " + disclosed_pct() + "% of the supply at launch]}")
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
    try:                                   # themes that also won in the creator analysis go first
        ww = set(__import__("creator_patterns").win_words())
    except Exception:
        ww = set()
    tr = sorted(tr, key=lambda x: x[0] not in ww)
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
        if approve and d.get("real"):
            prop["status"] = "launching"           # launch_real() runs next, with every limit checked again
        elif approve:
            cost = (LAUNCH_COST_SOL + dev_buy_sol()) * float(sol_usd or 0)
            d["launches"] = (d["launches"] or [])[-99:] + [{"id": pid, "name": prop["name"], "symbol": prop["symbol"],
                                                            "at": time.time(), "cost_usd": round(cost, 2), "simulated": True,
                                                            "dev_buy_pct": DEV_BUY_PCT}]
        _save()
        return prop


def disclosed_pct(sol_usd=None):
    px = _px(sol_usd)
    sol = real_buy_sol(px) if px else dev_buy_sol()
    return format(__import__("pump_launch").tokens_for(sol * 0.98) / SUPPLY * 100, ".2f")


def real_buy_sol(sol_usd):
    """Creator buy for a real launch: DEV_BUY_PCT% of the supply, cut so creation + buy stay inside BUDGET_PER_LAUNCH."""
    px = float(sol_usd or 0)
    if px <= 0:
        return 0.0
    return max(0.0, min(dev_buy_sol(), BUDGET_PER_LAUNCH / px - LAUNCH_COST_SOL))


def real_spent(day=None):
    L = [x for x in _load()["launches"] if not x.get("simulated")]
    return sum(x.get("cost_usd", 0) for x in L if day is None or x.get("day") == day), sum(1 for x in L if day is None or x.get("day") == day)


def limits_ok(sol_usd, wallet_lamports, now=None):
    """('' , buy_sol) when a real launch fits every limit, else (why, 0)."""
    px = float(sol_usd or 0)
    if px <= 0:
        return "sem preço do SOL agora", 0.0
    today = time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))
    buy = real_buy_sol(px)
    cost = (LAUNCH_COST_SOL + buy) * px
    day_usd, day_n = real_spent(today)
    all_usd, _ = real_spent()
    if day_n >= MAX_PER_DAY:
        return "já foram " + str(MAX_PER_DAY) + " lançamentos reais hoje", 0.0
    if cost > BUDGET_PER_LAUNCH + 0.01:
        return "custo de US$ " + format(cost, ".2f") + " passa do orçamento de US$ " + format(BUDGET_PER_LAUNCH, "g"), 0.0
    if day_usd + cost > DAILY_BUDGET + 0.01:
        return "passaria do limite de US$ " + format(DAILY_BUDGET, "g") + " por dia (hoje: US$ " + format(day_usd, ".2f") + ")", 0.0
    if all_usd + cost > RESERVE_USD + 0.01:
        return "a reserva de US$ " + format(RESERVE_USD, "g") + " para lançamentos acabou (usado: US$ " + format(all_usd, ".2f") + ")", 0.0
    need = (LAUNCH_COST_SOL + buy) * 1e9 + KEEP_SOL_USD / px * 1e9 + 0.003e9
    if wallet_lamports < need:
        return ("pouco SOL na carteira: precisa de ~" + format(need / 1e9, ".3f") + " SOL (lançamento + US$ " + format(KEEP_SOL_USD, "g") +
                " que sempre ficam em SOL), tem " + format(wallet_lamports / 1e9, ".3f")), 0.0
    return "", buy


def launch_real(pid, sol_usd, rpc, signer, launcher=None):
    """The owner approved `pid` with real mode on -> (launch dict, '') or (None, why)."""
    import liq_bot as lb
    import pump_launch as pl
    launcher = launcher or pl.launch
    d = _load()
    prop = next((p for p in d["proposals"] if p["id"] == pid), None)
    if not prop or prop.get("status") != "launching":
        return None, "proposta não está aprovada para lançamento"
    me = str(signer.pubkey())
    why, buy = limits_ok(sol_usd, lb.sol_lamports(rpc, me))
    if why:
        prop["status"] = "blocked"
        _save()
        return None, why
    res, why = launcher(rpc, signer, prop["name"], prop["symbol"], prop["description"], logo_png(prop["name"]),
                        int(buy * 1e9), lb.simulate)
    now = time.time()
    if not res:
        prop["status"] = "unknown" if "não confirmou a tempo" in why else "failed"
        prop["error"] = why
        if prop["status"] == "unknown":              # may have landed: count it so the limits stay safe
            d["launches"] = d["launches"][-99:] + [{"id": pid, "name": prop["name"], "symbol": prop["symbol"], "at": now,
                                                   "day": time.strftime("%Y-%m-%d", time.gmtime(now)), "simulated": False,
                                                   "cost_usd": round((LAUNCH_COST_SOL + buy) * float(sol_usd), 2), "unconfirmed": True}]
        _save()
        return None, why
    pct = pl.tokens_for(buy * 0.98) / SUPPLY * 100
    rec = {"id": pid, "name": prop["name"], "symbol": prop["symbol"], "at": now, "day": time.strftime("%Y-%m-%d", time.gmtime(now)),
           "simulated": False, "mint": res["mint"], "sig": res["sig"], "buy_sol": round(buy, 4), "dev_buy_pct": round(pct, 3),
           "cost_usd": round((LAUNCH_COST_SOL + buy) * float(sol_usd), 2)}
    prop["status"] = "launched"
    prop["mint"] = res["mint"]
    d["launches"] = d["launches"][-99:] + [rec]
    _save()
    return rec, ""


def launched_text(rec):
    return ("🚀 LANÇADA DE VERDADE NA PUMP.FUN: " + rec["name"] + " ($" + rec["symbol"] + ")\n"
            "Contrato: " + rec["mint"] + "\nhttps://pump.fun/coin/" + rec["mint"] + "\nTransação: https://solscan.io/tx/" + rec["sig"] +
            "\n💰 Compra do criador: " + format(rec["buy_sol"], "g") + " SOL (~" + format(rec["dev_buy_pct"], ".2f") +
            "% das moedas, pública) · custo total ~US$ " + format(rec["cost_usd"], ".2f") +
            "\n🔒 O bot NUNCA vende essas moedas sozinho. Divulgue só em canais seus, sem prometer lucro.")


def social_line():
    has = any(os.getenv(k, "").strip() for k in ("MEME_TWITTER", "MEME_TELEGRAM", "MEME_WEBSITE"))
    try:
        matters = __import__("creator_patterns").social_matters()
    except Exception:
        matters = False
    if has:
        return "\n📣 Redes que vão na moeda: " + ", ".join(os.getenv(k) for k in ("MEME_TWITTER", "MEME_TELEGRAM", "MEME_WEBSITE") if os.getenv(k, "").strip())
    if matters:
        return ("\n📣 Na análise, moedas com X ou Telegram deram bem mais certo. Crie um perfil seu e coloque o link em "
                "MEME_TWITTER / MEME_TELEGRAM no Render.")
    return ""


def auto_due(now, last):
    """Automatic proposal now? In the best launch hours found by the creator analysis (one per 3-hour block),
    else every AUTO_EVERY_S."""
    try:
        best = __import__("creator_patterns").best_hours_utc()
    except Exception:
        best = []
    if len(best) < MAX_PER_DAY or len(best) < 4:
        return now - last >= AUTO_EVERY_S
    h = time.gmtime(now).tm_hour
    block = h // 3 * 3
    if block not in best:
        return False
    start = now - (h - block) * 3600 - time.gmtime(now).tm_min * 60 - time.gmtime(now).tm_sec
    return last < start


def proposal_text(p):
    head = "🚀 PROPOSTA DE MEMECOIN (REAL — APROVAR CRIA NA PUMP.FUN)\n" if real() else "🧪 PROPOSTA DE MEMECOIN (SIMULAÇÃO)\n"
    return (head +
            "Nome: " + p["name"] + "\nSímbolo: $" + p["symbol"] + "\nTema em alta: \"" + p["theme"] + "\" (moedas com volume: " +
            ", ".join(p.get("theme_examples") or []) + ")\nTexto por: " + p.get("source", "?") + "\n\n" + p["description"] +
            "\n\n📣 Posts de lançamento:\n• " + "\n• ".join(p.get("posts") or []) +
            "\n\n" + dev_buy_line() + social_line() + ("\n⚠️ APROVAR cria a moeda DE VERDADE com dinheiro da carteira (simulado antes de enviar)." if real()
                                        else "\n⚠️ APROVAR agora só registra uma SIMULAÇÃO: nada é criado na blockchain."))


def _px(sol_usd=None):
    px = float(sol_usd or 0)
    if not px:
        try:
            px = float(__import__("copy_trader")._sol_usd() or 0)
        except Exception:
            px = 0.0
    return px


def dev_buy_line(sol_usd=None):
    px = _px(sol_usd)
    sol = real_buy_sol(px) if px else dev_buy_sol()
    pct = (__import__("pump_launch").tokens_for(sol * 0.98) / SUPPLY * 100) if sol else 0.0
    usd = (sol + LAUNCH_COST_SOL) * px
    return ("💰 Lançamento: ~" + format(LAUNCH_COST_SOL, "g") + " SOL de criação + compra do criador de ~" + format(sol, ".3f") +
            " SOL (~" + format(pct, ".2f") + "% das moedas)" + (" = ~US$ " + format(usd, ".2f") if px else "") +
            " · máx. US$ " + format(BUDGET_PER_LAUNCH, "g") + " por lançamento · a compra do criador aparece para todos · sem venda automática")


def status_text(sol_usd=None):
    d = _load()
    L = ["🤖 CRIADOR DE MEMECOINS — " + ("🟢 propostas automáticas a cada " + format(AUTO_EVERY_S / 3600, "g") + " h" if d.get("auto")
                                          else "⏸️ propostas só quando você pedir") +
         (" · 💵 REAL (cada APROVAR cria a moeda na Pump.fun)" if d.get("real") else " · SIMULAÇÃO"),
         "⚙️ Máx. " + str(MAX_PER_DAY) + " por dia · orçamento por lançamento US$ " + format(BUDGET_PER_LAUNCH, "g") + " · por dia US$ " +
         format(DAILY_BUDGET, "g") + " · reserva total US$ " + format(RESERVE_USD, "g") + " · sempre deixa US$ " + format(KEEP_SOL_USD, "g") + " em SOL",
         "📊 Propostas: " + str(len(d["proposals"])) + " · lançadas de verdade: " + str(real_spent()[1]) + " (US$ " +
         format(real_spent()[0], ".2f") + ") · simuladas: " + str(sum(1 for x in d["launches"] if x.get("simulated"))),
         dev_buy_line(sol_usd)]
    try:
        best = __import__("creator_patterns").best_hours_utc()
        if len(best) >= 4 and d.get("auto"):
            L.append("🕒 Propostas automáticas nos melhores horários da análise de criadores (Brasília): " +
                     ", ".join(format((h - 3) % 24, "02d") + "h" for h in sorted(best)))
    except Exception:
        pass
    try:
        tr = trends()
        if tr:
            L.append("🔥 Tendências agora: " + ", ".join(w + " (" + "/".join(ex[:2]) + ")" for w, s, ex in tr[:6]))
    except Exception:
        pass
    for x in d["launches"][-3:]:
        L.append("• " + x["name"] + " ($" + x["symbol"] + ") — " + time.strftime("%d/%m %H:%M", time.gmtime(x["at"])) +
                 (" (simulada)" if x.get("simulated") else (" ⚠️ sem confirmação" if x.get("unconfirmed") else " https://pump.fun/coin/" + x.get("mint", ""))))
    return "\n".join(L)
