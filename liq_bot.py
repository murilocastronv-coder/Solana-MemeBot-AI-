"""LIQUIDATION robot - Kamino markets (main + the small isolated ones), REAL money (owner, Nov 16).

How it works:
1. Markets: Kamino's MAIN market + the small markets where the backtest found liquidations (liq_bt_cache).
2. Scan (one market per pass, each every SCAN_S): the borrowers with debt are read in two steps - first only the 64
   bytes of their debt/limit numbers (cheap, works for the big main market too), then the full account of those
   above PRE_LTV. Their LTV is recomputed with live prices; those at >= WATCH_LTV are watched.
3. Every FAST_S (2 s) the watched borrowers, their reserves and the LIVE ORACLE prices (Kamino's Scope feed, only the
   entries needed) are re-read. At >= TRIGGER_LTV the bot builds Kamino's own liquidation sequence (refresh reserves ->
   refresh obligation -> liquidate v2), SIMULATES it and only sends it when the simulation succeeds.
4. Debt in USDC, USDT or SOL (owner): the capital stays in USDC; for USDT/SOL debt the bot swaps just the needed
   amount on Jupiter first (SOL is wrapped inside the liquidation transaction and unwrapped at its end).
5. Sending: Jito block engine (bundle only: the tip is paid only when the liquidation lands) + the normal RPC.
6. What it receives (collateral + Kamino's bonus) is sold back to USDC on Jupiter; leftovers of the swapped debt token too.
Limits: US$ LIQ_USD per liquidation, stops for the day after -LIQ_DAILY_LOSS, one try per obligation per minute,
a minimum to receive (99.5% of the repaid value). Honest: professional liquidators stream the chain (Geyser) and win
most races; this bot wins when they are slow or absent.
"""
import base64
import logging
import os
import struct
import threading
import time

import kamino as k
import signals as sg

E = sg._f
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
SOL = "So11111111111111111111111111111111111111112"
DEBTS = (USDC, USDT, SOL)                              # preferred debts (owner later: ANY token, bought on Jupiter)
LIQ_USD = E("LIQ_USD", 50)
DAILY_LOSS = E("LIQ_DAILY_LOSS_USD", 10)
SCAN_S = E("LIQ_SCAN_S", 120)
FAST_S = E("LIQ_FAST_S", 2)
PRE_LTV = E("LIQ_PRE_LTV", 0.75)                     # step 1 of the scan (numbers of the last refresh)
WATCH_LTV = E("LIQ_WATCH_LTV", 0.97)                 # main market: thousands of leveraged loops sit at 85-95%
MAX_WATCH = int(E("LIQ_MAX_WATCH", 300))               # closest ones only (re-read every FAST_S)
# owner (Nov 16): more chances = collateral that MOVES (xStocks, HYPE, ZEC, BTC...) against debt. Positions in an
# elevation group are correlated loops (SOL vs staked SOL, stable vs stable) that sit at 99.8% for days; they get
# only a part of the watch list, and volatile ones are watched from further away (a fast drop crosses 10% in minutes).
VOLATILE_WATCH_LTV = E("LIQ_VOLATILE_WATCH_LTV", 0.90)
VOLATILE_SLOTS = int(E("LIQ_VOLATILE_SLOTS", 200))


def volatile(ob):
    return ob.get("elevation", 0) == 0


def watch_from(ob):
    return VOLATILE_WATCH_LTV if volatile(ob) else WATCH_LTV
TRIGGER_LTV = E("LIQ_TRIGGER_LTV", 0.995)
MAX_MARKETS = int(E("LIQ_MAX_MARKETS", 60))              # owner: all Kamino markets (~40)
CU_LIMIT = int(E("LIQ_CU_LIMIT", 1_000_000))
CU_PRICE = int(E("LIQ_CU_PRICE_MICROLAMPORTS", 50_000))
JITO_TIP = int(E("LIQ_JITO_TIP_LAMPORTS", 20_000))
JITO_URL = os.getenv("LIQ_JITO_URL", "https://mainnet.block-engine.jito.wtf/api/v1")
MIN_RECEIVE = E("LIQ_MIN_RECEIVE", 0.995)            # receive at least 99.5% of the repaid value (bonus comes on top)
MIN_BONUS_SWAP = E("LIQ_MIN_BONUS_FOR_SWAP", 0.02)   # USDT/SOL debt (needs swaps) only when the bonus is >= 2%
SOL_RESERVE = E("LIQ_SOL_RESERVE", 0.03)              # SOL kept for fees
KAMINO_API = "https://api.kamino.finance/v2/kamino-market"
KEY = "liq_bot"
_LOCK = threading.RLock()
_MEM = {}
_RES = {}        # market -> {"at": t, "reserves": {address: reserve}}
_WATCH = {}      # obligation address -> decoded obligation
_TRIED = {}
_TIPS = {"at": 0.0, "list": []}
_CAL = {}        # obligation -> Kamino exact LTV / our estimate, from the last exact check (learned per borrower)


# ---------------------------------------------------------------- state
def _load():
    with _LOCK:
        if not _MEM:
            d = {"enabled": False, "real": False, "log": [], "stats": {}, "trades": [], "markets": []}
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
            pg_store.put(KEY, dict(_MEM))
    except Exception as exc:
        logging.warning("liq: save failed (%s)", type(exc).__name__)


def note(text):
    logging.warning("liq %s", text)
    with _LOCK:
        d = _load()
        d["log"] = (d.get("log") or [])[-49:] + [time.strftime("%d/%m %H:%M", time.gmtime()) + " " + text]


def stat(name, n=1):
    with _LOCK:
        s = _load().setdefault("stats", {})
        s[name] = s.get(name, 0) + n


def set_enabled(on, real=None):
    with _LOCK:
        d = _load()
        d["enabled"] = bool(on)
        if real is not None:
            d["real"] = bool(real)
        _save()


def enabled():
    return bool(_load().get("enabled"))


def real():
    return bool(_load().get("real"))


def day_pnl(now=None):
    day = time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))
    return sum(float(t.get("pnl") or 0) for t in _load().get("trades") or [] if t.get("day") == day)


# ---------------------------------------------------------------- markets / scan
def markets(fetch=None):
    """[(name, address)] - owner (Nov 16): ALL Kamino markets. Main market first, then the ones where the backtest
    found liquidations (most first), then every other market Kamino lists."""
    import snipe_backtest as sbt
    main, rest = [], []
    try:
        for m in (fetch or sg._get)(KAMINO_API) or []:
            a = m.get("lendingMarket")
            if a:
                (main if m.get("isPrimary") else rest).append((str(m.get("name") or a[:6]), a))
    except Exception as exc:
        logging.warning("liq markets api: %s", type(exc).__name__)
    bt = sbt._get("liq_bt_cache", {})
    names = {a: n for p, n, a in bt.get("markets") or [] if p == "Kamino"}
    ranked = [(names[a], a) for _, a in sorted(((len(v), a) for a, v in (bt.get("liqs") or {}).items() if a in names), reverse=True)]
    out, have = [], set()
    for n, a in main + ranked + rest + list((n, a) for a, n in names.items()):
        if a not in have:
            have.add(a)
            out.append((n, a))
    return out[:MAX_MARKETS]


def _gpa(rpc, size, market, extra=None, data_slice=None):
    flt = [{"dataSize": size}, {"memcmp": {"offset": 32, "bytes": market}}] + (extra or [])
    opts = {"encoding": "base64", "filters": flt}
    if data_slice:
        opts["dataSlice"] = data_slice
    rows = rpc("getProgramAccounts", [k.KLEND, opts]) or []
    return [(r["pubkey"], base64.b64decode(r["account"]["data"][0])) for r in rows]


def _multi(rpc, keys, data_slice=None):
    out = {}
    for i in range(0, len(keys), 100):
        opts = {"encoding": "base64"}
        if data_slice:
            opts["dataSlice"] = data_slice
        got = (rpc("getMultipleAccounts", [keys[i:i + 100], opts]) or {}).get("value") or []
        for a, acc in zip(keys[i:i + 100], got):
            if acc:
                out[a] = base64.b64decode(acc["data"][0])
    return out


def reserves_of(rpc, market, now=None, max_age=600):
    now = now or time.time()
    c = _RES.get(market)
    if c and now - c["at"] < max_age:
        return c["reserves"]
    res = {}
    for a, data in _gpa(rpc, k.RESERVE_SIZE, market):
        r = k.reserve(a, data)
        if r:
            res[a] = r
    _RES[market] = {"at": now, "reserves": res}
    return res


def live_prices(rpc, reserves):
    """Reserves with the LIVE Scope oracle price (only the needed entries are read). Falls back to the reserve price."""
    need = {}
    for r in reserves.values():
        idx = k.scope_entries(r.get("chain") or [0, 0, 0, 0])
        if idx and k._real(r["scope"]):
            need.setdefault(r["scope"], set()).update(idx)
    prices = {}
    for feed, idx in need.items():
        idx = sorted(idx)
        groups, cur = [], [idx[0]]
        for i in idx[1:]:
            if i - cur[0] < 64:
                cur.append(i)
            else:
                groups.append(cur)
                cur = [i]
        groups.append(cur)
        for g in groups:
            off = k.SCOPE_HEAD + g[0] * k.SCOPE_ENTRY
            got = _multi(rpc, [feed], {"offset": off, "length": (g[-1] - g[0] + 1) * k.SCOPE_ENTRY}).get(feed)
            if got:
                prices.setdefault(feed, {}).update(k.scope_slice(got, g[0]))
    out = {}
    for a, r in reserves.items():
        px = k.scope_price(prices.get(r["scope"], {}), r.get("chain") or [0, 0, 0, 0]) if k._real(r["scope"]) else None
        out[a] = dict(r, price=px, price_ref=r["price"]) if px else r
    return out


def scan_market(rpc, market, now=None):
    """Borrowers of the market at >= WATCH_LTV go to the watch list. Returns (borrowers, watched)."""
    res = live_prices(rpc, reserves_of(rpc, market, now, max_age=0))
    head = _gpa(rpc, k.OBLIGATION_SIZE, market, [{"memcmp": {"offset": 2287, "bytes": "2"}}],   # has_debt == 1
                {"offset": 2208, "length": 64})
    close = []
    for a, d in head:
        if len(d) < 64:
            continue
        bf = k._u128(d, 0) / k.SF
        unhealthy = k._u128(d, 48) / k.SF
        if unhealthy > 0 and bf / unhealthy >= PRE_LTV:
            close.append(a)
    full = _multi(rpc, close, {"offset": 0, "length": k.OBLIGATION_HEAD})
    w = 0
    for a in close:
        ob = k.obligation(a, full.get(a, b""))
        if not ob or not ob["has_debt"]:
            continue
        ltv = k.ltv_now(ob, res)
        if ltv is not None:
            ltv *= _CAL.get(a, 1.0)
        if ltv is not None and ltv >= watch_from(ob):
            ob["ltv"] = ltv
            _WATCH[a] = ob
            w += 1
    _trim_watch()
    return len(head), w


def _trim_watch():
    if len(_WATCH) > MAX_WATCH:
        items = sorted(_WATCH.items(), key=lambda kv: -kv[1].get("ltv", 0))
        vol = [kv for kv in items if volatile(kv[1])][:VOLATILE_SLOTS]
        cor = [kv for kv in items if not volatile(kv[1])][:max(0, MAX_WATCH - len(vol))]
        _WATCH.clear()
        _WATCH.update(vol + cor)


def refresh_watch(rpc, now=None):
    """Re-read the watched obligations, their reserves and the live oracle. Returns [(ob, reserves, ltv)] to liquidate."""
    if not _WATCH:
        return []
    obs = list(_WATCH)
    res_keys = sorted({x["reserve"] for ob in _WATCH.values() for x in ob["deposits"] + ob["borrows"]})
    accs = _multi(rpc, obs + res_keys)
    reserves = {}
    for a in res_keys:
        r = k.reserve(a, accs.get(a, b""))
        if r:
            reserves[a] = r
    reserves = live_prices(rpc, reserves)
    out = []
    for a in obs:
        ob = k.obligation(a, accs.get(a, b""))
        if not ob or not ob["has_debt"]:
            _WATCH.pop(a, None)
            continue
        ltv = k.ltv_now(ob, reserves)
        if ltv is not None:
            ltv *= _CAL.get(a, 1.0)
        if ltv is None or ltv < watch_from(ob) * 0.97:
            _WATCH.pop(a, None)
            continue
        ob["ltv"] = ltv
        _WATCH[a] = ob
        if ltv >= TRIGGER_LTV:
            out.append((ob, reserves, ltv))
    return out


# ---------------------------------------------------------------- wallet
def token_balance(rpc, owner, mint, token_program=k.TOKEN):
    try:
        v = (rpc("getTokenAccountBalance", [k.ata(owner, mint, token_program)]) or {}).get("value") or {}
        return int(v.get("amount") or 0)
    except Exception:
        return 0


def sol_lamports(rpc, owner):
    return int((rpc("getBalance", [owner]) or {}).get("value") or 0)


def _jupiter_swap(in_mint, out_mint, amount_raw, owner):
    import jupiter_v2_executor as jx
    q = jx.order(in_mint, out_mint, int(amount_raw), taker=owner)
    signed = jx.sign_order(q, os.getenv("BOT_WALLET_ENCRYPTION_PASSPHRASE", ""))
    return jx.execute_signed(signed, q["requestId"])


def top_up(rpc, owner, send=None, chat=None):
    """Keep about LIQ_USD of USDC in the wallet (bought with SOL, keeping SOL_RESERVE for fees)."""
    usdc = token_balance(rpc, owner, USDC) / 1e6
    if usdc >= LIQ_USD * 0.9:
        return usdc
    need = LIQ_USD - usdc
    import copy_trader
    px = float(copy_trader._sol_usd() or 0)
    sol = sol_lamports(rpc, owner) / 1e9
    if px <= 0 or sol - SOL_RESERVE < need / px:
        note("saldo baixo: USDC " + format(usdc, ".2f") + " e SOL " + format(sol, ".4f") + " (precisa ~US$ " +
             format(LIQ_USD, "g") + " + " + format(SOL_RESERVE, "g") + " SOL para taxas)")
        return usdc
    r = _jupiter_swap(SOL, USDC, int(need / px * 1e9), owner)
    note("troquei SOL por US$ " + format(r["total_output_amount"] / 1e6, ".2f") + " de USDC (capital de liquidação)")
    if send and chat:
        send(chat, "💧 Capital de liquidação: troquei SOL por US$ " + format(r["total_output_amount"] / 1e6, ".2f") + " de USDC.")
    return token_balance(rpc, owner, USDC) / 1e6


# ---------------------------------------------------------------- one liquidation
def plan(ob, reserves, usdc_raw):
    """(dict with repay, withdraw, amount, min_receive, usd) or (None, why). Prefers USDC debt (no swap)."""
    debts = [(b, reserves.get(b["reserve"])) for b in ob["borrows"]]
    debts = [(b, r) for b, r in debts if r and r["price"] > 0]
    if not debts:
        return None, "dívida sem preço"
    b, repay = max(debts, key=lambda x: (x[1]["mint"] == USDC, x[1]["mint"] in DEBTS, x[0]["value"]))
    cols = [(d, reserves.get(d["reserve"])) for d in ob["deposits"]]
    cols = [(d, r) for d, r in cols if r and r["price"] > 0
            and (ob["elevation"] > 0 or r.get("threshold_pct", 1) > 0)]   # threshold 0 = Kamino refuses (error 6087)
    if not cols:
        return None, "sem garantia com preço"
    d, withdraw = max(cols, key=lambda x: x[0]["value"])
    cap_usd = min(usdc_raw / 1e6, LIQ_USD)
    if repay["mint"] != USDC:
        if withdraw["min_bonus_bps"] / 1e4 < MIN_BONUS_SWAP:
            return None, "bônus " + format(withdraw["min_bonus_bps"] / 100, ".1f") + "% pequeno para trocar moeda"
        cap_usd *= 0.98                                   # room for the swap cost
    amount = int(min(cap_usd / repay["price"] * 10 ** repay["decimals"], b["amount"]))
    usd = amount / 10 ** repay["decimals"] * repay["price"]
    if usd < 1:
        return None, "sem USDC suficiente"
    min_receive = int(usd / withdraw["price"] * 10 ** withdraw["decimals"] * MIN_RECEIVE)
    return {"repay": repay, "withdraw": withdraw, "amount": amount, "min_receive": min_receive, "usd": usd}, ""


def acquire(rpc, me, p):
    """USDT/SOL debt: buy the amount on Jupiter with USDC. Returns the amount really available (base units)."""
    mint = p["repay"]["mint"]
    if mint == USDC:
        return p["amount"]
    tp = p["repay"]["token_program"]
    have = sol_lamports(rpc, me) - int(SOL_RESERVE * 1e9) if mint == SOL else token_balance(rpc, me, mint, tp)
    if have >= p["amount"]:
        return p["amount"]
    usdc_in = int(p["usd"] * 1.004 * 1e6)
    if mint not in DEBTS:                                   # any other token: check the round trip on Jupiter first
        import jupiter_v2_executor as jx
        out = int(jx.order(USDC, mint, usdc_in)["outAmount"])
        back = int(jx.order(mint, USDC, out)["outAmount"])
        loss = 1 - back / usdc_in
        if loss > p["withdraw"]["min_bonus_bps"] / 1e4 * 0.5:
            note("dívida em " + p["repay"]["name"] + ": ida e volta na Jupiter perde " + format(loss * 100, ".1f") +
                 "% (bônus " + format(p["withdraw"]["min_bonus_bps"] / 100, ".1f") + "%) — não compensa")
            return 0
    r = _jupiter_swap(USDC, mint, usdc_in, me)
    note("troquei US$ " + format(r["total_input_amount"] / 1e6, ".2f") + " USDC por " + p["repay"]["name"] + " para pagar a dívida")
    have = sol_lamports(rpc, me) - int(SOL_RESERVE * 1e9) if mint == SOL else token_balance(rpc, me, mint, tp)
    return max(0, min(p["amount"], have))


def _ix_transfer(src, dst, lamports):
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey
    return Instruction(Pubkey.from_string(k.SYSTEM), struct.pack("<IQ", 2, int(lamports)),
                       [AccountMeta(Pubkey.from_string(src), True, True), AccountMeta(Pubkey.from_string(dst), False, True)])


def _ix_token(data, metas):
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey
    return Instruction(Pubkey.from_string(k.TOKEN), bytes(data),
                       [AccountMeta(Pubkey.from_string(a), s, w) for a, s, w in metas])


def tip_account():
    if time.time() - _TIPS["at"] > 3600 or not _TIPS["list"]:
        try:
            import requests
            r = requests.post(JITO_URL + "/bundles", json={"jsonrpc": "2.0", "id": 1, "method": "getTipAccounts", "params": []}, timeout=8)
            _TIPS["list"] = list(r.json().get("result") or [])
        except Exception:
            _TIPS["list"] = _TIPS["list"] or []
        _TIPS["at"] = time.time()
    return _TIPS["list"][int(time.time()) % len(_TIPS["list"])] if _TIPS["list"] else None


def build_tx(rpc, signer_pub, ob, reserves, p, blockhash=None, tip=True):
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    me = str(signer_pub)
    repay, withdraw = p["repay"], p["withdraw"]
    ixs = [set_compute_unit_limit(CU_LIMIT), set_compute_unit_price(CU_PRICE)]
    wsol = k.ata(me, SOL)
    if repay["mint"] == SOL:                               # wrap the SOL to repay inside the same transaction
        ixs += [k.ix_create_ata(me, me, SOL, k.TOKEN), _ix_transfer(me, wsol, p["amount"]), _ix_token([17], [(wsol, False, True)])]
    need = [(withdraw["c_mint"], k.TOKEN), (withdraw["mint"], withdraw["token_program"])]
    have = _multi(rpc, [k.ata(me, m, pr) for m, pr in need], {"offset": 0, "length": 0})
    for m, pr in need:
        if k.ata(me, m, pr) not in have and not (m == SOL and repay["mint"] == SOL):
            ixs.append(k.ix_create_ata(me, me, m, pr))
    ixs += k.liquidation_ixs(me, ob, reserves, repay, withdraw, p["amount"], p["min_receive"])
    if SOL in (repay["mint"], withdraw["mint"]):           # unwrap: WSOL left/received goes back to native SOL
        ixs.append(_ix_token([9], [(wsol, False, True), (me, False, True), (me, True, False)]))
    t = tip_account() if tip and JITO_TIP > 0 else None
    if t:
        ixs.append(_ix_transfer(me, t, JITO_TIP))
    bh = blockhash or ((rpc("getLatestBlockhash", [{"commitment": "confirmed"}]) or {}).get("value") or {}).get("blockhash")
    msg = MessageV0.try_compile(Pubkey.from_string(me), ixs, [], Hash.from_string(bh))
    if t and len(_raw(msg)) > 1232:                        # too big with the tip: send without Jito
        return build_tx(rpc, signer_pub, ob, reserves, p, bh, tip=False)
    return msg


def _raw(msg):
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction
    return bytes(VersionedTransaction.populate(msg, [Signature.default()] * msg.header.num_required_signatures))


def simulate(rpc, msg):
    """(ok, short reason) - the RPC runs the transaction without sending it."""
    raw = _raw(msg)
    if len(raw) > 1232:
        return False, "transação grande demais (" + str(len(raw)) + " bytes)"
    r = rpc("simulateTransaction", [base64.b64encode(raw).decode(), {"encoding": "base64", "sigVerify": False,
                                                                       "replaceRecentBlockhash": True, "commitment": "processed"}]) or {}
    v = r.get("value") or {}
    if v.get("err") is None:
        return True, ""
    logs = [l for l in v.get("logs") or [] if "rror" in l or "failed" in l]
    return False, (str(v.get("err")) + " " + (logs[-1] if logs else ""))[:180]


def send_tx(rpc, msg, signer, wait_s=40):
    from solders.transaction import VersionedTransaction
    tx = VersionedTransaction(msg, [signer])
    b64 = base64.b64encode(bytes(tx)).decode()
    try:                                                    # Jito: lands only if it succeeds (tip paid only then)
        import requests
        requests.post(JITO_URL + "/transactions?bundleOnly=true", json={"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                                                                       "params": [b64, {"encoding": "base64"}]}, timeout=8)
    except Exception as exc:
        logging.info("liq jito: %s", type(exc).__name__)
    sig = rpc("sendTransaction", [b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}])
    end = time.time() + wait_s
    while time.time() < end:
        time.sleep(2)
        st = ((rpc("getSignatureStatuses", [[sig]]) or {}).get("value") or [None])[0]
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            return sig, st.get("err") is None
    return sig, False


def _back_to_usdc(rpc, me, mint, token_program, decimals, price, keep_lamports=0):
    """Sell what is left of a token (SOL above the reserve) back to USDC. Returns the USDC received."""
    if mint == USDC:
        return 0.0
    amt = (sol_lamports(rpc, me) - keep_lamports) if mint == SOL else token_balance(rpc, me, mint, token_program)
    if amt <= 0 or amt / 10 ** decimals * price < 1:
        return 0.0
    try:
        return _jupiter_swap(mint, USDC, amt, me)["total_output_amount"] / 1e6
    except Exception as exc:
        note("venda de volta para USDC falhou (" + type(exc).__name__ + "); fica na carteira")
        return 0.0


def exact_ltv(rpc, payer, ob, reserves):
    """Kamino's OWN health number: simulate refresh reserves + refresh obligation and read the refreshed obligation
    back from the simulation (no tokens needed). (ltv or None, reason)."""
    from solders.compute_budget import set_compute_unit_limit
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    involved = list(dict.fromkeys([d["reserve"] for d in ob["deposits"]] + [b["reserve"] for b in ob["borrows"]]))
    if any(a not in reserves for a in involved):
        return None, "reserva desconhecida"
    ixs = [set_compute_unit_limit(1_000_000)] + [k.ix_refresh_reserve(reserves[a]) for a in involved] + [k.ix_refresh_obligation(ob, reserves)]
    msg = MessageV0.try_compile(Pubkey.from_string(str(payer)), ixs, [], Hash.default())
    r = rpc("simulateTransaction", [base64.b64encode(_raw(msg)).decode(), {
        "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "processed",
        "accounts": {"encoding": "base64", "addresses": [ob["address"]]}}]) or {}
    v = r.get("value") or {}
    if v.get("err") is not None:
        logs = [l for l in v.get("logs") or [] if "rror" in l]
        return None, (str(v.get("err")) + " " + (logs[-1] if logs else ""))[:150]
    acc = (v.get("accounts") or [None])[0]
    o = k.obligation(ob["address"], base64.b64decode(acc["data"][0])) if acc else None
    if not o or o["unhealthy"] <= 0:
        return None, "sem dados do empréstimo"
    return o["bf_debt"] / o["unhealthy"], ""


_NOTED = {}


def _note_once(key, text, now, every=3600):
    if now - _NOTED.get(key, 0) >= every:
        _NOTED[key] = now
        note(text)


def attempt(rpc, ob, reserves, ltv, send=None, chat=None, signer=None, now=None):
    now = now or time.time()
    if now < _TRIED.get(ob["address"], 0):
        return None
    _TRIED[ob["address"]] = now + 60
    if ob["deposited"] < 1:                                  # dust / bad debt: nothing worth receiving
        _TRIED[ob["address"]] = now + 3600
        return None
    signer = signer or __import__("rent_reclaim").bot_signer()
    me = str(signer.pubkey())
    try:                                                      # Kamino's own number before anything else
        exact, why = exact_ltv(rpc, signer.pubkey(), ob, reserves)
    except Exception as exc:
        exact, why = None, type(exc).__name__
    if exact is not None and ltv > 0:                          # learn this borrower's gap: next estimates use it
        base = ltv / _CAL.get(ob["address"], 1.0)
        _CAL[ob["address"]] = exact / base if base > 0 else 1.0
        logging.info("liq ltv estimate %.3f vs kamino %.3f (%s)", ltv, exact, ob["address"][:6])
    if exact is None or exact < 1.0:
        stat("saudável na conta do Kamino")
        _TRIED[ob["address"]] = now + (120 if exact is not None and exact > 0.97 else 600)
        if exact is not None and exact < watch_from(ob):
            _WATCH.pop(ob["address"], None)
        if exact is None:
            _note_once("exact" + ob["address"], "não consegui a conta exata do Kamino para " + ob["address"][:6] + ": " + why, now)
        return None
    usdc_before = token_balance(rpc, me, USDC)
    p, why = plan(ob, reserves, usdc_before)
    if not p:
        stat("puladas")
        _note_once(ob["address"] + why, "liquidável (Kamino: LTV " + format(min(exact, 9.99) * 100, ".0f") + "%) mas pulei: " + why +
                   " · " + ob["address"][:6], now)
        return None
    stat("oportunidades")
    repay, withdraw = p["repay"], p["withdraw"]
    if not real():
        if repay["mint"] == USDC:
            ok, why = simulate(rpc, build_tx(rpc, signer.pubkey(), ob, reserves, p))
            stat("simulação ok" if ok else "simulação falhou")
            note(("SIMULAÇÃO OK (observando): pagaria US$ " + format(p["usd"], ".2f") + " e receberia " + withdraw["name"]) if ok
                 else "simulação recusou " + ob["address"][:6] + " (" + withdraw["name"] + "): " + why)
        else:
            note("observando: liquidável com dívida em " + repay["name"] + " (no real eu trocaria USDC antes) · " + ob["address"][:6])
        return None
    if DAILY_LOSS > 0 and day_pnl(now) <= -DAILY_LOSS:
        note("limite de perda do dia atingido: não envio")
        return None
    sol_before = sol_lamports(rpc, me)
    if repay["mint"] != USDC:
        try:
            p["amount"] = acquire(rpc, me, p)
        except Exception as exc:
            note("não consegui trocar USDC por " + repay["name"] + ": " + type(exc).__name__)
            return None
        if p["amount"] <= 0:
            return None
        p["usd"] = p["amount"] / 10 ** repay["decimals"] * repay["price"]
        p["min_receive"] = int(p["usd"] / withdraw["price"] * 10 ** withdraw["decimals"] * MIN_RECEIVE)
    msg = build_tx(rpc, signer.pubkey(), ob, reserves, p)
    ok, why = simulate(rpc, msg)
    if not ok:
        stat("simulação falhou")
        note("simulação recusou " + ob["address"][:6] + " (" + withdraw["name"] + "): " + why)
        _back_to_usdc(rpc, me, repay["mint"], repay["token_program"], repay["decimals"], repay["price"], sol_before)
        return None
    stat("simulação ok")
    sig, done = send_tx(rpc, msg, signer)
    if not done:
        stat("perdeu a disputa")
        note("enviei mas não confirmou (outro bot foi mais rápido ou expirou) · " + str(sig)[:10])
        _back_to_usdc(rpc, me, repay["mint"], repay["token_program"], repay["decimals"], repay["price"], sol_before)
        return None
    stat("liquidou")
    _back_to_usdc(rpc, me, withdraw["mint"], withdraw["token_program"], withdraw["decimals"], withdraw["price"],
                  int(SOL_RESERVE * 1e9) if withdraw["mint"] == SOL else 0)
    if repay["mint"] != withdraw["mint"]:
        _back_to_usdc(rpc, me, repay["mint"], repay["token_program"], repay["decimals"], repay["price"],
                      int(SOL_RESERVE * 1e9) if repay["mint"] == SOL else 0)
    usdc_after = token_balance(rpc, me, USDC)
    sol_px = float(__import__("copy_trader")._sol_usd() or 0)
    fees = max(0, sol_before - sol_lamports(rpc, me)) / 1e9 * sol_px if SOL not in (repay["mint"], withdraw["mint"]) else 0.0
    pnl = (usdc_after - usdc_before) / 1e6 - fees
    with _LOCK:
        d = _load()
        d["trades"] = (d.get("trades") or [])[-199:] + [{"t": now, "day": time.strftime("%Y-%m-%d", time.gmtime(now)),
                                                         "market": ob["market"], "paid": p["usd"], "pnl": pnl, "sig": sig}]
    _save()
    txt = ("💧 LIQUIDOU (dinheiro real): pagou US$ " + format(p["usd"], ".2f") + " de dívida em " + repay["name"] + ", recebeu " +
           withdraw["name"] + " com bônus e vendeu tudo de volta para USDC → " + format(pnl, "+.2f") + " USD\nhttps://solscan.io/tx/" + str(sig))
    note(txt.split("\n")[0])
    if send and chat:
        send(chat, txt)
    return sig


# ---------------------------------------------------------------- loop
def cycle(rpc, send=None, chat=None, now=None, state=None):
    """One pass: scan one market that is due, check the watched borrowers, liquidate the ones that can be."""
    now = now or time.time()
    st = state if state is not None else _STATE
    if now - st.get("markets_at", 0) > 3600 or not st.get("markets"):
        st["markets"], st["markets_at"] = markets(), now
        with _LOCK:
            _load()["markets"] = st["markets"]
    for name, m in st["markets"]:
        if now - st.setdefault("scanned", {}).get(m, 0) >= SCAN_S:
            st["scanned"][m] = now
            try:
                n, w = scan_market(rpc, m, now)
                st.setdefault("borrowers", {})[m] = n
            except BaseException as exc:                   # includes a panic of the Solana library
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                logging.warning("liq scan %s: %s: %s", name, type(exc).__name__, str(exc)[:150])
            break                                          # one market per pass: spreads the reads
    for ob, reserves, ltv in refresh_watch(rpc, now):
        try:
            attempt(rpc, ob, reserves, ltv, send, chat, now=now)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            logging.exception("liq attempt failed")
            note("erro ao tentar " + ob["address"][:6] + ": " + type(exc).__name__ + ": " + str(exc)[:120])
    try:                                                   # owner: Save (Solend) too, same capital and limits
        import save_bot
        save_bot.cycle(rpc, send, chat, now)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        logging.warning("save cycle: %s: %s", type(exc).__name__, str(exc)[:150])
    if now - st.get("saved", 0) > 60:
        st["saved"] = now
        _save()


_STATE = {}


def _save_line():
    try:
        import save_bot
        return save_bot.status_line()
    except Exception:
        return "🏦 Save (Solend): iniciando"


def status_text():
    d = _load()
    s = d.get("stats") or {}
    tr = d.get("trades") or []
    L = ["💧 ROBÔ DE LIQUIDAÇÃO (Kamino + Save) — " +
         ("🟢 LIGADO · 💵 DINHEIRO REAL" if d.get("enabled") and d.get("real") else
          "🟡 LIGADO · só observando" if d.get("enabled") else "⏸️ DESLIGADO"),
         "⚙️ Até US$ " + format(LIQ_USD, "g") + " por liquidação · paga dívida em USDC, USDT ou SOL · preço do oráculo a cada " +
         format(FAST_S, "g") + " s · envio Jito + RPC · para no dia com −US$ " + format(DAILY_LOSS, "g") +
         " (hoje " + format(day_pnl(), "+.2f") + ") · só envia se a simulação na rede der certo",
         "🏦 Mercados vigiados: " + str(len(d.get("markets") or _STATE.get("markets") or [])) + " · devedores lidos: " +
         str(sum((_STATE.get("borrowers") or {}).values())) + " · vigiados de perto: " + str(len(_WATCH)) +
         " (garantia que oscila: " + str(sum(1 for o in _WATCH.values() if volatile(o))) + ")",
         _save_line(),
         "🩺 Descartadas pela conta exata do protocolo (ainda saudáveis): " + str(s.get("saudável na conta do Kamino", 0)),
         "📊 Oportunidades reais " + str(s.get("oportunidades", 0)) + " · puladas " + str(s.get("puladas", 0)) + " · simulação ok " +
         str(s.get("simulação ok", 0)) + " · recusadas " + str(s.get("simulação falhou", 0)) + " · perdeu disputa " +
         str(s.get("perdeu a disputa", 0)) + " · LIQUIDOU " + str(s.get("liquidou", 0)),
         "💰 Resultado: " + str(len(tr)) + " liquidações · total " + format(sum(t.get("pnl", 0) for t in tr), "+.2f") + " USD"]
    for x in (d.get("log") or [])[-6:]:
        L.append("• " + x)
    return "\n".join(L)
