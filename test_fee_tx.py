import unittest

from solders.hash import Hash
from solders.instruction import CompiledInstruction
from solders.keypair import Keypair
from solders.message import MessageAddressTableLookup, MessageHeader, MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

import fee_tx


def resolve(msg, lookup_names):
    """Names of the accounts each instruction uses (static keys, then writable then readonly lookups)."""
    names = [str(k) for k in msg.account_keys] + lookup_names
    return [(names[ix.program_id_index], [names[a] for a in bytes(ix.accounts)]) for ix in msg.instructions]


class FeeTxTests(unittest.TestCase):
    def setUp(self):
        self.user, self.owner = Keypair(), Keypair()
        self.w1, self.ro1, self.prog, self.table = (Pubkey.new_unique() for _ in range(4))
        keys = [self.user.pubkey(), self.w1, self.ro1, self.prog]
        lookups = [MessageAddressTableLookup(self.table, bytes([7, 8]), bytes([9]))]
        ixs = [CompiledInstruction(3, bytes([1, 2]), bytes([0, 1, 2, 4, 5, 6]))]
        self.msg = MessageV0(MessageHeader(1, 0, 2), keys, Hash.default(), ixs, lookups)
        self.lookup_names = ["LW7", "LW8", "LR9"]

    def test_swap_accounts_unchanged_and_fee_appended(self):
        before = resolve(self.msg, self.lookup_names)
        new = fee_tx.add_fee_transfer(self.msg, self.user.pubkey(), self.owner.pubkey(), 4_000_000)
        after = resolve(new, self.lookup_names)
        self.assertEqual(after[0], before[0])                                  # swap untouched
        prog, accounts = after[1]
        self.assertEqual(prog, "11111111111111111111111111111111")
        self.assertEqual(accounts, [str(self.user.pubkey()), str(self.owner.pubkey())])
        self.assertEqual(int.from_bytes(bytes(new.instructions[1].data)[4:], "little"), 4_000_000)
        # owner wallet writable, system program read-only, user still the only signer
        self.assertEqual(new.header.num_required_signatures, 1)
        self.assertTrue(new.is_maybe_writable(list(new.account_keys).index(self.owner.pubkey())))
        self.assertFalse(new.is_maybe_writable(list(new.account_keys).index(fee_tx.SYSTEM_PROGRAM)))
        tx = VersionedTransaction(new, [self.user])
        self.assertTrue(len(bytes(tx)) < fee_tx.MAX_TX_BYTES)

    def test_refuses_if_user_is_not_the_payer(self):
        with self.assertRaises(fee_tx.FeeTxError):
            fee_tx.add_fee_transfer(self.msg, self.owner.pubkey(), self.user.pubkey(), 1)

    def test_system_program_already_present(self):
        keys = [self.user.pubkey(), self.w1, fee_tx.SYSTEM_PROGRAM]
        msg = MessageV0(MessageHeader(1, 0, 1), keys, Hash.default(), [CompiledInstruction(2, b"", bytes([0, 1]))], [])
        new = fee_tx.add_fee_transfer(msg, self.user.pubkey(), self.owner.pubkey(), 5)
        self.assertEqual(len(new.account_keys), 4)
        self.assertEqual(resolve(new, [])[0], (str(fee_tx.SYSTEM_PROGRAM), [str(self.user.pubkey()), str(self.w1)]))


if __name__ == "__main__":
    unittest.main()
