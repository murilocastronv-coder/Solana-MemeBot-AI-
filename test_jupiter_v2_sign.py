import base64, unittest
from unittest.mock import patch
from solders.keypair import Keypair
from solders.hash import Hash
from solders.message import MessageV0
from solders.instruction import Instruction, AccountMeta
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction
import jupiter_v2_executor as jv

WALLET, MM = Keypair(), Keypair()


def unsigned(payer, signers):
    ix = Instruction(Pubkey.from_string("11111111111111111111111111111111"), b"",
                     [AccountMeta(k.pubkey(), is_signer=True, is_writable=True) for k in signers])
    msg = MessageV0.try_compile(payer.pubkey(), [ix], [], Hash.default())
    n = msg.header.num_required_signatures
    tx = VersionedTransaction.populate(msg, [Signature.default()] * n)
    return {"transaction": base64.b64encode(bytes(tx)).decode()}


class SignTests(unittest.TestCase):
    def setUp(self):
        for p in (patch("jupiter_v2_executor.get_address", return_value=str(WALLET.pubkey())),
                  patch("jupiter_v2_executor.export_private_key", return_value=(str(WALLET.pubkey()), str(WALLET)))):
            p.start(); self.addCleanup(p.stop)

    def signed(self, order):
        return VersionedTransaction.from_bytes(base64.b64decode(jv.sign_order(order, "pw")))

    def test_own_fee_payer(self):
        tx = self.signed(unsigned(WALLET, [WALLET]))
        self.assertNotEqual(tx.signatures[0], Signature.default())

    def test_market_maker_fee_payer_is_partially_signed(self):
        tx = self.signed(unsigned(MM, [MM, WALLET]))
        keys = list(tx.message.account_keys)
        i = keys.index(WALLET.pubkey())
        self.assertNotEqual(tx.signatures[i], Signature.default())       # we signed
        self.assertEqual(tx.signatures[keys.index(MM.pubkey())], Signature.default())  # MM signs later

    def test_rejects_when_wallet_not_a_signer(self):
        with self.assertRaises(jv.OrderError):
            jv.sign_order(unsigned(MM, [MM]), "pw")


if __name__ == "__main__":
    unittest.main()
