"""Solana Jupiter swap execution adapter. Disabled unless explicitly enabled.
No seed phrase is accepted from Telegram. Never log signed transactions.
Requires a dedicated RPC and Jupiter API key. This is NOT a trading strategy.
"""
import os, base64, requests, math
import jupiter_rate_limiter
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
    return os.getenv("LIVE_TRADING_ENABLED","true").strip().lower()!="false"

def _headers():
    key=os.getenv("JUPITER_API_KEY","")
    headers={"Content-Type":"application/json"}
    if key:headers["x-api-key"]=key
    return headers

def quote(input_mint,output_mint,amount,slippage_bps=100):
    if input_mint==output_mint or not 1<=int(amount) or not 1<=int(slippage_bps)<=300:
        raise SwapError("Invalid quote request")
    url="https://api.jup.ag/swap/v1/quote"
    r=jupiter_rate_limiter.get(url,params={"inputMint":input_mint,"outputMint":output_mint,"amount":str(int(amount)),"slippageBps":int(slippage_bps),"restrictIntermediateTokens":"true"},headers=_headers(),timeout=TIMEOUT)
    r.raise_for_status()
    data=r.json()
    if not data.get("routePlan") or data.get("inputMint")!=input_mint or data.get("outputMint")!=output_mint:
        raise SwapError("No valid swap route")
    if int(data.get("inAmount",0))!=int(amount) or int(data.get("outAmount",0))<=0:
        raise SwapError("Invalid quote amounts")
    if float(data.get("priceImpactPct",0) or 0)>1:
        raise SwapError("Price impact too high")
    return data

def build_swap(quote_response,public_address):
    r=jupiter_rate_limiter.post("https://api.jup.ag/swap/v1/swap",json={"quoteResponse":quote_response,"userPublicKey":public_address,"wrapAndUnwrapSol":True,"dynamicComputeUnitLimit":True},headers=_headers(),timeout=TIMEOUT)
    r.raise_for_status()
    payload=r.json()
    if not payload.get("swapTransaction"):raise SwapError("No swap transaction")
    return payload

def rpc(method,params):
    url=os.getenv("SOLANA_RPC_URL","https://api.mainnet-beta.solana.com")
    r=requests.post(url,json={"jsonrpc":"2.0","id":1,"method":method,"params":params},timeout=TIMEOUT)
    r.raise_for_status()
    data=r.json()
    if data.get("error"):raise SwapError("RPC rejected "+method)
    return data.get("result")

def simulate_swap(input_mint,output_mint,amount,slippage_bps=100):
    """Read-only transaction simulation; never signs or submits a swap."""
    address=get_address()
    if not address:raise SwapError("No wallet configured")
    route=quote(input_mint,output_mint,amount,slippage_bps)
    payload=build_swap(route,address)
    raw=payload["swapTransaction"]
    tx=VersionedTransaction.from_bytes(base64.b64decode(raw,validate=True))
    if str(tx.message.account_keys[0])!=address:raise SwapError("Unexpected fee payer")
    result=rpc("simulateTransaction",[raw,{"encoding":"base64","sigVerify":False,"replaceRecentBlockhash":True,"commitment":"processed"}])
    if not isinstance(result,dict) or result.get("value",{}).get("err") is not None:
        raise SwapError("Swap simulation failed")
    return {"input_mint":input_mint,"output_mint":output_mint,"input_amount":int(amount),
            "quoted_output":int(route["outAmount"]),"simulation_ok":True,
            "units_consumed":result.get("value",{}).get("unitsConsumed")}

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
