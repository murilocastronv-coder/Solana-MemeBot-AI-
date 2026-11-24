"""PARIS SPORTIFS (owner, Nov 24): football value-betting signals in Telegram + a 1-year backtest.

Strategy ("value betting" against the sharpest bookmaker):
- Pinnacle's odds are the most accurate public estimate of each result. Removing their margin (no-vig) gives the FAIR
  probability of home win / draw / away win and of over / under 2.5 goals.
- A bet is a VALUE bet when some bookmaker pays more than the fair odds: price x fair probability - 1 >= EDGE (3%).
- Stake: a quarter of the Kelly fraction, never more than 2% of the bank.
- Only odds from MIN_ODDS to MAX_ODDS (long shots lose the edge to variance).

Data: football-data.co.uk (results + odds of the main European leagues; fixtures.csv has the next matches with odds).
The owner places the bets himself (bookmakers forbid robots and close accounts that use them): every signal says the
match, the bet and the MINIMUM odds - bet only if his bookmaker pays at least that.

Nothing here is a guarantee: even a real 3% edge loses for weeks at a time."""
import csv
import io
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

LEAGUES = {"E0": "🏴 Premier League", "E1": "🏴 Championship", "SP1": "🇪🇸 La Liga", "I1": "🇮🇹 Serie A",
           "D1": "🇩🇪 Bundesliga", "F1": "🇫🇷 Ligue 1", "N1": "🇳🇱 Eredivisie", "P1": "🇵🇹 Primeira Liga",
           "B1": "🇧🇪 Jupiler", "T1": "🇹🇷 Süper Lig", "SC0": "🏴 Scottish Prem"}
SEASON_URL = "https://www.football-data.co.uk/mmz4281/{season}/{div}.csv"
FIXTURES_URL = "https://www.football-data.co.uk/fixtures.csv"
EDGE = float(os.getenv("PARIS_EDGE", "0.03"))            # minimum value: price x fair probability - 1
MIN_ODDS = float(os.getenv("PARIS_MIN_ODDS", "1.5"))
MAX_ODDS = float(os.getenv("PARIS_MAX_ODDS", "6.0"))
KELLY = 0.25                                            # quarter Kelly
MAX_STAKE = 0.02                                        # never more than 2% of the bank on one bet
BANK_CHOICES = (50, 100, 200, 500, 1000)
CHECK_EVERY_S = 3 * 3600                                # look for new signals every 3 h
AHEAD_DAYS = 3                                          # signals for matches in the next 3 days
NAMES = {"H": "vitória do mandante", "D": "empate", "A": "vitória do visitante", "O": "mais de 2,5 gols",
         "U": "menos de 2,5 gols"}
_LOCK = threading.RLock()
BT = {"running": False}
PHOTO = None                                            # telegram_bot.send_photo


# ------------------------------------------------------------------ storage
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("paris_bot.json")


def load():
    d = {"on": False, "bank": 100.0, "sent": [], "last_check": 0}
    try:
        p = _path()
        if p.exists():
            d.update(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError, RuntimeError):
        pass
    return d


def save(d):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".paris.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


# ------------------------------------------------------------------ data
def _get(url):
    r = requests.get(url, timeout=20, headers={"User-Agent": "ParisBot/1.0"})
    r.raise_for_status()
    raw = r.content
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def parse_csv(text):
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        r = {(k or "").strip().lstrip("﻿"): (v or "").strip() for k, v in r.items() if k}
        if r.get("HomeTeam") and r.get("AwayTeam") and r.get("Date"):
            rows.append(r)
    return rows


def parse_date(r):
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%y %H:%M", "%d/%m/%Y", "%d/%m/%y"):
        try:
            s = r["Date"] + (" " + r["Time"] if r.get("Time") and "%H" in fmt else "")
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
    return None


def fl(r, *keys):
    for k in keys:
        try:
            v = float(r.get(k, "") or "nan")
            if v > 1.0:
                return v
        except ValueError:
            continue
    return None


def season_code(d):
    y = d.year if d.month >= 7 else d.year - 1
    return "%02d%02d" % (y % 100, (y + 1) % 100)


def seasons_for(start, end):
    out, d = [], start
    while d <= end + timedelta(days=200):
        c = season_code(d)
        if c not in out:
            out.append(c)
        d += timedelta(days=150)
    return [c for c in out if c <= season_code(end)]


def history(days=365, get=None, now=None):
    """Finished matches of the last `days` days with odds, all leagues, oldest first."""
    get = get or _get
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    rows = []
    for season in seasons_for(start, end):
        for div in LEAGUES:
            try:
                for r in parse_csv(get(SEASON_URL.format(season=season, div=div))):
                    d = parse_date(r)
                    if d and start <= d <= end and r.get("FTHG", "") != "":
                        r["_date"], r["_div"] = d, div
                        rows.append(r)
            except Exception as exc:
                logging.info("paris history %s %s: %s", season, div, type(exc).__name__)
    rows.sort(key=lambda r: r["_date"])
    return rows


# ------------------------------------------------------------------ strategy
def fair_probs(odds):
    """No-vig probabilities from a bookmaker's odds (proportional method)."""
    if not odds or any(o is None or o <= 1.0 for o in odds):
        return None
    inv = [1.0 / o for o in odds]
    s = sum(inv)
    return [x / s for x in inv]


def candidates(r, price="max"):
    """The value bets of one match: [(key, fair_prob, price, edge)] (at most one 1X2 and one goals bet)."""
    out = []
    pre = {"max": ("Max", "BbMx"), "avg": ("Avg", "BbAv")}[price]
    p1x2 = fair_probs([fl(r, "PSH", "PH"), fl(r, "PSD", "PD"), fl(r, "PSA", "PA")])
    if p1x2:
        best = None
        for key, p, side in zip("HDA", p1x2, "HDA"):
            o = fl(r, pre[0] + side, pre[1] + side)
            if o and MIN_ODDS <= o <= MAX_ODDS:
                e = o * p - 1
                if e >= EDGE and (best is None or e > best[3]):
                    best = (key, p, o, e)
        if best:
            out.append(best)
    pou = fair_probs([fl(r, "P>2.5", "PC>2.5"), fl(r, "P<2.5", "PC<2.5")])
    if pou:
        best = None
        for key, p, side in zip("OU", pou, (">2.5", "<2.5")):
            o = fl(r, pre[0] + side, pre[1] + side)
            if o and MIN_ODDS <= o <= MAX_ODDS:
                e = o * p - 1
                if e >= EDGE and (best is None or e > best[3]):
                    best = (key, p, o, e)
        if best:
            out.append(best)
    return out


def stake_frac(p, o):
    k = (o * p - 1) / (o - 1)
    return max(0.0, min(MAX_STAKE, k * KELLY))


def won(r, key):
    try:
        h, a = int(float(r["FTHG"])), int(float(r["FTAG"]))
    except (KeyError, ValueError):
        return None
    return {"H": h > a, "D": h == a, "A": h < a, "O": h + a > 2, "U": h + a < 3}[key]


def backtest(rows, bank=100.0, price="max"):
    """Every value bet of the period, quarter-Kelly stakes on the running bank (bets of the same day share it)."""
    start = bank
    curve, bets, peak, dd = [], [], bank, 0.0
    by_month, by_league = {}, {}
    day, day_bank = None, bank
    for r in rows:
        d = r["_date"].date()
        if d != day:
            day, day_bank = d, bank
        for key, p, o, e in candidates(r, price):
            w = won(r, key)
            if w is None:
                continue
            st = round(day_bank * stake_frac(p, o), 2)
            if st <= 0:
                continue
            res = st * (o - 1) if w else -st
            bank += res
            m = r["_date"].strftime("%Y-%m")
            by_month[m] = by_month.get(m, 0.0) + res
            by_league[r["_div"]] = by_league.get(r["_div"], 0.0) + res
            bets.append({"t": r["_date"].timestamp(), "key": key, "odds": o, "edge": e, "stake": st, "res": res, "won": w})
            peak = max(peak, bank)
            dd = max(dd, (peak - bank) / peak if peak else 0)
            curve.append([int(r["_date"].timestamp()), round(bank, 2)])
    staked = sum(b["stake"] for b in bets)
    return {"start": start, "bank": round(bank, 2), "profit": round(bank - start, 2), "bets": len(bets),
            "wins": sum(1 for b in bets if b["won"]), "staked": round(staked, 2),
            "roi": (bank - start) / staked * 100 if staked else 0.0, "max_dd": dd * 100, "curve": curve,
            "by_month": by_month, "by_league": by_league,
            "avg_odds": sum(b["odds"] for b in bets) / len(bets) if bets else 0.0,
            "matches": len(rows)}


def backtest_text(rmax, ravg, days=365):
    def line(r):
        return ((("✅ LUCRO " if r["profit"] > 0 else "❌ PREJUÍZO ") + format(r["profit"], "+.2f") + " € (banca " +
                 format(r["start"], ".0f") + " → " + format(r["bank"], ".2f") + ")") +
                "\n   " + str(r["bets"]) + " apostas · acerto " + (format(r["wins"] / r["bets"] * 100, ".0f") + "%" if r["bets"] else "-") +
                " · odd média " + format(r["avg_odds"], ".2f") + " · ROI " + format(r["roi"], "+.1f") + "% · queda máxima " +
                format(r["max_dd"], ".0f") + "%")
    L = ["🧪 BACKTEST " + str(days) + " DIAS — PARIS SPORTIFS (futebol, " + str(len(LEAGUES)) + " ligas, " + str(rmax["matches"]) + " jogos)",
         "Estratégia: aposta quando a odd paga é pelo menos " + format(EDGE * 100, ".0f") + "% maior que a odd JUSTA da Pinnacle "
         "(sem a margem dela) · 1X2 e mais/menos 2,5 gols · odds de " + format(MIN_ODDS, "g") + " a " + format(MAX_ODDS, "g") +
         " · stake ¼ Kelly, no máximo " + format(MAX_STAKE * 100, ".0f") + "% da banca.", "",
         "1) Apostando na MELHOR odd do mercado (comparando casas):", line(rmax), "",
         "2) Apostando na odd MÉDIA (uma casa só, sem comparar):", line(ravg), ""]
    if rmax["by_month"]:
        L.append("Por mês (melhor odd): " + " · ".join(m[5:] + "/" + m[2:4] + " " + format(v, "+.0f")
                                                         for m, v in sorted(rmax["by_month"].items())))
    if rmax["by_league"]:
        L.append("Por liga (melhor odd): " + " · ".join(LEAGUES.get(k, k).split(" ", 1)[-1] + " " + format(v, "+.0f")
                                                         for k, v in sorted(rmax["by_league"].items(), key=lambda kv: -kv[1])))
    L += ["", "⚠️ O resultado depende de pegar a odd alta: comparar casas (ou usar a que paga mais) é o que faz o lucro. "
          "O passado não garante o futuro; com apostas sempre há semanas no vermelho."]
    return "\n".join(L)


def run_backtest(send, chat, kb=None, days=365):
    if BT["running"]:
        send(chat, "⏳ O backtest já está rodando, espera o resultado.", kb)
        return
    BT["running"] = True
    try:
        send(chat, "🧪 Backtest de 1 ano começou: baixando resultados e odds de " + str(len(LEAGUES)) +
             " ligas de futebol (football-data.co.uk). Leva 1–3 minutos.")
        rows = history(days)
        if not rows:
            send(chat, "⛔ Não consegui baixar os dados dos jogos agora. Tente de novo mais tarde.", kb)
            return
        bank = load()["bank"]
        rmax, ravg = backtest(rows, bank, "max"), backtest(rows, bank, "avg")
        try:
            import demo_report
            png = demo_report.chart_png([("Melhor odd", bank, rmax["curve"], (90, 200, 255)),
                                         ("Odd média", bank, ravg["curve"], (255, 196, 0))])
            if png and PHOTO:
                PHOTO(chat, png, "📈 Backtest 1 ano: banca ao longo do tempo (azul = melhor odd, amarelo = odd média)")
        except Exception as exc:
            logging.info("paris chart: %s", type(exc).__name__)
        send(chat, backtest_text(rmax, ravg, days), kb)
    except Exception as exc:
        send(chat, "⛔ Backtest: " + str(exc)[:200], kb)
    finally:
        BT["running"] = False


# ------------------------------------------------------------------ live signals
def upcoming(get=None, now=None):
    get = get or _get
    now = now or datetime.now(timezone.utc)
    rows = []
    for r in parse_csv(get(FIXTURES_URL)):
        d = parse_date(r)
        if r.get("Div") in LEAGUES and d and now <= d <= now + timedelta(days=AHEAD_DAYS):
            r["_date"], r["_div"] = d, r["Div"]
            rows.append(r)
    rows.sort(key=lambda r: r["_date"])
    return rows


def signal_id(r, key):
    return r["_date"].strftime("%Y%m%d") + ":" + r["HomeTeam"] + ":" + r["AwayTeam"] + ":" + key


def signal_text(r, key, p, o, e, bank):
    fair = 1 / p
    min_odds = fair * (1 + EDGE)
    st = bank * stake_frac(p, o)
    return (LEAGUES.get(r["_div"], r["_div"]) + " · " + r["_date"].strftime("%d/%m %H:%M") + " UTC\n"
            "⚽ " + r["HomeTeam"] + " x " + r["AwayTeam"] + "\n"
            "🎯 Aposta: " + NAMES[key] + "\n"
            "💶 Odd MÍNIMA: " + format(min_odds, ".2f") + " (justa " + format(fair, ".2f") + " · melhor no mercado " +
            format(o, ".2f") + ", valor " + format(e * 100, "+.1f") + "%)\n"
            "💰 Stake sugerida: " + format(st, ".2f") + " € (" + format(st / bank * 100, ".1f") + "% da banca)\n"
            "Só aposte se a sua casa pagar a odd mínima ou mais.")


def find_signals(d, get=None, now=None):
    out = []
    for r in upcoming(get, now):
        for key, p, o, e in candidates(r, "max"):
            sid = signal_id(r, key)
            if sid not in d["sent"]:
                out.append((sid, signal_text(r, key, p, o, e, d["bank"])))
    return out


def cycle(send, chat, now=None, get=None, force=False):
    now_ts = now or time.time()
    with _LOCK:
        d = load()
        if not force and (not d["on"] or now_ts - d.get("last_check", 0) < CHECK_EVERY_S):
            return 0
        d["last_check"] = now_ts
        try:
            sig = find_signals(d, get, datetime.fromtimestamp(now_ts, timezone.utc))
        except Exception as exc:
            save(d)
            logging.info("paris signals: %s", type(exc).__name__)
            if force:
                send(chat, "⛔ Não consegui ler os próximos jogos agora (" + type(exc).__name__ + "). Tente mais tarde.", keyboard())
            return 0
        for sid, text in sig:
            send(chat, "⚽ SINAL DE VALOR\n" + text)
            d["sent"] = (d["sent"] + [sid])[-500:]
        save(d)
    if force and not sig:
        send(chat, "⚽ Nenhuma aposta de valor nos próximos " + str(AHEAD_DAYS) + " dias por enquanto. "
             "Eu procuro de novo a cada " + str(CHECK_EVERY_S // 3600) + " h.", keyboard())
    return len(sig)


# ------------------------------------------------------------------ Telegram
ACTIONS = {"pb", "/paris", "pb_on", "pb_off", "pb_now", "pb_bt", "pb_bank"}


def status_text():
    d = load()
    return "\n".join([
        "⚽ PARIS SPORTIFS — sinais de VALOR no futebol",
        "Status: " + ("🟢 sinais ligados (procuro a cada " + str(CHECK_EVERY_S // 3600) + " h)" if d["on"] else "🔴 sinais desligados"),
        "💶 Banca: " + format(d["bank"], ".0f") + " € (as stakes sugeridas são calculadas sobre ela)",
        "",
        "Como funciona: eu comparo as odds das casas com a odd JUSTA da Pinnacle (a casa mais precisa, sem a margem dela). "
        "Quando alguma casa paga pelo menos " + format(EDGE * 100, ".0f") + "% a mais que o justo, te mando o sinal: jogo, aposta, "
        "odd mínima e stake (¼ Kelly, no máximo " + format(MAX_STAKE * 100, ".0f") + "% da banca). Ligas: " +
        ", ".join(v.split(" ", 1)[-1] for v in LEAGUES.values()) + ". Mercados: 1X2 e mais/menos 2,5 gols.",
        "Você aposta na sua casa (as casas proíbem robôs). Só aposte se ela pagar a odd mínima.",
        "⚠️ Não existe lucro garantido: mesmo com vantagem real há semanas no vermelho. Aposte só o que pode perder."])


def keyboard():
    d = load()
    return [[{"text": "🔴 Desligar sinais" if d["on"] else "🟢 Ligar sinais", "callback_data": "pb_off" if d["on"] else "pb_on"},
             {"text": "🔎 Sinais agora", "callback_data": "pb_now"}],
            [{"text": "🧪 Backtest 1 ano", "callback_data": "pb_bt"},
             {"text": "💶 Banca " + format(d["bank"], ".0f") + " €", "callback_data": "pb_bank"}],
            [{"text": "🔄 Atualizar", "callback_data": "pb"}]]


def handle(action, send, chat, run_async):
    if action in ("pb_on", "pb_off"):
        with _LOCK:
            d = load()
            d["on"] = action == "pb_on"
            d["last_check"] = 0
            save(d)
        if d["on"]:
            send(chat, "🟢 Sinais ligados. Vou procurar agora e depois a cada " + str(CHECK_EVERY_S // 3600) + " h.")
            run_async(lambda: cycle(send, chat, force=True))
            return True
    elif action == "pb_now":
        send(chat, "🔎 Procurando apostas de valor nos próximos " + str(AHEAD_DAYS) + " dias…")
        run_async(lambda: cycle(send, chat, force=True))
        return True
    elif action == "pb_bt":
        run_async(lambda: run_backtest(send, chat, keyboard()))
        return True
    elif action == "pb_bank":
        with _LOCK:
            d = load()
            ch = list(BANK_CHOICES)
            d["bank"] = float(ch[(ch.index(int(d["bank"])) + 1) % len(ch)] if int(d["bank"]) in ch else ch[1])
            save(d)
    send(chat, status_text(), keyboard())
    return True
