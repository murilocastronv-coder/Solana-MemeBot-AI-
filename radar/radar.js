"use strict";
const $=id=>document.getElementById(id);
let coins=[],busy=false;
const money=v=>!Number.isFinite(+v)?"—":new Intl.NumberFormat("en-US",{style:"currency",currency:"USD",notation:+v>=1000000?"compact":"standard",maximumFractionDigits:+v<1?8:2}).format(+v);
const compact=v=>Number.isFinite(+v)?new Intl.NumberFormat("en-US",{notation:"compact",maximumFractionDigits:1}).format(+v):"—";
const fmtPct=v=>Number.isFinite(+v)?Number(v).toFixed(1)+"%":"—";
const ageDays=v=>(Date.now()-Number(v||0))/86400000;
function validMint(s){return typeof s==="string"&&/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(s)}
function uniquePairs(rows){const map=new Map();for(const p of rows){if(p?.chainId!=="solana"||!validMint(p?.baseToken?.address))continue;const a=p.baseToken.address,prior=map.get(a);if(!prior||Number(p.liquidity?.usd||0)>Number(prior.liquidity?.usd||0))map.set(a,p);}return [...map.values()];}
async function load(){
 if(busy)return;busy=true;$("refresh").disabled=true;$("status").textContent="Buscando mercado...";$("notice").textContent="Consultando tokens e pares na DexScreener...";
 try{
  const r=await fetch("https://api.dexscreener.com/token-profiles/latest/v1",{cache:"no-store"});if(!r.ok)throw Error("Lista indisponível: HTTP "+r.status);
  const profiles=await r.json();if(!Array.isArray(profiles))throw Error("Formato inesperado da lista");
  const addresses=[...new Set(profiles.filter(x=>x.chainId==="solana"&&validMint(x.tokenAddress)).map(x=>x.tokenAddress))].slice(0,90);
  if(!addresses.length)throw Error("Nenhum token Solana retornado nesta amostra.");
  const batches=[];for(let i=0;i<addresses.length;i+=30)batches.push(addresses.slice(i,i+30));
  const results=await Promise.allSettled(batches.map(async batch=>{const response=await fetch("https://api.dexscreener.com/tokens/v1/solana/"+batch.join(","),{cache:"no-store"});if(!response.ok)throw Error("HTTP "+response.status);const data=await response.json();return Array.isArray(data)?data:[];}));
  const ok=results.filter(x=>x.status==="fulfilled");if(!ok.length)throw Error("Consulta de pares indisponível");
  coins=uniquePairs(ok.flatMap(x=>x.value));$("updated").textContent=new Date().toLocaleTimeString("pt-BR");$("status").textContent="● Dados atualizados";$("notice").textContent="Amostra de perfis recentes da DexScreener, não é uma varredura completa de todas as memecoins. Dados podem estar atrasados. "+(ok.length<results.length?"Algumas consultas falharam.":"");render();
 }catch(e){$("status").textContent="● Consulta falhou";$("notice").textContent="Não foi possível atualizar: "+e.message+". Tente novamente. Possível limitação da API ou do navegador.";render();}
 finally{busy=false;$("refresh").disabled=false;}
}
function render(){
 const min=Number($("liquidity").value),type=$("type").value,q=$("search").value.trim().toLowerCase();
 const pass=p=>Number(p.liquidity?.usd||0)>=min&&Number(p.volume?.h24||0)>=20000&&ageDays(p.pairCreatedAt)>=1&&Number(p.fdv||0)/Math.max(1,Number(p.liquidity?.usd||0))<=100;
 const filtered=coins.filter(p=>pass(p)&&((type==="all")||(type==="new"&&ageDays(p.pairCreatedAt)<=7)||(type==="established"&&ageDays(p.pairCreatedAt)>7))).filter(p=>[p.baseToken?.name,p.baseToken?.symbol,p.baseToken?.address].some(v=>String(v||"").toLowerCase().includes(q))).sort((a,b)=>Number(b.liquidity?.usd||0)-Number(a.liquidity?.usd||0)).slice(0,60);
 $("total").textContent=coins.length;$("qualified").textContent=coins.filter(pass).length;
 const root=$("coins");root.replaceChildren();
 if(!filtered.length){const p=document.createElement("p");p.textContent="Nenhuma moeda passou pelos filtros nesta amostra. Tente reduzir a liquidez mínima ou atualizar.";root.append(p);return;}
 for(const p of filtered){
 const mint=p.baseToken.address,card=document.createElement("article");card.className="coin";
 const top=document.createElement("div");top.className="coin-top";const title=document.createElement("div");const h=document.createElement("h3");h.textContent=(p.baseToken.name||"Sem nome")+" ("+(p.baseToken.symbol||"?")+")";const sub=document.createElement("small");sub.textContent=(ageDays(p.pairCreatedAt)<=7?"Nova":"Estabelecida")+" • "+(p.dexId||"DEX")+" • Par criado há "+Math.max(0,Math.floor(ageDays(p.pairCreatedAt)))+" dias";title.append(h,sub);const tag=document.createElement("span");tag.className="tag";tag.textContent="Analisar";top.append(title,tag);card.append(top);
 const metrics=document.createElement("div");metrics.className="metrics";for(const [label,val] of [["Preço",money(p.priceUsd)],["Liquidez",money(p.liquidity?.usd)],["Volume 24h",money(p.volume?.h24)],["Variação 24h",fmtPct(p.priceChange?.h24)],["FDV",compact(p.fdv)]]){const m=document.createElement("div");m.className="metric";const small=document.createElement("span");small.textContent=label;const strong=document.createElement("strong");strong.textContent=val;m.append(small,strong);metrics.append(m);}card.append(metrics);
 const contract=document.createElement("div");contract.className="contract";contract.textContent="Contrato (mint): "+mint;card.append(contract);
 const actions=document.createElement("div");actions.className="coin-actions";const copy=document.createElement("button");copy.textContent="Copiar contrato";copy.onclick=async()=>{try{await navigator.clipboard.writeText(mint);copy.textContent="✓ Copiado";setTimeout(()=>copy.textContent="Copiar contrato",1600);}catch{copy.textContent="Falhou: copie o texto acima";}};actions.append(copy);
 if(typeof p.url==="string"&&p.url.startsWith("https://dexscreener.com/")){const a=document.createElement("a");a.href=p.url;a.target="_blank";a.rel="noopener noreferrer";a.className="link secondary";a.textContent="Ver gráfico";actions.append(a);}
 const risk=document.createElement("p");risk.className="risk";risk.textContent="⚠️ Não auditado: mint/freeze authority, holders, honeypot e risco de rug pull não verificados.";card.append(actions,risk);root.append(card);
 }
}
$("refresh").onclick=load;for(const id of ["type","liquidity","search"])$(id).addEventListener(id==="search"?"input":"change",render);
load();setInterval(()=>{if(!document.hidden)load();},60000);document.addEventListener("visibilitychange",()=>{if(!document.hidden)load();});
