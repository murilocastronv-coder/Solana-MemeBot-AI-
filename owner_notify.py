"""Owner notifications and visitor stats (owner, Nov 18): the owner's main account hears about every new person
who opens the bot, every deposit, buy, sell and withdrawal of the users, and sees visitors in the owner panel.

telegram_bot calls setup(send, owner_chat_fn) at startup. Stats live in pg_store key "bot_stats"."""
import json
import logging
import threading
import time

KEY = "bot_stats"
_LOCK = threading.RLock()
_MEM = {}
_SEND = {"send": None, "owner": None}


def setup(send, owner_chat_fn):
    _SEND.update(send=send, owner=owner_chat_fn)


def notify(text):
    try:
        owner = _SEND["owner"]() if _SEND["owner"] else None
        if owner and _SEND["send"]:
            _SEND["send"](owner, text)
    except Exception as exc:
        logging.info("owner notify: %s", type(exc).__name__)


def _load():
    with _LOCK:
        if not _MEM:
            d = {"users": {}, "days": {}}
            try:
                import pg_store
                if pg_store.enabled():
                    d.update(pg_store.get(KEY) or {})
            except Exception:
                pass
            _MEM.update(d)
        return _MEM


def _save():
    try:
        import pg_store
        if pg_store.enabled():
            pg_store.put(KEY, json.loads(json.dumps(_MEM)))
    except Exception as exc:
        logging.warning("bot stats save: %s", type(exc).__name__)


def who(chat):
    u = (_load()["users"].get(str(chat)) or {})
    name = u.get("name") or "sem nome"
    return name + (" (@" + u["username"] + ")" if u.get("username") else "") + " · id " + str(chat)


def visit(chat, sender=None, now=None):
    """Count a visit of a non-owner account; tells the owner the first time a person opens the bot."""
    now = now or time.time()
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    sender = sender or {}
    with _LOCK:
        d = _load()
        key = str(chat)
        new = key not in d["users"]
        u = d["users"].setdefault(key, {"first": now, "visits": 0})
        u["visits"] = int(u.get("visits", 0)) + 1
        u["last"] = now
        name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x).strip()
        if name:
            u["name"] = name[:60]
        if sender.get("username"):
            u["username"] = str(sender["username"])[:40]
        dd = d["days"].setdefault(day, {"visitors": [], "new": 0})
        first_today = key not in dd["visitors"]
        if first_today:
            dd["visitors"].append(key)
        if new:
            dd["new"] = int(dd.get("new", 0)) + 1
        for old in sorted(d["days"])[:-30]:
            d["days"].pop(old, None)
        if new or first_today or now - float(u.get("saved", 0)) > 3600:
            u["saved"] = now
            _save()                                   # not on every button tap
        total = len(d["users"])
    if new:
        notify("👤 NOVA PESSOA NO BOT: " + who(chat) + "\nTotal de pessoas: " + str(total) + " · hoje: " + str(len(dd["visitors"])))
    return new


def summary(now=None):
    now = now or time.time()
    d = _load()
    today = d["days"].get(time.strftime("%Y-%m-%d", time.gmtime(now))) or {}
    week = set()
    for day in sorted(d["days"])[-7:]:
        week |= set(d["days"][day].get("visitors") or [])
    return ("👀 Pessoas que abriram o bot: " + str(len(d["users"])) + " no total · hoje " + str(len(today.get("visitors") or [])) +
            " (" + str(today.get("new", 0)) + " novas) · últimos 7 dias " + str(len(week)))


# ---------------------------------------------------------------- deposits
def check_deposits(chats=None, balance=None, now=None):
    """Tells the owner when a user's wallet got SOL that no trade of the bot explains (a deposit)."""
    import user_trade as ut
    import user_copy as uc
    import user_wallets
    if chats is None:                                  # every account with a wallet (not only those that traded)
        folder = user_wallets._base_dir() / "user_wallets"
        chats = [f.stem for f in folder.glob("*.json")] if folder.is_dir() else []
    found = []
    for chat in chats:
        try:
            address = user_wallets.get_address(chat)
            if not address:
                continue
            cur = (balance or uc.sol_balance)(address)
            d0 = ut.load(chat)
            last = d0.get("last_sol")
            if float(d0.get("busy_until") or 0) > time.time():
                ut.update(chat, lambda d, cur=cur: d.__setitem__("last_sol", int(cur)))
                continue                              # a trade still being confirmed: not a deposit
            ut.update(chat, lambda d, cur=cur: d.__setitem__("last_sol", int(cur)))
            if last is not None and cur - int(last) >= 1_000_000:          # 0.001 SOL or more
                found.append((chat, cur - int(last)))
                notify("📥 DEPÓSITO: " + who(chat) + " depositou " + format((cur - int(last)) / 1e9, ".4f") +
                       " SOL\nSaldo agora: " + format(cur / 1e9, ".4f") + " SOL")
        except Exception as exc:
            logging.info("deposit check %s: %s", chat, type(exc).__name__)
    return found


def mark_balance(chat):
    """After the bot's own buy / sell / withdrawal: the new balance is not a deposit."""
    try:
        import user_trade as ut
        import user_copy as uc
        import user_wallets
        cur = uc.sol_balance(user_wallets.get_address(chat))
        ut.update(chat, lambda d: d.__setitem__("last_sol", int(cur)))
    except Exception:
        pass
