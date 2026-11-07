"""Manual paper positions with market-price TP/SL alerts. No real trades."""
import json,os,re,threading,time,requests
from pathlib import Path
LOCK=threading.RLock()
PATH=Path(os.getenv("PAPER_POSITIONS_FILE","/tmp/memebot_positions.json"))
MINT=re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
def load():
 try:
  data=json.loads(PATH.read_text())
  return data if isinstance(data,dict) else {}
 except (OSError,ValueError):return {}
POSITIONS=load()
def save():
 PATH.parent.mkdir(parents=True,exist_ok=True)
 tmp=PATH.with_suffix(".tmp");tmp.write_text(json.dumps(POSITIONS));tmp.replace(PATH)
def add(chat,mint,entry,usd,tp,sl):
 if not MINT.fullmatch(mint):raise ValueError("Contrato Solana inválido.")
 if not (0<entry<1e9 and 0<usd<=100000 and 0<tp<=1000 and 0<sl<100):raise ValueError("Preço, valor ou percentuais inválidos.")
 with LOCK:
  positions=POSITIONS.setdefault(str(chat),{})
  if len(positions)>=15 and mint not in positions:raise ValueError("Máximo de 15 posições simuladas.")
  positions[mint]={"entry":entry,"usd":usd,"tp":tp,"sl":sl,"created":time.time(),"status":"open"}
  save()
def remove(chat,mint):
 with LOCK:
  p=POSITIONS.get(str(chat),{})
  if mint not in p:return False
  del p[mint];save();return True
def list_open(chat):
 with LOCK:return [(k,v.copy()) for k,v in POSITIONS.get(str(chat),{}).items() if v.get("status")=="open"]
def quote(mints):
 prices={}
 for i in range(0,len(mints),30):
  r=requests.get("https://api.dexscreener.com/tokens/v1/solana/"+",".join(mints[i:i+30]),timeout=20,headers={"Accept":"application/json"})
  r.raise_for_status()
  pairs=r.json()
  if not isinstance(pairs,list):continue
  for pair in pairs:
   if pair.get("chainId")!="solana":continue
   mint=pair.get("baseToken",{}).get("address")
   try:price=float(pair.get("priceUsd") or 0);liq=float(pair.get("liquidity",{}).get("usd") or 0)
   except (TypeError,ValueError):continue
   if mint in mints and price>0 and (mint not in prices or liq>prices[mint][1]):prices[mint]=(price,liq)
 return {k:v[0] for k,v in prices.items()}
def check(chat,send,format_price):
 items=[(mint,p) for mint,p in list_open(chat) if p.get("source")!="auto"]
 if not items:return
 prices=quote([mint for mint,_ in items])
 for mint,p in items:
  current=prices.get(mint)
  if current is None:continue
  entry=p["entry"];tp_price=entry*(1+p["tp"]/100);sl_price=entry*(1-p["sl"]/100)
  change=(current/entry-1)*100
  hit="TP" if current>=tp_price else "SL" if current<=sl_price else None
  if not hit:continue
  with LOCK:
   record=POSITIONS.get(str(chat),{}).get(mint)
   if not record or record.get("status")!="open":continue
   record["status"]="alerted_"+hit.lower();save()
  try:send(chat,("🎯 TAKE PROFIT" if hit=="TP" else "🛑 STOP LOSS")+" ATINGIDO (SIMULAÇÃO)\nContrato:\n"+mint+"\nEntrada: $"+format_price(entry)+"\nPreço observado: $"+format_price(current)+"\nVariação estimada: "+format(change,"+.2f")+"%\n⚠️ Confira na Phantom antes de decidir vender. O bot NÃO vendeu e a cotação pode ter mudado.")
  except Exception:
   with LOCK:
    record=POSITIONS.get(str(chat),{}).get(mint)
    if record and record.get("status")=="alerted_"+hit.lower():record["status"]="open";save()
   raise
