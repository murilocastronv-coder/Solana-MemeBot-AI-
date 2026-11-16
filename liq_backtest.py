"""LIQUIDATION backtest on SMALL lending markets (owner, Nov 16): "protocolos menores - backtest with 50 and 60 USD".

What it measures, with REAL on-chain data of the last DAYS days (Helius full transactions):
- markets: the isolated / non-main lending markets of Kamino (klend) and Save (ex-Solend), from their public APIs;
- every successful liquidation in those markets (program log "Instruction: Liquidate..."): who liquidated (fee
  payer), what it paid and what it received (token balance changes of that wallet), valued in USD with today's
  DexScreener price (an approximation for tokens that moved a lot);
- the liquidator's profit (received - paid), the size (paid) and the bonus %;
- competition: how many different wallets liquidated each market and the share of the top one.
What YOU would have made with US$ C: a liquidation can be partial, so with C dollars you take
min(1, C / size) of each liquidation's profit, minus the network fee - IF your bot had won the race every time.
That is an UPPER LIMIT: the wallets that won those liquidations were faster. The report says so.
Liquidations done with a flash loan (paid ~0, only the profit shows up) are counted with the size estimated from the
median bonus of the market.
"""
import logging
import re
import statistics
import threading
import time

import signals as sg
import snipe_backtest as sbt

DAYS = 90
KAMINO_API = "https://api.kamino.finance/v2/kamino-market"
SAVE_API = "https://api.solend.fi/v1/markets/configs?scope=all&deployment=production"
MAX_MARKETS = int(sg._f("LIQ_BT_MAX_MARKETS", 40))
CALLS_PER_MARKET = int(sg._f("LIQ_BT_CALLS_PER_MARKET", 40))     # 100 transactions per call
FIRST_REPORT_S = 170
BG_MAX_S = sg._f("LIQ_BT_BG_MAX_S", 3600)
TX_FEE_USD = 0.02
CAPITALS = (50, 60)
LIQ_RE = re.compile(r"Instruction: Liquidat", re.I)
STABLE = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 1.0, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": 1.0}
WSOL = "So11111111111111111111111111111111111111112"
_RUNNING = threading.Lock()


# ---------------------------------------------------------------- markets
def markets(fetch=None):
    """[(protocol, name, address)] of the NON-main markets (the small ones)."""
    fetch = fetch or sg._get
    out = []
    try:
        for m in fetch(KAMINO_API) or []:
            addr = m.get("lendingMarket") or m.get("address")
            if addr and not m.get("isPrimary"):
                out.append(("Kamino", str(m.get("name") or addr[:6]), addr))
    except Exception as exc:
        logging.warning("liq bt: kamino markets: %s", type(exc).__name__)
    try:
        data = fetch(SAVE_API) or []
        for m in data if isinstance(data, list) else data.get("results") or []:
            addr = m.get("address")
            if addr and not m.get("isPrimary"):
                out.append(("Save", str(m.get("name") or addr[:6]), addr))
    except Exception as exc:
        logging.warning("liq bt: save markets: %s", type(exc).__name__)
    return out[:MAX_MARKETS]


def _page(rpc, address, token=None, lte=None):
    opts = {"transactionDetails": "full", "sortOrder": "desc", "limit": 100, "encoding": "json",
            "maxSupportedTransactionVersion": 1, "filters": {"status": "succeeded"}}
    if token:
        opts["paginationToken"] = token
    elif lte:
        opts["filters"]["blockTime"] = {"lte": int(lte)}
    res = rpc("getTransactionsForAddress", [address, opts])
    if isinstance(res, dict):
        return res.get("data") or [], res.get("paginationToken")
    return res or [], None


# ---------------------------------------------------------------- one liquidation
def liquidation_of(tx):
    """{'t', 'who', 'deltas': {mint: amount}} for a liquidation transaction, else None."""
    meta = tx.get("meta") or {}
    if meta.get("err") or not LIQ_RE.search(" ".join(meta.get("logMessages") or [])):
        return None
    keys = sbt._keys(tx)
    if not keys:
        return None
    who = keys[0]
    deltas = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") != who:
                continue
            amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            deltas[b.get("mint")] = deltas.get(b.get("mint"), 0.0) + sign * amt
    pre, post = (meta.get("preBalances") or [0])[0], (meta.get("postBalances") or [0])[0]
    sol = (post - pre + int(meta.get("fee") or 0)) / 1e9            # native SOL moved besides the fee
    if abs(sol) > 0.002:                                              # ignore rent of temporary accounts
        deltas[WSOL] = deltas.get(WSOL, 0.0) + sol
    return {"t": int(tx.get("blockTime") or 0), "who": who, "deltas": {m: d for m, d in deltas.items() if abs(d) > 1e-12}}


def prices(mints, fetch=None):
    """USD price per mint (DexScreener, today) - stablecoins fixed at 1."""
    fetch = fetch or sg._get
    out = dict(STABLE)
    todo = [m for m in mints if m not in out]
    for i in range(0, len(todo), 30):
        try:
            rows = fetch(sg.DEX + "/tokens/v1/solana/" + ",".join(todo[i:i + 30])) or []
        except Exception:
            continue
        best = {}
        for p in rows if isinstance(rows, list) else []:
            m = (p.get("baseToken") or {}).get("address")
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if m in todo and liq > best.get(m, (0, 0))[0] and p.get("priceUsd"):
                best[m] = (liq, float(p["priceUsd"]))
        out.update({m: v[1] for m, v in best.items()})
    return out


def value(liq, px):
    """(paid USD, received USD) or None when a token has no price."""
    paid = got = 0.0
    for m, d in liq["deltas"].items():
        if m not in px:
            return None
        usd = d * px[m]
        if usd < 0:
            paid -= usd
        else:
            got += usd
    return paid, got


# ---------------------------------------------------------------- report
def simulate(events, capital):
    """Per month: what US$ capital would have taken (pro rata), assuming it won every race. (USD, count)"""
    total, n = 0.0, 0.0
    for e in events:
        if e["profit"] <= 0 or e["size"] <= 0:
            continue
        share = min(1.0, capital / e["size"])
        gain = e["profit"] * share - TX_FEE_USD
        if gain > 0:
            total += gain * e["w"]
            n += e["w"]
    return total, n


def report(store, now, final=False, fetch=None):
    mk = store.get("markets") or []
    evs = [dict(e, market=k) for k, lst in (store.get("liqs") or {}).items() for e in lst]
    L = ["💧 BACKTEST LIQUIDAÇÃO — mercados MENORES de empréstimo (Kamino isolados + Save/Solend pools), últimos " +
         str(DAYS) + " dias, dados REAIS",
         "Mercados encontrados: " + str(len(mk)) + " · lidos: " + str(len(store.get("done") or {})) +
         " · transações lidas: " + format(store.get("txs", 0), ",") + " · liquidações achadas: " + str(len(evs))]
    if not evs:
        L.append("Nenhuma liquidação achada ainda." + ("" if final else " O download continua e manda o relatório final."))
        return "\n".join(L)
    px = prices({m for e in evs for m in e["deltas"]}, fetch)
    rows = []
    for e in evs:
        v = value(e, px)
        if v is None:
            continue
        paid, got = v
        rows.append(dict(e, paid=paid, profit=got - paid))
    by_m = {}
    for e in rows:
        by_m.setdefault(e["market"], []).append(e)
    bonus_all = [e["profit"] / e["paid"] for e in rows if e["paid"] > 1 and 0 < e["profit"] / e["paid"] < 0.5]
    med_all = statistics.median(bonus_all) if bonus_all else 0.05
    events = []
    for k, lst in by_m.items():
        bon = [e["profit"] / e["paid"] for e in lst if e["paid"] > 1 and 0 < e["profit"] / e["paid"] < 0.5]
        med = statistics.median(bon) if bon else med_all
        for e in lst:
            size = e["paid"] if e["paid"] > 1 else (e["profit"] / med if med > 0 else 0)
            span = max(1.0, (now - (store.get("span") or {}).get(k, now - DAYS * 86400)) / 86400)
            events.append(dict(e, size=size, w=30.0 / span))     # weight: this market's events -> per month
    days = max(1.0, max((now - v) / 86400 for v in (store.get("span") or {now: now - 86400}).values()))
    prof_m = sum(e["profit"] * e["w"] for e in events if e["profit"] > 0)
    n_m = sum(e["w"] for e in events)
    who = {}
    for e in events:
        who[e["who"]] = who.get(e["who"], 0) + 1
    top = max(who.values()) / len(events)
    L.append("Liquidações com preço: " + str(len(events)) + " (histórico lido de até " + format(days, ".0f") + " dias; ~" +
             format(n_m, ".0f") + " por mês) · lucro dos liquidantes: ~US$ " + format(prof_m, ",.0f") +
             " por mês · bônus mediano " + format(med_all * 100, ".1f") + "%")
    L.append("🏁 Concorrência: " + str(len(who)) + " carteiras diferentes liquidaram; a maior ficou com " +
             format(top * 100, ".0f") + "% das liquidações")
    L.append("🏦 Mercados com mais lucro (por mês):")
    names = {a: p + " " + n for p, n, a in mk}
    tops = sorted(by_m.items(), key=lambda kv: -sum(max(0, e["profit"]) for e in kv[1]))[:6]
    days = max(1.0, days)
    for k, lst in tops:
        w = {e["who"] for e in lst}
        sp = max(1.0, (now - (store.get("span") or {}).get(k, now - DAYS * 86400)) / 86400)
        L.append("• " + names.get(k, k[:6]) + ": " + str(len(lst)) + " liquidações em " + format(sp, ".0f") + " dias · lucro US$ " +
                 format(sum(max(0, e["profit"]) for e in lst) / sp * 30, ",.0f") + "/mês · " + str(len(w)) + " liquidante(s)")
    L.append("💵 SE o seu bot ganhasse TODAS as disputas (limite máximo, irreal):")
    for c in CAPITALS:
        tot, n = simulate(events, c)
        L.append("• Com US$ " + str(c) + ": US$ " + format(tot, ",.2f") + " por mês (" +
                 format(n, ".0f") + " liquidações/mês, " + format(tot / c * 100, ".0f") + "% ao mês)")
    small = [e for e in events if 0 < e["size"] <= 60]
    L.append("🔎 Liquidações pequenas (até US$ 60, que caberiam inteiras no seu capital): " + str(len(small)) +
             " · lucro delas ~US$ " + format(sum(max(0, e["profit"]) * e["w"] for e in small), ",.2f") + "/mês")
    if not final:
        L.append("⏳ PARCIAL: continua lendo os mercados sozinho; manda o relatório final (até ~1 h).")
    L.append("⚠️ Valores com o preço de HOJE dos tokens; quem liquidou foi mais rápido que um bot comum, então o real "
             "seria uma fração disso. Não é promessa de lucro.")
    return "\n".join(L)


def start(rpc, send=None, chat=None, fetch=None):
    if rpc is None:
        return "precisa do SOLANA_RPC_URL (Helius) no Render"
    if not _RUNNING.acquire(blocking=False):
        return "o teste já está rodando"

    def work():
        try:
            now = time.time()
            store = sbt._get("liq_bt_cache", {})
            if not store.get("markets") or now - store.get("at", 0) > 86400:
                store = {"markets": markets(fetch), "done": {}, "liqs": {}, "txs": 0, "cursor": {}, "at": now}
            began = time.time()
            sent = False
            for proto, name, addr in store["markets"]:
                if addr in store["done"]:
                    continue
                if time.time() - began > BG_MAX_S:
                    break
                cur = store["cursor"].get(addr) or {}
                calls = cur.get("calls", 0)
                token, lte = cur.get("token"), cur.get("lte")
                finished = False
                while calls < CALLS_PER_MARKET:
                    if not sent and time.time() - began > FIRST_REPORT_S:
                        sbt._put("liq_bt_cache", store)
                        if send and chat:
                            send(chat, report(store, now, fetch=fetch))
                        sent = True
                    try:
                        txs, token = _page(rpc, addr, token, lte)
                    except Exception as exc:
                        logging.warning("liq bt %s: %s: %s", addr[:6], type(exc).__name__, str(exc)[:150])
                        time.sleep(3)
                        calls += 1
                        continue
                    calls += 1
                    store["txs"] = store.get("txs", 0) + len(txs)
                    for tx in txs:
                        lq = liquidation_of(tx)
                        if lq:
                            store["liqs"].setdefault(addr, []).append(lq)
                    oldest = min((int(t.get("blockTime") or now) for t in txs), default=0)
                    if oldest:
                        store.setdefault("span", {})[addr] = max(oldest, now - DAYS * 86400)
                    if not txs or oldest < now - DAYS * 86400 or (not token and len(txs) < 100):
                        finished = True
                        break
                    lte = oldest - 1 if not token else None
                store["cursor"][addr] = {"calls": calls, "token": token, "lte": lte}
                if finished or calls >= CALLS_PER_MARKET:
                    store["done"][addr] = calls
                sbt._put("liq_bt_cache", store)
            sbt._put("liq_bt_cache", store)
            if send and chat:
                send(chat, report(store, now, final=True, fetch=fetch))
        except Exception as exc:
            logging.exception("liq backtest failed")
            if send and chat:
                send(chat, "⛔ Backtest de liquidação falhou: " + type(exc).__name__ + ": " + str(exc)[:150])
        finally:
            _RUNNING.release()

    threading.Thread(target=work, daemon=True).start()
    return ""
