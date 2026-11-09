"""Paper trading with live public BTC/USDT prices. Simulated orders only.

    python -m btc_trend_pullback.paper                                   # conservador, futures
    python -m btc_trend_pullback.paper --strategy ambas --profile both   # 4 contas simuladas
    python -m btc_trend_pullback.paper --report   # metrics + whether 30 days were reached
    python -m btc_trend_pullback.paper --unlock   # clear a safety lock after you checked the cause

Same Engine and rules as the backtest. No API key, no wallet, no real orders:
there is no code here that can send an order to an exchange.

Safety locks (block NEW entries, send an alert, need --unlock):
- 3 failed price requests in a row (connection failure);
- last closed candle older than 2 bars (stale price);
- duplicate order id or an entry while a position is already open;
- stop-loss not confirmed (the position is closed immediately).
The daily loss lock (2%) clears by itself on the next UTC day.
"""
import argparse
import json
import logging
import os
import sys
import time

import requests

from .backtest import metrics, verdict
from .config import PRESETS, assert_paper_only, preset
from .engine import Engine, SimBroker
from .strategy import BAR, Context

log = logging.getLogger("btc_paper")
STATE_DIR = os.getenv("BTC_PAPER_DIR", os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data_cache"))
MIN_PAPER_DAYS = 30
STALE_SECONDS = 2 * BAR + 60
MAX_FAILS = 3

URLS = {"futures": "https://fapi.binance.com/fapi/v1/klines", "spot": "https://api.binance.com/api/v3/klines"}
LIMIT = {"futures": 1500, "spot": 1000}


class BinanceFeed:
    """Closed 5m candles from Binance's public REST API (no key)."""

    def __init__(self, profile, keep, session=None):
        self.url, self.limit, self.keep = URLS[profile], LIMIT[profile], keep
        self.http = session or requests.Session()
        self.window = []

    def _get(self, **params):
        r = self.http.get(self.url, params={"symbol": "BTCUSDT", "interval": "5m", **params}, timeout=10)
        r.raise_for_status()
        return [{"t": int(x[0]) // 1000, "o": float(x[1]), "h": float(x[2]), "l": float(x[3]),
                 "c": float(x[4]), "v": float(x[5])} for x in r.json()]

    def closed_candles(self, now):
        if not self.window:
            rows, end = [], None
            while len(rows) < self.keep:
                params = {"limit": self.limit}
                if end:
                    params["endTime"] = end * 1000 - 1
                page = self._get(**params)
                if not page:
                    break
                rows = page + rows
                end = page[0]["t"]
        else:
            rows = self.window + self._get(limit=50)
        uniq = {c["t"]: c for c in rows if c["t"] + BAR <= now}   # drop the candle still forming
        self.window = [uniq[t] for t in sorted(uniq)][-self.keep:]
        return self.window


def make_alert(path):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("PAPER_ALERT_CHAT_ID")

    def alert(msg):
        line = time.strftime("%Y-%m-%d %H:%M:%S UTC ", time.gmtime()) + msg
        log.warning(msg)
        try:
            with open(path, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
        if token and chat:
            try:
                requests.post("https://api.telegram.org/bot%s/sendMessage" % token,
                              json={"chat_id": chat, "text": "[PAPER BTC] " + msg}, timeout=10)
            except requests.RequestException:
                pass
    return alert


class PaperTrader:
    def __init__(self, cfg, state_path=None, feed=None, alert=None, broker_fail_stop=False):
        self.cfg = cfg
        self.path = state_path or os.path.join(STATE_DIR, "paper_%s_%s.json" % (cfg.name, cfg.profile))
        self.alert = alert or make_alert(os.path.splitext(self.path)[0] + "_alerts.log")
        self.feed = feed or BinanceFeed(cfg.profile, keep=cfg.warmup_5m + 500)
        st = self._load()
        self.started_at = st.get("started_at")
        self.fails = st.get("fails", 0)
        self.broker = SimBroker(cfg, st.get("order_ids", []), fail_stop=broker_fail_stop)
        self.engine = Engine(cfg, self.broker, funding=None, alert=self.alert, state=st.get("engine"))

    def _load(self):
        try:
            with open(self.path) as f:
                st = json.load(f)
        except (OSError, ValueError):
            return {}
        if st.get("fingerprint") not in (None, self.cfg.fingerprint()):
            raise SystemExit("Os parâmetros mudaram desde o início do paper trading. "
                             "Apague %s para recomeçar a contagem de 30 dias." % self.path)
        return st

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        st = {"started_at": self.started_at, "fails": self.fails, "fingerprint": self.cfg.fingerprint(),
              "profile": self.cfg.profile, "order_ids": sorted(self.broker.used_ids), "engine": self.engine.state()}
        with open(self.path + ".tmp", "w") as f:
            json.dump(st, f)
        os.replace(self.path + ".tmp", self.path)

    def step(self, now=None):
        now = int(now or time.time())
        try:
            window = self.feed.closed_candles(now)
            if not window:
                raise ValueError("resposta vazia")
        except Exception as e:  # network, HTTP, bad JSON
            self.fails += 1
            log.warning("falha ao buscar preços (%d/%d): %s", self.fails, MAX_FAILS, e)
            if self.fails >= MAX_FAILS:
                self.engine.lock("falha de conexão com a fonte de preços")
            self.save()
            return
        self.fails = 0
        if now - (window[-1]["t"] + BAR) > STALE_SECONDS:
            self.engine.lock("preço desatualizado (última vela fechou há %ds)" % (now - window[-1]["t"] - BAR))
            self.save()
            return
        if self.started_at is None:
            # start fresh from the latest closed bar: no trades are invented on past data
            self.started_at = now
            self.engine.last_bar_t = window[-1]["t"]
            self.alert("Paper trading iniciado (perfil %s, saldo %.2f USD, LIVE_TRADING_ENABLED=false)."
                       % (self.cfg.profile, self.engine.cash))
            self.save()
            return
        ctx = Context(window, self.cfg)
        before = len(self.engine.trades), self.engine.pos
        for i, c in enumerate(window):
            if c["t"] > (self.engine.last_bar_t or 0):
                self.engine.on_bar(ctx, i)
        if not before[1] and self.engine.pos:
            p = self.engine.pos
            self.alert("Compra simulada %.5f BTC a %.2f | stop %.2f (confirmado) | alvo %.2f"
                       % (p["qty"], p["entry"], p["stop"], p["target"]))
        for t in self.engine.trades[before[0]:]:
            self.alert("Saída simulada (%s) a %.2f | resultado %+.4f USD | saldo %.2f"
                       % (t["reason"], t["exit"], t["pnl"], self.engine.cash))
        self.save()

    def report(self, now=None):
        now = now or time.time()
        days = (now - self.started_at) / 86400 if self.started_at else 0.0
        m = metrics(self.engine, self.cfg.start_equity)
        v = verdict(m, self.cfg.max_dd_limit, self.cfg.require_oos_profit)
        lines = ["Paper trading BTC/USDT [%s/%s] — %.1f de %d dias mínimos"
                 % (self.cfg.name, self.cfg.profile, days, MIN_PAPER_DAYS),
                 "Saldo %.2f USD (%+.2f%%) | drawdown máx %.2f%% | operações %d | acerto %.1f%% | fator de lucro %s"
                 % (m["saldo_final"], m["retorno_liquido_pct"], m["drawdown_max_pct"], m["operacoes"],
                    m["taxa_acerto_pct"], "%.2f" % m["fator_lucro"] if m["fator_lucro"] != float("inf") else "inf"),
                 "Ganho médio %.4f | perda média %.4f | seq. máx. perdas %d | sinais recusados %s"
                 % (m["ganho_medio"], m["perda_media"], m["seq_max_perdas"], m["sinais_recusados"] or "{}"),
                 "Posição aberta: %s" % (self.engine.pos or "nenhuma"),
                 "Bloqueio de segurança: %s | limite diário atingido %d vez(es) | interrompida: %s"
                 % (self.engine.lock_reason or "nenhum", m["limite_diario_atingido"], self.engine.halted or "não")]
        if days < MIN_PAPER_DAYS:
            lines.append("Ainda em teste: faltam %.1f dias." % (MIN_PAPER_DAYS - days))
        else:
            lines.append("Resultado: " + ("passou" if v["passou"] else "NÃO PASSOU: " + "; ".join(v["motivos"]))
                         + (" — " + v["observacao"] if v["observacao"] else ""))
        lines.append("Simulação. Nenhuma ordem real é enviada. Não é promessa de lucro.")
        return "\n".join(lines)


def main(argv=None):
    assert_paper_only()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", choices=["futures", "spot", "both"], default="futures")
    ap.add_argument("--strategy", choices=list(PRESETS) + ["ambas"], default="conservador")
    ap.add_argument("--state", help="arquivo de estado (só com uma estratégia e um perfil)")
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--unlock", action="store_true")
    a = ap.parse_args(argv)
    names = list(PRESETS) if a.strategy == "ambas" else [a.strategy]
    profiles = ["futures", "spot"] if a.profile == "both" else [a.profile]
    if a.state and len(names) * len(profiles) > 1:
        ap.error("--state só funciona com uma estratégia e um perfil")
    traders = [PaperTrader(preset(n, p), a.state) for n in names for p in profiles]
    if a.report:
        print("\n\n".join(t.report() for t in traders))
        return
    if a.unlock:
        for t in traders:
            print("[%s/%s] bloqueio removido: %s" % (t.cfg.name, t.cfg.profile, t.engine.lock_reason))
            t.engine.lock_reason = None
            t.save()
        return
    while True:
        for t in traders:
            t.step()
        if a.once:
            print("\n\n".join(t.report() for t in traders))
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
