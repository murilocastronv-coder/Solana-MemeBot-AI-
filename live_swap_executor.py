"""Solana Jupiter swap execution adapter. Disabled unless explicitly enabled.
No seed phrase is accepted from Telegram. Never log signed transactions.
Requires a dedicated RPC and Jupiter API key. This is NOT a trading strategy.
"""
import os, base64, requests
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solders.signature import Signature
from bot_wallet_service import export_private_key, get_address
from solders.keypair import Keypair

SOL_MINT="So11111111111111111111111111111111111111112"
USDC_MINT="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TIMEOUT=18

class SwapError(RuntimeError): pass

def configured():
    return bool(os.getenv("JUPITER_API_KEY") and os.getenv("SOLANA_RPC_URL") and os.getenv("LIVE_TRADING_ENABLED")=="true")

def _headers():
    key=os.getenv("JUPITER_API_KEY","")
    if not key:raise SwapError("JUPITER_API_KEY missing")
    return {"x-api-key":key,"Content-Type":"application/json"}

def quote(input_mint,output_mint,amount,slippage_bps=100):
    if input_mint==output_mint or not 1<=int(amount) or not 1<=int(slippage_bps)<=300:
        raise SwapError("Invalid quote request")
    url="https://api.jup.ag/swap/v1/quote"
    r=requests.get(url,params={"inputMint":input_mint,"outputMint":output_mint,"amount":str(int(amount)),"slippageBps":int(slippage_bps),"restrictIntermediateTokens":"true"},headers=_headers(),timeout=TIMEOUT)
    r.raise_for_status()
    data=r.json()
    if not data.get("routePlan") or data.get("inputMint")!=input_mint or data.get("outputMint")!=output_mint:
        raise SwapError("No valid swap route")
    return data

def build_swap(quote_response,public_address):
    r=requests.post("https://api.jup.ag/swap/v1/swap",json={"quoteResponse":quote_response,"userPublicKey":public_address,"wrapAndUnwrapSol":True,"dynamicComputeUnitLimit":True},headers=_headers(),timeout=TIMEOUT)
    r.raise_for_status()
    payload=r.json()
    if not payload.get("swapTransaction"):raise SwapError("No swap transaction")
    return payload

def rpc(method,params):
    url=os.getenv("SOLANA_RPC_URL")
    if not url:raise SwapError("SOLANA_RPC_URL missing")
    r=requests.post(url,json={"jsonrpc":"2.0","id":1,"method":method,"params":params},timeout=TIMEOUT)
    r.raise_for_status()
    data=r.json()
    if data.get("error"):raise SwapError("RPC rejected "+method)
    return data.get("result")

def execute_swap(input_mint,output_mint,amount,passphrase,slippage_bps=100):
    """Sign and submit exactly one Jupiter route. Caller MUST enforce position/risk limits,
    token verification, idempotency, trade authorization and confirmation monitoring.
    Never retry ambiguous send results without reconciling signature first.
    """
    if not configured():raise SwapError("Live swap executor is disabled")
    address=get_address()
    if not address:raise SwapError("No wallet configured")
    route=quote(input_mint,output_mint,amount,slippage_bps)
    payload=build_swap(route,address)
    _,secret=export_private_key(passphrase)
    keypair=Keypair.from_base58_string(secret)
    unsigned=VersionedTransaction.from_bytes(base64.b64decode(payload["swapTransaction"]))
    if str(unsigned.message.account_keys[0])!=address:
        raise SwapError("Unexpected transaction fee payer")
    # Do not sign untrusted aggregator transactions without independently
    # verifying instruction program IDs, account metas, and token amounts.
    raise SwapError("Signing blocked pending instruction verification and transaction simulation")
