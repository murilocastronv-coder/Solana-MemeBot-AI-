"""GRADUATION backtest v2 (owner, Nov 16: "it lost 6 in a row live - improve the hit rate and test it").

Why v2: v1 read the price only every 30 min after the first 20 min, so a dip under the stop that recovered before the
next reading was never seen - the backtest looked better than live trading (live sells at once at -25%).
v2 uses 1-MINUTE CANDLES (high/low) of each coin's PumpSwap pool (GeckoTerminal, free, 1 read per coin), from the
graduation (first candle of the pool) to 4 h after. Inside a candle the stop is checked first with the LOW
(pessimistic), the peak is updated with the HIGH, the trailing stop is checked with the next candles.

It also tests ENTRY FILTERS, known at the moment of the buy only (no peeking):
- delay after graduation: 1 / 2 / 5 / 10 min;
- move since graduation (entry price / graduation price): any, still up (>= 1.0), strong (>= 1.3), dip (<= 0.9);
- volume since graduation (USD): any, >= 20k, >= 60k;
- how fast it graduated after launch: any, < 30 min, > 2 h.
Each combination is chosen on the first 70% of the time and checked on the last 30% (out of sample).
Coins: the graduated coins found by grad_backtest (pump.fun launches sampled over 3 months, curve 'complete' flag).
"""
import hashlib
import logging
import random
import threading
import time

import snipe_backtest as sbt
import signals as sg

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL = "So11111111111111111111111111111111111111112"
COST = sg._f("GRAD_BT_COST_PER_SIDE", 0.02)
USD, CAPITAL = 10.0, 500.0
MAX_OPEN = int(sg._f("GRAD_MAX_OPEN", 4))
DAILY_LOSS = sg._f("GRAD_DAILY_LOSS_USD", 20)
HOLD_S = 4 * 3600
SHARDS = 16
DELAYS = (60, 120, 300, 600)
MOVES = (("qualquer", None, None), ("subindo ≥1,0x", 1.0, None), ("forte ≥1,3x", 1.3, None), ("queda ≤0,9x", None, 0.9))
VOLS = (("vol qualquer", 0), ("vol ≥20 mil", 20_000), ("vol ≥60 mil", 60_000))
SPEEDS = (("qualquer tempo", None, None), ("graduou <30 min", None, 1800), ("graduou >2 h", 7200, None))
EXITS = (("sl25 trail+50/20%", 0.25, 0, 0.50, 0.20, 240), ("sl15 trail+30/15%", 0.15, 0, 0.30, 0.15, 240),
         ("sl20 tp50", 0.20, 0.50, 0, 0, 240), ("sl10 tp20 30min", 0.10, 0.20, 0, 0, 30),
         ("sl30 trail+100/25%", 0.30, 0, 1.00, 0.25, 240), ("sl40 trail+60/30% 2h", 0.40, 0, 0.60, 0.30, 120))
_RUNNING = threading.Lock()
BG_MAX_S = sg._f("GRAD_BT_BG_MAX_S", 3600)       # candles keep downloading in the background up to this long


# ---------------------------------------------------------------- data
def pool_of(mint):
    """Canonical PumpSwap pool of a graduated pump.fun coin (index 0, creator = pump 'pool-authority' PDA)."""
    from solders.pubkey import Pubkey
    m = Pubkey.from_string(mint)
    creator = Pubkey.find_program_address([b"pool-authority", bytes(m)], Pubkey.from_string(PUMP_PROGRAM))[0]
    return str(Pubkey.find_program_address([b"pool", (0).to_bytes(2, "little"), bytes(creator), bytes(m),
                                            bytes(Pubkey.from_string(WSOL))], Pubkey.from_string(PUMPSWAP))[0])


def _gecko_pool(mint, fetch):
    data = fetch(sg.GECKO + "/networks/solana/tokens/" + mint + "/pools?page=1") or {}
    for p in data.get("data") or []:
        dex = str((((p.get("relationships") or {}).get("dex") or {}).get("data") or {}).get("id") or "")
        if "pump" in dex and "fun" not in dex:
            return ((p.get("attributes") or {}).get("address"))
    return None


def _ohlcv(pool, before, fetch):
    url = (sg.GECKO + "/networks/solana/pools/" + pool + "/ohlcv/minute?aggregate=1&before_timestamp=" + str(int(before)) +
           "&limit=1000&currency=usd")
    data = fetch(url) or {}
    if not isinstance(data, dict):
        return []
    rows = ((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return sorted([[int(r[0])] + [float(x) for x in r[1:6]] for r in rows], key=lambda r: r[0])


def candles(mint, launch, fetch=None):
    """[[t, o, h, l, c, vol_usd], ...] from the graduation to 4 h after, or [] (no pool / no data)."""
    import requests
    fetch = fetch or sg._get
    pool = pool_of(mint)
    try:
        rows = _ohlcv(pool, launch + 5 * 3600, fetch)
    except requests.HTTPError as exc:
        if getattr(exc.response, "status_code", None) not in (400, 404):
            raise
        pool = _gecko_pool(mint, fetch)
        if not pool:
            return []
        rows = _ohlcv(pool, launch + 5 * 3600, fetch)
    if not rows:                                      # graduated more than 5 h after the launch
        rows = _ohlcv(pool, launch + 48 * 3600, fetch)
    for _ in range(3):                                # page back to the pool's first candle (the graduation)
        if len(rows) < 1000:
            break
        older = _ohlcv(pool, rows[0][0], fetch)
        if not older:
            break
        rows = older + [r for r in rows if r[0] > older[-1][0]]
    if not rows:
        return []
    g = rows[0][0]
    if rows[-1][0] < g + HOLD_S and launch + 5 * 3600 < g + HOLD_S:
        later = _ohlcv(pool, g + HOLD_S + 60, fetch)
        rows += [r for r in later if r[0] > rows[-1][0]]
    out = []
    for r in rows:
        if r[0] > g + HOLD_S:
            break
        out.append([r[0]] + [float(format(x, ".4g")) for x in r[1:5]] + [int(r[5])])
    return out


def _shard(m):
    return "grad_c_" + str(int(hashlib.md5(m.encode()).hexdigest(), 16) % SHARDS)


def load_all():
    out = {}
    for i in range(SHARDS):
        out.update(sbt._get("grad_c_" + str(i), {}))
    return out


def _save(store, keys):
    for k in keys:
        sbt._put(k, store[k])


# ---------------------------------------------------------------- simulation
def entry_ok(c, launch, delay, move, vol, speed):
    """(index of the entry candle, entry price) when the coin passes the filters at the moment of the buy, else None."""
    if not c:
        return None
    g = c[0][0]
    t = g + delay
    i = next((k for k, r in enumerate(c) if r[0] >= t), None)
    if i is None or i == 0:
        return None
    before = c[:i]
    px = before[-1][4]                                # last close before the buy
    first = c[0][1] or c[0][4]
    if not px or not first:
        return None
    m = px / first
    if move[1] is not None and m < move[1]:
        return None
    if move[2] is not None and m > move[2]:
        return None
    if vol[1] and sum(r[5] for r in before) < vol[1]:
        return None
    took = g - launch
    if speed[1] is not None and took < speed[1]:
        return None
    if speed[2] is not None and took >= speed[2]:
        return None
    return i, px * 1.0


def trade(c, i, entry, ex):
    """(return, exit time, reason) on 1-min candles, stop checked first with the low."""
    name, sl, tp, arm, drop, hold = ex
    t0 = c[i][0]
    peak = 1.0
    for r in c[i:]:
        t, o, h, lo, cl = r[0], r[1], r[2], r[3], r[4]
        if t - t0 >= hold * 60:
            return o / entry * (1 - COST) ** 2 - 1, t, "tempo"
        if lo / entry <= 1 - sl:
            return min(o / entry, 1 - sl) * (1 - COST) ** 2 - 1, t, "stop"
        if arm and peak >= 1 + arm and lo / entry <= peak * (1 - drop):
            return min(o / entry, peak * (1 - drop)) * (1 - COST) ** 2 - 1, t, "stop móvel"
        if tp and h / entry >= 1 + tp:
            return (1 + tp) * (1 - COST) ** 2 - 1, t, "alvo"
        peak = max(peak, h / entry)
    return c[-1][4] / entry * (1 - COST) ** 2 - 1, c[-1][0], "fim dos dados"


def simulate(rows, start, end, rule):
    delay, move, vol, speed, ex = rule
    open_until, day_pnl, pnl = [], {}, []
    for launch, c in sorted(((l, c) for l, c in rows if c and start <= c[0][0] < end), key=lambda x: x[1][0][0]):
        e = entry_ok(c, launch, delay, move, vol, speed)
        if not e:
            continue
        t_in = c[e[0]][0]
        open_until = [x for x in open_until if x > t_in]
        day = time.strftime("%Y-%m-%d", time.gmtime(t_in))
        if len(open_until) >= MAX_OPEN or day_pnl.get(day, 0) <= -DAILY_LOSS:
            continue
        r, t_out, _ = trade(c, e[0], e[1], ex)
        x = USD * r - 0.04
        open_until.append(t_out)
        day_pnl[day] = day_pnl.get(day, 0) + x
        pnl.append(x)
    gw, gl = sum(v for v in pnl if v > 0), -sum(v for v in pnl if v < 0)
    eq = peak = CAPITAL
    dd = 0.0
    for v in pnl:
        eq += v
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak)
    return {"n": len(pnl), "pnl": sum(pnl), "win": sum(1 for v in pnl if v > 0) / len(pnl) if pnl else 0.0,
            "pf": gw / gl if gl > 0 else None, "dd": dd}


def rules():
    for d in DELAYS:
        for mv in MOVES:
            for vo in VOLS:
                for sp in SPEEDS:
                    for ex in EXITS:
                        yield d, mv, vo, sp, ex


def _name(rule):
    d, mv, vo, sp, ex = rule
    return ((str(d // 60) + " min") + " depois · " + mv[0] + " · " + vo[0] + " · " + sp[0] + " · saída " + ex[0])


def _fmt(s):
    return (str(s["n"]) + " ops · acerto " + format(s["win"] * 100, ".0f") + "% · " + format(s["pnl"], "+.2f") + " USD · PF " +
            ("—" if s["pf"] is None else format(s["pf"], ".2f")) + " · queda máx " + format(s["dd"] * 100, ".0f") + "%")


def report(store, launch, grads, final=False):
    rows = [(launch.get(m, 0), c) for m, c in store.items() if c and m in launch]
    L = ["🎓 BACKTEST GRADUADOS v2 — velas de 1 MINUTO (máxima e mínima), filtros de entrada",
         "Moedas que graduaram na amostra: " + str(grads) + " · com velas baixadas: " + str(len(rows)) + " · US$ " +
         format(USD, "g") + " por compra · máx. " + str(MAX_OPEN) + " abertas · para no dia com −US$ " + format(DAILY_LOSS, "g") +
         " · custo " + format(COST * 100, ".0f") + "% por lado"]
    if len(rows) < 20:
        L.append("⏳ Poucas moedas ainda; o download continua sozinho e manda outro relatório.")
        return "\n".join(L), False, None
    t0 = min(c[0][0] for _, c in rows)
    t1 = max(c[0][0] for _, c in rows) + 1
    cut = t0 + (t1 - t0) * 0.7
    live = simulate(rows, t0, t1, (60, MOVES[0], VOLS[0], SPEEDS[0], EXITS[0]))
    L.append("📌 Regra que está rodando no real (1 min, sem filtro, sl25 trail+50/20%), período todo: " + _fmt(live))
    scored = []
    for rule in rules():
        tr = simulate(rows, t0, cut, rule)
        if tr["n"] >= 15:
            scored.append((tr["pnl"], tr, rule))
    scored.sort(key=lambda x: -x[0])
    ok, best = False, None
    if scored:
        L.append("🏆 5 melhores no TREINO (70%) e o resultado deles no TESTE (30% finais, fora da amostra):")
        for _, tr, rule in scored[:5]:
            te = simulate(rows, cut, t1, rule)
            good = te["n"] >= 20 and te["pnl"] > 0 and te["pf"] is not None and te["pf"] >= 1.3
            L.append("• " + _name(rule) + "\n   treino " + _fmt(tr) + "\n   TESTE " + _fmt(te) + (" ✅" if good else " ❌"))
            if good and not ok:
                ok, best = True, rule
    else:
        L.append("🏆 Nenhuma combinação com 15+ operações no treino ainda.")
    if not final:
        L.append("⏳ PARCIAL: as velas continuam baixando sozinhas (~1 moeda a cada 2–3 s); chega outro relatório no fim.")
    L.append("✅ Achou regra com lucro fora da amostra: " + _name(best) if ok else
             "❌ Nenhuma regra com lucro confiável fora da amostra (precisa ≥ 20 ops no teste, lucro e PF ≥ 1,3).")
    L.append("⚠️ Velas de 1 min da GeckoTerminal; custos fixos de " + format(COST * 100, ".0f") +
             "% por lado (no real pode ser mais); liquidez não simulada. Passado não garante futuro.")
    return "\n".join(L), ok, best


def start(send=None, chat=None, fetch=None, first_report_s=170):
    """Download the 1-min candles of every graduated coin of the sample (background), report at ~3 min and at the end."""
    if not _RUNNING.acquire(blocking=False):
        return "o teste v2 já está rodando (as velas continuam baixando)"

    def work():
        try:
            g = sbt._get("grad_bt_cache", {"launch": {}, "complete": {}})
            launch = g.get("launch") or {}
            grads = sorted(m for m, ok in (g.get("complete") or {}).items() if ok)
            random.Random(7).shuffle(grads)            # spread over the 3 months, so a partial report is fair
            store = load_all()
            began = time.time()
            sent_first = False
            dirty = set()
            stats = {"com velas": 0, "sem velas": 0, "erro": 0, "limite": 0}
            for m in grads:
                if m in store:
                    continue
                if time.time() - began > BG_MAX_S:
                    break
                for attempt in range(6):                   # rate limited: wait and try the SAME coin again
                    if not sent_first and time.time() - began > first_report_s:
                        _save(store_sh(store), dirty)
                        dirty.clear()
                        txt, _, _ = report(store, launch, len(grads))
                        if send and chat:
                            send(chat, txt + "\n📥 até agora: " + ", ".join(k + " " + str(v) for k, v in stats.items()))
                        sent_first = True
                    try:
                        store[m] = candles(m, launch.get(m, 0), fetch)
                        stats["com velas" if store[m] else "sem velas"] += 1
                        break
                    except sg.GeckoBusy:
                        stats["limite"] += 1
                        time.sleep(20)
                    except Exception as exc:
                        code = getattr(getattr(exc, "response", None), "status_code", None)
                        if code == 429:
                            stats["limite"] += 1
                            time.sleep(30)
                            continue
                        stats["erro"] += 1
                        logging.warning("grad candles %s: %s: %s", m[:8], type(exc).__name__, str(exc)[:150])
                        store[m] = []
                        break
                if m in store:
                    dirty.add(_shard(m))
                n = sum(stats.values())
                if n % 25 == 0:
                    logging.warning("grad candles progress: %s", stats)
                if dirty and int(time.time() - began) % 60 < 3:
                    _save(store_sh(store), dirty)
                    dirty.clear()
            _save(store_sh(store), dirty or {_shard(m) for m in store})
            txt, ok, best = report(store, launch, len(grads), final=True)
            sbt._put("grad_v2_winner", {"ok": ok, "rule": _rule_json(best) if best else None, "at": time.time()})
            if send and chat:
                send(chat, txt + "\n📥 download: " + ", ".join(k + " " + str(v) for k, v in stats.items()))
        except Exception as exc:
            logging.exception("grad candles failed")
            if send and chat:
                send(chat, "⛔ Backtest graduados v2 falhou: " + type(exc).__name__ + ": " + str(exc)[:150])
        finally:
            _RUNNING.release()

    threading.Thread(target=work, daemon=True).start()
    return ""


def store_sh(store):
    sh = {}
    for m, c in store.items():
        sh.setdefault(_shard(m), {})[m] = c
    return sh


def _rule_json(rule):
    d, mv, vo, sp, ex = rule
    return {"delay": d, "move": list(mv), "vol": list(vo), "speed": list(sp), "exit": list(ex)}
