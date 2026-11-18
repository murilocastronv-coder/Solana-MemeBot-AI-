"""Results of the two demo wallets (owner, Nov 18): the big-coins robot and the sniper each have a fictitious wallet;
the balance is saved over time (curve) so the owner sees whether it grows: balance now, balance per day, and a chart."""
import io
import time

CURVE_EVERY_S = 3600          # one point per hour
CURVE_MAX = 24 * 45


def snapshot(d, value, now=None):
    """Adds (time, balance) to d['curve'] at most once an hour. Returns True when a point was added."""
    now = now or time.time()
    curve = list(d.get("curve") or [])
    if curve and now - curve[-1][0] < CURVE_EVERY_S:
        curve[-1] = [curve[-1][0], round(value, 2)] if now - curve[-1][0] < 60 else curve[-1]
        d["curve"] = curve
        return False
    curve.append([int(now), round(value, 2)])
    d["curve"] = curve[-CURVE_MAX:]
    return True


def per_day(d, start_usd, now=None):
    """[(day number, balance at the end of that day)] since the demo started."""
    now = now or time.time()
    started = float(d.get("started") or now)
    out = {}
    for t, v in d.get("curve") or []:
        out[int((t - started) // 86400)] = v
    return sorted(out.items())


def wallet_text(name, d, value, now=None):
    now = now or time.time()
    start = float(d.get("start_usd") or 200)
    days = (now - float(d.get("started") or now)) / 86400
    h = d.get("hist") or []
    wins = sum(1 for x in h if x.get("pnl", 0) > 0)
    L = ["👛 " + name + " — " + ("🟢 ligada" if d.get("on") else "🔴 desligada"),
         "💰 Saldo: US$ " + format(value, ".2f") + " (começou com US$ " + format(start, "g") + ") → " +
         format(value - start, "+.2f") + " USD (" + format((value / start - 1) * 100, "+.1f") + "%)",
         "📅 Dia " + format(min(days, 999), ".0f") + " de 30 · vendas " + str(len(h)) + " · no lucro " + str(wins) +
         (" (" + format(wins / len(h) * 100, ".0f") + "%)" if h else "") + " · abertas " + str(len(d.get("pos") or {}))]
    days_list = per_day(d, start, now)[-7:]
    if days_list:
        L.append("📈 Saldo por dia: " + " · ".join("dia " + str(k) + ": " + format(v, ".2f") for k, v in days_list))
    if days >= 30:
        L.append("✅ Teste de 1 mês completo: " + ("GANHOU " if value > start else "PERDEU ") + format(abs(value - start), ".2f") + " USD")
    return "\n".join(L)


def chart_png(series, now=None):
    """Line chart of the balances [(name, start_usd, [[t, v], ...], color)] as PNG bytes (Pillow)."""
    from PIL import Image, ImageDraw
    import video_bot
    W, H, PAD = 1000, 600, 70
    img = Image.new("RGB", (W, H), (18, 20, 30))
    d = ImageDraw.Draw(img)
    pts = [(t, v) for _, start, c, _ in series for t, v in (c or [[time.time(), start]])] + [(0, s) for _, s, _, _ in series]
    ts = [t for t, _ in pts if t] or [time.time()]
    vs = [v for _, v in pts]
    t0, t1 = min(ts), max(max(ts), min(ts) + 3600)
    lo, hi = min(vs), max(vs)
    if hi - lo < 1:
        lo, hi = lo - 5, hi + 5
    X = lambda t: PAD + (t - t0) / (t1 - t0) * (W - 2 * PAD)
    TOP = 125                                       # room for the title and the legend
    Y = lambda v: H - PAD - (v - lo) / (hi - lo) * (H - PAD - TOP)
    f = video_bot._font(22, False)
    fb = video_bot._font(26)
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        d.line([(PAD, Y(v)), (W - PAD, Y(v))], fill=(50, 54, 70))
        d.text((8, Y(v) - 12), format(v, ".0f"), font=f, fill=(170, 170, 190))
    d.text((PAD, 15), "Saldo das carteiras de teste (US$)", font=fb, fill=(255, 255, 255))
    for i, (name, start, curve, color) in enumerate(series):
        c = curve or [[int(time.time()), start]]
        d.line([(PAD, Y(start)), (W - PAD, Y(start))], fill=tuple(x // 3 for x in color), width=1)
        xy = [(X(t), Y(v)) for t, v in c]
        if len(xy) == 1:
            xy = [(PAD, xy[0][1]), xy[0]]
        d.line(xy, fill=color, width=4)
        d.text((PAD + i * 430, 62), "■ " + name + ": " + format(c[-1][1], ".2f"), font=f, fill=color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def results(chat, now=None):
    """(text, png) with both demo wallets."""
    import big_bot
    import sniper_demo
    now = now or time.time()
    bd = big_bot.demo(chat)
    sd = sniper_demo.state(chat)
    sig = big_bot.cached_signals() or []
    bprices = {m: px for _, m, px, _, _ in sig if px}
    bval = big_bot.demo_value(bd, bprices)
    sval = sniper_demo.value(sd, {})
    text = ("📊 RESULTADOS DAS CARTEIRAS DE TESTE (dinheiro de mentira, preços reais)\n\n" +
            wallet_text("Carteira fictícia ROBÔ MOEDAS GRANDES", bd, bval, now) + "\n\n" +
            wallet_text("Carteira fictícia SNIPER", sd, sval, now) +
            "\n\nO gráfico mostra o saldo de cada carteira hora a hora; a linha fraca é o valor inicial (acima dela = lucro).")
    png = chart_png([("Robô moedas grandes", float(bd.get("start_usd") or 200), bd.get("curve"), (90, 200, 255)),
                     ("Sniper", float(sd.get("start_usd") or 200), sd.get("curve"), (255, 190, 60))], now)
    return text, png
