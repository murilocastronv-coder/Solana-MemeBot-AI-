import base64
import unittest

from solders.keypair import Keypair
from solders.transaction import Transaction

import rent_reclaim as r


class ReclaimTests(unittest.TestCase):
    def test_closes_only_empty_accounts(self):
        signer = Keypair()
        owner = str(signer.pubkey())
        empty = str(Keypair().pubkey())
        full = str(Keypair().pubkey())
        sent = []

        def acct(pubkey, amount):
            return {"pubkey": pubkey, "account": {"lamports": 2039280, "data": {"parsed": {"info": {
                "owner": owner, "state": "initialized", "tokenAmount": {"amount": amount}}}}}}

        def rpc(method, params):
            if method == "getTokenAccountsByOwner":
                if params[1]["programId"] == r.TOKEN_PROGRAMS[0]:
                    return {"result": {"value": [acct(empty, "0"), acct(full, "5")]}}
                return {"result": {"value": []}}
            if method == "getLatestBlockhash":
                return {"result": {"value": {"blockhash": "11111111111111111111111111111111"}}}
            if method == "sendTransaction":
                sent.append(params[0])
                return {"result": "sig"}
        closed, lamports, sig = r.reclaim(rpc, signer)
        self.assertEqual((closed, lamports, sig), (1, 2039280, "sig"))
        tx = Transaction.from_bytes(base64.b64decode(sent[0]))
        keys = [str(k) for k in tx.message.account_keys]
        self.assertIn(empty, keys)
        self.assertNotIn(full, keys)

    def test_nothing_to_close(self):
        rpc = lambda m, p: {"result": {"value": []}}
        self.assertEqual(r.reclaim(rpc, Keypair()), (0, 0, None))


if __name__ == "__main__":
    unittest.main()
