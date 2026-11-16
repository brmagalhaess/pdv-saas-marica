import { api, records } from "./core.js";

const starterPlans = [
  { slug:"essential", name:"Essencial", price_month_cents:14900, price_year_cents:149000, terminal_limit:1, shop_limit:1 },
  { slug:"management", name:"Gestão", price_month_cents:22900, price_year_cents:229000, terminal_limit:1, shop_limit:1 },
  { slug:"advanced", name:"Avançado", price_month_cents:34900, price_year_cents:349000, terminal_limit:3, shop_limit:2 }
];
const featureNames = {
  pdv_local:"Frente de caixa offline, fiado e trocas",
  cloud_dashboard:"Painel de gestão e conferência de caixas",
  stock:"Estoque por loja",
  finance:"Financeiro e comissão de vendedores",
  reports:"Relatórios, curva ABC e exportação CSV",
  priority_support:"Prioridade de suporte prevista"
};

export function currency(cents) {
  return new Intl.NumberFormat("pt-BR",{style:"currency",currency:"BRL",minimumFractionDigits:2,maximumFractionDigits:2}).format(Number(cents)/100);
}
export function shortCurrency(cents) {
  const digits=Number(cents)%100===0?0:2;
  return new Intl.NumberFormat("pt-BR",{style:"currency",currency:"BRL",minimumFractionDigits:digits,maximumFractionDigits:digits}).format(Number(cents)/100);
}
export function registerHref(slug) {
  return "/login/?mode=register"+(slug?"&plan="+encodeURIComponent(slug):"");
}
// Cards follow the plans on sale: unknown plans get a card, retired ones lose theirs.
export function planLayout(plans, cardSlugs) {
  const active=plans.map(plan=>plan.slug);
  return {
    show:active,
    create:active.filter(slug=>!cardSlugs.includes(slug)),
    hide:cardSlugs.filter(slug=>!active.includes(slug))
  };
}
export function lowestMonthlyCents(plans) {
  const prices=plans.map(plan=>Number(plan.price_month_cents)).filter(price=>price>0);
  return prices.length?Math.min(...prices):null;
}

function start() {
  const $ = selector => document.querySelector(selector);
  const grid=$("#plan-grid");
  let plans = starterPlans;
  let billing = "month";

  function newCard(slug) {
    const card=grid.querySelector(".plan-card:not(.featured-plan)").cloneNode(true);
    card.dataset.plan=slug;
    card.querySelector(".plan-top span").textContent="Plano";
    card.querySelector(".plan-top p").textContent="";
    card.querySelector("ul").replaceChildren();
    return card;
  }
  function renderPrices() {
    const annualNote=$("#billing-year span");
    annualNote.hidden=!plans.every(plan=>Number(plan.price_year_cents)===Number(plan.price_month_cents)*10);
    const cards=new Map([...grid.querySelectorAll("[data-plan]")].map(card=>[card.dataset.plan,card]));
    planLayout(plans,[...cards.keys()]).hide.forEach(slug=>{cards.get(slug).hidden=true;});
    plans.forEach(plan => {
      const card=cards.get(plan.slug)||newCard(plan.slug);
      card.hidden=false;
      grid.append(card);
      card.querySelector(".plan-top h3").textContent=plan.name;
      card.querySelector("[data-price]").textContent=currency(billing==="month"?plan.price_month_cents:plan.price_year_cents);
      card.querySelector("[data-cycle]").textContent=billing==="month"?"/mês":"/ano";
      card.querySelector(".plan-meta").textContent=
        plan.terminal_limit+" "+(plan.terminal_limit===1?"terminal":"terminais")+" · "+
        plan.shop_limit+" "+(plan.shop_limit===1?"loja":"lojas");
      card.querySelector("a.button").href=registerHref(plan.slug);
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
    const lowest=lowestMonthlyCents(plans);
    if(lowest!==null)document.querySelectorAll("[data-price-from]").forEach(node=>{node.textContent=shortCurrency(lowest);});
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

  // A failed or empty answer keeps the prices already on the page; visitors never see API errors.
  api.get("/plans",{tenant:false}).then(response=>{
    const current=records(response);
    if(current.length){
      plans=current;
      renderPrices();
    }
  }).catch(error=>{
    console.warn("Planos da plataforma indisponíveis; mantendo os valores iniciais.",error);
  });
  renderPrices();
}

if(typeof document!=="undefined")start();
