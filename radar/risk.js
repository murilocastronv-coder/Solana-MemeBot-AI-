"use strict";
// On-demand third-party risk checks. A failure is UNKNOWN, never SAFE.
const riskCache=new Map();
const riskInFlight=new Map();
function pct(x){const n=Number(x);return Number.isFinite(n)&&n>=0&&n<=100? n:null;}
function assessReport(data){
 if(!data||typeof data!=="object"||!Array.isArray(data.risks))return {label:"Dados incompletos",details:["Relatório de risco não disponível em formato esperado."],top:null};
 const details=[];const risks=data.risks.filter(r=>r&&typeof r==="object");
 for(const r of risks.slice(0,8)){const name=String(r.name||r.description||"Risco informado").slice(0,150);details.push("⚠️ "+name);}
 const holders=Array.isArray(data.topHolders)?data.topHolders:[];
 const holderPcts=holders.map(h=>pct(h?.pct)).filter(n=>n!==null);
 const top=holderPcts.length?holderPcts.slice(0,10).reduce((a,b)=>a+b,0):null;
 if(top!==null){details.push("10 maiores carteiras listadas: "+top.toFixed(1)+"% dos tokens (pode incluir pools/contratos).");if(top>=50)details.push("⚠️ Concentração elevada (≥50%) — exige investigação.");}
 else details.push("Concentração de carteiras: dados indisponíveis.");
 if(data.mintAuthority!==null&&data.mintAuthority!==undefined&&data.mintAuthority!==false)details.push("⚠️ Autoridade de emissão informada no relatório.");
 if(data.freezeAuthority!==null&&data.freezeAuthority!==undefined&&data.freezeAuthority!==false)details.push("⚠️ Autoridade de congelamento informada no relatório.");
 const severe=risks.some(r=>Number(r.level)>=3)||top!==null&&top>=50;
 const label=severe?"Risco elevado":risks.length?"Alertas identificados":"Sem alertas neste relatório (não garante segurança)";
 return {label,details:details.length?details:["Nenhum alerta listado pela fonte; riscos podem existir."],top};
}
async function inspectToken(mint,output,button){
 if(!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(mint))return;
 button.disabled=true;output.textContent="Consultando análise externa de risco...";
 try{
  let result=riskCache.get(mint);
  if(!result){
   let pending=riskInFlight.get(mint);
   if(!pending){
    pending=(async()=>{
     const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),12000);
     try{
      const response=await fetch("https://api.rugcheck.xyz/v1/tokens/"+encodeURIComponent(mint)+"/report",{signal:controller.signal,headers:{"Accept":"application/json"}});
      if(!response.ok)throw Error("HTTP "+response.status);
      return assessReport(await response.json());
     }finally{clearTimeout(timeout);}
    })();
    riskInFlight.set(mint,pending);
   }
   try{result=await pending;riskCache.set(mint,result);}finally{riskInFlight.delete(mint);}
  }
  output.replaceChildren();
  const heading=document.createElement("strong");heading.textContent="Rugcheck: "+result.label;output.append(heading);
  for(const detail of result.details){const line=document.createElement("div");line.textContent=detail;output.append(line);}
  const source=document.createElement("a");source.href="https://rugcheck.xyz/tokens/"+encodeURIComponent(mint);source.target="_blank";source.rel="noopener noreferrer";source.textContent="Abrir análise na Rugcheck";output.append(source);
 }catch(e){output.textContent="Risco desconhecido: consulta indisponível (API, CORS ou limite de acesso). Confira manualmente na Rugcheck. Não interprete como seguro.";}
 finally{button.disabled=false;}
}
