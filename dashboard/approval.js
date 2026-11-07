"use strict";
// Approval inbox prototype. No blockchain transactions, wallet signatures or swaps.
// Proposals are locally stored and never count as executed orders.
(function () {
 const key="solana_memebot_approval_drafts_v1";
 const $=id=>document.getElementById(id);
 let drafts=[];
 try {const value=JSON.parse(localStorage.getItem(key)||"[]");if(Array.isArray(value))drafts=value.filter(x=>x&&typeof x==="object").slice(-30);}catch{}
 function persist(){localStorage.setItem(key,JSON.stringify(drafts));render();}
 function render(){
  const root=$("approvalInbox");if(!root)return;root.replaceChildren();
  if(!drafts.length){root.textContent="Nenhuma proposta pendente. Crie uma proposta de teste abaixo.";return;}
  for(const d of drafts.slice().reverse()){
   const item=document.createElement("div");item.className="position";
   const text=document.createElement("p");text.textContent=d.side+" "+d.token+" • US$ "+d.usd.toFixed(2)+" • "+d.status;item.append(text);
   if(d.status==="PENDENTE"){
    const approve=document.createElement("button");approve.textContent="Revisar proposta (teste)";
    approve.onclick=()=>{d.status="REVISADA — SEM TRANSAÇÃO";persist();$("approvalNotice").textContent="Proposta revisada. Nenhuma assinatura foi solicitada e nenhuma ordem foi enviada.";};
    const reject=document.createElement("button");reject.className="secondary";reject.textContent="Rejeitar";
    reject.onclick=()=>{d.status="REJEITADA";persist();};item.append(approve,reject);
   }root.append(item);
  }
 }
 $("draftApproval")?.addEventListener("click",()=>{
  const token=$("approvalToken").value.trim(),usd=Number($("approvalUsd").value),side=$("approvalSide").value;
  if(!/^[A-Za-z0-9._-]{1,20}$/.test(token)||!Number.isFinite(usd)||usd<=0||usd>2||!["COMPRA","VENDA"].includes(side)){
   $("approvalNotice").textContent="Informe token válido e valor entre US$ 0,01 e US$ 2.";return;
  }
  drafts.push({id:Date.now(),token,usd,side,status:"PENDENTE"});drafts=drafts.slice(-30);persist();
  $("approvalNotice").textContent="Proposta de TESTE criada apenas neste navegador. Não é uma notificação Phantom.";
 });
 render();
})();
