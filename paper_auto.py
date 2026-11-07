"""Isolated paper-trading engine: never signs or submits transactions."""
import os,math,threading,time
import paper_positions as paper
LOCK=threading.RLock()
ORDER_USD=10.0
TP_PCT=15.0
SL_PCT=8.0
START_BALANCE=50.0
MAX_OPEN=3
MAX_DAY_LOSS=3.0
STATE={}
def get(chat):
 with LOCK:
  k=str(chat)
  if k not in STATE:STATE[k]={"enabled":False,"balance":START_BALANCE,"realized":0.0,"day_loss":0.0,"day":time.strftime("%Y-%m-%d",time.gmtime()),"opened":set(),"closed":set()}
  return STATE[k]
def set_enabled(chat,enabled):
 with LOCK:get(chat)["enabled"]=bool(enabled)
def status(chat):
 with LOCK:
  st=get(chat);open_items=[(mint,p) for mint,p in paper.list_open(chat) if p.get("source")=="auto"]
  return {"enabled":st["enabled"],"balance":st["balance"],"realized":st["realized"],"open":len(open_items),"daily_loss":st["day_loss"]}
def candidate(p):
 try:
  mint=p.get("baseToken",{}).get("address")
  price=float(p.get("priceUsd") or 0);liq=float(p.get("liquidity",{}).get("usd") or 0)
  volume=float(p.get("volume",{}).get("h24") or 0);fdv=float(p.get("fdv") or 0)
  age=(time.time()*1000-float(p.get("pairCreatedAt") or 0))/3600000
  change=float(p.get("priceChange",{}).get("h24") or 0)
  return (bool(mint) and bool(paper.MINT.fullmatch(mint)) and math.isfinite(price) and price>0 and
    liq>=30000 and volume>=20000 and age>=24 and fdv>0 and fdv/liq<=100 and
    -20<=change<=80)
 except (TypeError,ValueError,ZeroDivisionError):return False
def enter(chat,rows,send,fmt):
 with LOCK:
  st=get(chat)
  if not st["enabled"]:return
  today=time.strftime("%Y-%m-%d",time.gmtime())
  if today!=st["day"]:st["day"]=today;st["day_loss"]=0
  if st["day_loss"]>=MAX_DAY_LOSS:return
  opened=paper.list_open(chat)
  if len(opened)>=MAX_OPEN or st["balance"]<ORDER_USD:return
  for p in rows:
   if not candidate(p):continue
   mint=p["baseToken"]["address"]
   if mint in st["opened"] or any(m==mint for m,_ in opened):continue
   entry=float(p["priceUsd"])
   paper.add(chat,mint,entry,ORDER_USD,TP_PCT,SL_PCT)
   with paper.LOCK:
    paper.POSITIONS[str(chat)][mint]["source"]="auto"
    paper.save()
   st["balance"]-=ORDER_USD
   st["opened"].add(mint)
   try:send(chat,"🧪 COMPRA SIMULADA (SEM DINHEIRO REAL)\n"+str(p["baseToken"].get("symbol","?"))+"\nContrato:\n"+mint+"\nValor: $10\nEntrada: $"+fmt(entry)+"\n🎯 TP +15%: $"+fmt(entry*1.15)+"\n🛑 SL -8%: $"+fmt(entry*.92)+"\nSaldo fictício livre: $"+fmt(st["balance"])+"\n⚠️ Não é uma ordem na Solana.")
   except Exception:pass
   break
def exit_positions(chat,send,fmt):
 with LOCK:
  st=get(chat)
  if not st["enabled"]:return
  items=[(m,p) for m,p in paper.list_open(chat) if p.get("source")=="auto"]
  if not items:return
  prices=paper.quote([m for m,_ in items])
  for mint,p in items:
   current=prices.get(mint)
   if current is None:continue
   entry=p["entry"]
   if current<entry*(1-SL_PCT/100):reason="SL"
   elif current>=entry*(1+TP_PCT/100):reason="TP"
   else:continue
   pnl=p["usd"]*(current/entry-1)
   if not math.isfinite(pnl):continue
   st["balance"]+=max(0,p["usd"]+pnl)
   st["realized"]+=pnl
   if pnl<0:st["day_loss"]+=-pnl
   paper.remove(chat,mint)
   try:send(chat,("🎯 TP" if reason=="TP" else "🛑 SL")+" — VENDA SIMULADA\nContrato:\n"+mint+"\nSaída observada: $"+fmt(current)+"\nResultado fictício: "+format(pnl,"+.2f")+" USD\nSaldo fictício livre: $"+fmt(st["balance"])+"\n⚠️ Nenhuma venda real foi feita. Slippage/taxas não incluídos.")
   except Exception:pass
