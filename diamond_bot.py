"""Diamond: patient conviction trader (real money, mode "diamond", owner's choice Nov 14).

Few, bigger, better trades in ESTABLISHED Solana tokens only, on pullbacks inside an uptrend. Every
15 minutes it decides buy / hold (and logs WHY for every coin it refused, so each round can be studied).
Telegram message on every trade with the reason.

Round 2 rules (hard and checkable on DexScreener data). Lesson of round 1 (zero trades): vague, all-at-once
conditions ("narrative", "conviction", "deep pullback") were never true together, so it never acted.
Each rule is now a number, the pullback is shallow (a real dip in an uptrend, not a crash), and after
IDLE_RELAX_S without a trade the pullback rule is relaxed one step (anti-paralysis).

UNIVERSE  U1 liquidity >= US$ 250k  U2 24 h volume >= US$ 1M  U3 pair >= 3 days old  U4 24 h trades >= 3,000
          U5 market cap >= US$ 10M  U6 SOL or USDC pair  U7 anti-scam checks pass
TREND     T1 24 h change >= +3% or 6 h change >= +3%   T2 6 h volume >= 15% of 24 h volume (volume not dying)
PULLBACK  E1 1 h change between -12% and -1.5% (relaxed: <= 0% after 4 h idle)   E2 5 min change >= 0 (stopped
          falling)   E3 1 h buys >= 0.8 x 1 h sells
SIZE      US$ 30, at most 1 open, one buy per 15 min cycle, same coin again only after 4 h.
EXIT      X1 stop -10% (thesis broken)  X2 from +12% trailing stop 6% under the top (big win runs)
          X3 end of the 12 h round: sells what is open and sends the report.
Nothing here guarantees profit.
"""
import json
import logging
import os
import tempfile
import threading
import time

import signals as sg

ORDER_USD = sg._f("DIAMOND_ORDER_USD", 30)
MAX_OPEN = int(sg._f("DIAMOND_MAX_OPEN", 1))
RESERVE_USD = 1.0
CYCLE_S = sg._f("DIAMOND_CYCLE_MINUTES", 15) * 60
ROUND_S = sg._f("DIAMOND_ROUND_HOURS", 12) * 3600
COOLDOWN_S = sg._f("DIAMOND_COOLDOWN_HOURS", 4) * 3600
IDLE_RELAX_S = sg._f("DIAMOND_IDLE_RELAX_HOURS", 4) * 3600

MIN_LIQ = sg._f("DIAMOND_MIN_LIQUIDITY_USD", 250_000)
MIN_VOL_24H = sg._f("DIAMOND_MIN_VOLUME_24H", 1_000_000)
MIN_AGE_S = sg._f("DIAMOND_MIN_AGE_DAYS", 3) * 86400
MIN_TXNS_24H = int(sg._f("DIAMOND_MIN_TXNS_24H", 3000))
MIN_MCAP = sg._f("DIAMOND_MIN_MCAP", 10_000_000)
MIN_TREND = sg._f("DIAMOND_MIN_TREND", 3)
MIN_VOL_SHARE_6H = sg._f("DIAMOND_MIN_VOL_SHARE_6H", 0.15)
PULL_MIN = sg._f("DIAMOND_PULLBACK_MIN", -12)
PULL_MAX = sg._f("DIAMOND_PULLBACK_MAX", -1.5)
MIN_BUY_SELL_1H = sg._f("DIAMOND_MIN_BUY_SELL_1H", 0.8)

STOP_LOSS = sg._f("DIAMOND_STOP_LOSS", 0.10)
TRAIL_ARM = sg._f("DIAMOND_TRAIL_ARM", 0.12)
TRAIL_DROP = sg._f("DIAMOND_TRAIL_DROP", 0.06)
EXIT = {"stop_loss": str(STOP_LOSS), "arm": str(1 + TRAIL_ARM), "drop": str(TRAIL_DROP)}
EXIT_TEXT = ("💎 Diamond: 🛑 stop -" + format(STOP_LOSS * 100, ".0f") + "% (tese quebrou) | 🚀 a partir de +" +
             format(TRAIL_ARM * 100, ".0f") + "% stop móvel " + format(TRAIL_DROP * 100, ".0f") +
             "% abaixo do topo | ⏱️ fim da rodada de " + format(ROUND_S / 3600, ".0f") + " h: vende")

# Established Solana tokens always checked (besides the coins DexScreener lists as active)
WATCHLIST = (
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",   # BONK
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",   # WIF
    "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",   # POPCAT
    "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",   # FARTCOIN
    "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",   # PENGU
    "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN",   # TRUMP
    "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5",    # MEW
    "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump",   # PNUT
    "ED5nyyWEzpPPiWimP8vYm7sD7TD3LAt3Q3gRTWHzPJBY",   # MOODENG
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",    # JUP
)

_LOCK = threading.RLock()
RULES = 2


def _path():
    import live_trade_state
    return live_trade_state._path().with_name("diamond_bot.json")


def _load():
    d = {"enabled": False, "round_start": 0, "tried": {}, "last_trade": 0, "log": [], "reported": True}
    p = _path()
    if p.exists():
        try:
            d.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return d


def _save(d):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".diamond.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def start_round(now=None):
    now = now or time.time()
    with _LOCK:
        d = _load()
        d.update(enabled=True, round_start=now, last_trade=now, reported=False, rules=RULES, log=[])
        _save(d)


def stop():
    with _LOCK:
        d = _load()
        d["enabled"] = False
        _save(d)


def enabled():
    return bool(_load()["enabled"])


def launch_round2():
    """Owner's request (Nov 14): run round 2 for the next 12 hours. Starts once."""
    with _LOCK:
        d = _load()
        if d.get("round2_v1"):
            return False
        d["round2_v1"] = True
        _save(d)
    start_round()
    return True


def _n(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def reason(p, now=None, relaxed=False):
    """'' when the coin passes every universe / trend / pullback rule, else the first rule it fails."""
    now = now or time.time()
    liq = _n((p.get("liquidity") or {}).get("usd"))
    vol = p.get("volume") or {}
    ch = p.get("priceChange") or {}
    tx = p.get("txns") or {}
    h24, h1 = tx.get("h24") or {}, tx.get("h1") or {}
    if liq < MIN_LIQ:
        return "U1 liquidez < US$ " + format(MIN_LIQ / 1000, ".0f") + " mil"
    if _n(vol.get("h24")) < MIN_VOL_24H:
        return "U2 volume 24 h baixo"
    created = _n(p.get("pairCreatedAt")) / 1000
    if not created or now - created < MIN_AGE_S:
        return "U3 menos de " + format(MIN_AGE_S / 86400, ".0f") + " dias"
    if _n(h24.get("buys")) + _n(h24.get("sells")) < MIN_TXNS_24H:
        return "U4 poucas operações 24 h"
    if _n(p.get("marketCap") or p.get("fdv")) < MIN_MCAP:
        return "U5 market cap < US$ " + format(MIN_MCAP / 1e6, ".0f") + " mi"
    if ((p.get("quoteToken") or {}).get("address")) not in sg.QUOTES:
        return "U6 par sem SOL/USDC"
    if max(_n(ch.get("h24")), _n(ch.get("h6"))) < MIN_TREND:
        return "T1 sem tendência de alta"
    if _n(vol.get("h6")) < MIN_VOL_SHARE_6H * _n(vol.get("h24")):
        return "T2 volume morrendo"
    lo, hi = PULL_MIN, (0 if relaxed else PULL_MAX)
    if not lo <= _n(ch.get("h1")) <= hi:
        return "E1 sem recuo (1 h fora de " + format(lo, "g") + "% a " + format(hi, "g") + "%)"
    if _n(ch.get("m5")) < 0:
        return "E2 ainda caindo (5 min)"
    if _n(h1.get("buys")) < MIN_BUY_SELL_1H * _n(h1.get("sells")):
        return "E3 vendedores dominando (1 h)"
    return ""


def watchlist_pairs(fetch=None):
    fetch = fetch or sg._get
    best = {}
    try:
        rows = fetch(sg.DEX + "/tokens/v1/solana/" + ",".join(WATCHLIST))
    except Exception:
        return []
    for p in rows if isinstance(rows, list) else []:
        mint = ((p or {}).get("baseToken") or {}).get("address")
        if mint and p.get("chainId") == "solana":
            if mint not in best or _n((p.get("liquidity") or {}).get("usd")) > _n((best[mint].get("liquidity") or {}).get("usd")):
                best[mint] = p
    return list(best.values())


def why_text(p):
    ch = p.get("priceChange") or {}
    h1 = (p.get("txns") or {}).get("h1") or {}
    return ("tendência 24 h " + format(_n(ch.get("h24")), "+.1f") + "% / 6 h " + format(_n(ch.get("h6")), "+.1f") +
            "%, recuo de " + format(_n(ch.get("h1")), "+.1f") + "% na última hora e já virando (" +
            format(_n(ch.get("m5")), "+.1f") + "% em 5 min), " + str(int(_n(h1.get("buys")))) + " compras x " +
            str(int(_n(h1.get("sells")))) + " vendas na hora, liquidez US$ " +
            format(_n((p.get("liquidity") or {}).get("usd")) / 1e3, ",.0f") + " mil, volume 24 h US$ " +
            format(_n((p.get("volume") or {}).get("h24")) / 1e6, ",.1f") + " mi")


def _buy(p, send, chat):
    import copy_trader as ct
    import live_engine
    import live_trade_state as st
    mint = p["baseToken"]["address"]
    symbol = (p.get("baseToken") or {}).get("symbol") or mint[:6]
    if not live_engine.ready():
        return "robô real não está pronto"
    allowed, why = st.can_open()
    if not allowed:
        return why
    positions = st._load()["positions"]
    if mint in positions:
        return "já tem a moeda"
    if st.positions_by_mode("diamond") >= MAX_OPEN:
        return "já tem posição Diamond aberta"
    if len(positions) >= st.MAX_POSITIONS:
        return "limite de posições da carteira"
    free = ct.wallet_usd(live_engine.get_address(), max_age=0)
    if free is None:
        return "saldo indisponível"
    if free < ORDER_USD + RESERVE_USD:
        return "saldo livre US$ " + format(free, ".2f")
    invested = sum(float(x.get("cost_usd") or 0) for x in positions.values())
    usd, _ = ct.exposure_cap(ORDER_USD, free + invested, invested, min_usd=ORDER_USD)
    if usd is None:
        return "limite de " + format(ct.MAX_EXPOSURE * 100, ".0f") + "% da conta em moedas"
    if send and chat:
        send(chat, "💎 DIAMOND COMPRANDO " + symbol + " — US$ " + format(usd, ".0f") + "\n📝 Motivo: " + why_text(p) +
             "\n📄 `" + mint + "`")
    settled = live_engine._execute_buy({"baseToken": {"address": mint, "symbol": symbol}}, mint, symbol, send, chat,
                                       live_engine.get_address(), None, "diamond", usd=usd)
    return "" if settled else "compra não confirmada"


def _end_round(send, chat, seller=None):
    import live_engine
    import live_trade_state as st
    for mint, pos in list(st._load()["positions"].items()):
        if pos.get("mode") == "diamond":
            try:
                (seller or live_engine.sell_now)(mint, "diamond_round_end", send, chat)
            except Exception as exc:
                logging.warning("diamond round-end sell failed: %s", type(exc).__name__)
    with _LOCK:
        d = _load()
        d.update(enabled=False, reported=True)
        _save(d)
    if send and chat:
        send(chat, "🏁 DIAMOND: fim da rodada.\n\n" + status_text())


def cycle(send=None, chat=None, pairs=None, now=None, safety=None, buyer=None, seller=None):
    """One 15-minute decision: buy the best coin passing every rule, else hold (and log why)."""
    now = now or time.time()
    d = _load()
    if not d["enabled"]:
        return None
    if now - float(d["round_start"]) >= ROUND_S:
        _end_round(send, chat, seller)
        return "fim"
    relaxed = now - float(d.get("last_trade") or d["round_start"]) >= IDLE_RELAX_S
    if pairs is None:
        pairs = watchlist_pairs() + sg.candidate_pairs()
    seen, why, ok = set(), {}, []
    for p in pairs:
        mint = ((p.get("baseToken") or {}).get("address"))
        if not mint or mint in seen:
            continue
        seen.add(mint)
        r = reason(p, now, relaxed)
        if r:
            why[r.split(" (")[0]] = why.get(r.split(" (")[0], 0) + 1
        else:
            ok.append(p)
    ok.sort(key=lambda p: _n((p.get("volume") or {}).get("h24")), reverse=True)
    decision = "HOLD (nenhuma moeda passou em todas as regras)"
    for p in ok:
        mint = p["baseToken"]["address"]
        sym = (p.get("baseToken") or {}).get("symbol") or mint[:6]
        if now - float(d["tried"].get(mint, 0)) < COOLDOWN_S:
            why["já tentou (4 h)"] = why.get("já tentou (4 h)", 0) + 1
            continue
        r = (safety or sg.safety_reason)(mint)
        if r:
            why["U7 anti-golpe"] = why.get("U7 anti-golpe", 0) + 1
            continue
        with _LOCK:
            dd = _load()
            dd["tried"][mint] = now
            dd["tried"] = {m: t for m, t in dd["tried"].items() if now - t < COOLDOWN_S}
            _save(dd)
        try:
            r = (buyer or _buy)(p, send, chat)
        except Exception as exc:
            r = type(exc).__name__
        if r:
            decision = "HOLD (" + sym + " passou nas regras, mas não comprou: " + r[:60] + ")"
            break
        decision = "BUY " + sym
        with _LOCK:
            dd = _load()
            dd["last_trade"] = now
            _save(dd)
        break
    top = ", ".join(k + "=" + str(v) for k, v in sorted(why.items(), key=lambda kv: -kv[1])[:8])
    line = time.strftime("%H:%M", time.gmtime(now)) + " " + decision + " | " + str(len(seen)) + " moedas" + \
        (" | recuo relaxado" if relaxed else "") + " | " + top
    logging.info("diamond cycle: %s", line)
    with _LOCK:
        dd = _load()
        dd["log"] = (dd.get("log") or [])[-60:] + [line]
        _save(dd)
    return decision


def status_text():
    import live_trade_state as st
    d = _load()
    since = float(d.get("round_start") or 0)
    left = max(0, since + ROUND_S - time.time()) if d["enabled"] else 0
    lines = ["💎 DIAMOND (dinheiro real) — " + ("LIGADO, faltam " + format(left / 3600, ".1f") + " h" if d["enabled"] else "DESLIGADO"),
             "Só tokens estabelecidos (liquidez US$ " + format(MIN_LIQ / 1e3, ".0f") + " mil+, volume 24 h US$ " +
             format(MIN_VOL_24H / 1e6, "g") + " mi+, " + format(MIN_AGE_S / 86400, ".0f") + "+ dias), compra recuo de " +
             format(-PULL_MAX, "g") + "% a " + format(-PULL_MIN, "g") + "% na hora dentro de tendência de alta, quando já vira para cima.",
             "💵 US$ " + format(ORDER_USD, ".0f") + " · máx. " + str(MAX_OPEN) + " aberta · decide a cada " +
             format(CYCLE_S / 60, ".0f") + " min · " + EXIT_TEXT]
    try:
        hist = [h for h in st.history() if h.get("mode") == "diamond" and h.get("pnl_usd") is not None
                and float(h.get("closed_at") or 0) >= since]
    except Exception:
        hist = []
    if hist:
        pnl = sum(h["pnl_usd"] for h in hist)
        lines.append("📊 Rodada: " + str(len(hist)) + " operações · " + str(sum(1 for h in hist if h["pnl_usd"] > 0)) +
                     " no lucro · total " + format(pnl, "+.2f") + " USD")
    else:
        lines.append("📊 Rodada: nenhuma operação fechada ainda.")
    log = d.get("log") or []
    if log:
        lines.append("🧾 Últimas decisões:\n" + "\n".join(log[-4:]))
    return "\n".join(lines)
