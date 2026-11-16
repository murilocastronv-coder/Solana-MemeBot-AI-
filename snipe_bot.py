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
USD = E("SNIPE_USD", 10)
MAX_OPEN = int(E("SNIPE_MAX_OPEN", 2))
DAILY_LOSS = E("SNIPE_DAILY_LOSS_USD", 20)
MIN_AGE_MIN = E("SNIPE_MIN_AGE_MIN", 3)
MAX_AGE_MIN = E("SNIPE_MAX_AGE_MIN", 60)
MIN_MCAP = E("SNIPE_MIN_MCAP", 8_000)
MAX_MCAP = E("SNIPE_MAX_MCAP", 150_000)
MIN_VOL_5M = E("SNIPE_MIN_VOL_5M", 3_000)
MIN_BUY_RATIO = E("SNIPE_MIN_BUY_RATIO", 1.5)
MIN_UP_5M = E("SNIPE_MIN_UP_5M", 5)
MAX_UP_5M = E("SNIPE_MAX_UP_5M", 40)
STOP = E("SNIPE_STOP", 0.20)
ARM = E("SNIPE_ARM", 0.30)
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
        d["cfg"] = {"entry": w["entry"], "exit": w["exit"]}
        d["enabled"], d["live_ok"] = True, True
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
    global MIN_VOL_5M, MIN_BUY_RATIO, MIN_UP_5M, MAX_UP_5M, STOP, ARM, DROP, MAX_HOLD_MIN
    if cfg and cfg.get("entry") and cfg.get("exit"):
        MIN_VOL_5M, MIN_BUY_RATIO, MIN_UP_5M, MAX_UP_5M = cfg["entry"]
        STOP, ARM, DROP, MAX_HOLD_MIN = cfg["exit"]


def exit_signal(ratio, peak, held_s, stop=None, arm=None, drop=None, max_hold_min=None):
    """('sell' | 'hold', reason). ratio = value now / cost, peak = best ratio so far."""
    stop = STOP if stop is None else stop
    arm = ARM if arm is None else arm
    drop = DROP if drop is None else drop
    max_hold_min = MAX_HOLD_MIN if max_hold_min is None else max_hold_min
    if ratio <= 1 - stop:
        return "sell", "stop_loss"
    if peak >= 1 + arm and ratio <= peak * (1 - drop):
        return "sell", "take_profit"
    if max_hold_min and held_s >= max_hold_min * 60 and peak < 1 + arm:
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


def metrics_reason(mcap, vol5, buys, sells, up5, min_vol=None, min_ratio=None, min_up=None, max_up=None):
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
    if creators:
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
    if DAILY_LOSS > 0 and day_pnl(now) <= -DAILY_LOSS:
        return None
    if len(_open()) >= MAX_OPEN:
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
        ok = (buy or _buy_real)(p, mint, sym, send, chat)
        note(("COMPROU " if ok else "compra NÃO confirmada ") + sym + " · mcap US$ " +
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
    lines = ["🎯 SNIPER 24 h — " + ("🟢 RODANDO" if d.get("enabled") else "⏸️ PAUSADO") + " (💵 DINHEIRO REAL)",
             "⚙️ US$ " + format(USD, "g") + " por compra · máx. " + str(MAX_OPEN) + " abertas · perda máx. do dia US$ " +
             format(DAILY_LOSS, "g") + " (hoje " + format(day_pnl(), "+.2f") + ")",
             "📥 Entra: pump.fun de " + format(MIN_AGE_MIN, "g") + "–" + format(MAX_AGE_MIN, "g") + " min, market cap US$ " +
             format(MIN_MCAP / 1000, "g") + "–" + format(MAX_MCAP / 1000, "g") + " mil, volume 5 min ≥ US$ " +
             format(MIN_VOL_5M, ",.0f") + ", compras/vendas ≥ " + format(MIN_BUY_RATIO, "g") + ", subiu " +
             format(MIN_UP_5M, "g") + "–" + format(MAX_UP_5M, "g") + "% em 5 min, anti-golpe",
             "📤 Sai: stop −" + format(STOP * 100, ".0f") + "% · de +" + format(ARM * 100, ".0f") + "% stop móvel " +
             format(DROP * 100, ".0f") + "% abaixo do topo · " + format(MAX_HOLD_MIN, "g") + " min sem decolar",
             "📊 " + str(len(h)) + " operações · " + str(len(wins)) + " no lucro · total " + format(total, "+.2f") +
             " USD · abertas " + str(len(_open())) + "/" + str(MAX_OPEN),
             "👀 Vigiando " + str(len(d.get("watch") or {})) + " lançamentos da última hora (direto da blockchain)"]
    for x in (d.get("log") or [])[-5:]:
        lines.append("• " + x)
    return "\n".join(lines)


try:                                   # the owner's applied backtest winner survives restarts
    _use_cfg((_load() or {}).get("cfg"))
except Exception:
    pass
