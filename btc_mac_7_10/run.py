"""Reproduce and stress-test MAc(7,10).

    python -m btc_mac_7_10.run --wfa results/wfa_equity.csv          # 2019-2022 from the project
    python -m btc_mac_7_10.run --wfa ... --binance-from 2023-01-01    # + true out-of-sample (Binance)

Nothing is optimised: the rule, band and costs are the project's; the risk controls
(stop 10%, risk 2%/trade, daily limit 6%, leverage <= 2x) were fixed before running.
"""
import argparse
import datetime as dt
import json
import math
import os

from . import data, rule, sim

CONFIGS = [
    sim.SimConfig(mode="original", leverage=1.0),
    sim.SimConfig(mode="original", leverage=2.0),
    sim.SimConfig(mode="original", leverage=1.0, long_only=True),
    sim.SimConfig(mode="controlado", leverage=2.0),
    sim.SimConfig(mode="controlado", leverage=2.0, long_only=True),
]


def _f(x, fmt="%.2f"):
    return "inf" if x == math.inf else fmt % x


def reproduce(years):
    lines = ["REPRODUÇÃO DO BACKTEST ORIGINAL (fórmula do projeto, custo 10 bps/lado)",
             "ano   trades  retorno     publicado   comprar e manter"]
    ok = True
    for y in sorted(years):
        r = rule.original_backtest([b["c"] for b in years[y]])
        pub = rule.PUBLISHED.get(y)
        same = pub and pub[0] == r["trades"] and abs(pub[1] - r["net_return_pct"]) < 0.01
        ok &= bool(same)
        lines.append("%s  %5d  %+9.2f%%  %s  %+9.2f%%  %s" % (
            y, r["trades"], r["net_return_pct"], ("%+9.2f%%" % pub[1]) if pub else "     -    ",
            r["bh_return_pct"], "✔" if same else "✘"))
    lines.append("Reprodução " + ("idêntica aos resultados publicados." if ok else "DIFERENTE do publicado."))
    return "\n".join(lines), ok


def evaluate(name, candles, funding=None):
    out = []
    for cfg in CONFIGS:
        res = sim.run(candles, cfg, funding)
        m = sim.metrics(res, candles, cfg)
        out.append({"periodo": name, "config": cfg.label(), "metricas": m, "veredito": sim.verdict(m)})
    return out


def table(rows):
    L = ["%-22s %-30s %9s %8s %5s %6s %6s %4s %5s %10s %8s %9s" % (
        "período", "configuração", "retorno", "DD máx", "ops", "acerto", "FL", "seq", "liq", "500 em 30d",
        "P(-90%)", "B&H")]
    for r in rows:
        m = r["metricas"]
        g = m["meta_30d"]
        L.append("%-22s %-30s %+8.1f%% %7.1f%% %5d %5.1f%% %6s %4d %5d %4d/%-5d %7.1f%% %+8.1f%%" % (
            r["periodo"], r["config"], m["retorno_liquido_pct"], m["drawdown_max_pct"], m["operacoes"],
            m["taxa_acerto_pct"], _f(m["fator_lucro"]), m["pior_seq_perdas"], m["liquidacoes"], g["atingiu"],
            g["janelas"], m["risco_ruina"]["p_perder_90"] * 100, m["bh_retorno_pct"]))
    return "\n".join(L)


def main(argv=None):
    from btc_trend_pullback.config import assert_paper_only
    assert_paper_only()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wfa", default=os.path.join("data_cache", "wfa_equity.csv"))
    ap.add_argument("--binance-from", help="início do teste fora da amostra real, ex.: 2023-01-01")
    ap.add_argument("--binance-to", help="fim (padrão: início do mês atual)")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    years = data.load_wfa_years(a.wfa)
    text, ok = reproduce(years)
    print(text + "\n")
    rows = []
    for y in sorted(years):
        rows += evaluate(y + " (seleção)", years[y])
    rows += evaluate("2019-2022 (seleção)", data.chain(years))
    if a.binance_from:
        start = dt.datetime.fromisoformat(a.binance_from).replace(tzinfo=dt.timezone.utc)
        now = dt.datetime.now(dt.timezone.utc)
        end = dt.datetime.fromisoformat(a.binance_to).replace(tzinfo=dt.timezone.utc) if a.binance_to else \
            now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        c5, funding = data.load_binance(start, end)
        rows += evaluate("%s→%s (fora da amostra)" % (start.date(), end.date()), c5, funding)
    print("SIMULAÇÃO REALISTA, saldo inicial US$ 50 (taxa 10 bps + slippage 5 bps por lado, funding 8h)")
    print(table(rows))
    oos = [r for r in rows if "fora da amostra" in r["periodo"]]
    print()
    if not oos:
        print("VEREDITO: NÃO APROVADA. 2019-2022 não é fora da amostra (a regra foi escolhida entre 3.669 usando "
              "2017-2022 inteiro). Rode com --binance-from 2023-01-01 para o teste fora da amostra real.")
    else:
        for r in oos:
            v = r["veredito"]
            print("%-30s %s" % (r["config"], "passou" if v["passou"] else "NÃO PASSOU: " + "; ".join(v["motivos"])))
    print("Simulação histórica. Não é promessa de lucro. Nenhuma ordem real foi enviada.")
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"reproducao_ok": ok, "resultados": rows}, f, indent=1, default=str)
    return rows


if __name__ == "__main__":
    main()
