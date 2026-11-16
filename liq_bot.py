"""LIQUIDATION robot - Kamino isolated (small) markets, REAL money (owner, Nov 16).

How it works:
1. Markets: the small Kamino markets where the backtest found liquidations (liq_bt_cache), else Kamino's non-main list.
2. Every SCAN_S each market's borrowers with debt are read (getProgramAccounts, only the bytes the bot uses) and their
   LTV is recomputed with TODAY's reserve prices (kamino.ltv_now). Those at >= WATCH_LTV of the limit are watched.
3. Every FAST_S the watched ones are re-read; at >= TRIGGER_LTV the bot builds Kamino's own liquidation sequence
   (refresh reserves -> refresh obligation -> liquidate v2), SIMULATES it on the RPC and only sends it when the
   simulation succeeds. Repays only USDC debt, with at most LIQ_USD per liquidation (the owner's capital).
4. What it receives (the collateral, with Kamino's liquidation bonus) is sold back to USDC on Jupiter.
Limits: US$ LIQ_USD per liquidation, stops for the day after -LIQ_DAILY_LOSS, one try per obligation per minute,
a minimum to receive (the program refuses to pay out less than ~what was repaid).
Honest: the professional liquidators read the chain faster (Geyser) - this bot wins only when they are slow or absent.
"""
import base64
import logging
import os
import threading
import time

import kamino as k
import signals as sg

E = sg._f
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = "So11111111111111111111111111111111111111112"
LIQ_USD = E("LIQ_USD", 50)
DAILY_LOSS = E("LIQ_DAILY_LOSS_USD", 10)
SCAN_S = E("LIQ_SCAN_S", 120)
FAST_S = E("LIQ_FAST_S", 8)
WATCH_LTV = E("LIQ_WATCH_LTV", 0.90)
TRIGGER_LTV = E("LIQ_TRIGGER_LTV", 0.995)
MAX_MARKETS = int(E("LIQ_MAX_MARKETS", 12))
CU_LIMIT = int(E("LIQ_CU_LIMIT", 1_000_000))
CU_PRICE = int(E("LIQ_CU_PRICE_MICROLAMPORTS", 50_000))
MIN_RECEIVE = E("LIQ_MIN_RECEIVE", 0.995)            # receive at least 99.5% of the repaid value (bonus comes on top)
SOL_RESERVE = E("LIQ_SOL_RESERVE", 0.03)              # SOL kept for fees
KEY = "liq_bot"
_LOCK = threading.RLock()
_MEM = {}
_RES = {}        # market -> {"at": t, "reserves": {address: reserve}}
_WATCH = {}      # obligation address -> decoded obligation
_TRIED = {}


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
def markets():
    """[(name, address)] - the small Kamino markets where liquidations happened (backtest), most first."""
    import snipe_backtest as sbt
    bt = sbt._get("liq_bt_cache", {})
    names = {a: n for p, n, a in bt.get("markets") or [] if p == "Kamino"}
    ranked = sorted(((len(v), a) for a, v in (bt.get("liqs") or {}).items() if a in names), reverse=True)
    out = [(names[a], a) for _, a in ranked]
    if not out:
        out = [(n, a) for n, a in names.items()]
    return out[:MAX_MARKETS]


def _gpa(rpc, size, market, extra=None, data_slice=None):
    flt = [{"dataSize": size}, {"memcmp": {"offset": 32, "bytes": market}}] + (extra or [])
    opts = {"encoding": "base64", "filters": flt}
    if data_slice:
        opts["dataSlice"] = data_slice
    rows = rpc("getProgramAccounts", [k.KLEND, opts]) or []
    return [(r["pubkey"], base64.b64decode(r["account"]["data"][0])) for r in rows]


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


def scan_market(rpc, market, now=None):
    """Borrowers of the market at >= WATCH_LTV go to the watch list. Returns (borrowers, watched)."""
    res = reserves_of(rpc, market, now, max_age=0)          # fresh prices every scan
    rows = _gpa(rpc, k.OBLIGATION_SIZE, market, [{"memcmp": {"offset": 2287, "bytes": "2"}}],   # has_debt == 1
                {"offset": 0, "length": k.OBLIGATION_HEAD})
    n = w = 0
    for a, data in rows:
        ob = k.obligation(a, data + bytes(k.OBLIGATION_HEAD - len(data)))
        if not ob or not ob["has_debt"]:
            continue
        n += 1
        ltv = k.ltv_now(ob, res)
        if ltv is not None and ltv >= WATCH_LTV:
            ob["ltv"] = ltv
            _WATCH[a] = ob
            w += 1
        else:
            _WATCH.pop(a, None)
    return n, w


def refresh_watch(rpc, now=None):
    """Re-read the watched obligations and their reserves. Returns [(ob, reserves, ltv)] at >= TRIGGER_LTV."""
    if not _WATCH:
        return []
    obs = list(_WATCH)
    res_keys = sorted({x["reserve"] for ob in _WATCH.values() for x in ob["deposits"] + ob["borrows"]})
    out = []
    accs = {}
    keys = obs + res_keys
    for i in range(0, len(keys), 100):
        got = (rpc("getMultipleAccounts", [keys[i:i + 100], {"encoding": "base64"}]) or {}).get("value") or []
        for a, acc in zip(keys[i:i + 100], got):
            if acc:
                accs[a] = base64.b64decode(acc["data"][0])
    reserves = {}
    for a in res_keys:
        r = k.reserve(a, accs.get(a, b""))
        if r:
            reserves[a] = r
    for a in obs:
        ob = k.obligation(a, accs.get(a, b""))
        if not ob or not ob["has_debt"]:
            _WATCH.pop(a, None)
            continue
        ltv = k.ltv_now(ob, reserves)
        if ltv is None or ltv < WATCH_LTV * 0.97:
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


def sol_balance(rpc, owner):
    return int((rpc("getBalance", [owner]) or {}).get("value") or 0) / 1e9


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
    sol = sol_balance(rpc, owner)
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
    """(repay reserve, withdraw reserve, amount, min_receive, repay USD) or (None, why)."""
    debts = [(b, reserves.get(b["reserve"])) for b in ob["borrows"]]
    debts = [(b, r) for b, r in debts if r and r["mint"] == USDC]
    if not debts:
        return None, "dívida não é em USDC"
    b, repay = max(debts, key=lambda x: x[0]["value"])
    cols = [(d, reserves.get(d["reserve"])) for d in ob["deposits"]]
    cols = [(d, r) for d, r in cols if r and r["price"] > 0]
    if not cols:
        return None, "sem garantia com preço"
    d, withdraw = max(cols, key=lambda x: x[0]["value"])
    amount = int(min(usdc_raw, LIQ_USD * 1e6, b["amount"]))
    if amount < 1_000_000:
        return None, "sem USDC suficiente"
    usd = amount / 1e6 * repay["price"]
    min_receive = int(usd / withdraw["price"] * 10 ** withdraw["decimals"] * MIN_RECEIVE)
    return (repay, withdraw, amount, min_receive, usd), ""


def build_tx(rpc, signer_pub, ob, reserves, repay, withdraw, amount, min_receive, blockhash=None):
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    me = str(signer_pub)
    ixs = [set_compute_unit_limit(CU_LIMIT), set_compute_unit_price(CU_PRICE)]
    need = [(withdraw["c_mint"], k.TOKEN), (withdraw["mint"], withdraw["token_program"])]
    have = (rpc("getMultipleAccounts", [[k.ata(me, m, p) for m, p in need], {"encoding": "base64"}]) or {}).get("value") or [None, None]
    for (m, p), acc in zip(need, have):
        if not acc:
            ixs.append(k.ix_create_ata(me, me, m, p))
    ixs += k.liquidation_ixs(me, ob, reserves, repay, withdraw, amount, min_receive)
    bh = blockhash or ((rpc("getLatestBlockhash", [{"commitment": "confirmed"}]) or {}).get("value") or {}).get("blockhash")
    msg = MessageV0.try_compile(Pubkey.from_string(me), ixs, [], Hash.from_string(bh))
    return msg


def simulate(rpc, msg):
    """(ok, short reason) - the RPC runs the transaction without sending it."""
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction
    tx = VersionedTransaction.populate(msg, [Signature.default()] * msg.header.num_required_signatures)
    raw = bytes(tx)
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
    sig = rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}])
    end = time.time() + wait_s
    while time.time() < end:
        time.sleep(2)
        st = ((rpc("getSignatureStatuses", [[sig]]) or {}).get("value") or [None])[0]
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            return sig, st.get("err") is None
    return sig, False


def attempt(rpc, ob, reserves, ltv, send=None, chat=None, signer=None, now=None):
    now = now or time.time()
    if now - _TRIED.get(ob["address"], 0) < 60:
        return None
    _TRIED[ob["address"]] = now
    stat("oportunidades")
    signer = signer or __import__("rent_reclaim").bot_signer()
    me = str(signer.pubkey())
    usdc_raw = token_balance(rpc, me, USDC)
    p, why = plan(ob, reserves, usdc_raw)
    if not p:
        stat("puladas")
        note("liquidável (LTV " + format(ltv * 100, ".1f") + "%) mas pulei: " + why + " · " + ob["address"][:6])
        return None
    repay, withdraw, amount, min_receive, usd = p
    msg = build_tx(rpc, signer.pubkey(), ob, reserves, repay, withdraw, amount, min_receive)
    ok, why = simulate(rpc, msg)
    if not ok:
        stat("simulação falhou")
        note("simulação recusou " + ob["address"][:6] + " (" + withdraw["name"] + "): " + why)
        return None
    stat("simulação ok")
    if not real():
        note("SIMULAÇÃO OK (modo observação): pagaria US$ " + format(usd, ".2f") + " e receberia " + withdraw["name"])
        return None
    if DAILY_LOSS > 0 and day_pnl(now) <= -DAILY_LOSS:
        note("limite de perda do dia atingido: não envio")
        return None
    before = token_balance(rpc, me, withdraw["mint"], withdraw["token_program"])
    sig, done = send_tx(rpc, msg, signer)
    if not done:
        stat("perdeu a disputa")
        note("enviei mas não confirmou (outro bot foi mais rápido ou expirou) · " + str(sig)[:10])
        return None
    stat("liquidou")
    got = token_balance(rpc, me, withdraw["mint"], withdraw["token_program"]) - before
    out_usd = got / 10 ** withdraw["decimals"] * withdraw["price"]
    if withdraw["mint"] != USDC and got > 0:
        try:
            r = _jupiter_swap(withdraw["mint"], USDC, got, me)
            out_usd = r["total_output_amount"] / 1e6
        except Exception as exc:
            note("liquidou, mas a venda de " + withdraw["name"] + " falhou (" + type(exc).__name__ + "); fica na carteira")
    pnl = out_usd - usd - (CU_LIMIT * CU_PRICE / 1e6 / 1e9 + 0.00001) * float(__import__("copy_trader")._sol_usd() or 0)
    with _LOCK:
        d = _load()
        d["trades"] = (d.get("trades") or [])[-199:] + [{"t": now, "day": time.strftime("%Y-%m-%d", time.gmtime(now)),
                                                         "market": ob["market"], "paid": usd, "got": out_usd, "pnl": pnl, "sig": sig}]
    _save()
    msg_txt = ("💧 LIQUIDOU (dinheiro real): pagou US$ " + format(usd, ".2f") + " da dívida, recebeu " + withdraw["name"] +
               " e vendeu por US$ " + format(out_usd, ".2f") + " → " + format(pnl, "+.2f") + " USD\nhttps://solscan.io/tx/" + str(sig))
    note(msg_txt.split("\n")[0])
    if send and chat:
        send(chat, msg_txt)
    return sig


# ---------------------------------------------------------------- loop
def cycle(rpc, send=None, chat=None, now=None, state=None):
    """One pass: scan the markets due, check the watched borrowers, liquidate the ones that can be."""
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
            except Exception as exc:
                logging.warning("liq scan %s: %s: %s", name, type(exc).__name__, str(exc)[:150])
            break                                          # one market per pass: spreads the reads
    for ob, reserves, ltv in refresh_watch(rpc, now):
        try:
            attempt(rpc, ob, reserves, ltv, send, chat, now=now)
        except Exception as exc:
            logging.exception("liq attempt failed")
            note("erro ao tentar " + ob["address"][:6] + ": " + type(exc).__name__ + ": " + str(exc)[:120])
    if now - st.get("saved", 0) > 60:
        st["saved"] = now
        _save()


_STATE = {}


def status_text():
    d = _load()
    s = d.get("stats") or {}
    tr = d.get("trades") or []
    L = ["💧 ROBÔ DE LIQUIDAÇÃO (Kamino, mercados menores) — " +
         ("🟢 LIGADO · 💵 DINHEIRO REAL" if d.get("enabled") and d.get("real") else
          "🟡 LIGADO · só observando" if d.get("enabled") else "⏸️ DESLIGADO"),
         "⚙️ Até US$ " + format(LIQ_USD, "g") + " por liquidação (USDC) · para no dia com −US$ " + format(DAILY_LOSS, "g") +
         " (hoje " + format(day_pnl(), "+.2f") + ") · só envia se a simulação na rede der certo",
         "🏦 Mercados vigiados: " + str(len(d.get("markets") or _STATE.get("markets") or [])) + " · devedores lidos: " +
         str(sum((_STATE.get("borrowers") or {}).values())) + " · perto de liquidar agora: " + str(len(_WATCH)),
         "📊 Oportunidades " + str(s.get("oportunidades", 0)) + " · puladas " + str(s.get("puladas", 0)) + " · simulação ok " +
         str(s.get("simulação ok", 0)) + " · recusadas " + str(s.get("simulação falhou", 0)) + " · perdeu disputa " +
         str(s.get("perdeu a disputa", 0)) + " · LIQUIDOU " + str(s.get("liquidou", 0)),
         "💰 Resultado: " + str(len(tr)) + " liquidações · total " + format(sum(t.get("pnl", 0) for t in tr), "+.2f") + " USD"]
    for x in (d.get("log") or [])[-6:]:
        L.append("• " + x)
    return "\n".join(L)
