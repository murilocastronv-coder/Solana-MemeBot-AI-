"""Backtest: >= 12 months, development + out-of-sample split, fees/slippage/funding.

    python -m btc_trend_pullback.backtest                 # last 18 full months, futures
    python -m btc_trend_pullback.backtest --profile both  # futures and spot
    python -m btc_trend_pullback.backtest --csv my_5m.csv --profile spot

Parameters come from config.StrategyConfig and are NOT tuned here. Change them
only by looking at the development period, never at the out-of-sample one.
Results are a historical simulation, not a promise of future profit.
"""
import argparse
import datetime as dt
import json
import math
import sys
from collections import defaultdict

from . import data as D
from .config import AGGRESSIVE_GOAL_USD, PRESETS, assert_paper_only, preset
from .engine import Engine, SimBroker
from .strategy import Context

MIN_DAYS = 365


def run_segment(ctx, start, end, cfg, funding=None):
    """Fresh account, bars [start, end). Indicators were warmed up on earlier bars."""
    eng = Engine(cfg, SimBroker(cfg), funding=funding)
    for i in range(start, end):
        eng.on_bar(ctx, i)
    eng.force_close(ctx.c5[end - 1])
    if eng.curve:
        t = eng.curve[-1][0]
        eng.curve[-1] = (t, min(eng.curve[-1][1], eng.cash), eng.cash)
    return eng


def max_drawdown(points):
    """points: (low, close) equity pairs. Peak from closes, trough from lows."""
    peak, worst = None, 0.0
    for low, close in points:
        peak = close if peak is None else max(peak, close)
        if peak > 0:
            worst = max(worst, (peak - low) / peak)
        peak = max(peak, close)
    return worst


def metrics(eng, start_equity):
    tr = eng.trades
    pnls = [t["pnl"] for t in tr]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    streak = best = 0
    for p in pnls:
        streak = streak + 1 if p <= 0 else 0
        best = max(best, streak)
    in_pos = sum(1 for _, lo, cl in eng.curve if lo != cl)
    return {
        "saldo_inicial": start_equity,
        "saldo_final": eng.cash,
        "retorno_liquido_pct": (eng.cash / start_equity - 1) * 100,
        "drawdown_max_pct": max_drawdown([(lo, cl) for _, lo, cl in eng.curve]) * 100,
        "operacoes": len(tr),
        "taxa_acerto_pct": (len(wins) / len(tr) * 100) if tr else 0.0,
        "fator_lucro": (gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0),
        "ganho_medio": (gross_win / len(wins)) if wins else 0.0,
        "perda_media": (-gross_loss / len(losses)) if losses else 0.0,
        "seq_max_perdas": best,
        "taxas_total": sum(t["fees"] for t in tr),
        "funding_total": sum(t["funding"] for t in tr),
        "tempo_posicionado_pct": in_pos / len(eng.curve) * 100 if eng.curve else 0.0,
        "sinais": eng.signals,
        "sinais_recusados": dict(eng.skips),
        "saidas": dict(_count(t["reason"] for t in tr)),
        "limite_diario_atingido": eng.daily_limit_hits,
        "interrompida": eng.halted,
        "pior_saldo": min((lo for _, lo, _ in eng.curve), default=eng.cash),
    }


def _count(items):
    out = defaultdict(int)
    for x in items:
        out[x] += 1
    return out


def buy_and_hold(c5, start, end, cfg):
    fee, slip = cfg.rules.taker_fee, cfg.slippage
    entry = c5[start]["o"] * (1 + slip)
    qty = cfg.start_equity / (entry * (1 + fee))
    pts = [(cfg.start_equity + qty * (c["l"] - entry), cfg.start_equity + qty * (c["c"] - entry))
           for c in c5[start:end]]
    final = qty * c5[end - 1]["c"] * (1 - slip) * (1 - fee)
    return {"retorno_liquido_pct": (final / cfg.start_equity - 1) * 100,
            "drawdown_max_pct": max_drawdown(pts) * 100, "saldo_final": final}


def regimes(c5, start, end, trades):
    """Per calendar month: BTC move (alta > +5%, queda < -5%, else lateral) and strategy PnL."""
    months = {}
    for c in c5[start:end]:
        key = dt.datetime.fromtimestamp(c["t"], dt.timezone.utc).strftime("%Y-%m")
        m = months.setdefault(key, {"open": c["o"], "close": c["c"]})
        m["close"] = c["c"]
    for t in trades:
        key = dt.datetime.fromtimestamp(t["exit_t"], dt.timezone.utc).strftime("%Y-%m")
        if key in months:
            months[key]["pnl"] = months[key].get("pnl", 0.0) + t["pnl"]
            months[key]["n"] = months[key].get("n", 0) + 1
    rows, summary = [], defaultdict(lambda: {"meses": 0, "pnl": 0.0, "operacoes": 0})
    for key in sorted(months):
        m = months[key]
        move = (m["close"] / m["open"] - 1) * 100
        regime = "alta" if move > 5 else "queda" if move < -5 else "lateral"
        rows.append({"mes": key, "btc_pct": move, "regime": regime, "pnl": m.get("pnl", 0.0), "operacoes": m.get("n", 0)})
        s = summary[regime]
        s["meses"] += 1
        s["pnl"] += m.get("pnl", 0.0)
        s["operacoes"] += m.get("n", 0)
    return rows, dict(summary)


def verdict(m, max_dd=0.10, require_profit=False):
    reasons = []
    if m["operacoes"] == 0:
        reasons.append("nenhuma operação executada")
    if m["fator_lucro"] <= 1:
        reasons.append("fator de lucro líquido %.2f <= 1" % m["fator_lucro"])
    if m["drawdown_max_pct"] > max_dd * 100:
        reasons.append("drawdown %.2f%% > %.0f%%" % (m["drawdown_max_pct"], max_dd * 100))
    if require_profit and m["retorno_liquido_pct"] < 0:
        reasons.append("perdeu dinheiro (%+.2f%%)" % m["retorno_liquido_pct"])
    if m.get("interrompida"):
        reasons.append("interrompida pela perda de 20%")
    passed = not reasons
    note = ""
    if passed and m["operacoes"] < 30:
        note = "amostra pequena (< 30 operações): resultado inconclusivo"
    return {"passou": passed, "motivos": reasons, "observacao": note}


def evaluate(c5, cfg, funding=None, oos_frac=0.3):
    if not c5:
        raise SystemExit("Sem dados.")
    ctx = Context(c5, cfg)
    first = cfg.warmup_5m
    if first >= len(c5) - 2:
        raise SystemExit("Dados insuficientes para aquecer os indicadores.")
    days = (c5[-1]["t"] - c5[first]["t"]) / 86400
    if days < MIN_DAYS:
        raise SystemExit("Período de teste de %.0f dias: são necessários pelo menos %d (12 meses)." % (days, MIN_DAYS))
    split = first + int((len(c5) - first) * (1 - oos_frac))
    out = {"config": cfg.__dict__ | {"rules": cfg.rules.__dict__}, "fingerprint": cfg.fingerprint(),
           "lacunas_5m": D.gaps(c5)}
    for name, a, b in (("desenvolvimento", first, split), ("fora_da_amostra", split, len(c5)),
                       ("completo", first, len(c5))):
        eng = run_segment(ctx, a, b, cfg, funding)
        m = metrics(eng, cfg.start_equity)
        rows, summary = regimes(c5, a, b, eng.trades)
        out[name] = {"inicio": _iso(c5[a]["t"]), "fim": _iso(c5[b - 1]["t"] + 300), "metricas": m,
                     "comprar_e_manter": buy_and_hold(c5, a, b, cfg), "regimes": summary, "meses": rows,
                     "veredito": verdict(m, cfg.max_dd_limit, cfg.require_oos_profit),
                     "meta_30d": _goal(eng, cfg) if cfg.name == "agressivo" else None}
    final_ok = out["fora_da_amostra"]["veredito"]["passou"] and out["completo"]["veredito"]["passou"]
    out["veredito_final"] = "PASSOU NO BACKTEST (seguir para 30+ dias de paper trading)" if final_ok \
        else "NÃO PASSOU NO TESTE"
    return out


def _goal(eng, cfg):
    """Best and worst 30-day rolling result vs the 50 -> 500 simulation goal. Information only."""
    pts = [(t, cl) for t, _, cl in eng.curve[::12]]
    best = worst = None
    j = 0
    for i, (t, v) in enumerate(pts):
        while pts[j][0] < t - 30 * 86400:
            j += 1
        if t - pts[j][0] >= 29 * 86400 and pts[j][1] > 0:
            r = v / pts[j][1]
            best = r if best is None else max(best, r)
            worst = r if worst is None else min(worst, r)
    target = AGGRESSIVE_GOAL_USD / cfg.start_equity
    return {"melhor_30d_x": best, "pior_30d_x": worst, "meta_x": target,
            "meta_atingida": bool(best and best >= target)}


def _iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def report(res, profile):
    L = []
    w = L.append
    w("=" * 72)
    w("BTC/USDT tendência + recuo (LONG) estratégia=%s perfil=%s config=%s"
      % (res["config"]["name"], profile, res["fingerprint"]))
    c = res["config"]
    w("Risco/op %.1f%% | alvo %.1fR | perda diária máx %.0f%% | entradas/dia %s | parada total em -%.0f%%"
      % (c["risk_per_trade"] * 100, c["reward_risk"], c["daily_loss_limit"] * 100,
         c["max_entries_per_day"] or "sem limite", c["kill_switch_drawdown"] * 100))
    r = res["config"]["rules"]
    w("Corretora simulada: %s | taxa %.3f%%/lado | slippage %.3f%%/lado | alavancagem máx %.1fx | funding=%s"
      % (r["name"], r["taker_fee"] * 100, res["config"]["slippage"] * 100,
         min(res["config"]["max_leverage"], r["max_leverage"]), "sim" if r["has_funding"] else "não"))
    w("Ordem mínima: %s BTC / %s USDT | lacunas nos dados: %d velas de 5m" % (r["min_qty"], r["min_notional"],
                                                                             res["lacunas_5m"]))
    for name in ("desenvolvimento", "fora_da_amostra", "completo"):
        s = res[name]
        m, bh, v = s["metricas"], s["comprar_e_manter"], s["veredito"]
        w("-" * 72)
        w("%s: %s -> %s" % (name.upper().replace("_", " "), s["inicio"], s["fim"]))
        pf = "inf" if m["fator_lucro"] == math.inf else "%.2f" % m["fator_lucro"]
        w("  Retorno líquido   %+8.2f%%   (comprar e manter BTC: %+8.2f%%)" % (m["retorno_liquido_pct"],
                                                                            bh["retorno_liquido_pct"]))
        w("  Drawdown máximo   %8.2f%%   (comprar e manter BTC: %8.2f%%)" % (m["drawdown_max_pct"],
                                                                           bh["drawdown_max_pct"]))
        w("  Operações %d | acerto %.1f%% | fator de lucro %s | seq. máx. perdas %d"
          % (m["operacoes"], m["taxa_acerto_pct"], pf, m["seq_max_perdas"]))
        w("  Ganho médio %.4f USD | perda média %.4f USD | taxas %.4f | funding %.4f"
          % (m["ganho_medio"], m["perda_media"], m["taxas_total"], m["funding_total"]))
        w("  Sinais %d | recusados %s | saídas %s" % (m["sinais"], m["sinais_recusados"] or "{}", m["saidas"] or "{}"))
        w("  Limite de perda diária atingido %d vez(es) | pior saldo %.2f USD%s"
          % (m["limite_diario_atingido"], m["pior_saldo"], " | INTERROMPIDA: " + m["interrompida"] if m["interrompida"] else ""))
        g = s.get("meta_30d")
        if g:
            fx = lambda x: "-" if x is None else "%.2fx" % x
            w("  Meta de simulação 50->500 (10x) em 30 dias: melhor janela %s, pior %s -> %s"
              % (fx(g["melhor_30d_x"]), fx(g["pior_30d_x"]), "atingida" if g["meta_atingida"] else "não atingida"))
        w("  Regimes: " + ", ".join("%s %d meses %d ops %+.4f USD" % (k, x["meses"], x["operacoes"], x["pnl"])
                                    for k, x in sorted(s["regimes"].items())))
        w("  Veredito: " + ("passou" if v["passou"] else "NÃO PASSOU: " + "; ".join(v["motivos"]))
          + (" — " + v["observacao"] if v["observacao"] else ""))
    w("=" * 72)
    w("RESULTADO: " + res["veredito_final"])
    w("Simulação histórica. Não é promessa de lucro. Nenhuma ordem real foi enviada.")
    return "\n".join(L)


def main(argv=None):
    assert_paper_only()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--months", type=int, default=18, help="meses de teste (mínimo 12)")
    ap.add_argument("--end", help="fim exclusivo YYYY-MM-DD (padrão: início do mês atual)")
    ap.add_argument("--oos", type=float, default=0.3, help="fração final reservada fora da amostra")
    ap.add_argument("--profile", choices=["futures", "spot", "both"], default="futures")
    ap.add_argument("--strategy", choices=list(PRESETS) + ["ambas"], default="ambas")
    ap.add_argument("--csv", help="CSV local de velas de 5m em vez do download")
    ap.add_argument("--json", help="salvar resultado completo em JSON")
    a = ap.parse_args(argv)
    if a.months < 12:
        ap.error("use pelo menos 12 meses")
    now = dt.datetime.now(dt.timezone.utc)
    end = dt.datetime.fromisoformat(a.end).replace(tzinfo=dt.timezone.utc) if a.end else \
        now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    results = {}
    names = list(PRESETS) if a.strategy == "ambas" else [a.strategy]
    for profile in (["futures", "spot"] if a.profile == "both" else [a.profile]):
        warm_days = math.ceil(preset("conservador").warmup_5m * 300 / 86400) + 2
        start = end - dt.timedelta(days=round(a.months * 30.44) + warm_days)
        market = "futures" if profile == "futures" else "spot"
        print("Carregando velas 5m (%s) %s -> %s ..." % (market, start.date(), end.date()), file=sys.stderr)
        c5 = D.load_csv(a.csv) if a.csv else D.load_klines_5m(start, end, market)
        funding = None
        if profile == "futures" and not a.csv:
            funding = D.load_funding(start, end) or None
            if funding is None:
                print("Sem histórico de funding: usando estimativa fixa.", file=sys.stderr)
        for name in names:
            res = evaluate(c5, preset(name, profile), funding, a.oos)
            print(report(res, profile))
            results[name + "_" + profile] = res
    if a.json:
        with open(a.json, "w") as f:
            json.dump(results, f, indent=1, default=lambda x: None if x == math.inf else str(x))
    return results


if __name__ == "__main__":
    main()
