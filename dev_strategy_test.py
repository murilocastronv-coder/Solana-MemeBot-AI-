"""DEV strategy test, 3+ months, with REAL on-chain trades (owner, Nov 16).

The live rule replayed on the past:
- a dev counts as approved on a date only if its trades CLOSED BEFORE that date pass the rule
  (>= 20 trades, hit rate >= 70%, profit factor >= 2, net profit > 0, >= 5 different coins) - no future data;
- when that dev bought a pump.fun coin, the bot buys DELAY seconds later at the real price of that moment
  (median of the next 3 trades by other wallets, read through Helius);
- it skips the buy when that price is already more than MAX_RUNUP above the dev's own price, or the coin is worth
  less than MIN_MCAP (pump.fun supply = 1 billion);
- it sells the same share the dev sold, DELAY seconds after each of the dev's sales (no trade = total loss);
- US$ per trade, max open positions, costs per side + network fee, like the real robot.
Honest limits: the 'same wallet for 2 days' and 'wallet with funds' checks cannot be rebuilt for past dates; the
history the bot downloaded per dev is its latest transactions, so the oldest months can have fewer devs (the
report shows the coverage per month). Every replayed trade is cached (PostgreSQL), so each run continues.
"""
import json
import logging
import threading
import time

import dev_bot as db
import launch_test as lt
import snipe_backtest as sbt

DAYS = db.E("DEV_STRAT_TEST_DAYS", 90)
DELAYS = (5, 10, 30)
LIVE_DELAY = 10
MAX_RUNUP = db.MAX_RUNUP_DEV
MIN_MCAP = db.CURVE_MIN_MCAP
MAX_S = db.E("DEV_STRAT_TEST_MINUTES", 3) * 60     # owner (Nov 16): an answer in 3 minutes, every tap continues
CAPITAL = db.E("DEV_STRAT_TEST_CAPITAL", 500)
_RUNNING = threading.Lock()
_STOP = threading.Event()


def _cache():
    return sbt._get("dev_strat_cache_v2", {})


def _qualifies(prior, min_win=None, min_trades=20):
    if len(prior) < min_trades:
        return False
    pnl = [float(q.get("pnl_usd") or 0) for q in prior]
    gw, gl = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x < 0)
    return (sum(1 for x in pnl if x > 0) / len(pnl) >= (db.TIERS[-1][1] if min_win is None else min_win) and sum(pnl) > 0
            and (gl == 0 or gw / gl >= 2) and len({q.get("mint") for q in prior}) >= max(db.MIN_TOKENS, 1))


def events(d=None, now=None, days=None, min_win=None, min_trades=20):
    """[(time, dev, trade)] of the period where the dev was already approved by its PAST trades."""
    d = d or db._load()
    now = now or time.time()
    since = now - float(days or DAYS) * 86400
    out, devs_ok = [], set()
    for dev, v in d["devs"].items():
        hist = [h for h in v.get("hist") or [] if db.pump_trade(h) and db.bt_ok(h) and h.get("opened") and h.get("closed")]
        for h in hist:
            t = float(h["opened"])
            if t < since:
                continue
            prior = [q for q in hist if float(q["closed"]) < t]
            if _qualifies(prior, min_win, min_trades):
                out.append((t, dev, h))
                devs_ok.add(dev)
    out.sort(key=lambda x: x[0])
    return out, devs_ok


def replay(rpc, dev, h, sol_usd):
    """{delay: [return, exit_time] | 'motivo'} for one dev trade."""
    opened = float(h["opened"])
    sells = h.get("sells") or [[h["closed"], 1.0]]
    first = lt._trades(rpc, h["mint"], int(opened) - 2, opened + max(DELAYS) + 5, sol_usd, pages=2)
    sells = sells[:1] if len(sells) == 1 else [sells[0], sells[-1]]      # first and last sale: 2 reads, not 8
    sale = [lt._trades(rpc, h["mint"], int(t), int(t) + max(DELAYS) + 5, sol_usd, pages=1) for t, _ in sells]
    dev_px = float(h.get("entry_usd") or 0)
    out = {}
    for delay in DELAYS:
        entry = lt._median3([p for t, p, who in first if t >= opened + delay and who != dev])
        if not entry:
            out[str(delay)] = "sem preço na entrada"
            continue
        if dev_px and entry > dev_px * (1 + MAX_RUNUP):
            out[str(delay)] = "preço já +" + format(MAX_RUNUP * 100, ".0f") + "% acima do DEV"
            continue
        left, got = 1.0, 0.0
        for i, ((t_sale, frac), trades) in enumerate(zip(sells, sale)):
            px = lt._median3([p for t, p, who in trades if t >= t_sale + delay and who != dev])
            part = left if i == len(sale) - 1 else left * min(float(frac), 1.0)
            got += part * ((px or 0.0) / entry)
            left -= part
        out[str(delay)] = [round(got * (1 - sbt.COST) ** 2 - 1, 4), float(sells[-1][0]) + delay, entry]   # entry: filters applied later
    return out


def download(rpc, sol_usd, evs, should_stop=None, on_progress=None, max_s=None):
    c = _cache()
    began = time.time()
    sbt.DEADLINE[0] = began + (max_s or MAX_S)
    todo = [(t, dev, h) for t, dev, h in evs if (dev + h["mint"] + str(int(t))) not in c]
    # spread over the whole period (not only the newest weeks) when the time is short
    step = max(1, len(todo) // 200)
    order = todo[::step] + [x for i, x in enumerate(todo) if i % step]
    for i, (t, dev, h) in enumerate(order):
        if (should_stop and should_stop()) or time.time() - began > (max_s or MAX_S):
            break
        try:
            c[dev + h["mint"] + str(int(t))] = replay(rpc, dev, h, sol_usd)
        except TimeoutError:
            break
        except Exception as exc:
            logging.warning("dev strategy test %s: %s: %s", h["mint"][:8], type(exc).__name__, str(exc)[:120])
            continue
        if i % 20 == 19:
            sbt._put("dev_strat_cache_v2", c)
        if on_progress:
            on_progress(i + 1, len(order))
    sbt.DEADLINE[0] = 0.0
    sbt._put("dev_strat_cache_v2", c)
    return c


def simulate(evs, c, delay, usd=None, max_open=None):
    usd = float(usd or db.buy_usd())
    max_open = int(max_open or db.MAX_OPEN)
    open_until, trades, rejected, months = [], [], {}, {}
    for t, dev, h in evs:
        r = (c.get(dev + h["mint"] + str(int(t))) or {}).get(str(delay))
        if r is None:
            continue                                     # not replayed yet
        if isinstance(r, str):
            rejected[r] = rejected.get(r, 0) + 1
            continue
        if len(r) > 2 and r[2] * sbt.SUPPLY < db.CURVE_MIN_MCAP:      # minimum applied now: changing it needs no new download
            k = "moeda abaixo de US$ " + format(db.CURVE_MIN_MCAP, ",.0f")
            rejected[k] = rejected.get(k, 0) + 1
            continue
        open_until = [x for x in open_until if x > t]
        if len(open_until) >= max_open:
            rejected["já tinha " + str(max_open) + " abertas"] = rejected.get("já tinha " + str(max_open) + " abertas", 0) + 1
            continue
        pnl = usd * r[0] - 2 * sbt.NET_FEE
        open_until.append(r[1])
        trades.append((t, dev, pnl))
        m = time.strftime("%Y-%m", time.gmtime(t))
        months[m] = months.get(m, 0.0) + pnl
    pnl = [x[2] for x in trades]
    eq, peak, dd = CAPITAL, CAPITAL, 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak > 0 else 0)
    gw, gl = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x < 0)
    return {"trades": len(pnl), "pnl": round(sum(pnl), 2), "win": sum(1 for x in pnl if x > 0) / len(pnl) if pnl else 0.0,
            "pf": round(gw / gl, 2) if gl > 0 else None, "dd": dd, "worst": min(pnl, default=0.0), "months": months,
            "rejected": rejected, "devs": len({x[1] for x in trades})}


def text(evs, devs_ok, c, now):
    done = sum(1 for t, dev, h in evs if (dev + h["mint"] + str(int(t))) in c)
    usd = db.buy_usd()
    L = ["🧪 TESTE DA ESTRATÉGIA DEV — últimos " + format(DAYS, ".0f") + " dias (dados REAIS da blockchain)",
         "Regra: DEV aprovado pelo histórico ANTERIOR (acerto ≥ " + format(db.TIERS[-1][1] * 100, ".0f") + "%, ≥ 20 ops, PF ≥ 2, lucro, ≥ " +
         str(db.MIN_TOKENS) + " moedas) compra → o bot compra X s depois se o preço não passou de +" + format(MAX_RUNUP * 100, ".0f") +
         "% e a moeda vale ≥ US$ " + format(db.CURVE_MIN_MCAP, ",.0f") + " · vende junto com o DEV · US$ " + format(usd, "g") +
         " por compra · máx. " + str(db.MAX_OPEN) + " abertas · custos " + format(sbt.COST * 100, ".0f") + "% por lado",
         "📊 Compras de DEVs aprovados no período: " + str(len(evs)) + " (" + str(len(devs_ok)) + " DEVs) · refeitas com preço real: " + str(done)]
    for delay in DELAYS:
        s = simulate(evs, c, delay)
        tag = " ← velocidade do robô hoje" if delay == LIVE_DELAY else ""
        if not s["trades"]:
            L.append("• Entrando " + str(delay) + " s depois" + tag + ": nenhuma operação")
            continue
        L.append("• Entrando " + str(delay) + " s depois" + tag + ": " + str(s["trades"]) + " ops (" + str(s["devs"]) + " DEVs) · acerto " +
                 format(s["win"] * 100, ".0f") + "% · resultado " + format(s["pnl"], "+.2f") + " USD · PF " +
                 ("—" if s["pf"] is None else format(s["pf"], ".2f")) + " · queda máx " + format(s["dd"] * 100, ".0f") +
                 "% de US$ " + format(CAPITAL, "g") + " · pior " + format(s["worst"], "+.2f"))
        if delay == LIVE_DELAY:
            if s["months"]:
                L.append("  📅 Por mês: " + " · ".join(k + " " + format(v, "+.2f") for k, v in sorted(s["months"].items())))
            if s["rejected"]:
                L.append("  🚫 Não comprou: " + " · ".join(k + ": " + str(v) for k, v in sorted(s["rejected"].items(), key=lambda kv: -kv[1])))
    live = simulate(evs, c, LIVE_DELAY)
    months = len(live["months"])
    if done < len(evs):
        L.append("⏳ PARCIAL: faltam " + str(len(evs) - done) + " compras para refazer — toque de novo (cada toque continua).")
    if live["trades"] >= 30 and live["pnl"] > 0 and (live["pf"] or 0) >= 1.3:
        L.append("✅ Com a velocidade de hoje (" + str(LIVE_DELAY) + " s) a estratégia deu lucro em " + str(live["trades"]) + " operações.")
    elif live["trades"] >= 30:
        L.append("❌ Com a velocidade de hoje (" + str(LIVE_DELAY) + " s) a estratégia NÃO deu lucro confiável.")
    else:
        L.append("⚠️ Menos de 30 operações: amostra pequena, ainda não prova nada.")
    if months < 3:
        L.append("⚠️ Só " + str(months) + " mês(es) com operações: o histórico baixado dos DEVs ainda não cobre 3 meses inteiros.")
    L.append("⚠️ Não dá para refazer no passado as regras de 'mesma carteira' e 'carteira com fundo'. Passado não garante futuro.")
    return "\n".join(L)


def cancel():
    _STOP.set()


def start(rpc, sol_usd, send=None, chat=None):
    if rpc is None:
        return "precisa do SOLANA_RPC_URL (Helius) no Render"
    if not _RUNNING.acquire(blocking=False):
        return "o teste já está rodando"
    _STOP.clear()
    now = time.time()
    evs, devs_ok = events(now=now)
    if not evs:
        _RUNNING.release()
        return "ainda não há compras de DEVs aprovados no histórico baixado (deixe o robô analisar mais DEVs)"

    def work():
        try:
            c = download(rpc, sol_usd, evs, _STOP.is_set)
            txt = text(evs, devs_ok, c, now)
            db.note("teste estratégia DEV: " + txt.split("\n")[3][:150] if len(txt.split("\n")) > 3 else txt[:150])
            if send and chat:
                send(chat, txt)
        except Exception as exc:
            logging.exception("dev strategy test failed")
            if send and chat:
                send(chat, "⛔ Teste da estratégia DEV falhou: " + type(exc).__name__ + ": " + str(exc)[:150])
        finally:
            _RUNNING.release()

    threading.Thread(target=work, daemon=True).start()
    return ""
