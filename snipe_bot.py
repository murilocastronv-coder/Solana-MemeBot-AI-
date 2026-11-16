"""SNIPER 24 h (owner, Nov 15): buys young pump.fun coins that show real traction, with REAL money.

Entry (every SNIPE_CYCLE_S, DexScreener data of the young coins):
- pump.fun coin, SNIPE_MIN_AGE_MIN..SNIPE_MAX_AGE_MIN minutes old, market cap inside the band;
- 5-min volume >= SNIPE_MIN_VOL_5M and buys / sells in 5 min >= SNIPE_MIN_BUY_RATIO (more buyers than sellers);
- 5-min move between SNIPE_MIN_UP_5M and SNIPE_MAX_UP_5M (moving, but not chasing a spike);
- anti-rug check (signals.safety_reason), one entry per coin ever, max SNIPE_MAX_OPEN open, daily loss limit.
Exit (exit_signal, used by the live exit monitor AND by the backtest, so both follow the same rule):
- stop -SNIPE_STOP; from +SNIPE_ARM the trailing stop sells SNIPE_DROP below the top;
- time stop: SNIPE_MAX_HOLD_MIN minutes without arming the trailing stop.
Nothing here promises profit; the backtest (snipe_backtest.py) measures the rule on past coins.
"""
import json
import logging
import os
import tempfile
import threading
import time

import signals as sg

E = sg._f
USD = E("SNIPE_USD", 3)                 # owner (Nov 16, 24/7 prompt): US$ 3 per coin
MAX_OPEN = int(E("SNIPE_MAX_OPEN", 3))
DAILY_LOSS = E("SNIPE_DAILY_LOSS_USD", 5)
OPERATIONAL_USD = E("SNIPE_OPERATIONAL_USD", 30)   # owner: US$ 60 = US$ 30 protected + US$ 30 to operate; the real
                                                   # sniper stops for good when its total loss reaches this
MIN_SCORE = E("SNIPE_MIN_SCORE", 0)
TP = E("SNIPE_TP", 0.20)                    # fixed take profit (0 = off)
COST_SIDE = E("SNIPE_COST_PER_SIDE", 0.03)  # swap fee + slippage per side on young coins (estimate)
NET_FEE_USD = E("SNIPE_NET_FEE_USD", 0.02)  # network + priority fee per transaction
MIN_AGE_MIN = E("SNIPE_MIN_AGE_MIN", 10)
MAX_AGE_MIN = E("SNIPE_MAX_AGE_MIN", 60)
MIN_MCAP = E("SNIPE_MIN_MCAP", 5_000)
MAX_MCAP = E("SNIPE_MAX_MCAP", 1_000_000)
MIN_VOL_5M = E("SNIPE_MIN_VOL_5M", 3_000)
MIN_BUY_RATIO = E("SNIPE_MIN_BUY_RATIO", 1.5)
MIN_UP_5M = E("SNIPE_MIN_UP_5M", 5)
MAX_UP_5M = E("SNIPE_MAX_UP_5M", 40)
STOP = E("SNIPE_STOP", 0.08)
ARM = E("SNIPE_ARM", 9.0)                   # trailing stop off by default (9 = +900%)
DROP = E("SNIPE_DROP", 0.12)
MAX_HOLD_MIN = E("SNIPE_MAX_HOLD_MIN", 30)
CYCLE_S = E("SNIPE_CYCLE_S", 20)
# owner (Nov 16): see EVERY new pump.fun coin, not only the few in the DexScreener lists. Every pump.fun create
# transaction uses the pump.fun mint authority account, so its signatures are exactly the new launches.
PUMP_MINT_AUTHORITY = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
NEW_PER_CYCLE = int(E("SNIPE_NEW_PER_CYCLE", 40))        # launches read per cycle (1 RPC call each)
WATCH_MAX = int(E("SNIPE_WATCH_MAX", 600))              # coins of the last hour followed on DexScreener
_LOCK = threading.RLock()


def apply_winner():
    """Owner's button: use the backtest's out-of-sample winner (only when it really won) and turn the sniper on."""
    import snipe_backtest as sbt
    w = sbt._get("snipe_bt_winner", {})
    if not w.get("ok"):
        return None
    with _LOCK:
        d = _load()
        d["cfg"] = {"entry": w["entry"], "exit": w["exit"], "ages": w.get("ages"), "window": w.get("window")}
        d["enabled"], d["live_ok"], d["paper"], d["sim_on"] = True, True, False, False
        _save(d)
    _use_cfg(d["cfg"])
    return d["cfg"]


def stop_live():
    with _LOCK:
        d = _load()
        d["live_ok"] = False
        _save(d)


def live_ok():
    return bool(_load().get("live_ok"))


def _use_cfg(cfg):
    global MIN_VOL_5M, MIN_BUY_RATIO, MIN_UP_5M, MAX_UP_5M, STOP, ARM, DROP, MAX_HOLD_MIN, MIN_AGE_MIN, MAX_AGE_MIN, TP, MIN_SCORE
    if cfg and cfg.get("window"):
        MIN_AGE_MIN, MAX_AGE_MIN = cfg["window"]                           # age group chosen by the backtest
    elif cfg and cfg.get("ages"):
        MIN_AGE_MIN, MAX_AGE_MIN = cfg["ages"][0], cfg["ages"][1] + 30      # winner for coins with 1h30+ of life
    if cfg and cfg.get("entry") and cfg.get("exit"):
        en, ex = list(cfg["entry"]), list(cfg["exit"])
        MIN_VOL_5M, MIN_BUY_RATIO, MIN_UP_5M, MAX_UP_5M = en[:4]
        MIN_SCORE = en[4] if len(en) > 4 else 0
        STOP, ARM, DROP, MAX_HOLD_MIN = ex[:4]
        TP = ex[4] if len(ex) > 4 else 0


def exit_signal(ratio, peak, held_s, stop=None, arm=None, drop=None, max_hold_min=None, tp=None):
    """('sell' | 'hold', reason). ratio = value now / cost, peak = best ratio so far."""
    tp = TP if tp is None else tp
    if tp and ratio >= 1 + tp:
        return "sell", "take_profit"
    stop = STOP if stop is None else stop
    arm = ARM if arm is None else arm
    drop = DROP if drop is None else drop
    max_hold_min = MAX_HOLD_MIN if max_hold_min is None else max_hold_min
    if ratio <= 1 - stop:
        return "sell", "stop_loss"
    if peak >= 1 + arm and ratio <= peak * (1 - drop):
        return "sell", "take_profit"
    if max_hold_min and held_s >= max_hold_min * 60 and (peak < 1 + arm or arm >= 9):
        return "sell", "time_stop"
    return "hold", ""


def entry_reason(p, now=None):
    """'' when the coin passes the entry rule, else why not (same numbers the backtest uses)."""
    now = now or time.time()
    if not sg.is_pump(p):
        return "não é pump.fun"
    created = p.get("pairCreatedAt")
    age = (now - float(created) / 1000) / 60 if created else None
    if age is None or not MIN_AGE_MIN <= age <= MAX_AGE_MIN:
        return "idade fora de " + format(MIN_AGE_MIN, "g") + "–" + format(MAX_AGE_MIN, "g") + " min"
    return metrics_reason(float(p.get("marketCap") or p.get("fdv") or 0), float((p.get("volume") or {}).get("m5") or 0),
                          float(((p.get("txns") or {}).get("m5") or {}).get("buys") or 0),
                          float(((p.get("txns") or {}).get("m5") or {}).get("sells") or 0),
                          float((p.get("priceChange") or {}).get("m5") or 0))


def _lin(x, lo, hi):
    return max(0.0, min(1.0, (x - lo) / (hi - lo))) if hi > lo else 0.0


def score(mcap, vol5, buys, sells, up5):
    """0-100 from the 5-minute numbers: organic volume, buyers vs sellers, number of buyers, momentum (not a spike),
    size of the market cap. Not a probability of success: the backtest decides which minimum is worth using."""
    ratio = buys / max(sells, 1)
    pts = (30 * _lin(vol5, 2_000, 20_000) + 25 * _lin(ratio, 0.8, 2.5) + 15 * _lin(buys, 10, 120) +
           20 * (_lin(up5, -5, 15) if up5 <= 15 else 1 - _lin(up5, 40, 120)) + 10 * _lin(mcap, 6_000, 40_000))
    return round(pts, 1)


def costs_ok(usd=None, tp=None):
    """The round trip (swap + slippage both sides + 2 network fees) must cost less than half of the take profit."""
    usd = USD if usd is None else usd
    tp = TP if tp is None else tp
    cost = 2 * COST_SIDE + 2 * NET_FEE_USD / max(usd, 0.01)
    return (not tp) or cost < tp / 2


def metrics_reason(mcap, vol5, buys, sells, up5, min_vol=None, min_ratio=None, min_up=None, max_up=None, min_score=None):
    min_vol = MIN_VOL_5M if min_vol is None else min_vol
    min_ratio = MIN_BUY_RATIO if min_ratio is None else min_ratio
    min_up = MIN_UP_5M if min_up is None else min_up
    max_up = MAX_UP_5M if max_up is None else max_up
    if not MIN_MCAP <= mcap <= MAX_MCAP:
        return "market cap fora da faixa"
    if vol5 < min_vol:
        return "volume 5 min baixo"
    if buys / max(sells, 1) < min_ratio:
        return "mais vendedores que compradores"
    if not min_up <= up5 <= max_up:
        return "movimento 5 min fora de " + format(min_up, "g") + "–" + format(max_up, "g") + "%"
    min_score = MIN_SCORE if min_score is None else min_score
    if min_score and score(mcap, vol5, buys, sells, up5) < min_score:
        return "nota abaixo de " + format(min_score, "g")
    return ""


# ---------------------------------------------------------------- state (PostgreSQL + disk)
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("snipe_bot.json")


def _load():
    d = {"enabled": True, "seen": {}, "log": []}
    import pg_store
    if pg_store.enabled():
        try:
            got = pg_store.get("snipe_bot")
            if got is not None:
                d.update(got)
                return d
        except Exception:
            pass
    p = _path()
    if p.exists():
        try:
            d.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return d


def _save(d):
    import pg_store
    if pg_store.enabled():
        try:
            pg_store.put("snipe_bot", d)
        except Exception as exc:
            logging.warning("snipe: postgres save failed (%s)", type(exc).__name__)
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".snipe.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def note(text):
    logging.info("snipe %s", text)
    with _LOCK:
        d = _load()
        d["log"] = (d.get("log") or [])[-99:] + [time.strftime("%d/%m %H:%M", time.gmtime()) + " " + text]
        _save(d)


def enabled():
    return bool(_load().get("enabled"))


def set_enabled(on):
    with _LOCK:
        d = _load()
        d["enabled"] = bool(on)
        _save(d)


def _dev_on():
    try:
        import dev_bot
        return dev_bot.enabled()
    except Exception:
        return False


def _history():
    try:
        import live_trade_state
        return [h for h in live_trade_state.history() if h.get("mode") == "snipe"]
    except Exception:
        return []


def _open():
    try:
        import live_trade_state
        return [m for m, p in live_trade_state._load()["positions"].items() if p.get("mode") == "snipe"]
    except Exception:
        return []


def day_pnl(now=None):
    day = time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))
    return sum(float(h.get("pnl_usd") or 0) for h in _history()
               if time.strftime("%Y-%m-%d", time.gmtime(float(h.get("closed_at") or 0))) == day)


# ---------------------------------------------------------------- simulation (paper) mode
def paper():
    return _load().get("paper", True)


def running():
    """Buying now? Simulation: only after the owner's INICIAR SNIPER. Real: only with the validated winner applied."""
    d = _load()
    if not d.get("enabled"):
        return False
    return bool(d.get("sim_on")) if d.get("paper", True) else bool(d.get("live_ok"))


def start_sim():
    with _LOCK:
        d = _load()
        d.update(enabled=True, paper=True, sim_on=True)
        _save(d)


def stop_all():
    with _LOCK:
        d = _load()
        d.update(sim_on=False, live_ok=False)
        _save(d)


def set_paper(on):
    with _LOCK:
        d = _load()
        d["paper"] = bool(on)
        _save(d)


def _paper_hist(d=None):
    return (d or _load()).get("paper_hist") or []


def paper_day_pnl(now=None, d=None):
    day = time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))
    return sum(h["pnl_usd"] for h in _paper_hist(d) if time.strftime("%Y-%m-%d", time.gmtime(h["closed"])) == day)


def _price(p):
    try:
        return float(p.get("priceUsd") or 0)
    except (TypeError, ValueError):
        return 0.0


def _buy_paper(p, mint, sym, send, chat, now=None):
    px = _price(p)
    if px <= 0:
        return False
    now = now or time.time()
    with _LOCK:
        d = _load()
        d.setdefault("paper_pos", {})[mint] = {"sym": sym, "entry": px, "peak": 1.0, "t": now, "usd": USD,
                                               "score": score(float(p.get("marketCap") or 0), float((p.get("volume") or {}).get("m5") or 0),
                                                              float(((p.get("txns") or {}).get("m5") or {}).get("buys") or 0),
                                                              float(((p.get("txns") or {}).get("m5") or {}).get("sells") or 0),
                                                              float((p.get("priceChange") or {}).get("m5") or 0))}
        _save(d)
    if send and chat:
        send(chat, "🧪 SNIPER (SIMULAÇÃO) comprou " + sym + " · US$ " + format(USD, "g") + " · nota " +
             format(d["paper_pos"][mint]["score"], ".0f") + "\nhttps://dexscreener.com/solana/" + mint)
    return True


def paper_exits(send=None, chat=None, now=None, fetch=None):
    """Walk the simulated positions with the same exit rule as the real one (DexScreener price, costs both sides)."""
    now = now or time.time()
    d = _load()
    pos = d.get("paper_pos") or {}
    if not pos:
        return 0
    try:
        rows = (fetch or sg._get)(sg.DEX + "/tokens/v1/solana/" + ",".join(list(pos)[:30])) or []
    except Exception:
        return 0
    px = {}
    for r in rows if isinstance(rows, list) else []:
        m = (r.get("baseToken") or {}).get("address")
        if m and _price(r) > 0:
            px.setdefault(m, _price(r))
    closed = 0
    with _LOCK:
        d = _load()
        pos = d.get("paper_pos") or {}
        for m, x in list(pos.items()):
            if m not in px:
                if now - x["t"] > 6 * 3600:                     # no price for 6 h: dead coin, total loss
                    ratio, why = 0.0, "sem preço (moeda morreu)"
                else:
                    continue
            else:
                ratio = px[m] / x["entry"]
                x["peak"] = max(x["peak"], ratio)
                act, why = exit_signal(ratio, x["peak"], now - x["t"])
                if act != "sell":
                    continue
            pnl = x["usd"] * (ratio * (1 - COST_SIDE) ** 2 - 1) - 2 * NET_FEE_USD
            d["paper_hist"] = (d.get("paper_hist") or [])[-499:] + [{"mint": m, "sym": x["sym"], "opened": x["t"], "closed": now,
                                                                     "result_pct": round((ratio - 1) * 100, 2), "pnl_usd": round(pnl, 4),
                                                                     "exit": why, "score": x.get("score")}]
            del pos[m]
            closed += 1
            if send and chat:
                send(chat, ("🟢" if pnl > 0 else "🔴") + " SNIPER (SIMULAÇÃO) vendeu " + x["sym"] + " " + format((ratio - 1) * 100, "+.1f") +
                     "% · líquido " + format(pnl, "+.2f") + " USD · " + why)
        d["paper_pos"] = pos
        _save(d)
    return closed


# ---------------------------------------------------------------- cycle
def scan_launches(rpc, now=None, get_tx=None):
    """Add the newest pump.fun launches (on-chain) to the watch list {mint: created}. Returns how many were added."""
    now = now or time.time()
    d = _load()
    opts = {"limit": 100}
    if d.get("auth_last"):
        opts["until"] = d["auth_last"]
    try:
        sigs = rpc("getSignaturesForAddress", [PUMP_MINT_AUTHORITY, opts]) or []
    except Exception as exc:
        logging.info("snipe launches: %s", type(exc).__name__)
        return 0
    if not sigs:
        return 0
    added, creators = {}, []
    for sg_ in [x for x in sigs if not x.get("err")][:NEW_PER_CYCLE]:
        try:
            if get_tx:
                tx = get_tx(rpc, sg_["signature"])
            else:
                tx = rpc("getTransaction", [sg_["signature"], {"encoding": "json", "commitment": "confirmed",
                                                               "maxSupportedTransactionVersion": int(os.getenv("SOLANA_MAX_TX_VERSION", "1"))}])
        except Exception:
            continue
        for b in ((tx or {}).get("meta") or {}).get("postTokenBalances") or []:
            m = b.get("mint")
            if m and m.endswith("pump"):
                added[m] = int(sg_.get("blockTime") or now)
                msg = ((tx or {}).get("transaction") or {}).get("message") or {}
                keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
                if keys:
                    creators.append((keys[0], m))      # owner (Nov 16): the creator goes to the dev analysis
                break
    try:
        import grad_bot
        grad_bot.add_launches(added)          # owner (Nov 16): graduation robot watches every launch
    except Exception as exc:
        logging.info("snipe grad feed: %s", type(exc).__name__)
    if creators and _dev_on():
        try:
            import dev_bot
            dev_bot.enqueue_creators(creators, now)
        except Exception as exc:
            logging.info("snipe creators: %s", type(exc).__name__)
    with _LOCK:
        dd = _load()
        dd["auth_last"] = sigs[0]["signature"]
        w = dd.get("watch") or {}
        w.update(added)
        dd["watch"] = dict(sorted(((m, t) for m, t in w.items() if now - t < MAX_AGE_MIN * 60), key=lambda kv: kv[1])[-WATCH_MAX:])
        _save(dd)
    return len(added)


def watched_pairs(now=None, fetch=None):
    """DexScreener data of the watched launches that are old enough to judge (30 coins per call)."""
    now = now or time.time()
    w = _load().get("watch") or {}
    mints = [m for m, t in sorted(w.items(), key=lambda kv: -kv[1]) if MIN_AGE_MIN * 60 <= now - t <= MAX_AGE_MIN * 60]
    out = []
    for i in range(0, len(mints), 30):
        try:
            rows = (fetch or sg._get)(sg.DEX + "/tokens/v1/solana/" + ",".join(mints[i:i + 30])) or []
        except Exception:
            break
        best = {}
        for p in rows if isinstance(rows, list) else []:
            m = (p.get("baseToken") or {}).get("address")
            if m and float((p.get("volume") or {}).get("m5") or 0) >= float((best.get(m) or {}).get("volume", {}).get("m5") or -1):
                best[m] = p
        out += list(best.values())
    return out


def cycle(send=None, chat=None, pairs=None, now=None, buy=None, safety=None, rpc=None, fetch=None):
    """One pass: pick at most one coin that passes the rule and buy it (real money). Returns the mint bought."""
    now = now or time.time()
    d = _load()
    if not d.get("enabled"):
        return None
    if rpc is not None:
        scan_launches(rpc, now)
    sim = d.get("paper", True)
    if sim:
        paper_exits(send, chat, now, fetch)
        d = _load()
    if DAILY_LOSS > 0 and (paper_day_pnl(now, d) if sim else day_pnl(now)) <= -DAILY_LOSS:
        return None
    if len(d.get("paper_pos") or {} if sim else _open()) >= MAX_OPEN:
        return None
    if not sim and OPERATIONAL_USD and sum(float(h.get("pnl_usd") or 0) for h in _history()) <= -OPERATIONAL_USD:
        return None                                   # the US$ 30 to operate are gone: the protected half stays
    if not costs_ok():
        return None
    if pairs is None:
        pairs = sg.candidate_pairs()
        if rpc is not None:
            have = {(p.get("baseToken") or {}).get("address") for p in pairs}
            pairs = pairs + [p for p in watched_pairs(now, fetch) if (p.get("baseToken") or {}).get("address") not in have]
    seen = d.get("seen") or {}
    for p in sorted(pairs, key=lambda x: -float((x.get("volume") or {}).get("m5") or 0)):
        mint = (p.get("baseToken") or {}).get("address")
        if not mint or mint in seen or entry_reason(p, now):
            continue
        why = (safety or sg.safety_reason)(mint)
        with _LOCK:
            dd = _load()
            dd.setdefault("seen", {})[mint] = now                 # one entry per coin, ever (cooldown)
            dd["seen"] = {m: t for m, t in dd["seen"].items() if now - t < 7 * 86400}
            _save(dd)
        sym = (p.get("baseToken") or {}).get("symbol") or mint[:6]
        if why:
            note(sym + " passou no sinal, NÃO comprei: anti-golpe: " + why)
            continue
        ok = (buy or (_buy_paper if sim else _buy_real))(p, mint, sym, send, chat)
        note(("SIMULAÇÃO comprou " if sim and ok else "COMPROU " if ok else "compra NÃO confirmada ") + sym + " · mcap US$ " +
             format(float(p.get("marketCap") or 0), ",.0f") + " · vol 5 min US$ " +
             format(float((p.get("volume") or {}).get("m5") or 0), ",.0f"))
        return mint if ok else None
    return None


def _buy_real(p, mint, sym, send, chat):
    import live_engine
    try:
        settled = live_engine._execute_buy({"baseToken": p.get("baseToken"), "liquidity": p.get("liquidity"),
                                            "marketCap": p.get("marketCap")}, mint, sym, send, chat,
                                           live_engine.get_address(), None, "snipe", usd=USD)
    except Exception as exc:
        note("compra falhou " + sym + ": " + type(exc).__name__)
        return False
    return bool(settled and settled.get("status") == "settled_buy")


def status_text():
    d = _load()
    h = _history()
    wins = [x for x in h if float(x.get("pnl_usd") or 0) > 0]
    total = sum(float(x.get("pnl_usd") or 0) for x in h)
    sim = d.get("paper", True)
    if sim:
        h = _paper_hist(d)
        wins = [x for x in h if x["pnl_usd"] > 0]
        total = sum(x["pnl_usd"] for x in h)
    on = running()
    lines = ["🎯 SNIPER 24 h — " + ("🟢 RODANDO" if on else "⏸️ PARADO") + (" (🧪 SIMULAÇÃO, dinheiro de mentira)" if sim else " (💵 DINHEIRO REAL)"),
             "⚙️ US$ " + format(USD, "g") + " por compra · máx. " + str(MAX_OPEN) + " abertas · perda máx. do dia US$ " +
             format(DAILY_LOSS, "g") + " (hoje " + format(paper_day_pnl(d=d) if sim else day_pnl(), "+.2f") + ")" +
             ("" if sim else " · para de vez se perder US$ " + format(OPERATIONAL_USD, "g") + " no total"),
             "📥 Entra: pump.fun de " + format(MIN_AGE_MIN, "g") + "–" + format(MAX_AGE_MIN, "g") + " min, market cap US$ " +
             format(MIN_MCAP / 1000, "g") + "–" + format(MAX_MCAP / 1000, "g") + " mil, volume 5 min ≥ US$ " +
             format(MIN_VOL_5M, ",.0f") + ", compras/vendas ≥ " + format(MIN_BUY_RATIO, "g") + ", subiu " +
             format(MIN_UP_5M, "g") + "–" + format(MAX_UP_5M, "g") + "% em 5 min" + (", nota ≥ " + format(MIN_SCORE, "g") if MIN_SCORE else "") +
             ", anti-golpe",
             "📤 Sai: " + ("alvo +" + format(TP * 100, ".0f") + "% · " if TP else "") + "stop −" + format(STOP * 100, ".0f") + "%" +
             (" · de +" + format(ARM * 100, ".0f") + "% stop móvel " + format(DROP * 100, ".0f") + "% abaixo do topo" if ARM < 9 else "") +
             " · " + format(MAX_HOLD_MIN, "g") + " min no máximo",
             "📊 " + str(len(h)) + " operações · " + str(len(wins)) + " no lucro · total " + format(total, "+.2f") +
             " USD · abertas " + str(len(d.get("paper_pos") or {}) if sim else len(_open())) + "/" + str(MAX_OPEN),
             "👀 Vigiando " + str(len(d.get("watch") or {})) + " lançamentos da última hora (direto da blockchain)"]
    for x in (d.get("log") or [])[-5:]:
        lines.append("• " + x)
    return "\n".join(lines)


try:                                   # the owner's applied backtest winner survives restarts
    _use_cfg((_load() or {}).get("cfg"))
except Exception:
    pass
