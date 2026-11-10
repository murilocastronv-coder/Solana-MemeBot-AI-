"""Put the fee inside the swap transaction itself (atomic: swap and fee land together or not at all).

Jupiter's /swap/v1/swap returns a v0 transaction for the user's wallet (the user pays the network
fee and is the only signer). We add one System Program transfer (user -> owner's wallet) at the
end and re-sign. Adding static account keys shifts the indexes of the accounts that follow them
(including the ones loaded from address lookup tables), so every instruction is re-indexed.
"""
from solders.instruction import CompiledInstruction
from solders.message import MessageHeader, MessageV0
from solders.pubkey import Pubkey

SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
MAX_TX_BYTES = 1232


class FeeTxError(RuntimeError):
    pass


def transfer_data(lamports):
    return (2).to_bytes(4, "little") + int(lamports).to_bytes(8, "little")


def add_fee_transfer(msg, payer, fee_wallet, lamports):
    """Return a new MessageV0 = msg + transfer(payer -> fee_wallet, lamports) as the last instruction."""
    if not isinstance(msg, MessageV0):
        raise FeeTxError("unsupported message version")
    payer, dest = Pubkey.from_string(str(payer)), Pubkey.from_string(str(fee_wallet))
    lamports = int(lamports)
    if lamports <= 0:
        raise FeeTxError("fee must be positive")
    keys = list(msg.account_keys)
    h = msg.header
    n_sig, ro_sig, ro_unsig = h.num_required_signatures, h.num_readonly_signed_accounts, h.num_readonly_unsigned_accounts
    if not keys or keys[0] != payer:
        raise FeeTxError("the user wallet must pay the transaction")
    if dest == payer:
        raise FeeTxError("fee wallet equals the payer")
    old_n = len(keys)
    writable_unsigned_end = old_n - ro_unsig                  # writable non-signers end here

    new_keys = list(keys)
    if dest in keys:
        i = keys.index(dest)
        if i < n_sig or i >= writable_unsigned_end:
            raise FeeTxError("fee wallet already used as signer or read-only")
    else:
        new_keys.insert(writable_unsigned_end, dest)          # writable, not a signer
    add_sys = SYSTEM_PROGRAM not in keys
    if add_sys:
        new_keys.append(SYSTEM_PROGRAM)                       # read-only, not a signer
    new_n = len(new_keys)
    position = {k: i for i, k in enumerate(new_keys)}

    def remap(i):
        return position[keys[i]] if i < old_n else i - old_n + new_n   # lookup accounts come after the static ones

    instructions = [CompiledInstruction(remap(ix.program_id_index), bytes(ix.data), bytes(remap(a) for a in bytes(ix.accounts)))
                    for ix in msg.instructions]
    instructions.append(CompiledInstruction(position[SYSTEM_PROGRAM], transfer_data(lamports),
                                            bytes([position[payer], position[dest]])))
    header = MessageHeader(n_sig, ro_sig, ro_unsig + (1 if add_sys else 0))
    return MessageV0(header, new_keys, msg.recent_blockhash, instructions, list(msg.address_table_lookups))
