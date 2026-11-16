"""Save (ex-Solend) part of the liquidation robot (owner, Nov 16: "coloca todos esses mercados").

Same flow and limits as the Kamino part (liq_bot): scan each Save pool's borrowers (two steps: 48 bytes of numbers,
then the full account of those near the limit), check Save's OWN number by simulating refresh reserves + refresh
obligation and reading the obligation back, and only at >= 100% plan, simulate and send the liquidation.
Capital, daily loss limit, swaps for non-USDC debt and the sale of what is received are shared with liq_bot.
"""
import base64
import logging
import time

import liq_bot as lb
import save as sv
import signals as sg

SAVE_API = "https://api.solend.fi/v1/markets/configs?scope=all&deployment=production"
PRE = sg._f("SAVE_PRE_LTV", 0.85)
WATCH = sg._f("SAVE_WATCH_LTV", 0.95)
EXACT_EVERY_S = sg._f("SAVE_EXACT_EVERY_S", 60)
_WATCH = {}
_RES = {}
_NEXT = {}
_STATE = {"markets": [], "markets_at": 0.0, "scanned": {}, "borrowers": {}}


def markets(fetch=None):
    out = []
    try:
        data = (fetch or sg._get)(SAVE_API) or []
        for m in data if isinstance(data, list) else (data.get("results") or []):
            if m.get("address"):
                out.append((str(m.get("name") or m["address"][:6]), m["address"]))
    except Exception as exc:
        logging.warning("save markets api: %s", type(exc).__name__)
    if not out:
        import snipe_backtest as sbt
        out = [(n, a) for p, n, a in (sbt._get("liq_bt_cache", {}).get("markets") or []) if p == "Save"]
    return out


def reserves_of(rpc, market, now=None, max_age=300):
    now = now or time.time()
    c = _RES.get(market)
    if c and now - c["at"] < max_age:
        return c["res"]
    rows = rpc("getProgramAccounts", [sv.SAVE, {"encoding": "base64", "filters": [
        {"dataSize": sv.RESERVE_SIZE}, {"memcmp": {"offset": 10, "bytes": market}}]}]) or []
    res = {}
    for r in rows:
        x = sv.reserve(r["pubkey"], base64.b64decode(r["account"]["data"][0]))
        if x:
            res[r["pubkey"]] = x
    _RES[market] = {"at": now, "res": res}
    return res


def scan_market(rpc, market, now=None):
    rows = rpc("getProgramAccounts", [sv.SAVE, {"encoding": "base64", "dataSlice": {"offset": 90, "length": 48}, "filters": [
        {"dataSize": sv.OBLIGATION_SIZE}, {"memcmp": {"offset": 10, "bytes": market}}]}]) or []
    close = []
    for r in rows:
        d = base64.b64decode(r["account"]["data"][0])
        if len(d) < 48:
            continue
        debt, unhealthy = sv._u128(d, 0) / sv.WAD, sv._u128(d, 32) / sv.WAD
        if unhealthy > 0 and debt / unhealthy >= PRE and debt >= 1:
            close.append(r["pubkey"])
    full = lb._multi(rpc, close)
    for a in close:
        ob = sv.obligation(a, full.get(a, b""))
        r = sv.ratio(ob) if ob else None
        if r is not None and r >= WATCH:
            ob["ltv"] = r
            _WATCH[a] = ob
    return len(rows)


def exact_ltv(rpc, payer, ob, reserves):
    from solders.compute_budget import set_compute_unit_limit
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    if any(x["reserve"] not in reserves for x in ob["deposits"] + ob["borrows"]):
        return None, "reserva desconhecida"
    msg = MessageV0.try_compile(Pubkey.from_string(str(payer)), [set_compute_unit_limit(1_000_000)] + sv.refresh_ixs(ob, reserves),
                                [], Hash.default())
    r = rpc("simulateTransaction", [base64.b64encode(lb._raw(msg)).decode(), {
        "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "processed",
        "accounts": {"encoding": "base64", "addresses": [ob["address"]]}}]) or {}
    v = r.get("value") or {}
    if v.get("err") is not None:
        return None, str(v.get("err"))[:120]
    acc = (v.get("accounts") or [None])[0]
    o = sv.obligation(ob["address"], base64.b64decode(acc["data"][0])) if acc else None
    return (sv.ratio(o), "") if o else (None, "sem dados")


def plan(ob, reserves, usdc_raw):
    debts = [(b, reserves.get(b["reserve"])) for b in ob["borrows"]]
    debts = [(b, r) for b, r in debts if r and r["price"] > 0]
    if not debts:
        return None, "dívida sem preço"
    b, repay = max(debts, key=lambda x: (x[1]["mint"] == lb.USDC, x[1]["mint"] in lb.DEBTS, x[0]["value"]))
    cols = [(d, reserves.get(d["reserve"])) for d in ob["deposits"]]
    cols = [(d, r) for d, r in cols if r and r["price"] > 0 and r["threshold_pct"] > 0]
    if not cols:
        return None, "sem garantia liquidável"
    d, withdraw = max(cols, key=lambda x: x[0]["value"])
    cap = min(usdc_raw / 1e6, lb.LIQ_USD) * (1.0 if repay["mint"] == lb.USDC else 0.98)
    if repay["mint"] != lb.USDC and withdraw["bonus_pct"] / 100 < lb.MIN_BONUS_SWAP:
        return None, "bônus " + str(withdraw["bonus_pct"]) + "% pequeno para trocar moeda"
    amount = int(min(cap / repay["price"] * 10 ** repay["decimals"], b["amount"]))
    usd = amount / 10 ** repay["decimals"] * repay["price"]
    if usd < 1:
        return None, "sem USDC suficiente"
    repay = dict(repay, name=repay["name"] or repay["mint"][:4])
    withdraw = dict(withdraw, name=withdraw["name"] or withdraw["mint"][:4])
    return {"repay": repay, "withdraw": withdraw, "amount": amount, "usd": usd}, ""


def build_tx(rpc, me, ob, reserves, p, tip=True):
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    import kamino as k
    repay, withdraw = p["repay"], p["withdraw"]
    ixs = [set_compute_unit_limit(lb.CU_LIMIT), set_compute_unit_price(lb.CU_PRICE)]
    wsol = k.ata(me, lb.SOL)
    if repay["mint"] == lb.SOL:
        ixs += [k.ix_create_ata(me, me, lb.SOL, k.TOKEN), lb._ix_transfer(me, wsol, p["amount"]), lb._ix_token([17], [(wsol, False, True)])]
    for m in (withdraw["c_mint"], withdraw["mint"]):
        if not (m == lb.SOL and repay["mint"] == lb.SOL):
            ixs.append(k.ix_create_ata(me, me, m, k.TOKEN))
    ixs += sv.refresh_ixs(ob, reserves)
    ixs.append(sv.ix_liquidate(me, ob, repay, withdraw, p["amount"], k.ata))
    if lb.SOL in (repay["mint"], withdraw["mint"]):
        ixs.append(lb._ix_token([9], [(wsol, False, True), (me, False, True), (me, True, False)]))
    t = lb.tip_account() if tip and lb.JITO_TIP > 0 else None
    if t:
        ixs.append(lb._ix_transfer(me, t, lb.JITO_TIP))
    bh = ((rpc("getLatestBlockhash", [{"commitment": "confirmed"}]) or {}).get("value") or {}).get("blockhash")
    msg = MessageV0.try_compile(Pubkey.from_string(me), ixs, [], Hash.from_string(bh))
    if t and len(lb._raw(msg)) > 1232:
        return build_tx(rpc, me, ob, reserves, p, tip=False)
    return msg


def attempt(rpc, ob, reserves, send=None, chat=None, signer=None, now=None):
    now = now or time.time()
    if now < _NEXT.get(ob["address"], 0):
        return None
    _NEXT[ob["address"]] = now + EXACT_EVERY_S
    signer = signer or __import__("rent_reclaim").bot_signer()
    me = str(signer.pubkey())
    try:
        exact, why = exact_ltv(rpc, signer.pubkey(), ob, reserves)
    except Exception as exc:
        exact, why = None, type(exc).__name__
    if exact is None or exact < 1.0:
        lb.stat("saudável na conta do Kamino")
        if exact is not None and exact < WATCH:
            _WATCH.pop(ob["address"], None)
        return None
    usdc_before = lb.token_balance(rpc, me, lb.USDC)
    p, why = plan(ob, reserves, usdc_before)
    if not p:
        lb.stat("puladas")
        lb._note_once("save" + ob["address"] + why, "Save: liquidável (" + format(min(exact, 9.99) * 100, ".0f") + "%) mas pulei: " +
                      why + " · " + ob["address"][:6], now)
        return None
    lb.stat("oportunidades")
    repay, withdraw = p["repay"], p["withdraw"]
    if not lb.real():
        lb.note("Save observando: liquidável (" + format(exact * 100, ".0f") + "%), pagaria US$ " + format(p["usd"], ".2f"))
        return None
    if lb.DAILY_LOSS > 0 and lb.day_pnl(now) <= -lb.DAILY_LOSS:
        return None
    sol_before = lb.sol_lamports(rpc, me)
    if repay["mint"] != lb.USDC:
        try:
            p["amount"] = lb.acquire(rpc, me, p)
        except Exception as exc:
            lb.note("Save: não consegui trocar USDC por " + repay["name"] + ": " + type(exc).__name__)
            return None
        if p["amount"] <= 0:
            return None
    msg = build_tx(rpc, me, ob, reserves, p)
    ok, why = lb.simulate(rpc, msg)
    if not ok:
        lb.stat("simulação falhou")
        lb.note("Save: simulação recusou " + ob["address"][:6] + ": " + why)
        lb._back_to_usdc(rpc, me, repay["mint"], repay["token_program"], repay["decimals"], repay["price"], sol_before)
        return None
    lb.stat("simulação ok")
    sig, done = lb.send_tx(rpc, msg, signer)
    if not done:
        lb.stat("perdeu a disputa")
        lb.note("Save: enviei mas não confirmou (outro bot foi mais rápido) · " + str(sig)[:10])
        lb._back_to_usdc(rpc, me, repay["mint"], repay["token_program"], repay["decimals"], repay["price"], sol_before)
        return None
    lb.stat("liquidou")
    lb._back_to_usdc(rpc, me, withdraw["mint"], withdraw["token_program"], withdraw["decimals"], withdraw["price"],
                     int(lb.SOL_RESERVE * 1e9) if withdraw["mint"] == lb.SOL else 0)
    if repay["mint"] != withdraw["mint"]:
        lb._back_to_usdc(rpc, me, repay["mint"], repay["token_program"], repay["decimals"], repay["price"],
                         int(lb.SOL_RESERVE * 1e9) if repay["mint"] == lb.SOL else 0)
    pnl = (lb.token_balance(rpc, me, lb.USDC) - usdc_before) / 1e6
    with lb._LOCK:
        d = lb._load()
        d["trades"] = (d.get("trades") or [])[-199:] + [{"t": now, "day": time.strftime("%Y-%m-%d", time.gmtime(now)),
                                                         "market": ob["market"], "paid": p["usd"], "pnl": pnl, "sig": sig, "protocol": "Save"}]
    lb._save()
    txt = ("💧 LIQUIDOU na SAVE (dinheiro real): pagou US$ " + format(p["usd"], ".2f") + " de dívida e vendeu a garantia de volta para "
           "USDC → " + format(pnl, "+.2f") + " USD\nhttps://solscan.io/tx/" + str(sig))
    lb.note(txt.split("\n")[0])
    if send and chat:
        send(chat, txt)
    return sig


def cycle(rpc, send=None, chat=None, now=None):
    now = now or time.time()
    st = _STATE
    if now - st["markets_at"] > 3600 or not st["markets"]:
        st["markets"], st["markets_at"] = markets(), now
    for name, m in st["markets"]:
        if now - st["scanned"].get(m, 0) >= lb.SCAN_S * 2:
            st["scanned"][m] = now
            try:
                st["borrowers"][m] = scan_market(rpc, m, now)
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                logging.warning("save scan %s: %s: %s", name, type(exc).__name__, str(exc)[:150])
            break
    for a, ob in list(_WATCH.items()):
        if now < _NEXT.get(a, 0):
            continue
        try:
            attempt(rpc, ob, reserves_of(rpc, ob["market"], now), send, chat, now=now)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            logging.warning("save attempt %s: %s: %s", a[:6], type(exc).__name__, str(exc)[:150])


def status_line():
    return ("🏦 Save (Solend): " + str(len(_STATE["markets"])) + " pools · devedores lidos " + str(sum(_STATE["borrowers"].values())) +
            " · perto do limite " + str(len(_WATCH)))
