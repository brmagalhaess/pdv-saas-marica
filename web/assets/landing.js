import { api, records, errorText } from "./core.js";

const $ = selector => document.querySelector(selector);
const starterPlans = [
  { slug:"essential", name:"Essencial", price_month_cents:14900, price_year_cents:149000, terminal_limit:1, shop_limit:1 },
  { slug:"management", name:"Gestão", price_month_cents:22900, price_year_cents:229000, terminal_limit:1, shop_limit:1 },
  { slug:"advanced", name:"Avançado", price_month_cents:34900, price_year_cents:349000, terminal_limit:3, shop_limit:2 }
];
const featureNames = {
  pdv_local:"Frente de caixa local",
  cloud_dashboard:"Painel de gestão",
  stock:"Estoque por loja",
  finance:"Financeiro",
  reports:"Relatórios (em implantação)",
  priority_support:"Prioridade de suporte prevista"
};
let plans = starterPlans;
let billing = "month";
function currency(cents) {
  return new Intl.NumberFormat("pt-BR",{style:"currency",currency:"BRL",minimumFractionDigits:2,maximumFractionDigits:2}).format(Number(cents)/100);
}
function renderPrices() {
  const annualNote=$("#billing-year span");
  annualNote.hidden=!plans.every(plan=>Number(plan.price_year_cents)===Number(plan.price_month_cents)*10);
  document.querySelectorAll("[data-plan]").forEach(card => {
    const plan=plans.find(item=>item.slug===card.dataset.plan);
    if(!plan)return;
    card.querySelector(".plan-top h3").textContent=plan.name;
    card.querySelector("[data-price]").textContent=currency(billing==="month"?plan.price_month_cents:plan.price_year_cents);
    card.querySelector("[data-cycle]").textContent=billing==="month"?"/mês":"/ano";
    card.querySelector(".plan-meta").textContent=
      plan.terminal_limit+" "+(plan.terminal_limit===1?"terminal":"terminais")+" · "+
      plan.shop_limit+" "+(plan.shop_limit===1?"loja":"lojas");
    const features=Array.isArray(plan.features)?plan.features:null;
    if(features&&features.length) {
      const list=card.querySelector("ul");
      list.replaceChildren();
      features.forEach(key=>{
        const row=document.createElement("li");
        row.textContent=featureNames[key]||String(key).replaceAll("_"," ");
        list.append(row);
      });
    }
    if(!card.querySelector("[data-system-support]")) {
      const support=document.createElement("li");
      support.setAttribute("data-system-support", "");
      support.textContent="Suporte ao sistema incluso";
      card.querySelector("ul").append(support);
    }
  });
  $("#billing-month").setAttribute("aria-pressed",String(billing==="month"));
  $("#billing-year").setAttribute("aria-pressed",String(billing==="year"));
}
$("#billing-month").addEventListener("click",()=>{billing="month";renderPrices();});
$("#billing-year").addEventListener("click",()=>{billing="year";renderPrices();});

function showOffer(kind) {
  const equipment=kind==="equipment";
  $("#software-offer").hidden=equipment;
  $("#equipamentos").hidden=!equipment;
  $("#offer-software").setAttribute("aria-pressed",String(!equipment));
  $("#offer-equipment").setAttribute("aria-pressed",String(equipment));
}
$("#offer-software").addEventListener("click",()=>showOffer("software"));
$("#offer-equipment").addEventListener("click",()=>showOffer("equipment"));
function offerFromHash() {
  if(location.hash==="#equipamentos")showOffer("equipment");
}
window.addEventListener("hashchange",offerFromHash);
offerFromHash();

const menuButton=$("#site-menu-button"),siteNav=$("#site-navigation");
function setMenu(open) {
  siteNav.classList.toggle("open",open);
  menuButton.setAttribute("aria-expanded",String(open));
  menuButton.setAttribute("aria-label",open?"Fechar menu":"Abrir menu");
  menuButton.textContent=open?"×":"☰";
}
menuButton.addEventListener("click",()=>setMenu(!siteNav.classList.contains("open")));
siteNav.querySelectorAll("a").forEach(link=>link.addEventListener("click",()=>setMenu(false)));
document.addEventListener("keydown",event=>{if(event.key==="Escape")setMenu(false);});

api.get("/plans",{tenant:false}).then(response=>{
  const current=records(response);
  if(current.length){
    plans=current;
    renderPrices();
    $("#price-source").textContent="Valores e limites carregados da plataforma. A contratação depende da confirmação da assinatura.";
  }else{
    $("#price-source").textContent="Planos indisponíveis no servidor. Valores acima são referências da oferta inicial.";
  }
}).catch(error=>{
  $("#price-source").textContent="Não foi possível atualizar os planos agora. Valores exibidos são referências da oferta inicial. "+errorText(error);
});
renderPrices();
