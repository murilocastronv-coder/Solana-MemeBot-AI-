"""Pump.fun creator analysis (owner, Nov 16): which launches worked, which creator wallets had several results, and the
HONEST patterns behind them, used by the memecoin creator's proposals.

1. Sample: real Pump.fun launches spread over the last DAYS days (skipping the newest 2 days, whose result is not
   known yet). Helius history of the Pump.fun mint-authority account, which only appears in coin creations, so every
   transaction read is a launch: name, symbol, creator, time, creator buy (SOL) and extra wallets that bought inside
   the creation transaction (a "bundle").
2. Result of each coin (DexScreener): graduated (left the bonding curve) and still worth >= US$ 30k = SUCCESS;
   graduated but now < US$ 30k = "subiu e caiu" (not counted as success); never graduated = failure.
3. Creator wallets: for each successful creator, its own launch history (Helius) and how many of those coins worked.
4. Honest filter: bundled launches, creators that hold > 20% at launch and serial launchers (> SPAM_LAUNCHES coins)
   are left out of the patterns: they are what we do NOT copy.
Patterns: success rate per UTC hour block, weekday, name length, socials (X/Telegram/site), creator buy size and the
words of the successful names. The memecoin creator uses the best hours, the winning themes and asks the owner for
socials when they matter. Names/logos stay original (no copy of any coin).
"""
import json
import logging
import threading
import time

import signals as sg

E = sg._f
KEY = "creator_patterns"
MINT_AUTHORITY = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
DAYS = E("CREATORS_DAYS", 14)
POINTS = int(E("CREATORS_POINTS", 42))          # Helius calls for the sample (100 launches each)
GOOD_MCAP = E("CREATORS_GOOD_MCAP_USD", 30_000)
SPAM_LAUNCHES = int(E("CREATORS_SPAM_LAUNCHES", 30))
TOP_CREATORS = int(E("CREATORS_TOP", 30))
MIN_BUCKET = 30                                  # fewer launches than this in a bucket: no conclusion
_RUN = threading.Lock()
_MEM = {}
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s):
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + raw


def _strings(data, off, count):
    out = []
    for _ in range(count):
        ln = int.from_bytes(data[off:off + 4], "little")
        if ln > 300 or off + 4 + ln > len(data):
            return None, off
        out.append(data[off + 4:off + 4 + ln].decode("utf-8", "replace"))
        off += 4 + ln
    return out, off


CREATE_DISCS = (bytes([24, 30, 200, 40, 5, 28, 7, 119]), bytes([214, 144, 76, 236, 95, 139, 49, 180]))


def parse_launch(tx):
    """dict(mint, name, symbol, creator, t, buy_sol, bundled, creator_pct) of a Pump.fun creation, else None."""
    import snipe_backtest as sbt
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None
    keys = sbt._keys(tx)
    msg = (tx.get("transaction") or {}).get("message") or {}
    ixs = list(msg.get("instructions") or [])
    for inner in meta.get("innerInstructions") or []:
        ixs += inner.get("instructions") or []
    info = None
    for ix in ixs:
        try:
            if keys[ix.get("programIdIndex")] != sbt.PUMP:
                continue
            data = b58decode(ix.get("data") or "")
        except Exception:
            continue
        if data[:8] in CREATE_DISCS:
            s, off = _strings(data, 8, 3)
            if s:
                creator = keys[0]
                if len(data) >= off + 32:
                    from solders.pubkey import Pubkey
                    creator = str(Pubkey.from_bytes(data[off:off + 32]))
                acc = ix.get("accounts") or []
                info = {"name": s[0][:40], "symbol": s[1][:15], "mint": keys[acc[0]] if acc else None, "creator": creator}
                break
    if not info or not info["mint"]:
        return None
    mint = info["mint"]
    pre, post = meta.get("preBalances") or [0], meta.get("postBalances") or [0]
    spent = (int(pre[0]) - int(post[0])) / 1e9 if pre and post else 0.0
    holders = {}
    for b in meta.get("postTokenBalances") or []:
        if b.get("mint") == mint:
            holders[b.get("owner")] = int((b.get("uiTokenAmount") or {}).get("amount") or 0)
    import pump_launch as pl
    curve = pl.pda([b"bonding-curve", mint], pl.PUMP)
    payer = keys[0]
    creator_amt = holders.get(payer, 0) + (holders.get(info["creator"], 0) if info["creator"] != payer else 0)
    bundled = sum(1 for o, a in holders.items() if a > 0 and o not in (curve, payer, info["creator"]))
    info.update(t=int(tx.get("blockTime") or 0), buy_sol=round(max(0.0, spent - 0.02), 4), bundled=bundled,
                creator_pct=round(creator_amt / 1e15 * 100, 3))
    return info


def sample(rpc, now=None, history=None, points=None, should_stop=None):
    import snipe_backtest as sbt
    history = history or sbt.history_call
    now = now or time.time()
    points = points or POINTS
    start, end = now - DAYS * 86400, now - 2 * 86400
    out, seen = [], set()
    for i in range(points):
        if should_stop and should_stop():
            break
        t = int(start + (end - start) * (i + 0.5) / points)
        try:
            txs = history(rpc, MINT_AUTHORITY, gte=t, lte=t + 3600, order="asc", limit=100)
        except Exception as exc:
            logging.warning("creators sample: %s", type(exc).__name__)
            continue
        for tx in txs or []:
            r = parse_launch(tx)
            if r and r["mint"] not in seen:
                seen.add(r["mint"])
                out.append(r)
    return out


def outcomes(mints, fetch=None):
    """mint -> {"grad", "mcap", "social"} from DexScreener (coins it does not know = not graduated, no socials)."""
    fetch = fetch or sg._get
    res = {}
    mints = list(mints)
    for i in range(0, len(mints), 30):
        try:
            pairs = fetch(sg.DEX + "/tokens/v1/solana/" + ",".join(mints[i:i + 30]))
        except Exception:
            time.sleep(2)
            continue
        for p in pairs if isinstance(pairs, list) else []:
            m = ((p or {}).get("baseToken") or {}).get("address")
            if not m:
                continue
            inf = p.get("info") or {}
            socials = {s.get("type") for s in inf.get("socials") or []}
            row = res.setdefault(m, {"grad": False, "mcap": 0.0, "x": False, "tg": False, "web": False})
            row["grad"] = row["grad"] or (p.get("dexId") not in ("pumpfun", None))
            row["mcap"] = max(row["mcap"], float(p.get("marketCap") or p.get("fdv") or 0))
            row["x"] = row["x"] or "twitter" in socials
            row["tg"] = row["tg"] or "telegram" in socials
            row["web"] = row["web"] or bool(inf.get("websites"))
        time.sleep(0.25)                               # DexScreener: 300 requests per minute
    return res


def label(o):
    if not o or not o.get("grad"):
        return "fail"
    return "win" if o.get("mcap", 0) >= GOOD_MCAP else "pumped_and_fell"


def honest(r, launches_of=None):
    return r.get("bundled", 0) == 0 and r.get("creator_pct", 0) <= 20 and (launches_of or {}).get(r["creator"], 0) <= SPAM_LAUNCHES


def _bucket_rates(rows, key):
    g = {}
    for r in rows:
        k = key(r)
        n, w = g.get(k, (0, 0))
        g[k] = (n + 1, w + (r["label"] == "win"))
    return {k: {"n": n, "win": w, "rate": w / n} for k, (n, w) in g.items()}


def _buy_bucket(sol):
    return "0" if sol < 0.01 else "<0.5" if sol < 0.5 else "0.5-2" if sol < 2 else ">2"


def _name_bucket(name):
    w = len(name.split())
    return "1 palavra" if w <= 1 else "2 palavras" if w == 2 else "3+ palavras"


def analyse(rows, creators=None):
    """rows already carry 'label'. creators: {wallet: {"launches", "wins"}}. -> report dict."""
    creators = creators or {}
    launches_of = {c: v.get("launches", 0) for c, v in creators.items()}
    hon = [r for r in rows if honest(r, launches_of)]
    base = sum(r["label"] == "win" for r in hon) / len(hon) if hon else 0.0
    hours = _bucket_rates(hon, lambda r: time.gmtime(r["t"]).tm_hour)
    blocks = _bucket_rates(hon, lambda r: time.gmtime(r["t"]).tm_hour // 3 * 3)
    best_hours = [h for h, v in sorted(blocks.items(), key=lambda kv: -kv[1]["rate"]) if v["n"] >= MIN_BUCKET][:4]
    social = _bucket_rates([r for r in hon if "x" in r], lambda r: "com X ou Telegram" if (r["x"] or r["tg"]) else "sem redes")
    words = {}
    for r in hon:
        for w in sg_words(r["name"]):
            n, k = words.get(w, (0, 0))
            words[w] = (n + 1, k + (r["label"] == "win"))
    win_words = sorted(((w, k, n) for w, (n, k) in words.items() if k >= 2), key=lambda x: (-x[1] / x[2], -x[1]))[:12]
    good_creators = sorted(((c, v) for c, v in creators.items() if v.get("wins", 0) >= 2 and v.get("launches", 0) <= SPAM_LAUNCHES),
                           key=lambda cv: (-cv[1]["wins"], cv[1]["launches"]))[:10]
    return {"at": time.time(), "launches": len(rows), "honest": len(hon), "base_rate": base,
            "wins": sum(r["label"] == "win" for r in hon), "fell": sum(r["label"] == "pumped_and_fell" for r in rows),
            "bundled": sum(1 for r in rows if r.get("bundled")), "spam": sum(1 for r in rows if launches_of.get(r["creator"], 0) > SPAM_LAUNCHES),
            "blocks": {str(k): v for k, v in blocks.items()}, "best_hours": best_hours,
            "weekday": {str(k): v for k, v in _bucket_rates(hon, lambda r: time.gmtime(r["t"]).tm_wday).items()},
            "names": _bucket_rates(hon, lambda r: _name_bucket(r["name"])),
            "social": social, "buy": _bucket_rates(hon, lambda r: _buy_bucket(r["buy_sol"])),
            "win_words": win_words, "win_names": [r["name"] for r in hon if r["label"] == "win"][:20],
            "creators": [(c, v["wins"], v["launches"]) for c, v in good_creators], "hours_n": len(hours)}


def sg_words(name):
    import memecoin_ai as mc
    return mc._words(name)


def creator_records(win_rows, launches_fn=None, fetch=None, limit=None):
    """For the creators of successful coins: how many coins each created and how many of them worked."""
    import wallet_profile as wp
    key = wp.helius_key()
    launches_fn = launches_fn or (lambda c: wp.helius_launches(c, key) if key else [])
    out = {}
    for c in list(dict.fromkeys(r["creator"] for r in win_rows))[:limit or TOP_CREATORS]:
        try:
            mints = [m for t, m in launches_fn(c) if m]
        except Exception:
            mints = []
        oc = outcomes(mints[:90], fetch) if mints else {}
        out[c] = {"launches": len(mints), "wins": sum(label(oc.get(m)) == "win" for m in mints[:90])}
    return out


def run(rpc, now=None, history=None, fetch=None, launches_fn=None):
    rows = sample(rpc, now, history)
    if not rows:
        return None, "não consegui ler os lançamentos da Pump.fun (Helius)"
    oc = outcomes([r["mint"] for r in rows], fetch)
    for r in rows:
        o = oc.get(r["mint"]) or {}
        r["label"] = label(o)
        r.update(x=bool(o.get("x")), tg=bool(o.get("tg")), web=bool(o.get("web")))
    creators = creator_records([r for r in rows if r["label"] == "win"], launches_fn, fetch)
    rep = analyse(rows, creators)
    _MEM.clear()
    _MEM.update(rep)
    try:
        import pg_store
        if pg_store.enabled():
            pg_store.put(KEY, json.loads(json.dumps(rep)))
    except Exception as exc:
        logging.warning("creators save: %s", type(exc).__name__)
    return rep, ""


def report():
    if not _MEM:
        try:
            import pg_store
            if pg_store.enabled():
                _MEM.update(pg_store.get(KEY) or {})
        except Exception:
            pass
    return dict(_MEM)


def start(rpc, send, chat):
    if not _RUN.acquire(blocking=False):
        return "a análise já está rodando"

    def go():
        try:
            rep, why = run(rpc)
            send(chat, text(rep) if rep else "⛔ Análise de criadores: " + why)
        except Exception as exc:
            logging.exception("creators run")
            send(chat, "⛔ Análise de criadores falhou: " + type(exc).__name__)
        finally:
            _RUN.release()
    threading.Thread(target=go, daemon=True).start()
    return ""


def _pct(v):
    return format(v * 100, ".1f") + "%"


def text(rep):
    if not rep or not rep.get("launches"):
        return "🔬 Análise de criadores: ainda não rodou. Aperte 🔬 Analisar criadores."
    L = ["🔬 CRIADORES DA PUMP.FUN — " + str(rep["launches"]) + " lançamentos reais de " + format(DAYS, "g") + " dias",
         "✅ Deram certo (graduaram e valem ≥ US$ " + format(GOOD_MCAP, ",.0f") + "): " + str(rep["wins"]) + " de " + str(rep["honest"]) +
         " lançamentos honestos = " + _pct(rep["base_rate"]),
         "🚫 Fora da análise (não copiamos): " + str(rep["bundled"]) + " com várias carteiras comprando junto no lançamento, " +
         str(rep["spam"]) + " de criadores em série · " + str(rep["fell"]) + " subiram e caíram"]
    blocks = rep.get("blocks") or {}
    if rep.get("best_hours"):
        L.append("🕒 Melhores horários (Brasília): " + ", ".join(
            format((int(h) - 3) % 24, "02d") + "h–" + format((int(h) - 3 + 3) % 24, "02d") + "h (" + _pct(blocks[str(h)]["rate"]) + ")"
            for h in rep["best_hours"]))
    for title, k in (("📣 Redes sociais", "social"), ("🔤 Nome", "names"), ("💰 Compra do criador (SOL)", "buy")):
        b = rep.get(k) or {}
        if b:
            L.append(title + ": " + " · ".join(n + " " + _pct(v["rate"]) + " (" + str(v["n"]) + ")" for n, v in
                                                sorted(b.items(), key=lambda kv: -kv[1]["rate"]) if v["n"] >= 10))
    if rep.get("win_words"):
        L.append("🔥 Palavras dos que deram certo: " + ", ".join(w + " " + str(k) + "/" + str(n) for w, k, n in rep["win_words"][:8]))
    if rep.get("creators"):
        L.append("👛 Carteiras com vários resultados (sem série/spam):")
        for c, w, n in rep["creators"][:6]:
            L.append("• " + c[:4] + "…" + c[-4:] + ": " + str(w) + " de " + str(n) + " moedas deram certo")
    else:
        L.append("👛 Nenhuma carteira honesta com 2+ moedas que deram certo nesta amostra.")
    L.append("ℹ️ As propostas passam a usar esses horários e temas. Nome e logo continuam originais. Nada disso garante resultado.")
    return "\n".join(L)


def best_hours_utc():
    return [int(h) for h in (report().get("best_hours") or [])]


def win_words():
    return [w for w, k, n in (report().get("win_words") or [])]


def social_matters():
    s = report().get("social") or {}
    a, b = s.get("com X ou Telegram"), s.get("sem redes")
    return bool(a and b and a["n"] >= MIN_BUCKET and a["rate"] > b["rate"] * 1.5)
