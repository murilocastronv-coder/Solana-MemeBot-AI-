"""Copy trade for every Telegram account (not the owner), each with its own unique wallet.

- Signals: the same wallets the bot found and audited (copy_trader), with the same filters.
  When a copied wallet buys, every active account buys; when it sells, they sell.
- Each account chooses SIMULATION (fake US$ 100) or REAL (its own wallet from user_wallets).
- Fee: FEE_RATE (4%) of every buy and every sell, sent in SOL to the owner's main wallet
  (FEE_WALLET, default: the main bot wallet). The fee is only charged when the trade went
  through; a fee transfer that fails is kept as owed and retried. REAL needs the account to
  accept the fee first.
- The owner keeps trading with the main engine (copy_trader / live_engine), untouched here.
Never log private keys.
"""
import base64
import json
import os
import tempfile
import threading
import time

import user_wallets

SOL_MINT = "So11111111111111111111111111111111111111112"
FEE_RATE = float(os.getenv("USER_FEE_PERCENT", "4")) / 100
DEFAULT_ORDER_USD = float(os.getenv("USER_ORDER_USD", "10"))
ORDER_CHOICES = (5, 10, 20, 32, 50)
MAX_POSITIONS = int(os.getenv("USER_MAX_POSITIONS", "8"))
SOL_RESERVE_LAMPORTS = int(float(os.getenv("USER_SOL_RESERVE", "0.01")) * 1e9)  # network fees + token account
START_SIM_USD = 100.0
PENDING_GIVE_UP_S = 300
SELL_RETRY_S = 60

_LOCK = threading.RLock()
_USER_LOCKS = {}


def _user_lock(chat):
    with _LOCK:
        return _USER_LOCKS.setdefault(str(chat), threading.RLock())


# ---------------------------------------------------------------- storage
def _dir():
    return user_wallets._base_dir() / "user_copy"


def _path(chat):
    return _dir() / (user_wallets._chat(chat) + ".json")


def _default():
    return {"mode": "off", "terms_at": None, "order_usd": DEFAULT_ORDER_USD, "sim_balance": START_SIM_USD,
            "positions": {}, "history": [], "fees_owed_lamports": 0, "fees_paid_lamports": 0,
            "sim_fees_usd": 0.0}


def load(chat):
    path = _path(chat)
    d = _default()
    if path.exists():
        try:
            d.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return d


def save(chat, d):
    path = _path(chat)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def users():
    folder = _dir()
    return [f.stem for f in folder.glob("*.json")] if folder.is_dir() else []


def any_active():
    return any(load(c)["mode"] in ("sim", "real") for c in users())


def busy_wallets():
    """Copied wallets that still have an open position for some account: must stay watched."""
    out = set()
    for c in users():
        out |= {p.get("leader") for p in load(c)["positions"].values()}
    out.discard(None)
    return out


def fee_wallet():
    explicit = os.getenv("FEE_WALLET", "").strip()
    if explicit:
        return explicit
    import bot_wallet_service
    return bot_wallet_service.get_address()


# ---------------------------------------------------------------- settings
def accept_terms(chat):
    with _user_lock(chat):
        d = load(chat)
        d["terms_at"] = int(time.time())
        save(chat, d)


def set_mode(chat, mode):
    if mode not in ("off", "sim", "real"):
        raise ValueError("modo inválido")
    if user_wallets.is_owner(chat):
        raise ValueError("o dono usa o copy trade principal")
    with _user_lock(chat):
        d = load(chat)
        if mode == "real" and not d.get("terms_at"):
            raise ValueError("aceite a taxa antes de ligar o REAL")
        if mode == "real":
            user_wallets.create_or_get(chat)
        d["mode"] = mode
        save(chat, d)


def set_order_usd(chat, usd):
    usd = float(usd)
    if usd not in ORDER_CHOICES:
        raise ValueError("valor inválido")
    with _user_lock(chat):
        d = load(chat)
        d["order_usd"] = usd
        save(chat, d)


# ---------------------------------------------------------------- chain helpers
def _rpc(method, params):
    from live_swap_executor import rpc
    return rpc(method, params)


def sol_balance(address):
    res = _rpc("getBalance", [address, {"commitment": "confirmed"}])
    return int(res["value"] if isinstance(res, dict) else res)


def token_balance(address, mint):
    res = _rpc("getTokenAccountsByOwner", [address, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    return sum(int(i["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for i in (res or {}).get("value", []))


def _keypair(chat):
    from solders.keypair import Keypair
    address, secret = user_wallets._private_key(chat)
    kp = Keypair.from_base58_string(secret)
    if str(kp.pubkey()) != address:
        raise RuntimeError("wallet mismatch")
    return address, kp


def transfer_sol(kp, to_address, lamports, wait_s=45):
    """Send SOL (the fee) and wait until it is confirmed. Returns the signature or raises."""
    from solders.hash import Hash
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction
    if lamports <= 0:
        return None
    blockhash = Hash.from_string(_rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"])
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Pubkey.from_string(to_address), lamports=int(lamports)))
    tx = Transaction([kp], Message.new_with_blockhash([ix], kp.pubkey(), blockhash), blockhash)
    sig = _rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64"}])
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        st = (_rpc("getSignatureStatuses", [[sig]]) or {}).get("value", [None])[0]
        if st:
            if st.get("err") is not None:
                raise RuntimeError("fee transfer failed on-chain")
            if st.get("confirmationStatus") in ("confirmed", "finalized"):
                return sig
        time.sleep(2)
    raise TimeoutError("fee transfer not confirmed yet")


def _sign(order_data, kp, address):
    """Sign a Jupiter v2 order as exactly one required signer (same rule as the main engine)."""
    from solders.message import to_bytes_versioned
    from solders.transaction import VersionedTransaction
    unsigned = VersionedTransaction.from_bytes(base64.b64decode(order_data["transaction"], validate=True))
    keys = unsigned.message.account_keys
    required = int(unsigned.message.header.num_required_signatures)
    idx = [i for i in range(required) if str(keys[i]) == address]
    if len(idx) != 1:
        raise RuntimeError("wallet must be exactly one required signer")
    sigs = list(unsigned.signatures)
    sigs[idx[0]] = kp.sign_message(to_bytes_versioned(unsigned.message))
    return base64.b64encode(bytes(VersionedTransaction.populate(unsigned.message, sigs))).decode("ascii")


def quote_order(chat, input_mint, output_mint, amount):
    """Jupiter order for the account's wallet (nothing is sent yet)."""
    import jupiter_v2_executor as jx
    address, _ = _keypair(chat)
    return jx.order(input_mint, output_mint, int(amount), taker=address)


def execute(chat, order_data):
    """Sign with the account's own wallet and send. Any error here = uncertain result."""
    import jupiter_v2_executor as jx
    address, kp = _keypair(chat)
    return jx.execute_signed(_sign(order_data, kp, address), order_data["requestId"])


def swap(chat, input_mint, output_mint, amount):
    return execute(chat, quote_order(chat, input_mint, output_mint, amount))


SLIPPAGE_BPS = int(os.getenv("USER_SLIPPAGE_BPS", "300"))


class NotSent(RuntimeError):
    """Nothing went to the blockchain (or it failed on-chain without effect). fallback=True: the
    atomic format was not possible for this route, the old path (swap, then fee) may be used."""
    def __init__(self, msg, fallback=False):
        super().__init__(msg)
        self.fallback = fallback


class Uncertain(RuntimeError):
    """Sent but not confirmed yet: check the wallet before deciding."""


def atomic_swap(chat, input_mint, output_mint, amount, fee_lamports=None, wait_s=60):
    """Swap with the fee INSIDE the same transaction (both land or neither does).
    fee_lamports=None: 4% of the SOL the swap is quoted to return (sells).
    Returns {"signature", "fee_lamports"}; raises NotSent or Uncertain."""
    import fee_tx
    import live_swap_executor as lx
    from solders.transaction import VersionedTransaction
    dest = fee_wallet()
    try:
        address, kp = _keypair(chat)
        if not dest:
            raise NotSent("fee wallet missing")
        route = lx.quote(input_mint, output_mint, int(amount), slippage_bps=SLIPPAGE_BPS)
        fee = int(fee_lamports if fee_lamports is not None else int(route["outAmount"]) * FEE_RATE)
        payload = lx.build_swap(route, address)
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(payload["swapTransaction"], validate=True))
        try:
            msg = fee_tx.add_fee_transfer(unsigned.message, address, dest, fee)
            signed = VersionedTransaction(msg, [kp])
            raw = bytes(signed)
            if len(raw) > fee_tx.MAX_TX_BYTES:
                raise fee_tx.FeeTxError("transaction too large with the fee")
        except fee_tx.FeeTxError as exc:
            raise NotSent(str(exc), fallback=True)
        encoded = base64.b64encode(raw).decode()
        sim = _rpc("simulateTransaction", [encoded, {"encoding": "base64", "commitment": "confirmed"}])
        if not isinstance(sim, dict) or (sim.get("value") or {}).get("err") is not None:
            raise NotSent("simulation failed")
    except (NotSent, Uncertain):
        raise
    except Exception as exc:
        raise NotSent(type(exc).__name__)
    try:
        sig = _rpc("sendTransaction", [encoded, {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}])
    except Exception:
        raise Uncertain("send not acknowledged")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            st = (_rpc("getSignatureStatuses", [[sig]]) or {}).get("value", [None])[0]
        except Exception:
            st = None
        if st:
            if st.get("err") is not None:
                raise NotSent("failed on-chain (nothing swapped, no fee)")
            if st.get("confirmationStatus") in ("confirmed", "finalized"):
                return {"signature": sig, "fee_lamports": fee}
        time.sleep(2)
    raise Uncertain("not confirmed yet")


def _record_fee_paid(chat, lamports):
    with _user_lock(chat):
        d = load(chat)
        d["fees_paid_lamports"] = int(d.get("fees_paid_lamports", 0)) + int(lamports)
        save(chat, d)


def _charge_fee(chat, lamports):
    """Send the fee to the owner's wallet; on failure keep it as owed (retried by upkeep)."""
    lamports = int(lamports)
    if lamports <= 0:
        return True
    dest = fee_wallet()
    try:
        if not dest:
            raise RuntimeError("fee wallet missing")
        _, kp = _keypair(chat)
        transfer_sol(kp, dest, lamports)
        ok = True
    except Exception:
        ok = False
    with _user_lock(chat):
        d = load(chat)
        if ok:
            d["fees_paid_lamports"] = int(d.get("fees_paid_lamports", 0)) + lamports
        else:
            d["fees_owed_lamports"] = int(d.get("fees_owed_lamports", 0)) + lamports
        save(chat, d)
    return ok


# ---------------------------------------------------------------- signals
def _notify(send, chat, text):
    if send:
        try:
            send(chat, text)
        except Exception:
            pass


def _run(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def on_buy(mint, leader, send=None, sync=False):
    """A copied wallet bought: every active account (not already holding it) buys too."""
    started = []
    for chat in users():
        if user_wallets.is_owner(chat):
            continue
        d = load(chat)
        if d["mode"] not in ("sim", "real") or mint in d["positions"]:
            continue
        started.append(chat)
        (_buy if sync else (lambda *a: _run(_buy, *a)))(chat, mint, leader, send)
    return started


def on_sell(mint, leader, fraction, send=None, sync=False):
    """A copied wallet sold: accounts holding that coin from it sell too (same part in SIM)."""
    started = []
    for chat in users():
        p = load(chat)["positions"].get(mint)
        if p and p.get("leader") == leader:
            started.append(chat)
            (_sell if sync else (lambda *a: _run(_sell, *a)))(chat, mint, fraction, send)
    return started


def _buy(chat, mint, leader, send):
    import copy_trader as ct
    with _user_lock(chat):
        d = load(chat)
        if d["mode"] not in ("sim", "real") or mint in d["positions"] or len(d["positions"]) >= MAX_POSITIONS:
            return
        real = d["mode"] == "real"
        order_usd = float(d["order_usd"])
        if not real and d["sim_balance"] < order_usd:
            return
        d["positions"][mint] = {"leader": leader, "real": real, "pending": True, "opened_at": time.time()}
        save(chat, d)                       # reserve: a second signal for the same coin is ignored
    try:
        sol_usd = ct._sol_usd()
        total = int(order_usd / sol_usd * 1e9)
        fee = int(total * FEE_RATE)
        amount = total - fee
        if real:
            address, _ = _keypair(chat)
            if sol_balance(address) < total + SOL_RESERVE_LAMPORTS:
                _drop(chat, mint)
                _notify(send, chat, "⚠️ Saldo insuficiente para copiar uma compra de US$ " + format(order_usd, ".0f") +
                        ". Deposite SOL (📥 Depositar) ou diminua o valor em ⚙️ Valor por cópia.")
                return
            try:
                atomic_swap(chat, SOL_MINT, mint, amount, fee_lamports=fee)      # fee inside the same transaction
                _record_fee_paid(chat, fee)
                tokens = token_balance(address, mint)
            except Uncertain:
                _mark_unconfirmed(chat, mint, order_usd, fee, atomic=True)
                return
            except NotSent as exc:
                if not exc.fallback:
                    _drop(chat, mint)               # nothing was sent, nothing charged
                    return
                try:                                # route can't take the fee inside: swap, then fee
                    od = quote_order(chat, SOL_MINT, mint, amount)
                except Exception:
                    _drop(chat, mint)
                    return
                try:
                    res = execute(chat, od)
                except Exception:
                    _mark_unconfirmed(chat, mint, order_usd, fee, atomic=False)
                    return
                tokens = int(res["total_output_amount"])
                _charge_fee(chat, fee)
        else:
            tokens = int(ct._order(SOL_MINT, mint, amount)["outAmount"])
        with _user_lock(chat):
            d = load(chat)
            d["positions"][mint] = {"leader": leader, "real": real, "tokens": tokens, "cost_usd": order_usd,
                                    "fee_usd": round(order_usd * FEE_RATE, 4), "opened_at": time.time()}
            if not real:
                d["sim_balance"] = round(d["sim_balance"] - order_usd, 6)
                d["sim_fees_usd"] = round(float(d.get("sim_fees_usd", 0.0)) + order_usd * FEE_RATE, 6)
            save(chat, d)
        _notify(send, chat, ("🟢 COPY COMPROU " if real else "👥 SIM copiou compra · ") + mint[:6] + "… · US$ " +
                format(order_usd, ".2f") + " (taxa " + format(FEE_RATE * 100, ".0f") + "%: US$ " +
                format(order_usd * FEE_RATE, ".2f") + ")\nsolscan.io/token/" + mint)
    except Exception:
        _drop(chat, mint)


def _mark_unconfirmed(chat, mint, order_usd, fee, atomic):
    """Sent but not confirmed: upkeep checks the wallet before deciding (and charges the fee only
    if it was not already inside the transaction)."""
    with _user_lock(chat):
        d = load(chat)
        if mint in d["positions"]:
            d["positions"][mint].update(cost_usd=order_usd, check_since=time.time(),
                                        fee_lamports=0 if atomic else fee, fee_inside=fee if atomic else 0)
            save(chat, d)


def _drop(chat, mint):
    with _user_lock(chat):
        d = load(chat)
        if d["positions"].pop(mint, None) is not None:
            save(chat, d)


def _sell(chat, mint, fraction, send):
    import copy_trader as ct
    with _user_lock(chat):
        p = load(chat)["positions"].get(mint)
        if not p or p.get("pending") or p.get("selling"):
            return
        d = load(chat)
        d["positions"][mint]["selling"] = True
        save(chat, d)
    try:
        sol_usd = ct._sol_usd()
        if p.get("real"):
            address, _ = _keypair(chat)
            amount = token_balance(address, mint)
            part = 1.0
            if amount <= 0:
                before = p.get("sol_before")
                if before is not None:     # an earlier sell went through after all: value it from the SOL received
                    got = max(0, sol_balance(address) - int(before))
                    fee = int(got / (1 - FEE_RATE) * FEE_RATE)                   # the fee went in the same transaction
                    _record_fee_paid(chat, fee)
                    _close(chat, mint, got / 1e9 * sol_usd, p, send, fee_usd=fee / 1e9 * sol_usd)
                else:
                    _close(chat, mint, 0.0, p, send, note="moeda não está mais na carteira")
                return
            before = sol_balance(address)
            try:
                res = atomic_swap(chat, mint, SOL_MINT, amount)                    # 4% fee inside the same transaction
                fee = res["fee_lamports"]
                _record_fee_paid(chat, fee)
                received = max(0, sol_balance(address) - before) + fee           # SOL from the sale, before the fee
            except Uncertain:
                with _user_lock(chat):
                    d = load(chat)
                    if mint in d["positions"]:
                        d["positions"][mint].update(selling=False, sol_before=before,
                                                    sell_retry_at=time.time() + SELL_RETRY_S)
                        save(chat, d)
                return
            except NotSent as exc:
                if not exc.fallback:
                    raise
                res = swap(chat, mint, SOL_MINT, amount)                           # old path: sale, then fee
                received = int(res["total_output_amount"])
                fee = int(received * FEE_RATE)
                _charge_fee(chat, fee)
        else:
            part = 1.0 if fraction is None or float(fraction) >= 0.95 else max(0.0, float(fraction))
            amount = int(int(p["tokens"]) * part)
            if amount <= 0:
                return
            received = int(ct._order(mint, SOL_MINT, amount)["outAmount"])
            fee = int(received * FEE_RATE)
        net_usd = (received - fee) / 1e9 * sol_usd
        if part < 1.0:
            cost_part = 0.0
            with _user_lock(chat):
                d = load(chat)
                q = d["positions"].get(mint)
                if q:
                    cost_part = float(q["cost_usd"]) * part
                    q.update(tokens=int(q["tokens"]) - amount, cost_usd=float(q["cost_usd"]) - cost_part, selling=False)
                    q["realized_usd"] = float(q.get("realized_usd", 0.0)) + net_usd - cost_part
                    d["sim_balance"] = round(d["sim_balance"] + net_usd, 6)
                    d["sim_fees_usd"] = round(float(d.get("sim_fees_usd", 0.0)) + fee / 1e9 * sol_usd, 6)
                    save(chat, d)
            _notify(send, chat, "👥 SIM vendeu " + format(part * 100, ".0f") + "% de " + mint[:6] + "… junto com a carteira: " +
                    format(net_usd - cost_part, "+.2f") + " USD (taxa " + format(FEE_RATE * 100, ".0f") + "%)")
            return
        _close(chat, mint, net_usd, p, send, fee_usd=fee / 1e9 * sol_usd)
    except Exception:
        with _user_lock(chat):
            d = load(chat)
            if mint in d["positions"]:
                d["positions"][mint].update(selling=False, sell_retry_at=time.time() + SELL_RETRY_S)
                save(chat, d)
        _notify(send, chat, "⏳ Não consegui vender " + mint[:6] + "… agora. Tento de novo em 1 min.")


def _close(chat, mint, net_usd, p, send, fee_usd=0.0, note=None):
    pnl = net_usd - float(p.get("cost_usd", 0.0)) + float(p.get("realized_usd", 0.0))
    with _user_lock(chat):
        d = load(chat)
        if d["positions"].pop(mint, None) is None:
            return
        if not p.get("real"):
            d["sim_balance"] = round(d["sim_balance"] + net_usd, 6)
            d["sim_fees_usd"] = round(float(d.get("sim_fees_usd", 0.0)) + fee_usd, 6)
        d["history"] = (d["history"] + [{"mint": mint, "leader": p.get("leader"), "real": bool(p.get("real")),
                                         "pnl_usd": round(pnl, 4), "closed_at": time.time()}])[-200:]
        save(chat, d)
    _notify(send, chat, ("" if p.get("real") else "👥 SIM ") + ("💰 LUCRO" if pnl > 0 else "🔻 PREJUÍZO") + " — VENDEU " +
            mint[:6] + "…: " + format(pnl, "+.2f") + " USD" +
            (" (taxa " + format(FEE_RATE * 100, ".0f") + "%: US$ " + format(fee_usd, ".2f") + ")" if fee_usd else "") +
            ("\n" + note if note else ""))


def upkeep(send=None):
    """Every minute: retry owed fees, confirm uncertain buys, retry failed sells."""
    for chat in users():
        d = load(chat)
        if int(d.get("fees_owed_lamports", 0)) > 0:
            owed = int(d["fees_owed_lamports"])
            with _user_lock(chat):
                d = load(chat)
                d["fees_owed_lamports"] = 0
                save(chat, d)
            _charge_fee(chat, owed)
        for mint, p in list(load(chat)["positions"].items()):
            try:
                if p.get("real") and p.get("pending") and p.get("check_since"):
                    address, _ = _keypair(chat)
                    tokens = token_balance(address, mint)
                    if tokens > 0:
                        with _user_lock(chat):
                            d = load(chat)
                            if mint in d["positions"]:
                                d["positions"][mint] = {"leader": p["leader"], "real": True, "tokens": tokens,
                                                        "cost_usd": p["cost_usd"], "opened_at": p["opened_at"]}
                                save(chat, d)
                        _charge_fee(chat, p.get("fee_lamports", 0))          # 0 when the fee was inside the tx
                        if p.get("fee_inside"):
                            _record_fee_paid(chat, p["fee_inside"])
                        _notify(send, chat, "🟢 COPY COMPROU " + mint[:6] + "… (compra confirmada na carteira)")
                    elif time.time() - float(p["check_since"]) > PENDING_GIVE_UP_S:
                        _drop(chat, mint)
                elif p.get("sell_retry_at") and time.time() >= float(p["sell_retry_at"]):
                    _sell(chat, mint, 1.0, send)
            except Exception:
                continue


# ---------------------------------------------------------------- panels
def status_text(chat):
    d = load(chat)
    mode = {"off": "DESLIGADO", "sim": "SIMULAÇÃO", "real": "REAL"}[d["mode"]]
    closed = d["history"]
    pnl = sum(h["pnl_usd"] for h in closed)
    lines = ["👥 SEU COPY TRADE — " + mode,
             "US$ " + format(d["order_usd"], ".0f") + " por cópia · máx. " + str(MAX_POSITIONS) + " posições · taxa " +
             format(FEE_RATE * 100, ".0f") + "% em cada compra e venda",
             "Copia as carteiras que o bot encontrou e auditou; vende quando elas venderem.",
             "Posições abertas: " + str(len(d["positions"])) + "/" + str(MAX_POSITIONS),
             "Resultado: " + format(pnl, "+.2f") + " USD em " + str(len(closed)) + " operações (" +
             str(sum(1 for h in closed if h["pnl_usd"] > 0)) + " no lucro)"]
    if d["mode"] == "sim" or not d.get("terms_at"):
        lines.append("Simulação: saldo US$ " + format(d["sim_balance"], ".2f") + " (taxas simuladas US$ " +
                     format(float(d.get("sim_fees_usd", 0.0)), ".2f") + ")")
    paid = int(d.get("fees_paid_lamports", 0)) / 1e9
    if paid or d.get("fees_owed_lamports"):
        lines.append("Taxas pagas: " + format(paid, ".4f") + " SOL" +
                     (" · pendente " + format(int(d["fees_owed_lamports"]) / 1e9, ".4f") + " SOL" if d.get("fees_owed_lamports") else ""))
    return "\n".join(lines)


def positions_text(chat):
    d = load(chat)
    if not d["positions"]:
        return "📦 Nenhuma posição copiada aberta."
    lines = ["📦 SUAS POSIÇÕES COPIADAS: " + str(len(d["positions"]))]
    for mint, p in d["positions"].items():
        held = (time.time() - float(p.get("opened_at") or time.time())) / 60
        lines.append("• " + mint[:6] + "… " + ("💵 REAL" if p.get("real") else "🧪 SIM") +
                     (" · confirmando" if p.get("pending") else "") + " · há " +
                     (format(held, ".0f") + " min" if held < 120 else format(held / 60, ".1f") + " h") +
                     " · US$ " + format(float(p.get("cost_usd", 0.0)), ".2f") +
                     ("\n  👛 Carteira copiada (toque para copiar):\n  " + p["leader"] if p.get("leader") else "") +
                     "\n  solscan.io/token/" + mint)
    return "\n".join(lines)


def owner_summary():
    """For the owner's status: accounts, how many are copying, fees received."""
    all_users = users()
    states = [load(c) for c in all_users]
    active = sum(1 for d in states if d["mode"] in ("sim", "real"))
    real = sum(1 for d in states if d["mode"] == "real")
    paid = sum(int(d.get("fees_paid_lamports", 0)) for d in states) / 1e9
    owed = sum(int(d.get("fees_owed_lamports", 0)) for d in states) / 1e9
    return ("👤 Usuários do copy: " + str(len(all_users)) + " (" + str(active) + " copiando, " + str(real) + " no REAL) · "
            "taxas recebidas: " + format(paid, ".4f") + " SOL" + (" · pendentes " + format(owed, ".4f") + " SOL" if owed else ""))
