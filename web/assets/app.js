import { api, getCsrf, setTenant, records, money, dateTime, dateOnly, quantity, el, errorText } from "./core.js";

const $ = selector => document.querySelector(selector);
const platform = location.pathname.startsWith("/platform/");
const state = { user: null, memberships: [], tenantId: "", role: "", shops: [], categories: [], plans: [], features: [], subscription: null, platformRoles: [], renderId: 0 };
const can = (...roles) => roles.includes(state.role);
const hasFeature = feature => state.features.includes(feature);
const isOwner = () => can("owner", "admin");
const canCatalog = () => can("owner", "admin", "manager", "inventory");
const canStock = () => can("owner", "admin", "manager", "inventory");
const canFinance = () => can("owner", "admin", "finance");
const canSales = () => can("owner", "admin", "manager");
const platformCan = (...roles) => !!state.user?.is_superuser || roles.some(role => state.platformRoles.includes(role));
const platformEdit = () => platformCan("platform_billing");
const platformSuperuser = () => !!state.user?.is_superuser;
const tenantOption = platform ? { tenant: false } : {};
const roleNames = { owner: "Proprietário", admin: "Administrador", manager: "Gerente", cashier: "Operador de caixa", inventory: "Estoque", finance: "Financeiro", viewer: "Consulta" };
const cancellationAccess = role => ["owner","admin","manager"].includes(role) ? "Autorizado" : role==="cashier" ? "Com PIN de gerente" : "Sem permissão";
const statusNames = { trial: "Em teste", active: "Ativa", past_due: "Pagamento pendente", expired:"Expirada", suspended: "Suspensa", cancelled: "Cancelada", paid: "Pago", open: "Em aberto", acknowledged:"Em análise", resolved:"Resolvido", ignored:"Ignorado", settled: "Liquidado", not_issued: "Não emitido", completed: "Concluída", voided: "Cancelada", refunded:"Estornada", queued:"Na fila", running:"Em andamento", needs_review:"Revisão necessária", succeeded:"Concluída", failed:"Falha", passed:"Aprovada", healthy:"Saudável", stale:"Atrasado", error:"Falha", unknown:"Aguardando", ativo:"Ativo", ativa:"Ativa", inativo:"Inativo", inativa:"Inativa", revoked:"Revogado", pareado:"Pareado", aguardando:"Aguardando", baixo:"Baixo", regular:"Regular", retirada:"Retirada" };
function editableMoney(cents) {
  const value = Number(cents);
  return Number.isFinite(value) ? (value / 100).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "";
}
function parseMoneyInput(raw) {
  let value = String(raw ?? "").trim().replace(/R\$|\s/g, "");
  if (!value) return NaN;
  if (value.includes(",")) value = value.replace(/\./g, "").replace(",", ".");
  else if (!/^\d+\.\d{1,2}$/.test(value)) value = value.replace(/\./g, "");
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.round(parsed * 100) : NaN;
}

const tenantNav = [
  { group: "Acompanhar", id: "reports", label: "Relatórios", icon: "▧", feature:"reports", roles: ["owner","admin","manager","finance","viewer"] },
  { group: "Acompanhar", id: "dashboard", label: "Visão geral", icon: "◫", feature:"cloud_dashboard", roles: ["owner","admin","manager","cashier","inventory","finance","viewer"] },
  { group: "Operação", id: "sales", label: "Vendas", icon: "▤", roles: ["owner","admin","manager","cashier","inventory","finance","viewer"] },
  { group: "Operação", id: "cash", label: "Caixas", icon: "▢", roles: ["owner","admin","manager","finance","viewer"] },
  { group: "Operação", id: "products", label: "Produtos", icon: "▦", roles: ["owner","admin","manager","cashier","inventory","finance","viewer"] },
  { group: "Operação", id: "categories", label: "Categorias", icon: "◈", roles: ["owner","admin","manager","inventory","viewer"] },
  { group: "Operação", id: "stock", label: "Estoque", icon: "▣", feature:"stock", roles: ["owner","admin","manager","inventory","finance","viewer"] },
  { group: "Relacionamento", id: "customers", label: "Clientes", icon: "♧", roles: ["owner","admin","manager","cashier","finance"] },
  { group: "Relacionamento", id: "suppliers", label: "Fornecedores", icon: "▱", roles: ["owner","admin","manager","inventory","finance"] },
  { group: "Relacionamento", id: "commissions", label: "Comissões", icon: "%", feature:"finance", roles: ["owner","admin","manager","finance"] },
  { group: "Relacionamento", id: "finance", label: "Financeiro", icon: "◎", feature:"finance", roles: ["owner","admin","manager","finance","viewer"] },
  { group: "Administração", id: "shops", label: "Lojas", icon: "⌂", roles: ["owner","admin","manager","viewer"] },
  { group: "Administração", id: "company", label: "Minha empresa", icon: "◧", roles: ["owner","admin"] },
  { group: "Administração", id: "users", label: "Equipe", icon: "♙", roles: ["owner","admin"] },
  { group: "Administração", id: "terminals", label: "Terminais", icon: "▥", roles: ["owner","admin"] },
  { group: "Administração", id: "printers", label: "Impressoras", icon: "⎙", roles: ["owner","admin"] },
  { group: "Administração", id: "subscription", label: "Assinatura", icon: "◉", roles: ["owner","admin"] },
  { group: "Administração", id: "integrations", label: "Integrações", icon: "◇", roles: ["owner","admin"] },
  { group: "Administração", id: "audit", label: "Auditoria", icon: "≡", roles: ["owner","admin"] },
  { group: "Administração", id: "sync_conflicts", label: "Conflitos offline", icon: "⚠", roles: ["owner","admin","manager"] }
];
const platformNav = [
  { group: "Plataforma", id: "overview", label: "Visão geral", icon: "◫", roles:["platform_support","platform_billing","platform_ops"] },
  { group: "Plataforma", id: "tenants", label: "Empresas", icon: "⌂", roles:["platform_support","platform_billing","platform_ops"] },
  { group: "Plataforma", id: "plans", label: "Planos", icon: "◉", roles:["platform_billing"] },
  { group: "Plataforma", id: "subscriptions", label: "Assinaturas", icon: "▤", roles:["platform_support","platform_billing"] },
  { group: "Plataforma", id: "devices", label: "Terminais", icon: "▥", roles:["platform_support","platform_ops"] },
  { group: "Plataforma", id: "platform_users", label: "Usuários e permissões", icon: "♙", roles:[], superuserOnly:true },
  { group: "Plataforma", id: "security", label: "Segurança e MFA", icon: "⌑", roles:["platform_support","platform_billing","platform_ops"] },
  { group: "Plataforma", id: "services", label: "Serviços", icon: "◇", roles:["platform_support","platform_billing","platform_ops"] },
  { group: "Plataforma", id: "backups", label: "Backups e restauração", icon: "◫", roles:["platform_ops"] },
  { group: "Plataforma", id: "releases", label: "Versões", icon: "↥", roles:["platform_ops"] },
  { group: "Plataforma", id: "invoices", label: "Cobranças", icon: "◎", roles:["platform_billing"] },
  { group: "Plataforma", id: "platform_audit", label: "Auditoria", icon: "≡", roles:["platform_support","platform_ops"] }
];

function toast(message, type = "success") {
  const item = el("div", "toast " + (type === "error" ? "error" : ""), message);
  if (type === "error") item.setAttribute("role", "alert");
  $("#toast-stack").append(item);
  setTimeout(() => item.remove(), 5200);
}
function handleError(error, target) {
  if (error?.status === 401) {
    location.assign("/login/?next=" + encodeURIComponent(location.pathname + location.hash));
    return;
  }
  if (error?.status === 0 || error?.status >= 500) {
    toast("Sem conexão com a nuvem. Para continuar as vendas, abra o terminal local em http://127.0.0.1:8010/.", "error");
  }
  if (target) {
    target.textContent = errorText(error);
    target.className = "feedback error";
    target.hidden = false;
    target.setAttribute("role", "alert");
  } else toast(errorText(error), "error");
}
function loadingSurface(label = "Carregando dados…") {
  const surface = el("div", "surface pad");
  surface.append(el("p", "muted", label));
  const block = el("div", "skeleton");
  block.style.height = "130px";
  surface.append(block);
  return surface;
}
function button(text, onClick, tone = "", extra = "") {
  const value = el("button", "button " + tone + " " + extra, text);
  value.type = "button";
  if (onClick) value.addEventListener("click", onClick);
  return value;
}
function page(title, description, section = "Painel") {
  const main = $("#page-content");
  main.replaceChildren();
  const outer = el("div", "page");
  const head = el("div", "page-head");
  const intro = el("div");
  intro.append(el("span", "eyebrow", section), el("h1", "", title), el("p", "", description));
  const actions = el("div", "page-actions");
  head.append(intro, actions);
  outer.append(head);
  main.append(outer);
  $("#breadcrumb-area").textContent = section;
  $("#breadcrumb-current").textContent = title;
  document.title = title + " · PDV2";
  return { outer, actions };
}
function surface(title, description = "") {
  const box = el("section", "surface pad");
  if (title) box.append(el("h2", "", title));
  if (description) box.append(el("p", "", description));
  return box;
}
function metric(label, value, note = "") {
  const card = el("div", "metric");
  card.append(el("span", "label", label), el("strong", "", value), el("small", "", note));
  return card;
}
function empty(title, description, icon = "◇") {
  const box = el("div", "empty");
  box.append(el("span", "empty-icon", icon), el("h3", "", title), el("p", "", description));
  return box;
}
function info(text, warning = false) {
  const node = el("div", "info-strip" + (warning ? " warn" : ""));
  node.append(el("span", "", warning ? "!" : "ⓘ"), el("span", "", text));
  return node;
}
function productIdentity(row) {
  const wrap = el("div", "product-identity");
  const image = el("img", "product-thumb product-thumb-large");
  image.src = row.image_url || "/assets/product-images/grocery.svg";
  image.alt = "Imagem de " + row.name;
  image.loading = "lazy";
  const copy = el("div", "product-identity-copy");
  copy.append(el("strong", "", row.name));
  const details = [row.variant, row.sku ? "SKU " + row.sku : ""].filter(Boolean).join(" · ");
  copy.append(el("small", "", details || "Sem variação cadastrada"));
  wrap.append(image, copy);
  return wrap;
}
function productCodes(row) {
  const wrap = el("div", "product-code-stack");
  wrap.append(el("span", "", row.barcode ? "EAN " + row.barcode : "Sem EAN"));
  if (row.scale_code) wrap.append(el("small", "", "PLU balança " + row.scale_code));
  return wrap;
}
function productFinancials(row) {
  const wrap = el("div", "product-financial-stack");
  const rows = [
    ["Venda", money(row.price_cents), "highlight"],
    ["Custo médio", Number(row.cost_cents) > 0 ? money(row.cost_cents) : "—", ""],
    ["Lucro bruto", Number(row.cost_cents) > 0 ? money(Number(row.price_cents || 0) - Number(row.cost_cents || 0)) : "—", ""]
  ];
  rows.forEach(([label, value, tone]) => {
    const line = el("div", "product-financial-line" + (tone ? " " + tone : ""));
    line.append(el("span", "", label), el("strong", "", value));
    wrap.append(line);
  });
  return wrap;
}
function productStatus(row) {
  const wrap = el("div", "product-status-stack");
  wrap.append(statusBadge(row.active ? "Ativo" : "Inativo"));
  wrap.append(el("small", "", row.stock_controlled === false ? "Produção própria · sem controle de saldo" : row.stock_minimum_milli ? `Estoque mínimo: ${quantity(row.stock_minimum_milli, row.unit)}` : "Estoque mínimo não definido"));
  return wrap;
}
function statusBadge(value) {
  const normalized = String(value ?? "").toLowerCase();
  const tone = ["suspended","cancelled","expired","revoked","voided","failed","refunded","inativo","inativa","baixo","retirada"].includes(normalized) ? "danger"
    : ["trial","past_due","pending","open","acknowledged","not_issued","queued","running","needs_review","aguardando"].includes(normalized) ? "warn"
    : ["active","settled","completed","succeeded","paid","resolved","ignored","passed","healthy","ativo","ativa","pareado","regular"].includes(normalized) ? "" : "neutral";
  return el("span", "badge " + tone, statusNames[normalized] || value || "—");
}
function table(columns, rows, actionsFor) {
  const wrap = el("div", "table-wrap");
  const node = el("table");
  const thead = el("thead");
  const header = el("tr");
  columns.forEach(column => header.append(el("th", "", column.label)));
  if (actionsFor) header.append(el("th", "", "Ações"));
  thead.append(header);
  const tbody = el("tbody");
  rows.forEach(row => {
    const tr = el("tr");
    columns.forEach(column => {
      const td = el("td");
      const value = column.render ? column.render(row) : row[column.key];
      if (value instanceof Node) td.append(value);
      else td.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
      tr.append(td);
    });
    if (actionsFor) {
      const td = el("td", "actions");
      actionsFor(row).forEach(action => {
        const control = el("button", "table-action" + (action.danger ? " danger" : ""), action.label);
        control.type = "button";
        control.addEventListener("click", action.onClick);
        td.append(control);
      });
      tr.append(td);
    }
    tbody.append(tr);
  });
  node.append(thead, tbody);
  wrap.append(node);
  return wrap;
}
function pagedContent(response, path, columns, actionsFor, emptyNode, options={}) {
  const host=el("div");
  const rows=[...records(response)];
  let next=response?.next_cursor||null;
  function draw() {
    host.replaceChildren(rows.length?table(columns,rows,actionsFor):emptyNode);
    if(!next)return;
    const footer=el("div","table-footer");
    footer.append(el("span","","Mais registros disponíveis."));
    const more=button("Carregar mais",async()=>{
      more.disabled=true;
      try {
        const separator=path.includes("?")?"&":"?";
        const result=await api.get(path+separator+"cursor="+encodeURIComponent(next),options);
        rows.push(...records(result));
        next=result?.next_cursor||null;
        draw();
      }catch(error){handleError(error);more.disabled=false;}
    },"ghost","small");
    footer.append(more);host.append(footer);
  }
  draw();
  return host;
}
async function collectPages(path, first, options={}) {
  const rows=[...records(first)],seen=new Set();
  let cursor=first?.next_cursor;
  while(cursor){
    if(seen.has(cursor))throw new Error("A paginação de usuários retornou um cursor repetido.");
    seen.add(cursor);
    const separator=path.includes("?")?"&":"?";
    const result=await api.get(path+separator+"cursor="+encodeURIComponent(cursor),options);
    rows.push(...records(result));cursor=result?.next_cursor;
  }
  return rows;
}
function detailRow(label, value) {
  const row = el("div", "detail-row");
  row.append(el("span", "", label));
  const item = el("span");
  if (value instanceof Node) item.append(value);
  else item.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
  row.append(item);
  return row;
}
function detailList(pairs) {
  const list = el("div", "detail-list");
  pairs.forEach(([label, value]) => list.append(detailRow(label, value)));
  return list;
}
function addressText(value) {
  if (!value) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "object") return Object.values(value).filter(part => typeof part === "string" && part.trim()).join(", ") || "—";
  return "—";
}
function httpsUrl(value) {
  try { const url=new URL(value); return url.protocol==="https:"?url.href:""; }
  catch { return ""; }
}
function terminalLink() { return "http://127.0.0.1:8010/"; }
function localDocker() { return ["localhost", "127.0.0.1"].includes(location.hostname); }
function licenseSource(value) {
  return { vip:"VIP (cortesia)", subscription:"Assinatura paga", manual:"Ativação manual", trial:"Teste" }[value] || (value ? String(value) : "—");
}
function copyableValue(label, value, help = "") {
  const field=el("div","connection-field");
  field.append(el("strong","",label));
  const row=el("div","connection-value"),input=el("input");
  input.readOnly=true;input.value=value||"";input.setAttribute("aria-label",label);
  input.addEventListener("focus",()=>input.select());
  const copy=button("Copiar",async()=>{
    try { await navigator.clipboard.writeText(input.value); toast(label+" copiado."); }
    catch { input.focus();input.select();toast("Selecione e copie o valor exibido."); }
  },"secondary","small");
  copy.disabled=!value;
  row.append(input,copy);field.append(row);
  if(help)field.append(el("small","",help));
  return field;
}
function terminalAccessBox(license) {
  const box=surface("Acesso e conexão do caixa","Configure a empresa uma vez. Depois, cada operador entra com o próprio PIN.");
  const grid=el("div","connection-grid");
  const key=license?.key;
  grid.append(copyableValue("Licença da empresa",key,"Identifica a empresa no primeiro vínculo. O código de pareamento é gerado separadamente para cada caixa."));
  const serverUrl=localDocker()?"http://cloud:8080":location.origin;
  grid.append(copyableValue("Endereço do servidor para o terminal",serverUrl,localDocker()
    ? "Use este endereço quando o terminal roda no Docker Compose deste projeto."
    : "Informe este endereço no terminal instalado na empresa."));
  if(localDocker())grid.append(copyableValue("Servidor fora do Docker, no mesmo computador",location.origin,"Use apenas se o terminal estiver instalado fora do Docker nesta máquina."));
  grid.append(copyableValue("Link para abrir o terminal neste computador",terminalLink(),"O aplicativo local precisa estar em execução neste computador."));
  box.append(grid);
  const actions=el("div","connection-actions"),open=el("a","button secondary","Abrir terminal local");
  open.href=terminalLink();open.target="_blank";open.rel="noopener noreferrer";
  actions.append(open);box.append(actions);
  return box;
}

let dialogCleanup = null;
function closeDialog() {
  if (dialogCleanup) dialogCleanup();
  dialogCleanup = null;
  $("#dialog-root").replaceChildren();
}
function openDialog(title, subtitle, content) {
  closeDialog();
  const previous = document.activeElement;
  const overlay = el("div", "dialog-backdrop");
  const panel = el("section", "dialog");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-labelledby", "dialog-title");
  const head = el("div", "dialog-head");
  const text = el("div");
  const titleNode = el("h2", "", title);
  titleNode.id = "dialog-title";
  text.append(titleNode);
  if (subtitle) text.append(el("p", "", subtitle));
  const close = button("×", closeDialog, "ghost", "dialog-close");
  close.setAttribute("aria-label", "Fechar janela");
  head.append(text, close);
  const body = el("div", "dialog-body");
  body.append(content);
  panel.append(head, body);
  overlay.append(panel);
  $("#dialog-root").append(overlay);
  const keydown = event => {
    if (event.key === "Escape") closeDialog();
    if (event.key !== "Tab") return;
    const focusables = [...panel.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href]')].filter(node => !node.hidden);
    if (!focusables.length) return;
    const first = focusables[0], last = focusables.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };
  overlay.addEventListener("click", event => { if (event.target === overlay) closeDialog(); });
  document.addEventListener("keydown", keydown);
  dialogCleanup = () => { document.removeEventListener("keydown", keydown); previous?.focus?.(); };
  const initial = panel.querySelector("input,select,textarea,button");
  (initial || close).focus();
  return body;
}
function confirmAction(title, description, action, label = "Confirmar") {
  const content = el("div");
  content.append(info(description, true));
  const message = el("p", "feedback error");
  message.hidden = true;
  content.append(message);
  const actions = el("div", "form-actions");
  const cancel = button("Voltar", closeDialog, "ghost");
  const accept = button(label, async () => {
    accept.disabled = true;
    try { await action(); closeDialog(); }
    catch (error) { handleError(error, message); accept.disabled = false; }
  }, "danger");
  actions.append(cancel, accept);
  content.append(actions);
  openDialog(title, "", content);
}

function setSidebar(open) {
  $("#sidebar").classList.toggle("open", open);
  $("#mobile-scrim").classList.toggle("open", open);
  $("#menu-button").setAttribute("aria-expanded", String(open));
  document.body.style.overflow = open ? "hidden" : "";
}
$("#menu-button").addEventListener("click", () => setSidebar(true));
$("#sidebar-close").addEventListener("click", () => setSidebar(false));
$("#mobile-scrim").addEventListener("click", () => setSidebar(false));
function canOpenPlatform() { return !!state.user?.is_staff && platformCan("platform_support", "platform_billing", "platform_ops"); }
function closeProfileMenu() {
  const menu = $("#profile-menu"), trigger = $("#avatar");
  if (!menu || !trigger) return;
  menu.hidden = true;
  trigger.setAttribute("aria-expanded", "false");
}
function renderProfileMenu() {
  const menu = $("#profile-menu");
  if (!menu) return;
  menu.replaceChildren();
  menu.append(el("strong", "profile-menu-name", state.user?.name || "Conta"));
  menu.append(el("small", "profile-menu-email", state.user?.email || ""));
  const links = [];
  const switches = [];
  if (canOpenPlatform()) switches.push(["Administração SaaS", "/platform/", "Planos, licenças e operação da plataforma"]);
  if (state.memberships?.length) {
    state.memberships.forEach(membership => switches.push([
      membership.tenant_name || "Painel da empresa",
      "/app/?tenant=" + encodeURIComponent(membership.tenant_id),
      `Empresa · ${roleNames[membership.role] || membership.role}`
    ]));
  }
  if (switches.length > 1 || (platform && state.memberships?.length)) {
    menu.append(el("small", "profile-menu-section-title", "Trocar área de acesso"));
    switches.forEach(([label, href, description]) => {
      const link = el("a", "profile-menu-link profile-menu-switch", label); link.href = href;
      link.append(el("small", "profile-menu-link-note", description));
      menu.append(link);
    });
    menu.append(el("div", "profile-menu-divider"));
  }
  if (canOpenPlatform()) links.push(["Segurança e MFA", "/platform/#security"]);
  if (links.length) {
    menu.append(el("div", "profile-menu-divider"));
    links.forEach(([label, href]) => {
      const link = el("a", "profile-menu-link", label); link.href = href; menu.append(link);
    });
  }
  menu.append(el("div", "profile-menu-divider"));
  const logout = button("Sair da conta", () => $("#logout").click(), "profile-menu-logout");
  menu.append(logout);
}
$("#avatar").addEventListener("click", event => {
  event.stopPropagation();
  const menu = $("#profile-menu"), trigger = $("#avatar");
  const open = menu.hidden;
  if (open) renderProfileMenu();
  menu.hidden = !open;
  trigger.setAttribute("aria-expanded", String(open));
});
document.addEventListener("click", event => {
  if (!event.target.closest(".profile-menu-wrap")) closeProfileMenu();
});
document.addEventListener("keydown", event => { if (event.key === "Escape") closeProfileMenu(); });
$("#logout").addEventListener("click", async () => {
  $("#logout").disabled = true;
  try { await api.post("/auth/logout", {}, { tenant: false }); location.assign("/login/"); }
  catch (error) { handleError(error); $("#logout").disabled = false; }
});

const resourceSpecs = {
  shops: {
    title: "Lojas", description: "Organize as unidades da empresa e os endereços de operação.",
    endpoint: "/shops", singular: "loja", create: isOwner, edit: isOwner, remove: false,
    fields: [
      { key: "name", label: "Nome da loja", required: true, max: 120 },
      { key: "address", label: "Endereço", type: "textarea", max: 500 },
      { key: "active", label: "Loja ativa", type: "checkbox", default: true }
    ],
    columns: [
      { label: "Loja", render: row => row.name },
      { label: "Endereço", render: row => addressText(row.address) },
      { label: "Estado", render: row => statusBadge(row.active ? "Ativa" : "Inativa") }
    ]
  },
  categories: {
    title: "Categorias", description: "Mantenha o catálogo organizado para encontrar produtos mais rápido.",
    endpoint: "/categories", singular: "categoria", create: canCatalog, edit: canCatalog, remove: false,
    fields: [
      { key: "name", label: "Nome da categoria", required: true, max: 120 },
      { key: "commission_bps", label: "Comissão da categoria (%) — opcional", type: "percent", help: "Se preenchida, substitui a comissão do vendedor nos itens desta categoria." },
      { key: "active", label: "Categoria ativa", type: "checkbox", default: true }
    ],
    columns: [
      { label: "Categoria", render: row => row.name },
      { label: "Comissão", render: row => row.commission_bps == null ? "Do vendedor" : percentText(row.commission_bps) },
      { label: "Estado", render: row => statusBadge(row.active ? "Ativa" : "Inativa") }
    ]
  },
  products: {
    title: "Produtos", description: "Preços, unidades, variações e dados básicos de cada item.",
    endpoint: "/products", singular: "produto", create: canCatalog, edit: canCatalog, remove: true, search: true,
    fields: [
      { key: "name", label: "Nome do produto", required: true, max: 160, full: true },
      { key: "sku", label: "Código interno (SKU)", generated: true, max: 40, help: "Gerado automaticamente pelo sistema e bloqueado para edição." },
      { key: "barcode", label: "Código de barras", generated: true, max: 13, help: "Código interno EAN-13 gerado para esta empresa; bloqueado para edição." },
      { key: "ncm", label: "NCM (opcional)", max: 8, inputMode: "numeric", pattern: "[0-9]{7,8}", help: "Informe o NCM do produto quando a empresa tiver a classificação fiscal definida." },
      { key: "scale_code", label: "Código PLU da balança (opcional)", max: 6, inputMode: "numeric", pattern: "[0-9]{1,6}", help: "Use o código numérico programado na balança para etiquetas EAN-13 com peso." },
      { key: "category_id", label: "Categoria", type: "source", source: "/categories", valueKey: "id", labelKey: "name" },
      { key: "variant", label: "Variação", placeholder: "Ex.: Tamanho M ou sabor morango", max: 100 },
      { key: "unit", label: "Unidade", type: "select", required: true, choices: [["UN","Unidade"],["KG","Quilo"],["L","Litro"]], default: "UN" },
      { key: "price_cents", label: "Preço de venda (R$)", type: "money", required: true, min: 0 },
      { key: "stock_minimum_milli", label: "Estoque mínimo (alerta)", type: "milli", min: 0, default: 0, help: "Apenas sinaliza reposição. Para adicionar unidades, use Operação > Estoque > Registrar entrada ou Ajustar estoque." },
      { key: "stock_controlled", label: "Controlar estoque", type: "checkbox", default: true, help: "Desative para produtos de produção própria, como pão, salgados e refeições vendidos por KG. O terminal permitirá vender mesmo sem saldo de entrada." },
      { key: "image_url", label: "Imagem do produto (URL opcional)", placeholder: "https://... ou /assets/product-images/...", max: 500, full: true, help: "Se ficar vazio, o PDV escolhe automaticamente uma imagem da categoria." },
      { key: "active", label: "Produto ativo", type: "checkbox", default: true }
    ],
    columns: [
      { label: "Produto", render: row => productIdentity(row) },
      { label: "Códigos", render: row => productCodes(row) },
      { label: "NCM", render: row => row.ncm || "—" },
      { label: "Unidade", render: row => row.unit === "KG" ? "Quilo (KG)" : row.unit === "L" ? "Litro (L)" : "Unidade (UN)" },
      { label: "Financeiro", render: row => productFinancials(row) },
      { label: "Margem", render: row => row.cost_cents == null || Number(row.cost_cents) === 0 || !Number(row.price_cents) ? "—" : `${((Number(row.price_cents) - Number(row.cost_cents)) / Number(row.price_cents) * 100).toLocaleString("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 })}%` },
      { label: "Status", render: row => productStatus(row) }
    ]
  },
  customers: {
    title: "Clientes", description: "Cadastro, limite de fiado, crédito na loja e extrato de cada cliente.",
    endpoint: "/customers", singular: "cliente", create: () => can("owner","admin","manager","cashier"), edit: () => can("owner","admin","manager","cashier"), remove: false,
    fields: [
      { key: "name", label: "Nome", required: true, max: 160, full: true },
      { key: "document", label: "CPF ou CNPJ", max: 18 },
      { key: "email", label: "E-mail", type: "email", max: 254 },
      { key: "phone", label: "Telefone", type: "tel", max: 30 },
      { key: "credit_limit_cents", label: "Limite de fiado (R$)", type: "money", min: 0, default: 0, help: "Valor máximo em aberto para vendas a prazo. Deixe 0 para não liberar fiado; acima do limite o caixa exige PIN de gerente." },
      { key: "active", label: "Cliente ativo", type: "checkbox", default: true }
    ],
    columns: [
      { label: "Nome", render: row => row.name },
      { label: "Documento", render: row => row.document },
      { label: "Telefone", render: row => row.phone },
      { label: "Fiado em aberto / limite", render: row => { const c = state.creditByCustomer?.[row.id]; const open = c?.open_credit_cents || 0; return row.credit_limit_cents || open ? `${money(open)} / ${money(row.credit_limit_cents || 0)}` : "—"; } },
      { label: "Crédito na loja", render: row => { const v = state.creditByCustomer?.[row.id]?.store_credit_cents || 0; return v ? money(v) : "—"; } },
      { label: "Estado", render: row => statusBadge(row.active ? "Ativo" : "Inativo") }
    ]
  },
  suppliers: {
    title: "Fornecedores", description: "Dados de contato para compras e entrada de mercadorias.",
    endpoint: "/suppliers", singular: "fornecedor", create: () => can("owner","admin","manager","inventory"), edit: () => can("owner","admin","manager","inventory"), remove: false,
    fields: [
      { key: "name", label: "Nome", required: true, max: 160, full: true },
      { key: "document", label: "CPF ou CNPJ", max: 18 },
      { key: "email", label: "E-mail", type: "email", max: 254 },
      { key: "phone", label: "Telefone", type: "tel", max: 30 },
      { key: "active", label: "Fornecedor ativo", type: "checkbox", default: true }
    ],
    columns: [
      { label: "Nome", render: row => row.name },
      { label: "Documento", render: row => row.document },
      { label: "E-mail", render: row => row.email },
      { label: "Telefone", render: row => row.phone },
      { label: "Estado", render: row => statusBadge(row.active ? "Ativo" : "Inativo") }
    ]
  }
};

const eanLeftOdd = ["0001101","0011001","0010011","0111101","0100011","0110001","0101111","0111011","0110111","0001011"];
const eanLeftEven = ["0100111","0110011","0011011","0100001","0011101","0111001","0000101","0010001","0001001","0010111"];
const eanRight = ["1110010","1100110","1101100","1000010","1011100","1001110","1010000","1000100","1001000","1110100"];
const eanParity = ["LLLLLL","LLGLGG","LLGGLG","LLGGGL","LGLLGG","LGGLLG","LGGGLL","LGLGLG","LGLGGL","LGGLGL"];
function validEan13(value) {
  const code = String(value ?? "");
  if (!/^\d{13}$/.test(code)) return false;
  const sum = [...code.slice(0, 12)].reduce((total, digit, index) => total + Number(digit) * (index % 2 ? 3 : 1), 0);
  return (10 - sum % 10) % 10 === Number(code[12]);
}
function ean13Svg(value) {
  const code = String(value ?? "");
  if (!validEan13(code)) return "";
  let bits = "101";
  const parity = eanParity[Number(code[0])];
  for (let index = 1; index <= 6; index++) bits += parity[index - 1] === "L" ? eanLeftOdd[Number(code[index])] : eanLeftEven[Number(code[index])];
  bits += "01010";
  for (let index = 7; index <= 12; index++) bits += eanRight[Number(code[index])];
  bits += "101";
  // 95 encoded modules plus 11 left / 7 right quiet modules, each fixed at 0.33 mm.
  const bars = [...bits].map((bit, index) => bit === "1" ? `<rect x="${11 + index}" y="0" width="1" height="${index < 3 || (index >= 45 && index < 50) || index >= 92 ? 39 : 36}"/>` : "").join("");
  return `<svg xmlns="http://www.w3.org/2000/svg" width="37.29mm" height="15.18mm" viewBox="0 0 113 46" role="img" aria-label="Código de barras ${code}" shape-rendering="crispEdges"><rect width="113" height="46" fill="#fff"/><g fill="#000">${bars}</g><text x="56.5" y="45" text-anchor="middle" fill="#000" font-family="Arial,sans-serif" font-size="8" letter-spacing="1">${code}</text></svg>`;
}
const productLabelStyles = `
  .product-print-label{box-sizing:border-box;width:50mm;height:30mm;margin:0;padding:1.5mm;background:#fff;color:#000;font-family:Arial,sans-serif;display:grid;grid-template-rows:6.2mm 4.4mm 15.18mm;row-gap:.4mm;overflow:hidden;break-inside:avoid;page-break-inside:avoid}
  .product-print-label *{box-sizing:border-box}
  .product-print-label-name{margin:0;font-size:8pt;font-weight:700;line-height:3.1mm;letter-spacing:0;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-clamp:2;overflow:hidden;overflow-wrap:anywhere}
  .product-print-label-price{display:flex;align-items:baseline;gap:1mm;white-space:nowrap;font-size:11pt;font-weight:700;line-height:4.4mm;letter-spacing:0}
  .product-print-label-unit{font-size:8pt;font-weight:400}
  .product-print-label-barcode{width:37.29mm;height:15.18mm;justify-self:center}
  .product-print-label-barcode svg{display:block;width:37.29mm;height:15.18mm;max-width:none}
  .product-print-page{box-sizing:border-box;margin:0;background:#fff;display:flex;align-items:center;justify-content:center;overflow:hidden;break-inside:avoid;page-break-inside:avoid}
  .product-print-page .product-print-label{flex:none}
`;
const defaultLabelSize = { width_mm: 50, height_mm: 30, gap_mm: 2, sensor: "gap" };
// The 50 × 30 mm design is scaled to the configured label stock.
function labelScale(size) { return Math.min(size.width_mm / 50, size.height_mm / 30); }
function labelSizeText(size) { return `${String(size.width_mm).replace(".", ",")} × ${String(size.height_mm).replace(".", ",")} mm`; }
function labelHtmlEscape(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));
}
function productLabelMarkup(product) {
  return `<article class="product-print-label"><div class="product-print-label-name" title="${labelHtmlEscape(product.name)}">${labelHtmlEscape(product.name)}</div><div class="product-print-label-price"><span>${labelHtmlEscape(money(product.price_cents))}</span><span class="product-print-label-unit">/ ${labelHtmlEscape(product.unit || "UN")}</span></div><div class="product-print-label-barcode">${ean13Svg(product.barcode)}</div></article>`;
}
function productLabelPageMarkup(product, size) {
  return `<div class="product-print-page" style="width:${size.width_mm}mm;height:${size.height_mm}mm"><div style="transform:scale(${labelScale(size)})">${productLabelMarkup(product)}</div></div>`;
}
function productLabelPrintDocument(product, quantity, size = defaultLabelSize) {
  if (!validEan13(product.barcode)) throw new Error("O produto precisa ter um EAN-13 válido para imprimir etiquetas.");
  if (!Number.isInteger(quantity) || quantity < 1 || quantity > 999) throw new Error("Informe uma quantidade entre 1 e 999 etiquetas.");
  const labels = Array.from({ length: quantity }, () => productLabelPageMarkup(product, size)).join("");
  return `<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>Etiquetas · ${labelHtmlEscape(product.name)}</title><style>
    html,body{margin:0;padding:0;background:#fff}body{width:${size.width_mm}mm}
    ${productLabelStyles}
    @page{size:${size.width_mm}mm ${size.height_mm}mm;margin:0}
    @media print{.product-print-page{break-after:page;page-break-after:always}.product-print-page:last-child{break-after:auto;page-break-after:auto}}
  </style></head><body>${labels}</body></html>`;
}
function printProductBarcode(product, quantity, size) {
  printLabelDocument(productLabelPrintDocument(product, quantity, size));
}
function printLabelDocument(markup) {
  const popup = window.open("", "_blank", "width=440,height=620");
  if (!popup) { toast("Permita pop-ups para imprimir a etiqueta.", "error"); return; }
  popup.document.open();
  popup.document.write(markup);
  popup.document.close();
  popup.focus();
  // Callers run only after a user action (dialog submit or button click).
  popup.print();
}
async function productLabelDialog(product) {
  const size = { ...defaultLabelSize, ...(await api.get("/label-settings")) };
  const sizeText = labelSizeText(size);
  const form = el("form", "label-print-form");
  const style = el("style"); style.textContent = productLabelStyles; form.append(style);
  form.append(info("Use impressora de etiquetas com sensor de intervalo. A Epson TM-T20 utiliza bobinas de recibo de 58 ou 80 mm e não alinha etiquetas com espaços.", true));
  const controls = el("div", "form-grid");
  const quantityField = el("div", "field"), quantityLabel = el("label", "", "Quantidade de etiquetas"), quantityInput = el("input");
  quantityInput.id = "label-print-quantity"; quantityInput.name = "quantity"; quantityInput.type = "number";
  quantityInput.min = "1"; quantityInput.max = "999"; quantityInput.step = "1"; quantityInput.value = "1"; quantityInput.required = true;
  quantityLabel.htmlFor = quantityInput.id;
  quantityField.append(quantityLabel, quantityInput, el("small", "", "De 1 a 999 cópias deste produto."));
  const sizeField = el("div", "field");
  sizeField.append(el("span", "field-label", "Tamanho da etiqueta"), el("strong", "label-print-size", sizeText), el("small", "", "Uma etiqueta por página. Altere em Administração → Impressoras."));
  controls.append(quantityField, sizeField); form.append(controls);
  const preview = el("figure", "label-print-preview"), caption = el("figcaption", "", "Prévia · " + sizeText);
  preview.append(caption);
  const barcodeValid = validEan13(product.barcode);
  if (barcodeValid) {
    const sheet = el("div", "label-print-sheet"); sheet.innerHTML = productLabelPageMarkup(product, size); preview.append(sheet);
  } else {
    preview.append(el("p", "feedback error", "O código de barras deste produto não é um EAN-13 válido. Corrija o cadastro antes de imprimir."));
  }
  form.append(preview);
  form.append(el("p", "label-print-help", "Na impressão do navegador, selecione papel " + sizeText + ", escala 100%, margens zero e desative cabeçalhos e rodapés. Nas preferências da impressora, desative o corte automático."));
  const message = el("p", "feedback error"); message.hidden = true; message.setAttribute("role", "alert"); form.append(message);
  const actions = el("div", "form-actions"), print = button("Imprimir etiquetas", null);
  print.type = "submit"; print.disabled = !barcodeValid;
  actions.append(button("Fechar", closeDialog, "ghost"), print); form.append(actions);
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    try { printProductBarcode(product, Number(quantityInput.value), size); message.hidden = true; }
    catch (error) { handleError(error, message); }
  });
  openDialog("Imprimir etiquetas", product.name, form);
}

async function fieldNode(field, value, creating = false) {
  const wrap = el("div", "field" + (field.full ? " full" : ""));
  const id = "field-" + field.key;
  const label = el("label", "", field.label);
  label.htmlFor = id;
  wrap.append(label);
  let input;
  if (field.type === "select" || field.type === "source") {
    input = el("select");
    const choices = field.type === "source"
      ? records(await api.get(field.source)).map(row => [row[field.valueKey], row[field.labelKey]])
      : field.choices;
    if (!field.required) {
      const blank = el("option", "", "Selecione, se desejar");
      blank.value = "";
      input.append(blank);
    }
    choices.forEach(([idValue, text]) => {
      const option = el("option", "", text);
      option.value = String(idValue);
      input.append(option);
    });
    input.value = value ?? field.default ?? "";
  } else if (field.type === "textarea" || field.type === "json") {
    input = el("textarea");
    const initial = value ?? field.default ?? "";
    input.value = field.type === "json" && initial && typeof initial === "object" ? JSON.stringify(initial, null, 2) : initial;
  } else {
    input = el("input");
    input.type = field.type === "money" ? "text" : field.type === "milli" ? "number" : field.type === "checkbox" ? "checkbox" : field.type || "text";
    if (field.type === "money") { input.inputMode = "decimal"; input.placeholder = "0,00"; input.value = value == null ? (field.default == null ? "" : editableMoney(field.default)) : editableMoney(value); input.addEventListener("blur", () => { const cents = parseMoneyInput(input.value); if (Number.isSafeInteger(cents) && cents >= 0) input.value = editableMoney(cents); }); }
    else if (field.type === "percent") { input.type = "number"; input.step = "0.01"; input.min = "0"; input.max = "100"; input.value = value == null ? (field.default == null ? "" : String(field.default / 100)) : String(value / 100); }
    else if (field.type === "milli") { input.step = "0.001"; input.value = value == null ? (field.default == null ? "" : String(field.default / 1000)) : String(value / 1000); }
    else if (field.type === "checkbox") input.checked = value == null ? !!field.default : !!value;
    else input.value = value ?? field.default ?? "";
  }
  input.id = id;
  input.name = field.key;
  if (field.generated) {
    input.readOnly = true;
    input.value = value || "";
    input.placeholder = creating ? "Gerado ao salvar" : "Não cadastrado";
    input.setAttribute("aria-readonly", "true");
  }
  input.required = !!field.required;
  if (field.max) input.maxLength = field.max;
  if (field.minLength) input.minLength = field.minLength;
  if (field.inputMode) input.inputMode = field.inputMode;
  if (field.pattern) input.pattern = field.pattern;
  if (field.min !== undefined) input.min = String(field.min);
  if (field.placeholder) input.placeholder = field.placeholder;
  if (field.type === "checkbox") {
    wrap.classList.add("checkbox-field");
    input.style.width = "18px";
    input.style.minHeight = "18px";
    wrap.prepend(input);
  } else wrap.append(input);
  if (field.help) wrap.append(el("small", "", field.help));
  return wrap;
}
async function formFor(fields, values = {}, creating = false) {
  const form = el("form", "form-grid");
  for (const field of fields) form.append(await fieldNode(field, values[field.key], creating));
  return form;
}
function formPayload(form, fields) {
  const payload = {};
  for (const field of fields) {
    if (field.generated) continue;
    const input = form.elements.namedItem(field.key);
    if (!input) continue;
    let value;
    if (field.type === "checkbox") value = input.checked;
    else if (field.type === "money") value = parseMoneyInput(input.value);
    else if (field.type === "milli") value = Math.round(Number(input.value) * 1000);
    else if (field.type === "percent") value = input.value.trim() === "" ? "" : Math.round(Number(input.value.replace(",", ".")) * 100);
    else if (field.type === "json") {
      try { value = JSON.parse(input.value || "{}"); }
      catch { throw new Error(field.label + ": JSON inválido."); }
    } else value = input.value.trim();
    if ((value === "" || value === null) && !field.required) continue;
    if (field.type === "percent" && (!Number.isSafeInteger(value) || value < 0 || value > 10000)) {
      throw new Error(field.label + ": informe um percentual entre 0 e 100.");
    }
    if ((field.type === "money" || field.type === "milli") && (!Number.isSafeInteger(value) || value < 0)) {
      throw new Error(field.label + ": informe um número válido.");
    }
    payload[field.key] = value;
  }
  return payload;
}
async function resourceForm(spec, record) {
  const editing = !!record;
  const fields = editing ? (spec.editFields || spec.fields) : spec.fields;
  const body = el("div");
  const formValues = editing && spec.endpoint === "/shops" ? { ...record, address: addressText(record.address) === "—" ? "" : addressText(record.address) } : record || {};
  const form = await formFor(fields, formValues, !editing);
  const message = el("p", "dialog-message feedback error");
  message.hidden = true;
  const actions = el("div", "form-actions");
  actions.append(button("Cancelar", closeDialog, "ghost"));
  const save = el("button", "button", editing ? "Salvar alterações" : "Cadastrar " + spec.singular);
  save.type = "submit";
  actions.append(save);
  form.append(message, actions);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    let payload;
    try { payload = formPayload(form, fields); }
    catch (error) { handleError(error, message); return; }
    if (spec.endpoint === "/shops" && typeof payload.address === "string") payload.address = { text: payload.address };
    save.disabled = true;
    message.hidden = true;
    try {
      if (editing) await api.patch(spec.endpoint + "/" + record.id, payload);
      else await api.post(spec.endpoint, payload);
      closeDialog();
      toast(editing ? "Alterações salvas." : "Cadastro concluído.");
      renderRoute();
    } catch (error) { handleError(error, message); save.disabled = false; }
  });
  body.append(form);
  openDialog(editing ? "Editar " + spec.singular : "Novo " + spec.singular, editing ? "Atualize os dados e salve." : "Preencha os campos para cadastrar.", body);
}
async function resourcePage(key) {
  const spec = resourceSpecs[key];
  const current = ++state.renderId;
  const view = page(spec.title, spec.description, "Gestão");
  const productOverview = key === "products" ? el("div", "products-overview") : null;
  if (productOverview) {
    view.outer.classList.add("products-page");
    view.outer.append(productOverview);
  }
  if (spec.create()) view.actions.append(button("+ Novo " + spec.singular, async () => {
    try { await resourceForm(spec); } catch (error) { handleError(error); }
  }));
  if (key === "products") {
    view.outer.append(info("O estoque mínimo é apenas um alerta. O saldo disponível do terminal é lançado por loja em Operação > Estoque, usando Registrar entrada, Importar compra CSV ou Ajustar estoque."));
    view.outer.append(info("Para itens fabricados pela própria empresa, como pão e refeições vendidos por KG, desative Controlar estoque no cadastro. O terminal aceitará a venda pelo preço do quilo sem exigir entrada ou saldo, mantendo esses itens identificados como produção própria."));
    view.outer.append(info("A emissão fiscal depende de configuração tributária e homologação próprias. Este catálogo ainda não torna o PDV apto a emitir NFC-e.", true));
  }
  const box = el("section", "surface");
  if (key === "products") box.classList.add("products-surface");
  view.outer.append(box);
  const toolbar = el("div", "toolbar");
  if (spec.search) {
    const field = el("div", "field");
    const label = el("label", "", "Buscar por nome, SKU ou código de barras");
    label.htmlFor = "resource-search";
    const input = el("input");
    input.id = "resource-search"; input.type = "search"; input.placeholder = "Digite para buscar produtos"; input.maxLength = 120;
    field.append(label, input); toolbar.append(field);
    let timer;
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => load(input.value.trim()), 250); });
  } else toolbar.append(el("span", "toolbar-meta", "Dados cadastrados nesta empresa"));
  box.append(toolbar);
  const content = el("div", key === "products" ? "products-content" : "");
  box.append(content);
  async function load(query = "") {
    const token = ++state.renderId;
    content.replaceChildren(loadingSurface());
    try {
      const suffix = query ? "?q=" + encodeURIComponent(query) : "";
      const result = await api.get(spec.endpoint + suffix);
      if (token !== state.renderId) return;
      const rows = records(result);
      content.replaceChildren();
      if (productOverview) {
        const active = rows.filter(row => row.active !== false).length;
        const weighted = rows.filter(row => row.unit === "KG").length;
        const withCost = rows.filter(row => Number(row.cost_cents) > 0).length;
        productOverview.replaceChildren(
          metric("Produtos encontrados", String(rows.length), query ? "Resultado da busca atual" : "Catálogo da empresa"),
          metric("Produtos ativos", String(active), "Disponíveis para operação"),
          metric("Vendidos por peso", String(weighted), "Unidade KG"),
          metric("Com custo médio", String(withCost), "Prontos para margem e lucro")
        );
      }
      if (!rows.length) {
        content.append(empty(query ? "Nenhum resultado" : "Nada cadastrado ainda", query ? "Tente outro termo de busca." : "Use o botão acima para adicionar o primeiro registro."));
        return;
      }
      if (key === "customers") {
        try { state.creditByCustomer = Object.fromEntries(records(await api.get("/customers/credit-summary")).map(r => [r.customer_id, r])); }
        catch { state.creditByCustomer = {}; }
        if (token !== state.renderId) return;
      }
      const actionsFor = spec.edit() || (spec.remove && spec.edit()) ? row => {
        const actions = [];
        if (key === "customers") actions.push({ label: "Extrato", onClick: () => customerStatement(row).catch(handleError) });
        if (spec.edit()) actions.push({ label: "Editar", onClick: () => resourceForm(spec, row).catch(handleError) });
        if (key === "products" && row.barcode) actions.push({ label: "Imprimir etiquetas", onClick: () => productLabelDialog(row).catch(handleError) });
        if (spec.remove && spec.edit()) actions.push({ label: "Desativar", danger: true, onClick: () => confirmAction("Desativar " + spec.singular, "O registro será desativado. O histórico permanece disponível para auditoria.", async () => {
          await api.delete(spec.endpoint + "/" + row.id);
          toast("Registro desativado.");
          renderRoute();
        }, "Desativar") });
        return actions;
      } : null;
      const draw = list => table(spec.columns, list, actionsFor);
      content.append(draw(rows));
      if (result?.next_cursor) {
        const footer = el("div", "table-footer");
        footer.append(el("span", "", "Mais registros disponíveis."));
        const more = button("Carregar mais", async () => {
          more.disabled = true;
          try {
            const next = await api.get(spec.endpoint + "?cursor=" + encodeURIComponent(result.next_cursor) + (query ? "&q=" + encodeURIComponent(query) : ""));
            if (token !== state.renderId) return;
            rows.push(...records(next));
            content.replaceChildren(draw(rows));
            if (next.next_cursor) {
              result.next_cursor = next.next_cursor;
              content.append(footer);
            }
          } catch (error) { handleError(error); more.disabled = false; }
        }, "ghost", "small");
        footer.append(more);
        content.append(footer);
      }
    } catch (error) {
      if (token !== state.renderId) return;
      content.replaceChildren(empty("Não foi possível carregar", errorText(error), "!"));
      handleError(error);
    }
  }
  if (current === state.renderId) load();
}

async function loadShops() {
  const result = await api.get("/shops");
  state.shops = records(result);
  return state.shops;
}
async function loadSubscription() {
  const result=await api.get("/subscription");
  state.subscription=result;
  state.features=Array.isArray(result.plan?.features)?result.plan.features:[];
  return result;
}
function shopName(id) { return state.shops.find(shop => String(shop.id) === String(id))?.name || "—"; }
function shopSelect(id = "shop-filter", all = false) {
  const field = el("div", "field");
  const label = el("label", "", "Loja");
  label.htmlFor = id;
  const select = el("select");
  select.id = id;
  if (all) {
    const option = el("option", "", "Todas as lojas");
    option.value = "";
    select.append(option);
  }
  state.shops.forEach(shop => {
    const option = el("option", "", shop.name);
    option.value = shop.id;
    select.append(option);
  });
  field.append(label, select);
  return { field, select };
}
async function dashboardPage() {
  const token = ++state.renderId;
  const view = page("Visão geral", "Acompanhe indicadores recebidos da operação e veja o que precisa de atenção.", "Acompanhar");
  view.outer.append(info("Os números mostram dados registrados na nuvem. O caixa local, a emissão fiscal e a operação com hardware ainda passam por validação.", true));
  const filter = surface(hasFeature("reports") ? "Filtrar período" : "Filtrar loja");
  const row = el("div", "form-grid");
  const shop = shopSelect("dashboard-shop", true);
  const from = el("div", "field"), to = el("div", "field");
  const fromLabel = el("label", "", "De"), toLabel = el("label", "", "Até");
  fromLabel.htmlFor = "dashboard-from"; toLabel.htmlFor = "dashboard-to";
  const fromInput = el("input"), toInput = el("input");
  fromInput.type = toInput.type = "date";
  fromInput.id = "dashboard-from"; toInput.id = "dashboard-to";
  from.append(fromLabel, fromInput); to.append(toLabel, toInput);
  row.append(shop.field);
  if(hasFeature("reports"))row.append(from,to);
  else filter.append(info("Filtros por período estão disponíveis nos planos Gestão e Avançado."));
  filter.append(row);
  view.outer.append(filter);
  const results = el("div");
  results.style.marginTop = "18px";
  view.outer.append(results);
  async function load() {
    const thisRequest = ++state.renderId;
    results.replaceChildren(loadingSurface());
    const params = new URLSearchParams();
    if (shop.select.value) params.set("shop_id", shop.select.value);
    if (fromInput.value) params.set("from", fromInput.value);
    if (toInput.value) params.set("to", toInput.value);
    try {
      const data = await api.get("/dashboard" + (params.size ? "?" + params : ""));
      if (thisRequest !== state.renderId) return;
      results.replaceChildren();
      const metrics = el("div", "metric-grid");
      metrics.append(
        metric("Faturamento registrado", money(data.revenue_cents), "Vendas recebidas"),
        metric("Vendas", String(data.sales_count ?? 0), "No período"),
        metric("Ticket médio", money(data.average_ticket_cents), "Valor por venda"),
        metric("Estoque abaixo do mínimo", String(data.low_stock_count ?? 0), "Itens para acompanhar")
      );
      results.append(metrics);
      const grid = el("div", "section-grid");
      const recent = surface("Vendas recentes", "Registros sincronizados do caixa local.");
      const sales = data.recent_sales || [];
      recent.append(sales.length
        ? table([{ label: "Venda", render: row => String(row.id || "").slice(0, 8) }, { label: "Total", render: row => money(row.total_cents) }, { label: "Quando", render: row => dateTime(row.created_at) }], sales.slice(0, 8), row => [{ label: "Ver", onClick: () => saleDetail(row.id) }])
        : empty("Sem vendas no período", "Quando houver eventos de venda recebidos, eles aparecerão aqui."));
      const finance = surface("Financeiro em aberto", "Valores de títulos cadastrados e ainda não liquidados.");
      finance.append(detailList([
        ["Contas a pagar", money(data.open_payables_cents)],
        ["Contas a receber", money(data.open_receivables_cents)]
      ]));
      grid.append(recent, finance);
      results.append(grid);
    } catch (error) {
      if (thisRequest !== state.renderId) return;
      results.replaceChildren(empty("Não foi possível carregar", errorText(error), "!"));
      handleError(error);
    }
  }
  if (token === state.renderId) load();
  [shop.select,...(hasFeature("reports")?[fromInput,toInput]:[])].forEach(input => input.addEventListener("change", load));
}

async function saleDetail(id) {
  const loading = el("div");
  loading.append(loadingSurface("Carregando venda…"));
  openDialog("Detalhes da venda", "Dados recebidos da operação local.", loading);
  try {
    const response = await api.get("/sales/" + encodeURIComponent(id));
    const sale = response.sale || response;
    const items = response.items || sale.items || [];
    const payments = response.payments || sale.payments || [];
    const content = el("div");
    content.append(info("O estado fiscal “Não emitido” indica que a NFC-e não foi gerada por esta versão.", true));
    content.append(detailList([
      ["Identificador", sale.id],
      ["Loja", shopName(sale.shop_id)],
      ["Terminal", sale.terminal_name || sale.terminal_id],
      ["Operador", sale.operator_name || "—"],
      ["Vendedor", sale.seller_name || "—"],
      ["Data", dateTime(sale.created_at)],
      ["Total", money(sale.total_cents)],
      ["Estado", statusBadge(sale.status)],
      ["Fiscal", statusBadge(sale.fiscal_state || sale.fiscal_status || "not_issued")]
    ]));
    const itemsTitle = el("h3", "", "Itens");
    itemsTitle.style.marginTop = "23px";
    content.append(itemsTitle);
    content.append(items.length
      ? table([{ label: "Produto", render: row => row.name }, { label: "Quantidade", render: row => quantity(row.quantity_milli, row.unit) }, { label: "Valor", render: row => money(row.line_total_cents) }], items)
      : empty("Sem itens disponíveis", "Os itens não foram enviados nesta resposta."));
    const payTitle = el("h3", "", "Pagamentos informados");
    payTitle.style.marginTop = "23px";
    content.append(payTitle);
    content.append(payments.length
      ? table([{ label: "Forma", render: row => paymentNames[row.method] || row.method }, { label: "Valor", render: row => money(row.amount_cents) }], payments)
      : empty("Sem pagamentos disponíveis", "Não há detalhes de pagamento nesta resposta."));
    const returns = response.returns || sale.returns || [];
    if (returns.length) {
      const returnTitle = el("h3", "", "Devoluções");
      returnTitle.style.marginTop = "23px";
      content.append(returnTitle, table([
        { label: "Data", render: r => dateTime(r.created_at) },
        { label: "Itens", render: r => r.items.map(i => `${i.name} × ${quantity(i.quantity_milli)}`).join(", ") },
        { label: "Valor", render: r => money(r.total_cents) },
        { label: "Devolvido em", render: r => refundNames[r.refund_method] || r.refund_method },
        { label: "Motivo", render: r => r.reason }
      ], returns));
    }
    if (canSales() && sale.status === "completed") {
      const actions = el("div", "form-actions");
      actions.append(button("Cancelar venda comercial", () => cancelSale(sale), "danger"));
      content.append(actions);
    }
    loading.replaceChildren(content);
  } catch (error) {
    loading.replaceChildren(empty("Não foi possível abrir a venda", errorText(error), "!"));
    handleError(error);
  }
}
function cancelSale(sale) {
  const body = el("div");
  body.append(info("O cancelamento estorna o estoque da venda uma vez. Ele não cancela nem emite documento fiscal.", true));
  const form = el("form", "auth-form");
  const field = el("div", "field");
  const label = el("label", "", "Motivo do cancelamento");
  label.htmlFor = "cancel-reason";
  const input = el("textarea");
  input.id = "cancel-reason";
  input.required = true; input.minLength = 3; input.maxLength = 240;
  field.append(label, input);
  const message = el("p", "feedback error");
  message.hidden = true;
  const actions = el("div", "form-actions");
  actions.append(button("Voltar", closeDialog, "ghost"));
  const submit = el("button", "button danger", "Confirmar cancelamento");
  submit.type = "submit";
  actions.append(submit);
  form.append(field, message, actions);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    submit.disabled = true;
    try {
      await api.post("/sales/" + sale.id + "/cancel", { reason: input.value.trim() });
      closeDialog(); toast("Venda comercial cancelada."); renderRoute();
    } catch (error) { handleError(error, message); submit.disabled = false; }
  });
  body.append(form);
  openDialog("Cancelar venda", "Esta ação altera registros financeiros e de estoque.", body);
}
async function salesPage() {
  const current = ++state.renderId;
  const view = page("Vendas", "Acompanhe as vendas recebidas dos terminais e abra os detalhes.", "Operação");
  view.outer.append(info("Pagamentos exibidos são informados pelo operador; a integração de pagamento e a NFC-e ainda não estão homologadas.", true));
  const box = el("section", "surface");
  const toolbar = el("div", "toolbar");
  const shop = shopSelect("sales-shop", true);
  const operator = personSelect("sales-operator", "Operador", "Todos os operadores");
  const seller = personSelect("sales-seller", "Vendedor", "Todos os vendedores");
  toolbar.append(shop.field, operator.field, seller.field, el("span", "toolbar-meta", "Vendas recebidas pela nuvem"));
  api.get("/sales/people").then(response => {
    const people = records(response);
    operator.fill(people.filter(row => row.operator));
    seller.fill(people.filter(row => row.seller));
  }).catch(handleError);
  box.append(toolbar);
  const content = el("div");
  box.append(content);
  view.outer.append(box);
  async function load(cursor = "") {
    const token = ++state.renderId;
    content.replaceChildren(loadingSurface());
    const params = new URLSearchParams({ limit: "30" });
    if (shop.select.value) params.set("shop_id", shop.select.value);
    if (operator.select.value) params.set("operator_id", operator.select.value);
    if (seller.select.value) params.set("seller_id", seller.select.value);
    if (cursor) params.set("cursor", cursor);
    try {
      const response = await api.get("/sales?" + params);
      if (token !== state.renderId) return;
      const rows = records(response);
      content.replaceChildren();
      if (!rows.length) {
        content.append(empty("Nenhuma venda recebida", "As vendas sincronizadas dos terminais aparecerão aqui."));
        return;
      }
      content.append(table([
        { label: "Venda", render: row => String(row.id).slice(0, 8) },
        { label: "Loja", render: row => shopName(row.shop_id) },
        { label: "Data", render: row => dateTime(row.created_at) },
        { label: "Operador / vendedor", render: row => [row.operator_name, row.seller_name].filter(Boolean).join(" / ") || "—" },
        { label: "Total", render: row => money(row.total_cents) },
        { label: "Estado", render: row => statusBadge(row.status) },
        { label: "Fiscal", render: row => statusBadge(row.fiscal_state || row.fiscal_status || "not_issued") }
      ], rows, row => [{ label: "Detalhes", onClick: () => saleDetail(row.id) }]));
      if (response.next_cursor) {
        const footer = el("div", "table-footer");
        footer.append(el("span", "", "Há mais vendas para consultar."));
        footer.append(button("Próxima página", () => load(response.next_cursor), "ghost", "small"));
        content.append(footer);
      }
    } catch (error) {
      if (token !== state.renderId) return;
      content.replaceChildren(empty("Não foi possível carregar", errorText(error), "!"));
      handleError(error);
    }
  }
  if (current === state.renderId) load();
  [shop.select, operator.select, seller.select].forEach(input => input.addEventListener("change", () => load()));
}
function personSelect(id, labelText, allText) {
  const field = el("div", "field");
  const label = el("label", "", labelText);
  label.htmlFor = id;
  const select = el("select");
  select.id = id;
  const all = el("option", "", allText);
  all.value = "";
  select.append(all);
  field.append(label, select);
  return { field, select, fill(people) {
    people.forEach(person => {
      const option = el("option", "", person.name);
      option.value = person.id;
      select.append(option);
    });
  } };
}

const creditSources = { return: "Devolução", sale: "Usado em venda", sale_cancel: "Venda cancelada", account_overpaid: "Devolução de fiado pago", manual: "Ajuste" };
const refundNames = { cash: "Dinheiro", store_credit: "Crédito na loja", account: "Abatido do fiado" };
async function customerStatement(customer) {
  const loading = el("div");
  loading.append(loadingSurface("Carregando extrato…"));
  openDialog("Extrato de " + customer.name, "Fiado, crédito na loja e devoluções do cliente.", loading);
  const data = await api.get(`/customers/${customer.id}/statement`);
  const content = el("div");
  content.append(detailList([
    ["Limite de fiado", money(data.customer.credit_limit_cents)],
    ["Fiado em aberto", money(data.open_credit_cents)],
    ["Disponível para fiado", money(data.available_credit_cents)],
    ["Crédito na loja", money(data.store_credit_cents)]
  ]));
  const openEntries = data.entries.filter(e => e.status === "open");
  const title = el("h3", "", "Vendas a prazo"); title.style.marginTop = "23px";
  content.append(title, data.entries.length ? table([
    { label: "Venda", render: e => e.description },
    { label: "Valor", render: e => money(e.amount_cents) },
    { label: "Vencimento", render: e => e.due_date.split("-").reverse().join("/") },
    { label: "Estado", render: e => e.overdue ? el("span", "badge danger", "Vencido") : statusBadge(e.status) }
  ], data.entries) : empty("Sem fiado", "Este cliente ainda não comprou a prazo."));
  if (openEntries.length && can("owner", "admin", "manager", "finance")) {
    const actions = el("div", "form-actions");
    actions.append(button(`Receber tudo em aberto (${money(openEntries.reduce((s, e) => s + e.amount_cents, 0))})`, () => confirmAction(
      "Receber fiado", "Confirme que o cliente pagou as parcelas em aberto. Elas serão baixadas como recebidas hoje.", async () => {
        await api.post(`/customers/${customer.id}/receive`, { entry_ids: openEntries.map(e => e.id) });
        toast("Fiado recebido."); renderRoute();
      }, "Confirmar recebimento")));
    content.append(actions);
  }
  const creditTitle = el("h3", "", "Crédito na loja"); creditTitle.style.marginTop = "23px";
  content.append(creditTitle, data.credits.length ? table([
    { label: "Data", render: c => dateTime(c.created_at) },
    { label: "Origem", render: c => creditSources[c.source] || c.source },
    { label: "Descrição", render: c => c.description },
    { label: "Valor", render: c => el("span", c.delta_cents < 0 ? "badge danger" : "badge", (c.delta_cents < 0 ? "− " : "+ ") + money(Math.abs(c.delta_cents))) }
  ], data.credits) : empty("Sem crédito na loja", "Devoluções em crédito aparecem aqui."));
  if (data.returns.length) {
    const returnTitle = el("h3", "", "Devoluções"); returnTitle.style.marginTop = "23px";
    content.append(returnTitle, table([
      { label: "Data", render: r => dateTime(r.created_at) }, { label: "Valor", render: r => money(r.total_cents) },
      { label: "Devolvido em", render: r => refundNames[r.refund_method] || r.refund_method }, { label: "Motivo", render: r => r.reason }
    ], data.returns));
  }
  loading.replaceChildren(content);
}
function percentText(bps) { return (Number(bps) / 100).toLocaleString("pt-BR", { maximumFractionDigits: 2 }) + "%"; }
const paymentNames = { cash: "Dinheiro (líquido de troco)", pix: "Pix", card_credit: "Crédito", card_debit: "Débito", card: "Cartão", credit_account: "A prazo (fiado)", store_credit: "Crédito na loja" };
function differenceBadge(cents) {
  if (cents == null) return statusBadge("open");
  const value = Number(cents);
  return el("span", "badge " + (value < 0 ? "danger" : value > 0 ? "warn" : ""),
    value === 0 ? "Sem diferença" : (value < 0 ? "Falta " : "Sobra ") + money(Math.abs(value)));
}
async function cashDetail(id) {
  const loading = el("div");
  loading.append(loadingSurface("Carregando caixa…"));
  openDialog("Conferência de caixa", "Abertura, vendas por forma, movimentos e fechamento.", loading);
  try {
    const row = await api.get("/cash-sessions/" + encodeURIComponent(id));
    const content = el("div");
    if (row.incomplete) content.append(info("A abertura deste caixa não foi recebida pela nuvem; o valor de abertura pode estar incompleto.", true));
    content.append(detailList([
      ["Loja", shopName(row.shop_id)], ["Terminal", row.terminal_name], ["Aberto por", row.operator_name || "—"],
      ["Abertura", dateTime(row.opened_at) + " · " + money(row.opening_cents)],
      ["Fechamento", row.closed_at ? dateTime(row.closed_at) : "Caixa aberto"],
      ["Fechado por", [row.closed_by_name, row.supervisor_name && "autorizado por " + row.supervisor_name].filter(Boolean).join(" · ") || "—"],
      ["Vendas", `${row.sales_count} · ${money(row.sales_total_cents)}`],
      ["Esperado em dinheiro", row.expected_cents == null ? "—" : money(row.expected_cents)],
      ["Contado", row.counted_cents == null ? "—" : money(row.counted_cents)],
      ["Diferença", differenceBadge(row.difference_cents)]
    ]));
    const methods = Object.entries(row.by_method || {});
    const payTitle = el("h3", "", "Recebido por forma de pagamento");
    payTitle.style.marginTop = "23px";
    content.append(payTitle, methods.length
      ? table([{ label: "Forma", render: r => paymentNames[r[0]] || r[0] }, { label: "Valor", render: r => money(r[1]) }], methods)
      : empty("Sem vendas neste caixa", "Nenhuma venda concluída foi sincronizada para este turno."));
    const moveTitle = el("h3", "", "Sangrias e suprimentos");
    moveTitle.style.marginTop = "23px";
    content.append(moveTitle, row.movements.length
      ? table([{ label: "Tipo", render: m => m.kind === "withdrawn" ? "Sangria" : "Suprimento" },
               { label: "Valor", render: m => money(m.amount_cents) }, { label: "Motivo", render: m => m.reason },
               { label: "Operador", render: m => m.operator_name || "—" }, { label: "Hora", render: m => dateTime(m.created_at) }], row.movements)
      : empty("Sem movimentos", "Não houve sangria nem suprimento neste turno."));
    loading.replaceChildren(content);
  } catch (error) {
    loading.replaceChildren(empty("Não foi possível abrir o caixa", errorText(error), "!"));
    handleError(error);
  }
}
async function cashPage() {
  const current = ++state.renderId;
  const view = page("Caixas", "Confira abertura, vendas por forma de pagamento, sangrias e quebra de cada turno.", "Operação");
  const box = el("section", "surface");
  const toolbar = el("div", "toolbar");
  const shop = shopSelect("cash-shop", true);
  const statusField = el("div", "field");
  const statusLabel = el("label", "", "Estado");
  statusLabel.htmlFor = "cash-status";
  const status = el("select");
  status.id = "cash-status";
  [["", "Todos"], ["open", "Abertos"], ["closed", "Fechados"], ["shortage", "Com falta"]].forEach(([value, text]) => {
    const option = el("option", "", text); option.value = value; status.append(option);
  });
  statusField.append(statusLabel, status);
  toolbar.append(shop.field, statusField);
  box.append(toolbar);
  const content = el("div");
  box.append(content);
  view.outer.append(box);
  async function load(cursor = "") {
    const token = ++state.renderId;
    content.replaceChildren(loadingSurface());
    const params = new URLSearchParams({ limit: "30" });
    if (shop.select.value) params.set("shop_id", shop.select.value);
    if (status.value) params.set("status", status.value);
    if (cursor) params.set("cursor", cursor);
    try {
      const response = await api.get("/cash-sessions?" + params);
      if (token !== state.renderId) return;
      const rows = records(response);
      content.replaceChildren();
      if (!rows.length) {
        content.append(empty("Nenhum caixa encontrado", "Os turnos abertos e fechados nos terminais aparecerão aqui após a sincronização."));
        return;
      }
      content.append(table([
        { label: "Terminal", render: row => `${row.terminal_name} · ${shopName(row.shop_id)}` },
        { label: "Operador", render: row => row.operator_name || "—" },
        { label: "Abertura", render: row => dateTime(row.opened_at) },
        { label: "Fechamento", render: row => row.closed_at ? dateTime(row.closed_at) : statusBadge("open") },
        { label: "Vendido", render: row => money(row.sales_total_cents) },
        { label: "Sangrias", render: row => money(row.withdrawn_cents) },
        { label: "Quebra", render: row => differenceBadge(row.difference_cents) }
      ], rows, row => [{ label: "Conferir", onClick: () => cashDetail(row.id) }]));
      if (response.next_cursor) {
        const footer = el("div", "table-footer");
        footer.append(el("span", "", "Há mais caixas para consultar."));
        footer.append(button("Próxima página", () => load(response.next_cursor), "ghost", "small"));
        content.append(footer);
      }
    } catch (error) {
      if (token !== state.renderId) return;
      content.replaceChildren(empty("Não foi possível carregar", errorText(error), "!"));
      handleError(error);
    }
  }
  if (current === state.renderId) load();
  [shop.select, status].forEach(input => input.addEventListener("change", () => load()));
}

async function commissionsPage() {
  const current = ++state.renderId;
  const view = page("Comissões", "Comissão acumulada por vendedor a partir das vendas sincronizadas. Cancelamentos estornam automaticamente.", "Relacionamento");
  view.outer.append(info("Defina o percentual de cada pessoa em Equipe e, se quiser, um percentual por categoria. O pagamento gera uma conta a pagar no Financeiro."));
  const box = el("section", "surface");
  const content = el("div");
  box.append(content);
  view.outer.append(box);
  const canPay = ["owner", "admin", "finance"].includes(state.role);
  async function load() {
    content.replaceChildren(loadingSurface());
    try {
      const rows = records(await api.get("/commissions/summary"));
      if (current !== state.renderId) return;
      content.replaceChildren();
      if (!rows.length) {
        content.append(empty("Nenhuma comissão ainda", "Configure percentuais em Equipe e escolha o vendedor no caixa."));
        return;
      }
      content.append(table([
        { label: "Vendedor", render: r => r.seller_name },
        { label: "Percentual", render: r => percentText(r.commission_bps) },
        { label: "Vendas", render: r => String(r.sales) },
        { label: "Base", render: r => money(r.base_cents) },
        { label: "A pagar", render: r => money(r.pending_cents) },
        { label: "Pago", render: r => money(r.paid_cents) }
      ], rows, r => canPay && r.pending_cents > 0 ? [{ label: "Pagar", onClick: () => payCommission(r) }] : []));
    } catch (error) {
      content.replaceChildren(empty("Não foi possível carregar", errorText(error), "!"));
      handleError(error);
    }
  }
  async function payCommission(row) {
    const fields = [{ key: "due_date", label: "Vencimento da conta a pagar", type: "date", required: true, default: new Date().toISOString().slice(0, 10) }];
    const form = await formFor(fields, null);
    form.prepend(info(`Fechar ${money(row.pending_cents)} de comissão de ${row.seller_name} em uma conta a pagar.`));
    const message = formMessage(form), submit = formButtons(form, "Gerar conta a pagar");
    form.addEventListener("submit", async event => {
      event.preventDefault(); if (!form.reportValidity()) return;
      submit.disabled = true;
      try {
        await api.post("/commissions/pay", { seller_id: row.seller_id, ...formPayload(form, fields) });
        closeDialog(); toast("Comissão fechada no Financeiro."); load();
      } catch (error) { handleError(error, message); submit.disabled = false; }
    });
    openDialog("Pagar comissão", "As comissões pendentes viram uma conta a pagar.", form);
  }
  load();
}

function reportCell(value, type) {
  if (value == null) return "—";
  if (type === "money") return money(value);
  if (type === "percent") return percentText(value);
  if (type === "milli") return (Number(value) / 1000).toLocaleString("pt-BR", { maximumFractionDigits: 3 });
  if (type === "int") return Number(value).toLocaleString("pt-BR");
  return String(value);
}
async function reportsPage() {
  const current = ++state.renderId;
  const view = page("Relatórios", "Vendas, lucro, curva ABC, fluxo de caixa e reposição. Exporte em CSV para Excel ou contabilidade.", "Acompanhar");
  view.outer.append(info("Lucro e custo usam o custo médio atual de cada produto; mantenha as entradas de estoque com custo para valores corretos."));
  const box = el("section", "surface");
  const toolbar = el("div", "toolbar");
  const kindField = el("div", "field"), kindLabel = el("label", "", "Relatório"), kind = el("select");
  kind.id = "report-kind"; kindLabel.htmlFor = kind.id; kindField.append(kindLabel, kind);
  const today = new Date(), first = new Date(today.getFullYear(), today.getMonth(), 1);
  const iso = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const dateField = (id, text, value) => {
    const field = el("div", "field"), label = el("label", "", text), input = el("input");
    input.type = "date"; input.id = id; input.value = value; label.htmlFor = id; field.append(label, input);
    return { field, input };
  };
  const from = dateField("report-from", "De", iso(first)), to = dateField("report-to", "Até", iso(today));
  const shop = shopSelect("report-shop", true);
  const exportButton = button("Exportar CSV", () => exportCsv(), "ghost");
  toolbar.append(kindField, from.field, to.field, shop.field, exportButton);
  box.append(toolbar);
  const content = el("div");
  box.append(content);
  view.outer.append(box);
  function params() {
    const p = new URLSearchParams({ from: from.input.value, to: to.input.value });
    if (shop.select.value) p.set("shop_id", shop.select.value);
    return p;
  }
  async function load() {
    const token = ++state.renderId;
    content.replaceChildren(loadingSurface());
    try {
      const data = await api.get(`/reports/${encodeURIComponent(kind.value)}?` + params());
      if (token !== state.renderId) return;
      content.replaceChildren();
      if (!data.rows.length) { content.append(empty("Sem dados no período", "Ajuste o período ou a loja.")); return; }
      const rows = Object.keys(data.totals || {}).length ? [...data.rows, { __total: true, ...data.totals }] : data.rows;
      content.append(table(data.columns.map((c, i) => ({ label: c.label,
        render: row => row.__total ? (c.key in row ? el("strong", "", reportCell(row[c.key], c.type)) : i === 0 ? el("strong", "", "Total") : "")
                                   : reportCell(row[c.key], c.type) })), rows));
    } catch (error) {
      if (token !== state.renderId) return;
      content.replaceChildren(empty("Não foi possível gerar o relatório", errorText(error), "!"));
      handleError(error);
    }
  }
  async function exportCsv() {
    const p = params(); p.set("format", "csv");
    try {
      const response = await fetch(`/api/v1/reports/${encodeURIComponent(kind.value)}?` + p,
        { credentials: "same-origin", headers: { "X-Tenant-ID": state.tenantId } });
      if (!response.ok) throw new Error("Não foi possível exportar o relatório.");
      const url = URL.createObjectURL(await response.blob());
      const link = el("a"); link.href = url; link.download = `${kind.value}_${from.input.value}_${to.input.value}.csv`;
      document.body.append(link); link.click(); link.remove(); URL.revokeObjectURL(url);
    } catch (error) { handleError(error); }
  }
  try {
    const catalog = records(await api.get("/reports"));
    if (current !== state.renderId) return;
    catalog.forEach(r => { const option = el("option", "", r.title); option.value = r.id; kind.append(option); });
  } catch (error) { content.replaceChildren(empty("Relatórios indisponíveis", errorText(error), "!")); handleError(error); return; }
  [kind, from.input, to.input, shop.select].forEach(input => input.addEventListener("change", load));
  load();
}

function idempotency(form, payload) {
  const signature = JSON.stringify(payload);
  if (form.dataset.signature !== signature) {
    form.dataset.signature = signature;
    form.dataset.key = globalThis.crypto?.randomUUID?.() || Date.now() + "-" + Math.random();
  }
  return form.dataset.key;
}
async function productPicker(idPrefix) {
  const wrap = el("div", "field");
  const label = el("label", "", "Produto");
  label.htmlFor = idPrefix + "-product";
  const search = el("input");
  search.type = "search"; search.placeholder = "Buscar nome, SKU ou código"; search.maxLength = 120;
  search.setAttribute("aria-label", "Buscar produto");
  const select = el("select");
  select.id = idPrefix + "-product";
  select.required = true;
  const helper = el("small", "", "Digite para encontrar itens em catálogos maiores.");
  wrap.append(label, search, select, helper);
  let serial = 0, timer, products = [];
  async function load(query = "") {
    const requestId = ++serial;
    const result = await api.get("/products" + (query ? "?q=" + encodeURIComponent(query) : ""));
    if (requestId !== serial) return;
    const current = select.value;
    select.replaceChildren();
    products = records(result).filter(item => item.active !== false);
    products.forEach(product => {
      const option = el("option", "", product.name + " · " + product.sku);
      option.value = product.id;
      select.append(option);
    });
    if (products.some(item => String(item.id) === String(current))) select.value = current;
    if (!products.length) {
      const option = el("option", "", "Nenhum produto encontrado");
      option.value = "";
      select.append(option);
    }
  }
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => load(search.value.trim()).catch(error => { helper.textContent = errorText(error); }), 230);
  });
  await load();
  return { wrap, select, getSelected: () => products.find(item => String(item.id) === String(select.value)) || null };
}
async function stockAdjustment(shopId) {
  const body = el("div");
  const form = el("form", "form-grid");
  const picker = await productPicker("adjust");
  picker.wrap.classList.add("full");
  const quantityField = el("div", "field");
  const quantityLabel = el("label", "", "Variação de quantidade");
  quantityLabel.htmlFor = "adjust-quantity";
  const amount = el("input");
  amount.id = "adjust-quantity"; amount.type = "number"; amount.step = "0.001"; amount.required = true;
  quantityField.append(quantityLabel, amount, el("small", "", "Use valor positivo para entrada e negativo para saída."));
  const reasonField = el("div", "field");
  const reasonLabel = el("label", "", "Motivo");
  reasonLabel.htmlFor = "adjust-reason";
  const reason = el("input");
  reason.id = "adjust-reason"; reason.required = true; reason.minLength = 3; reason.maxLength = 240;
  reasonField.append(reasonLabel, reason);
  const message = el("p", "feedback error full"); message.hidden = true;
  const actions = el("div", "form-actions full");
  actions.append(button("Cancelar", closeDialog, "ghost"));
  const submit = el("button", "button", "Registrar ajuste");
  submit.type = "submit"; actions.append(submit);
  form.append(picker.wrap, quantityField, reasonField, message, actions);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const delta = Math.round(Number(amount.value) * 1000);
    if (!Number.isSafeInteger(delta) || !delta) { handleError(new Error("Informe uma quantidade diferente de zero."), message); return; }
    const payload = { shop_id: shopId, product_id: picker.select.value, delta_milli: delta, reason: reason.value.trim() };
    payload.idempotency_key = idempotency(form, payload);
    submit.disabled = true;
    try {
      await api.post("/stock/movements", payload);
      closeDialog(); toast("Estoque atualizado."); renderRoute();
    } catch (error) { handleError(error, message); submit.disabled = false; }
  });
  body.append(form);
  openDialog("Ajustar estoque", "A movimentação ficará registrada para auditoria.", body);
}
async function stockReceipt(shopId) {
  const suppliers = records(await api.get("/suppliers"));
  const body = el("div");
  const form = el("form");
  const fields = el("div", "form-grid");
  const supplierField = el("div", "field");
  const supplierLabel = el("label", "", "Fornecedor");
  supplierLabel.htmlFor = "receipt-supplier";
  const supplier = el("select"); supplier.id = "receipt-supplier";
  const blank = el("option", "", "Sem fornecedor vinculado"); blank.value = ""; supplier.append(blank);
  suppliers.forEach(item => { const option = el("option", "", item.name); option.value = item.id; supplier.append(option); });
  supplierField.append(supplierLabel, supplier);
  const referenceField = el("div", "field");
  const referenceLabel = el("label", "", "Referência");
  referenceLabel.htmlFor = "receipt-reference";
  const reference = el("input"); reference.id = "receipt-reference"; reference.maxLength = 120; reference.placeholder = "Ex.: NF 123";
  referenceField.append(referenceLabel, reference);
  fields.append(supplierField, referenceField);
  form.append(fields);
  const heading = el("h3", "", "Itens da entrada");
  heading.style.marginTop = "25px";
  form.append(heading);
  const rows = el("div");
  form.append(rows);
  async function addRow() {
    const number = rows.children.length + 1;
    const item = el("div", "surface pad");
    item.style.marginTop = "10px";
    const grid = el("div", "form-grid");
    const picker = await productPicker("receipt-" + number);
    const qtyField = el("div", "field");
    const qtyLabel = el("label", "", "Quantidade");
    qtyLabel.htmlFor = "receipt-quantity-" + number;
    const qty = el("input"); qty.type = "number"; qty.step = "0.001"; qty.min = "0.001"; qty.required = true; qty.id = qtyLabel.htmlFor;
    qtyField.append(qtyLabel, qty);
    const costField = el("div", "field");
    const costLabel = el("label", "", "Custo desta entrada (R$)");
    costLabel.htmlFor = "receipt-cost-" + number;
    const cost = el("input"); cost.type = "text"; cost.inputMode = "decimal"; cost.placeholder = "0,00"; cost.required = true; cost.id = costLabel.htmlFor;
    cost.addEventListener("blur", () => { const cents = parseMoneyInput(cost.value); if (Number.isSafeInteger(cents) && cents >= 0) cost.value = editableMoney(cents); });
    costField.append(costLabel, cost, el("small", "", "Informe o valor pago nesta entrada. O sistema calcula o custo médio automaticamente."));
    grid.append(picker.wrap, qtyField, costField);
    item.append(grid);
    const remove = button("Remover item", () => item.remove(), "ghost", "small");
    remove.style.marginTop = "10px";
    item.append(remove);
    item._getValue = () => ({
      product_id: picker.select.value,
      quantity_milli: Math.round(Number(qty.value) * 1000),
      unit_cost_cents: parseMoneyInput(cost.value),
    });
    rows.append(item);
  }
  const add = button("+ Adicionar item", () => addRow().catch(handleError), "secondary", "small");
  add.style.marginTop = "12px";
  form.append(add);
  const message = el("p", "feedback error"); message.hidden = true;
  const actions = el("div", "form-actions");
  actions.append(button("Cancelar", closeDialog, "ghost"));
  const submit = el("button", "button", "Registrar entrada"); submit.type = "submit";
  actions.append(submit);
  form.append(message, actions);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const items = [...rows.children].map(row => row._getValue());
    if (!items.length || items.some(item => !Number.isSafeInteger(item.quantity_milli) || item.quantity_milli <= 0 || !Number.isSafeInteger(item.unit_cost_cents) || item.unit_cost_cents < 0 || !item.product_id)) {
      handleError(new Error("Inclua pelo menos um item com quantidade e custo válidos."), message); return;
    }
    const payload = { shop_id: shopId, items };
    if (supplier.value) payload.supplier_id = supplier.value;
    if (reference.value.trim()) payload.reference = reference.value.trim();
    payload.idempotency_key = idempotency(form, payload);
    submit.disabled = true;
    try {
      await api.post("/stock/receipts", payload);
      closeDialog(); toast("Entrada registrada."); renderRoute();
    } catch (error) { handleError(error, message); submit.disabled = false; }
  });
  body.append(form);
  openDialog("Entrada de mercadoria", "Informe o custo pago nesta entrada. O histórico fica registrado e o custo médio é recalculado automaticamente.", body);
  await addRow();
}
function csvCells(line, delimiter) {
  const cells = []; let cell = ""; let quoted = false;
  for (let index = 0; index < line.length; index++) {
    const char = line[index];
    if (char === '"' && line[index + 1] === '"') { cell += '"'; index++; continue; }
    if (char === '"') { quoted = !quoted; continue; }
    if (char === delimiter && !quoted) { cells.push(cell.trim()); cell = ""; continue; }
    cell += char;
  }
  cells.push(cell.trim());
  return cells;
}
function normalizedHeader(value) {
  return String(value || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/[^a-z0-9]+/g, " ").trim();
}
function importMoney(raw) {
  const cents = parseMoneyInput(raw);
  if (!Number.isSafeInteger(cents) || cents < 0) throw new Error(`Valor de compra inválido: ${raw}`);
  return cents;
}
async function stockReceiptImport(shopId) {
  const body = el("div");
  const form = el("form", "form-grid");
  const fileField = el("div", "field full");
  const label = el("label", "", "Arquivo de compra (CSV)");
  const file = el("input"); file.type = "file"; file.accept = ".csv,.txt,text/csv"; file.required = true;
  const hint = el("small", "", "Colunas aceitas: SKU ou código de barras, quantidade e custo unitário. Use ponto e vírgula ou vírgula.");
  fileField.append(label, file, hint); form.append(fileField);
  const summary = el("div", "info-strip"); summary.textContent = "Escolha o arquivo para conferir os itens antes de registrar."; form.append(summary);
  const message = el("p", "feedback error full"); message.hidden = true;
  const actions = el("div", "form-actions full"); actions.append(button("Cancelar", closeDialog, "ghost"));
  const submit = el("button", "button", "Registrar compra"); submit.type = "submit"; submit.disabled = true; actions.append(submit); form.append(message, actions); body.append(form);
  let parsed = null;
  file.addEventListener("change", async () => {
    parsed = null; submit.disabled = true; message.hidden = true;
    try {
      const selected = file.files?.[0];
      if (!selected) return;
      if (selected.size > 10 * 1024 * 1024) throw new Error("O arquivo deve ter no máximo 10 MB.");
      const text = await selected.text();
      const lines = text.split(/\r?\n/).filter(line => line.trim());
      if (lines.length < 2) throw new Error("O CSV precisa ter cabeçalho e pelo menos um item.");
      const delimiter = lines[0].includes(";") ? ";" : ",";
      const headers = csvCells(lines[0], delimiter).map(normalizedHeader);
      const find = names => names.map(normalizedHeader).map(name => headers.indexOf(name)).find(index => index >= 0);
      const identityIndex = find(["sku", "codigo", "codigo interno", "barcode", "codigo de barras", "ean", "gtin"]);
      const quantityIndex = find(["quantidade", "qtd", "quant", "quantity"]);
      const costIndex = find(["custo", "custo unitario", "valor compra", "preco compra", "unit cost"]);
      if (identityIndex === undefined || quantityIndex === undefined || costIndex === undefined) throw new Error("O cabeçalho precisa conter SKU ou código de barras, quantidade e custo unitário.");
      const products = []; let cursor = "";
      do {
        const response = await api.get("/products?limit=100" + (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""));
        products.push(...records(response)); cursor = response.next_cursor || "";
      } while (cursor);
      const byCode = new Map(products.flatMap(product => [[String(product.sku).toUpperCase(), product], [String(product.barcode), product]]));
      const items = []; const unknown = [];
      for (let rowIndex = 1; rowIndex < lines.length; rowIndex++) {
        const cells = csvCells(lines[rowIndex], delimiter); const code = String(cells[identityIndex] || "").trim();
        if (!code) continue;
        const product = byCode.get(code.toUpperCase());
        if (!product) { unknown.push(code); continue; }
        const rawQuantity = String(cells[quantityIndex] || "").trim().replace(/\s/g, "").includes(",")
          ? String(cells[quantityIndex] || "").trim().replace(/\./g, "").replace(",", ".")
          : String(cells[quantityIndex] || "").trim();
        const quantityMilli = Math.round(Number(rawQuantity) * 1000);
        if (!Number.isSafeInteger(quantityMilli) || quantityMilli <= 0) throw new Error(`Quantidade inválida na linha ${rowIndex + 1}.`);
        const item = { product_id: product.id, quantity_milli: quantityMilli };
        if (!String(cells[costIndex] || "").trim()) throw new Error(`Custo ausente na linha ${rowIndex + 1}.`);
        item.unit_cost_cents = importMoney(cells[costIndex]);
        items.push(item);
      }
      if (unknown.length) throw new Error(`Produtos não encontrados: ${unknown.slice(0, 5).join(", ")}${unknown.length > 5 ? "…" : ""}`);
      if (!items.length) throw new Error("Nenhum item válido foi encontrado no arquivo.");
      const buffer = await selected.arrayBuffer();
      const digest = await crypto.subtle.digest("SHA-256", buffer);
      const hash = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("");
      parsed = { items, source_file: { name: selected.name, mime: selected.type || "text/csv", size: selected.size, sha256: hash }, reference: selected.name };
      summary.className = "info-strip"; summary.textContent = `${items.length} item(ns) prontos para entrada. O arquivo ${selected.name} ficará vinculado ao registro.`; submit.disabled = false;
    } catch (error) { message.textContent = errorText(error); message.hidden = false; }
  });
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (!parsed) return;
    submit.disabled = true; message.hidden = true;
    try { await api.post("/stock/receipts", { shop_id: shopId, ...parsed, idempotency_key: idempotency(form, parsed) }); closeDialog(); toast("Compra importada e estoque atualizado."); renderRoute(); }
    catch (error) { handleError(error, message); submit.disabled = false; }
  });
  openDialog("Importar compra para o estoque", "O arquivo é conferido antes de lançar quantidade e custo dos produtos.", body);
}
async function stockPage() {
  const current = ++state.renderId;
  const view = page("Estoque", "Consulte saldos por loja, ajustes e entradas de mercadoria.", "Operação");
  const filter = surface("Filtrar estoque");
  const shop = shopSelect("stock-shop");
  const searchField=el("div","field");
  const searchLabel=el("label","","Buscar produto");searchLabel.htmlFor="stock-search";
  const search=el("input");search.id="stock-search";search.type="search";search.maxLength=120;search.placeholder="Nome, SKU ou código de barras";
  searchField.append(searchLabel,search);
  const filterFields=el("div","form-grid");filterFields.append(shop.field,searchField);
  filter.append(filterFields);
  view.outer.append(filter);
  const results = el("div");
  results.style.marginTop = "16px";
  view.outer.append(results);
  if (!state.shops.length) {
    results.append(empty("Cadastre uma loja", "O estoque é controlado por loja."));
    return;
  }
  if (canStock()) {
    view.actions.append(button("Ajustar estoque", () => stockAdjustment(shop.select.value).catch(handleError), "ghost"));
    view.actions.append(button("+ Registrar entrada", () => stockReceipt(shop.select.value).catch(handleError)));
    view.actions.append(button("Importar compra CSV", () => stockReceiptImport(shop.select.value).catch(handleError), "secondary"));
  }
  async function load() {
    const token = ++state.renderId;
    results.replaceChildren(loadingSurface());
    try {
      const id = encodeURIComponent(shop.select.value);
      const query=search.value.trim();
      const balancePath="/stock?shop_id="+id+(query?"&q="+encodeURIComponent(query):"");
      const [balance, movementResult, receiptResult] = await Promise.all([
        api.get(balancePath),
        api.get("/stock/movements?shop_id=" + id),
        api.get("/stock/receipts?shop_id=" + id)
      ]);
      if (token !== state.renderId) return;
      results.replaceChildren();
      const balances = records(balance);
      const balanceBox = surface("Saldos atuais", "O saldo central pode exigir reconciliação de eventos offline.");
      const columns=[
            { label: "Produto", render: row => row.name },
            { label: "SKU", render: row => row.sku },
            { label: "Saldo", render: row => row.stock_controlled === false ? "Produção própria" : quantity(row.quantity_milli, row.unit) },
            { label: "Mínimo", render: row => row.stock_controlled === false ? "—" : quantity(row.stock_minimum_milli, row.unit) },
            { label: "Atenção", render: row => row.below_minimum ? statusBadge("Baixo") : statusBadge("Regular") }
          ];
      const balanceContent=el("div");
      balanceContent.append(balances.length?table(columns,balances):empty(query?"Nenhum resultado":"Sem produtos nesta loja",query?"Tente outro termo de busca.":"Cadastre produtos para consultar saldos."));
      balanceBox.append(balanceContent);
      if(balance.next_cursor){
        const footer=el("div","table-footer");
        const more=button("Carregar mais",async()=>{
          more.disabled=true;
          try{
            const next=await api.get(balancePath+"&cursor="+encodeURIComponent(balance.next_cursor));
            if(token!==state.renderId)return;
            balances.push(...records(next));
            balanceContent.replaceChildren(table(columns,balances));
            balance.next_cursor=next.next_cursor;
            if(!next.next_cursor)footer.remove();else more.disabled=false;
          }catch(error){handleError(error);more.disabled=false;}
        },"ghost","small");
        footer.append(el("span","","Mais produtos disponíveis."),more);
        balanceBox.append(footer);
      }
      results.append(balanceBox);
      const grid = el("div", "section-grid");
      grid.style.marginTop = "16px";
      const movementBox = surface("Movimentações recentes", "Ajustes e baixas registrados na nuvem.");
      const movements = records(movementResult);
      movementBox.append(movements.length
        ? table([{ label: "Produto", render: row => row.product_name || row.product_id }, { label: "Variação", render: row => quantity(row.delta_milli, row.unit || "UN") }, { label: "Motivo", render: row => row.reason }, { label: "Data", render: row => dateTime(row.created_at) }], movements.slice(0, 20))
        : empty("Sem movimentações", "Os ajustes registrados aparecerão aqui."));
      const receiptBox = surface("Entradas recentes", "Mercadorias recebidas e aplicadas ao saldo.");
      const receipts = records(receiptResult);
      receiptBox.append(receipts.length
        ? table([{ label: "Referência", render: row => row.source_file?.name || row.reference || String(row.id).slice(0, 8) }, { label: "Fornecedor", render: row => row.supplier_name || row.supplier_id }, { label: "Data", render: row => dateTime(row.created_at) }], receipts.slice(0, 20))
        : empty("Sem entradas", "Registre a primeira entrada de mercadoria."));
      grid.append(movementBox, receiptBox);
      results.append(grid);
      const costHistory = receipts.flatMap(receipt => (receipt.items || []).map(item => ({
        ...item,
        reference: receipt.source_file?.name || receipt.reference || String(receipt.id).slice(0, 8),
        created_at: receipt.created_at
      })));
      const costHistoryBox = surface("Histórico de custos", "Cada entrada preserva o custo pago e o custo médio após o lançamento.");
      costHistoryBox.append(costHistory.length
        ? table([
          { label: "Produto", render: row => row.product_name || row.product_id },
          { label: "Quantidade", render: row => quantity(row.quantity_milli, row.unit || "UN") },
          { label: "Custo da entrada", render: row => money(row.unit_cost_cents) },
          { label: "Custo médio após entrada", render: row => row.average_cost_cents == null ? "—" : money(row.average_cost_cents) },
          { label: "Referência", render: row => row.reference },
          { label: "Data", render: row => dateTime(row.created_at) }
        ], costHistory.slice(0, 40))
        : empty("Sem histórico de custos", "Os custos informados nas entradas aparecerão aqui."));
      costHistoryBox.style.marginTop = "16px";
      results.append(costHistoryBox);
    } catch (error) {
      if (token !== state.renderId) return;
      results.replaceChildren(empty("Não foi possível carregar o estoque", errorText(error), "!"));
      handleError(error);
    }
  }
  if (current === state.renderId) load();
  shop.select.addEventListener("change", load);
  let searchTimer;
  search.addEventListener("input",()=>{
    clearTimeout(searchTimer);
    searchTimer=setTimeout(load,250);
  });
}

function formMessage(form) {
  const node = el("p", "feedback error");
  node.hidden = true;
  form.append(node);
  return node;
}
function formButtons(form, label) {
  const actions = el("div", "form-actions");
  const submit = el("button", "button", label);
  submit.type = "submit";
  actions.append(button("Cancelar", closeDialog, "ghost"), submit);
  form.append(actions);
  return submit;
}
async function financeForm() {
  const form = await formFor([
    { key: "kind", label: "Tipo", type: "select", required: true, choices: [["payable","Conta a pagar"],["receivable","Conta a receber"]] },
    { key: "description", label: "Descrição", required: true, full: true, max: 180 },
    { key: "amount_cents", label: "Valor (R$)", type: "money", required: true, min: 0 },
    { key: "due_date", label: "Vencimento", type: "date", required: true },
    { key: "customer_id", label: "Cliente", type: "source", source: "/customers", valueKey: "id", labelKey: "name" },
    { key: "supplier_id", label: "Fornecedor", type: "source", source: "/suppliers", valueKey: "id", labelKey: "name" }
  ]);
  const message = formMessage(form), submit = formButtons(form, "Cadastrar título");
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    submit.disabled = true;
    try {
      const payload = formPayload(form, [
        { key:"kind" },{ key:"description" },{ key:"amount_cents",type:"money" },
        { key:"due_date" },{ key:"customer_id" },{ key:"supplier_id" }
      ]);
      if (payload.amount_cents <= 0) throw new Error("Informe um valor maior que zero.");
      if (payload.kind === "payable") delete payload.customer_id;
      if (payload.kind === "receivable") delete payload.supplier_id;
      await api.post("/finance", payload);
      closeDialog(); toast("Título cadastrado."); renderRoute();
    } catch (error) { handleError(error, message); submit.disabled = false; }
  });
  openDialog("Novo título", "Cadastre uma conta a pagar ou a receber.", form);
}
async function financePage() {
  const token = ++state.renderId;
  const view = page("Financeiro", "Títulos a pagar e a receber registrados pela empresa.", "Relacionamento");
  if (canFinance()) view.actions.append(button("+ Novo título", () => financeForm().catch(handleError)));
  const box = surface("Títulos", "A liquidação registra o valor integral nesta versão.");
  view.outer.append(box);
  box.append(loadingSurface());
  try {
    const response = await api.get("/finance");
    if (token !== state.renderId) return;
    box.lastChild.remove();
    const rows = records(response);
    box.append(pagedContent(response,"/finance",[
      { label:"Tipo", render: row => row.kind === "payable" ? "A pagar" : "A receber" },
      { label:"Descrição", render: row => row.description },
      { label:"Valor", render: row => money(row.amount_cents) },
      { label:"Vencimento", render: row => dateOnly(row.due_date) },
      { label:"Estado", render: row => statusBadge(row.status || (row.settled_at ? "settled" : "open")) }
    ], canFinance() ? row => row.status !== "open" ? [] : [{ label:"Liquidar", onClick: () => confirmAction("Liquidar título", "Confirme o pagamento ou recebimento integral de " + money(row.amount_cents) + ".", async () => { await api.post("/finance/" + row.id + "/settle", {}); toast("Título liquidado."); renderRoute(); }, "Liquidar") }] : null,
      empty("Nenhum título cadastrado", "Os lançamentos financeiros aparecerão aqui.")));
  } catch (error) { if (token === state.renderId) { box.replaceChildren(empty("Falha ao carregar", errorText(error), "!")); handleError(error); } }
}

const userFields = [
  { key:"name", label:"Nome", required:true, max:160, full:true },
  { key:"email", label:"E-mail", type:"email", required:true, max:254, full:true },
  { key:"password", label:"Senha de acesso", type:"password", required:true, minLength:8, max:128, full:true, help:"Mínimo de 8 caracteres, incluindo letra maiúscula, minúscula, número e símbolo." },
  { key:"role", label:"Papel", type:"select", required:true, choices:[["admin","Administrador"],["manager","Gerente"],["cashier","Operador de caixa"],["inventory","Estoque"],["finance","Financeiro"],["viewer","Consulta"]] },
  { key:"pin", label:"PIN offline (opcional)", type:"password", placeholder:"6 a 12 dígitos", max:12, help:"Usado apenas no terminal local." }
];
async function userCreate() {
  const fields = userFields.map(field => field.key === "role" && state.role === "owner"
    ? { ...field, choices:[["owner","Proprietário"],...field.choices] } : field);
  const form = await formFor(fields);
  const message = formMessage(form), submit = formButtons(form, "Adicionar pessoa");
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (!form.reportValidity()) return;
    submit.disabled = true;
    try {
      const payload = formPayload(form, fields);
      if (!/^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z\d]).{8,}$/.test(payload.password)) throw new Error("A senha precisa ter 8 caracteres, maiúscula, minúscula, número e símbolo.");
      if (payload.pin && !/^\d{6,12}$/.test(payload.pin)) throw new Error("O PIN deve ter de 6 a 12 números.");
      await api.post("/users", payload);
      closeDialog(); toast("Pessoa adicionada."); renderRoute();
    } catch (error) { handleError(error, message); submit.disabled = false; }
  });
  openDialog("Adicionar pessoa", "Defina o papel e, se necessário, um PIN separado da senha.", form);
}
async function userEdit(row) {
  const choices=[["admin","Administrador"],["manager","Gerente"],["cashier","Operador de caixa"],["inventory","Estoque"],["finance","Financeiro"],["viewer","Consulta"]];
  if(state.role==="owner")choices.unshift(["owner","Proprietário"]);
  const fields = [
    { key:"name", label:"Nome", required:true, max:160 },
    { key:"role", label:"Papel", type:"select", required:true, choices },
    { key:"commission_bps", label:"Comissão sobre vendas (%)", type:"percent", default:0, help:"Aplicada quando esta pessoa é o vendedor da venda (ou o operador, se nenhum vendedor for escolhido)." },
    { key:"active", label:"Acesso ativo", type:"checkbox", default:true }
  ];
  const form = await formFor(fields, row);
  const message = formMessage(form), submit = formButtons(form, "Salvar alterações");
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (!form.reportValidity()) return;
    submit.disabled = true;
    try { await api.patch("/users/" + row.id, formPayload(form, fields)); closeDialog(); toast("Acesso atualizado."); renderRoute(); }
    catch (error) { handleError(error, message); submit.disabled = false; }
  });
  openDialog("Editar acesso", "Alterações de papel e desativação são auditadas.", form);
}
function userPin(row) {
  const form = el("form", "auth-form");
  const field = el("div", "field");
  const label = el("label", "", "Novo PIN de " + row.name);
  label.htmlFor = "user-pin";
  const input = el("input"); input.id = "user-pin"; input.type = "password"; input.inputMode = "numeric"; input.pattern = "[0-9]{6,12}"; input.minLength = 6; input.maxLength = 12; input.required = true;
  field.append(label, input, el("small", "", "6 a 12 dígitos. Não reutilize a senha web."));
  form.append(field);
  const message = formMessage(form), submit = formButtons(form, "Definir PIN");
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (!form.reportValidity()) return;
    submit.disabled = true;
    try { await api.post("/users/" + row.id + "/pin", { pin: input.value }); closeDialog(); toast("PIN atualizado."); renderRoute(); }
    catch (error) { handleError(error, message); submit.disabled = false; }
  });
  openDialog("PIN do terminal", "Use um PIN diferente da senha de acesso à web.", form);
}
async function usersPage() {
  const token = ++state.renderId;
  const view = page("Equipe", "Defina os operadores do caixa, seus papéis e seus PINs de acesso.", "Administração");
  view.actions.append(button("+ Adicionar pessoa", () => userCreate().catch(handleError)));
  view.outer.append(info("O PIN é a chave pessoal do operador no terminal. Ele pode ser definido ou trocado aqui, mas não é exibido depois de salvo. Após qualquer alteração, atualize os operadores no terminal."));
  const operatorBox=surface("Logins disponíveis no caixa");view.outer.append(operatorBox);operatorBox.append(loadingSurface());
  const box = surface("Acessos cadastrados"); view.outer.append(box); box.append(loadingSurface());
  try {
    const response = await api.get("/users?limit=100");
    if (token !== state.renderId) return;
    const rows=await collectPages("/users?limit=100",response);
    if(token!==state.renderId)return;
    box.lastChild.remove();
    operatorBox.lastChild.remove();
    const operators=rows.filter(row=>row.active&&row.has_pin);
    operatorBox.append(operators.length?table([
      {label:"Operador",render:row=>row.name},
      {label:"Papel",render:row=>roleNames[row.role]||row.role},
      {label:"Pode cancelar venda",render:row=>cancellationAccess(row.role)}
    ],operators,row=>[{label:"Trocar PIN",onClick:()=>userPin(row)}])
      :empty("Nenhum login de caixa pronto","Ative uma pessoa e defina um PIN para ela aparecer na tela de login do terminal."));
    box.append(pagedContent(response,"/users?limit=100",[
      { label:"Pessoa", render: row => row.name },{ label:"E-mail", render: row => row.email },
      { label:"Papel", render: row => roleNames[row.role] || row.role },{ label:"PIN do caixa", render: row => row.has_pin ? "Definido" : "Definir PIN" },
      { label:"Acesso", render: row => statusBadge(row.active ? "Ativo" : "Inativo") }
    ], row => [...(row.role==="owner"&&state.role!=="owner"?[]:[{label:"Editar",onClick:()=>userEdit(row).catch(handleError)}]),{label:row.has_pin?"Trocar PIN":"Definir PIN",onClick:()=>userPin(row)}],
      empty("Equipe vazia", "Adicione pessoas para distribuir o acesso.")));
  } catch (error) { if (token === state.renderId) { operatorBox.replaceChildren();box.replaceChildren(empty("Falha ao carregar", errorText(error), "!")); handleError(error); } }
}

function pairingDialog(terminal) {
  const body = el("div");
  body.append(info("No primeiro vínculo, informe a licença da empresa, o endereço do servidor e este código no terminal. O vínculo permanece salvo depois de fechar o caixa."));
  if(state.subscription?.license?.key)body.append(copyableValue("Licença da empresa",state.subscription.license.key));
  body.append(copyableValue("Endereço do servidor",localDocker()?"http://cloud:8080":location.origin));
  const code = el("div", "pairing-code", terminal.pairing_code || "Código indisponível");
  code.setAttribute("aria-label", "Código de pareamento");
  body.append(code,copyableValue("Código de pareamento",terminal.pairing_code,"Uso único. Expira em 10 minutos."),
    detailList([["Terminal",terminal.name],["Validade",dateTime(terminal.pairing_expires_at)]]));
  const actions = el("div", "form-actions");
  const open=el("a","button secondary","Abrir terminal local");open.href=terminalLink();open.target="_blank";open.rel="noopener noreferrer";
  actions.append(open,button("Concluir", renderRoute));body.append(actions);
  openDialog("Parear terminal", "Guarde o código antes de fechar esta janela.", body);
}
async function terminalCreate() {
  if (!state.shops.length) throw new Error("Cadastre uma loja antes de criar um terminal.");
  const form = el("form", "form-grid");
  const shop = shopSelect("terminal-shop");
  const nameField = el("div", "field"); const nameLabel = el("label", "", "Nome do terminal"); nameLabel.htmlFor = "terminal-name";
  const name = el("input"); name.id = "terminal-name"; name.required = true; name.maxLength = 120; name.placeholder = "Ex.: Caixa 01";
  nameField.append(nameLabel, name); form.append(shop.field, nameField);
  const message = formMessage(form), submit = formButtons(form, "Gerar código");
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (!form.reportValidity()) return;
    submit.disabled = true;
    try { const result = await api.post("/terminals", {shop_id:shop.select.value,name:name.value.trim()}); pairingDialog(result); }
    catch (error) { handleError(error, message); submit.disabled = false; }
  });
  openDialog("Novo terminal", "Cada terminal ocupa uma vaga do plano.", form);
}
async function terminalsPage() {
  const token = ++state.renderId;
  const view = page("Terminais", "Conecte cada caixa à empresa uma única vez e libere o acesso dos operadores.", "Administração");
  view.actions.append(button("+ Novo terminal", () => terminalCreate().catch(handleError)));
  const access=el("div");view.outer.append(access);
  view.outer.append(info("Para conectar: crie um terminal, abra o aplicativo local e informe a licença, o servidor e o código de pareamento. O código expira em 10 minutos; o vínculo permanece após o primeiro acesso."));
  const box = surface("Dispositivos da empresa"); view.outer.append(box); box.append(loadingSurface());
  const operatorBox=surface("Operadores habilitados para entrar no caixa","Defina ou troque o PIN em Equipe. No terminal, use Atualizar operadores para baixar mudanças.");
  view.outer.append(operatorBox);operatorBox.append(loadingSurface());
  try {
    const [response,subscription,usersFirst]=await Promise.all([api.get("/terminals"),api.get("/subscription"),api.get("/users?limit=100")]);
    if (token !== state.renderId) return;
    const users=await collectPages("/users?limit=100",usersFirst);
    if(token!==state.renderId)return;
    state.subscription=subscription;
    const license=subscription.license||{};
    access.replaceChildren(terminalAccessBox(license));
    if(!license.key)access.prepend(info("A licença ainda não está disponível. Ative uma assinatura ou solicite acesso VIP à plataforma.",true));
    const canActivate=license.can_activate!==false&&(!license.status||['active','trial','past_due'].includes(license.status));
    if(!canActivate)
      access.prepend(info("A licença está "+String(statusNames[license.status]||license.status||"indisponível").toLowerCase()+". Regularize a assinatura ou o acesso VIP antes de conectar um novo terminal.",true));
    const limit=Number(license.terminal_limit??subscription.plan?.terminal_limit);
    const used=Number(license.terminal_used);
    const createButton=view.actions.querySelector("button");
    if(createButton&&(!canActivate||Number.isFinite(limit)&&Number.isFinite(used)&&used>=limit))createButton.disabled=true;
    box.lastChild.remove();const rows=records(response);
    if(Number.isFinite(limit)&&Number.isFinite(used))box.append(info(`${used} de ${limit} terminais utilizados; ${Math.max(0,limit-used)} disponíveis no plano.`));
    box.append(pagedContent(response,"/terminals",[
      {label:"Terminal",render:row=>row.name},{label:"Loja",render:row=>shopName(row.shop_id)},
      {label:"Dispositivo",render:row=>row.device_id || "Aguardando pareamento"},{label:"Versão",render:row=>row.version},
      {label:"Fila",render:row=>row.heartbeat_pending_events??0},{label:"Último contato",render:row=>dateTime(row.last_seen_at)},{label:"Estado",render:row=>statusBadge(row.revoked ? "Revogado" : row.paired ? "Pareado" : "Aguardando")}
    ], row => row.revoked ? [] : [
      ...(canActivate?[{label:"Novo código",onClick:async()=>{
        try { const updated=await api.patch("/terminals/"+row.id,{reissue_pairing:true}); pairingDialog(updated); }
        catch(error){handleError(error);}
      }}]:[]),
      {label:"Revogar",danger:true,onClick:()=>confirmAction("Revogar terminal", "Este terminal deixará de receber novas autorizações. As vendas offline já realizadas ainda exigem reconciliação.", async()=>{ await api.patch("/terminals/"+row.id,{revoked:true}); toast("Terminal revogado."); renderRoute(); },"Revogar")}
    ], empty("Nenhum terminal", "Crie o primeiro caixa para gerar um código de pareamento.")));
    operatorBox.lastChild.remove();
    const available=users.filter(row=>row.active&&row.has_pin);
    operatorBox.append(available.length?table([
      {label:"Login",render:row=>row.name},
      {label:"Nível",render:row=>roleNames[row.role]||row.role},
      {label:"Cancelamento",render:row=>cancellationAccess(row.role)}
    ],available,row=>[{label:"Trocar PIN",onClick:()=>userPin(row)}])
      :empty("Nenhum operador liberado","Abra Equipe e defina um PIN para ao menos uma pessoa ativa."));
    const teamLink=el("a","button secondary small","Gerenciar equipe e PINs");teamLink.href="#users";
    const actions=el("div","connection-actions");actions.append(teamLink);operatorBox.append(actions);
  } catch (error) { if (token === state.renderId) { access.replaceChildren();operatorBox.replaceChildren();box.replaceChildren(empty("Falha ao carregar",errorText(error),"!")); handleError(error); } }
}

async function subscriptionPage() {
  const token = ++state.renderId;
  const view = page("Assinatura e licença", "Veja o acesso da empresa, os limites do plano e os dados para conectar o caixa.", "Administração");
  view.actions.append(button("Solicitar assinatura", async () => {
    try {
      const plans = records(await api.get("/plans", { tenant:false }));
      if (!plans.length) throw new Error("Nenhum plano disponível para assinatura.");
      billingForm(plans);
    } catch (error) { handleError(error); }
  }));
  const box = surface("Plano da empresa"); view.outer.append(box); box.append(loadingSurface());
  try {
    const data = await api.get("/subscription");
    if (token !== state.renderId) return;
    state.subscription=data;
    box.lastChild.remove(); const plan = data.plan || {},license=data.license||{};
    box.append(detailList([
      ["Plano",plan.name || "Não atribuído"],["Estado da assinatura",statusBadge(data.status)],
      ["Estado da licença",statusBadge(license.status)],
      ["Tipo de acesso",licenseSource(license.source)],
      [license.source==="vip"?"Preço de referência (VIP sem cobrança)":"Mensalidade de referência",money(plan.price_month_cents)],
      ["Limite de terminais",plan.terminal_limit ?? "—"],
      ["Terminais utilizados",license.terminal_used??"—"],["Terminais disponíveis",license.terminal_available??"—"],
      ["Limite de lojas",plan.shop_limit ?? "—"],["Fim do teste",dateOnly(data.trial_ends_at)],
      ["Fim do período",dateOnly(data.current_period_end)]
    ]));
    view.outer.append(terminalAccessBox(license));
    if(!license.key)view.outer.append(info("A chave da licença será exibida quando a assinatura estiver pronta para uso.",true));
    if(license.can_activate===false)view.outer.append(info("A licença não permite conectar um novo terminal no momento. Regularize a assinatura ou solicite acesso VIP à plataforma.",true));
    if (plan.features && typeof plan.features === "object") {
      const feature = surface("Recursos do plano", "Recursos comerciais contratados; acompanhe a implantação no atendimento.");
      const list = el("ul", "feature-list");
      (Array.isArray(plan.features) ? plan.features : Object.entries(plan.features).filter(([,value])=>value).map(([key])=>key)).forEach(item=>list.append(el("li","",String(item))));
      feature.append(list); view.outer.append(feature);
    }
  } catch (error) { if (token === state.renderId) { box.replaceChildren(empty("Falha ao carregar",errorText(error),"!")); handleError(error); } }
}
async function companyPage() {
  const token=++state.renderId;
  const view=page("Minha empresa","Dados usados para identificação e configuração fiscal.","Administração");
  view.outer.append(info("O documento deve corresponder ao emitente antes de solicitar qualquer emissão fiscal.",true));
    const box=surface("Dados da empresa");view.outer.append(box);box.append(loadingSurface());
  try {
    const company=await api.get("/ops/company");
    if(token!==state.renderId)return;
    box.lastChild.remove();
    const form=el("form","form-grid");
    const nameField=el("div","field"),nameLabel=el("label","","Nome da empresa"),name=el("input");
    name.id="company-name";nameLabel.htmlFor=name.id;name.required=true;name.maxLength=160;name.value=company.name||"";
    nameField.append(nameLabel,name);
    const documentField=el("div","field"),documentLabel=el("label","","CPF ou CNPJ do emitente"),documentNumber=el("input");
    documentNumber.id="company-document";documentLabel.htmlFor=documentNumber.id;documentNumber.inputMode="numeric";
    documentNumber.maxLength=30;documentNumber.value=company.document||"";
    documentField.append(documentLabel,documentNumber,el("small","","Informe 11 ou 14 dígitos. A emissão fiscal também exige configuração do provedor."));
    const discountField=el("div","field"),discountLabel=el("label","","Desconto máximo autorizado (%)"),discount=el("input");
    discount.id="company-discount";discountLabel.htmlFor=discount.id;discount.type="number";discount.min="0";discount.max="100";discount.step="0.01";
    discount.value=(Number(company.max_discount_bps ?? 10000)/100).toFixed(2).replace(/\.00$/,"");
    discountField.append(discountLabel,discount,el("small","","Limite para qualquer desconto, inclusive quando autorizado por um gerente. Ex.: 10 = até 10% do total."));
    const hoursField=el("fieldset","business-hours-fieldset"),hoursLegend=el("legend","","Horário de funcionamento");
    hoursField.append(hoursLegend,el("small","","O terminal impede a abertura do caixa fora destes horários. Um caixa já aberto continua funcionando e recebe um alerta."));
    const hourLabels={mon:"Segunda",tue:"Terça",wed:"Quarta",thu:"Quinta",fri:"Sexta",sat:"Sábado",sun:"Domingo"};
    const hoursInputs={};const savedHours=company.business_hours||{};
    Object.entries(hourLabels).forEach(([day,labelText])=>{
      const row=el("div","business-hours-row"),toggle=el("input"),open=el("input"),close=el("input");
      toggle.type="checkbox";toggle.checked=Boolean(savedHours[day]?.enabled);open.type="time";close.type="time";
      open.value=savedHours[day]?.open||"08:00";close.value=savedHours[day]?.close||"18:00";
      const label=el("label","business-hours-day",labelText);label.prepend(toggle);
      row.append(label,open,el("span","business-hours-separator","até"),close);hoursField.append(row);hoursInputs[day]={toggle,open,close};
    });
    const plainField=(id,labelText,value,max=160,placeholder="")=>{const field=el("div","field"),label=el("label","",labelText),input=el("input");input.id=id;label.htmlFor=id;input.maxLength=max;input.value=value||"";if(placeholder)input.placeholder=placeholder;field.append(label,input);return {field,input}};
    const trade=plainField("company-trade-name","Nome fantasia",company.trade_name,160);
    const stateRegistration=plainField("company-state-registration","Inscrição estadual",company.state_registration,30,"Não contribuinte, se aplicável");
    const municipalRegistration=plainField("company-municipal-registration","Inscrição municipal",company.municipal_registration,30);
    const addressLine=plainField("company-address-line","Logradouro",company.address_line,180);
    const addressNumber=plainField("company-address-number","Número",company.address_number,20);
    const addressComplement=plainField("company-address-complement","Complemento",company.address_complement,80);
    const addressNeighborhood=plainField("company-address-neighborhood","Bairro",company.address_neighborhood,100);
    const addressCity=plainField("company-address-city","Cidade",company.address_city,100);
    const addressState=plainField("company-address-state","UF",company.address_state,2,"RJ");addressState.input.maxLength=2;addressState.input.style.textTransform="uppercase";
    const addressZip=plainField("company-address-zip","CEP",company.address_zip,8,"00000000");addressZip.input.inputMode="numeric";
    const taxField=el("div","field"),taxLabel=el("label","","Regime tributário"),tax=el("select");tax.id="company-tax-regime";taxLabel.htmlFor=tax.id;[["simples_nacional","Simples Nacional"],["simples_excesso","Simples Nacional · excesso de sublimite"],["regime_normal","Regime normal"]].forEach(([value,labelText])=>{const option=el("option","",labelText);option.value=value;tax.append(option)});tax.value=company.tax_regime||"simples_nacional";taxField.append(taxLabel,tax);
    const environmentField=el("div","field"),environmentLabel=el("label","","Emissão fiscal"),environment=el("select");environment.id="company-fiscal-environment";environmentLabel.htmlFor=environment.id;[["non_fiscal","Comprovante não fiscal · atual"],["homologation","NFC-e em homologação"],["production","NFC-e em produção · requer provedor"]].forEach(([value,labelText])=>{const option=el("option","",labelText);option.value=value;environment.append(option)});environment.value=company.fiscal_environment||"non_fiscal";environmentField.append(environmentLabel,environment,el("small","","A chave de acesso, QR Code, protocolo e impostos só aparecem após autorização real da SEFAZ por um provedor configurado."));
    const seriesField=el("div","field"),seriesLabel=el("label","","Série NFC-e"),series=el("input");series.id="company-nfce-series";series.type="number";series.min="1";series.max="999";series.value=company.nfce_series||1;seriesLabel.htmlFor=series.id;seriesField.append(series);
    const nextNumberField=el("div","field"),nextNumberLabel=el("label","","Próximo número NFC-e"),nextNumber=el("input");nextNumber.id="company-nfce-next-number";nextNumber.type="number";nextNumber.min="1";nextNumber.value=company.nfce_next_number||1;nextNumberLabel.htmlFor=nextNumber.id;nextNumberField.append(nextNumber);
    form.append(nameField,trade.field,documentField,stateRegistration.field,municipalRegistration.field,addressLine.field,addressNumber.field,addressComplement.field,addressNeighborhood.field,addressCity.field,addressState.field,addressZip.field,taxField,environmentField,seriesField,nextNumberField,discountField,hoursField);
    const message=formMessage(form);
    const actions=el("div","form-actions"),save=el("button","button","Salvar dados");save.type="submit";
    actions.append(save);form.append(actions);
    form.addEventListener("submit",async event=>{
      event.preventDefault();if(!form.reportValidity())return;
      save.disabled=true;
      try {
        const digits=documentNumber.value.replace(/\D/g,"");
        if(digits&&!([11,14].includes(digits.length)))throw new Error("Informe um CPF com 11 dígitos ou CNPJ com 14 dígitos.");
        const maxDiscount=Number(String(discount.value).replace(",","."));
        if(!Number.isFinite(maxDiscount)||maxDiscount<0||maxDiscount>100)throw new Error("O desconto máximo deve ficar entre 0% e 100%.");
        const companyPayload={name:name.value.trim(),trade_name:trade.input.value.trim(),document:digits,
          state_registration:stateRegistration.input.value.trim(),municipal_registration:municipalRegistration.input.value.trim(),
          address_line:addressLine.input.value.trim(),address_number:addressNumber.input.value.trim(),address_complement:addressComplement.input.value.trim(),
          address_neighborhood:addressNeighborhood.input.value.trim(),address_city:addressCity.input.value.trim(),address_state:addressState.input.value.trim().toUpperCase(),
          address_zip:addressZip.input.value.replace(/\D/g,""),tax_regime:tax.value,fiscal_environment:environment.value,
          nfce_series:Number(series.value),nfce_next_number:Number(nextNumber.value),max_discount_bps:Math.round(maxDiscount*100),
          business_hours:Object.fromEntries(Object.entries(hoursInputs).map(([day,inputs])=>[day,{enabled:inputs.toggle.checked,open:inputs.toggle.checked?inputs.open.value:"",close:inputs.toggle.checked?inputs.close.value:""}]))};
        await api.patch("/ops/company",companyPayload);
        toast("Dados da empresa atualizados.");renderRoute();
      }catch(error){handleError(error,message);save.disabled=false;}
    });
    box.append(form);
  }catch(error){if(token===state.renderId){box.replaceChildren(empty("Falha ao carregar",errorText(error),"!"));handleError(error);}}
}
async function auditPage() {
  const token = ++state.renderId;
  const view = page("Auditoria", "Acompanhe alterações realizadas no painel e nos terminais.", "Administração");
  const box = surface("Eventos recentes"); view.outer.append(box); box.append(loadingSurface());
  try {
    const response = await api.get("/audit");
    if (token !== state.renderId) return;
    box.lastChild.remove(); const rows = records(response);
    box.append(pagedContent(response,"/audit",[
      {label:"Data",render:row=>dateTime(row.created_at)},{label:"Quem",render:row=>row.actor_email || row.actor_id},
      {label:"Ação",render:row=>row.action},{label:"Recurso",render:row=>row.subject_type},
      {label:"Identificador",render:row=>row.subject_id}
    ], null, empty("Sem eventos", "Alterações auditadas aparecerão aqui.")));
  } catch (error) { if (token === state.renderId) { box.replaceChildren(empty("Falha ao carregar",errorText(error),"!")); handleError(error); } }
}

async function syncConflictsPage() {
  const token = ++state.renderId;
  const view = page("Conflitos offline", "Reveja vendas sincronizadas com divergência de estoque e registre a decisão da equipe.", "Administração");
  view.actions.append(button("Atualizar", () => renderRoute(), "secondary"));
  const box = surface("Pendências", "Uma venda não é apagada: o terminal continua vendendo dentro da política local e esta fila orienta o ajuste de estoque.");
  view.outer.append(info("Conflitos são gerados quando dois terminais trabalham offline e a confirmação central deixa o estoque negativo. Resolva depois de conferir a contagem física.", true), box);
  box.append(loadingSurface());
  try {
    const response = await api.get("/sync/conflicts?status=open");
    if (token !== state.renderId) return;
    box.lastChild.remove();
    const rows = records(response);
    box.append(pagedContent(response, "/sync/conflicts?status=open", [
      { label: "Data", render: row => dateTime(row.created_at) },
      { label: "Terminal", render: row => row.terminal_name || "—" },
      { label: "Motivo", render: row => row.reason === "negative_stock" ? "Estoque negativo" : row.reason },
      { label: "Itens", render: row => (row.detail?.items || []).map(item => `${item.product_name} (${Number(item.central_balance_milli || 0) / 1000})`).join(" · ") || "Ver detalhes" },
      { label: "Estado", render: row => statusBadge(row.status) }
    ], row => [{ label: "Resolver", onClick: () => resolveSyncConflict(row) }],
      empty("Nenhum conflito pendente", "As vendas offline estão conciliadas.")));
  } catch (error) {
    if (token === state.renderId) { box.replaceChildren(empty("Falha ao carregar", errorText(error), "!")); handleError(error); }
  }
}

async function resolveSyncConflict(row) {
  const content = el("div");
  const items = (row.detail?.items || []).map(item => `${item.product_name}: saldo central ${Number(item.central_balance_milli || 0) / 1000}`).join("\n");
  content.append(info(items || "Confira a venda e o estoque físico antes de resolver.", true));
  const field = el("div", "field"), label = el("label", "", "Observação da resolução");
  const textarea = el("textarea"); textarea.rows = 4; textarea.placeholder = "Ex.: feita entrada de 2 unidades após conferência física"; label.htmlFor = "conflict-resolution"; textarea.id = "conflict-resolution"; field.append(label, textarea);
  const message = formMessage(content); message.hidden = true;
  const actions = el("div", "form-actions");
  const cancel = button("Voltar", closeDialog, "ghost");
  const accept = button("Marcar como resolvido", async () => {
    accept.disabled = true;
    try { await api.patch("/sync/conflicts/" + encodeURIComponent(row.id), { status: "resolved", resolution: textarea.value.trim() }); closeDialog(); toast("Conflito resolvido e auditado."); renderRoute(); }
    catch (error) { handleError(error, message); accept.disabled = false; }
  });
  actions.append(cancel, accept); content.append(field, message, actions);
  openDialog("Resolver conflito offline", "A decisão ficará registrada no histórico da empresa.", content);
}

const platformApi = path => api.get("/platform" + path, { tenant:false });
async function platformOverview() {
  const token = ++state.renderId;
  const view = page("Visão geral", "Acompanhe empresas, assinaturas e terminais da plataforma.", "Plataforma");
  if (platformSuperuser()) {
    view.actions.append(button("Usuários e permissões", () => { location.hash = "#platform_users"; }, "secondary"));
  }
  view.outer.append(info("O MRR soma apenas assinaturas pagas. Acessos VIP e ativações manuais aparecem em separado e não representam receita.", true));
  const output = el("div"); view.outer.append(output); output.append(loadingSurface());
  try {
    const [metrics,tenantResult] = await Promise.all([
      api.get("/ops/platform/metrics",{tenant:false}),
      platformApi("/tenants?limit=8")
    ]);
    if (token !== state.renderId) return;
    const tenants=records(tenantResult);
    const billingValue=value=>value===null||value===undefined?"—":money(value);
    const cards=el("div","metric-grid");
    cards.append(
      metric("Empresas cadastradas",String(metrics.tenants_total??0),String(metrics.tenants_active??0)+" ativas"),
      metric("Assinaturas ativas",String(metrics.subscriptions_active??0),String(metrics.subscriptions_trial??0)+" em teste"),
      metric("MRR de assinaturas",billingValue(metrics.mrr_cents),metrics.mrr_cents==null?"Acesso de cobrança":"Somente contratos pagos; VIP excluído"),
      metric("Cobranças pagas",billingValue(metrics.paid_invoices_cents),metrics.paid_invoices_cents==null?"Acesso de cobrança":"Total confirmado em faturas"),
      metric("Empresas VIP",String(metrics.subscriptions_vip??0),"Acesso cortesia, fora do MRR"),
      metric("Pagamento pendente",String(metrics.subscriptions_past_due??0),"Assinaturas sinalizadas"),
      metric("Terminais cadastrados",String(metrics.terminals_total??0),String(metrics.terminals_revoked??0)+" revogados"),
      metric("Tarefas na fila",String(metrics.pending_jobs??0),"Integrações"),
      metric("Precisam de revisão",String(metrics.jobs_needing_review??0),"Intervenção administrativa")
    );
    const box=surface("Empresas","Primeiros registros em ordem alfabética.");
    box.append(tenants.length ? table([
      {label:"Empresa",render:row=>row.name},{label:"Plano",render:row=>row.subscription?.plan?.name},
      {label:"Assinatura",render:row=>statusBadge(row.subscription?.status)},
      {label:"Criada em",render:row=>dateTime(row.created_at)}
    ],tenants) : empty("Nenhuma empresa", "Cadastros aparecerão aqui."));
    output.replaceChildren(cards,box);
  } catch(error) { if(token===state.renderId){ output.replaceChildren(empty("Falha ao carregar",errorText(error),"!"));handleError(error); } }
}
async function platformTenants() {
  const token=++state.renderId;
  const view=page("Empresas","Organizações cadastradas na plataforma.","Plataforma");
  const box=surface("Empresas");view.outer.append(box);box.append(loadingSurface());
  try {
    const response=await platformApi("/tenants");
    if(token!==state.renderId)return;
    box.lastChild.remove();const rows=records(response);
    box.append(pagedContent(response,"/platform/tenants",[
      {label:"Empresa",render:row=>row.name},{label:"Documento",render:row=>row.document},
      {label:"Plano",render:row=>row.subscription?.plan?.name},
      {label:"Acesso",render:row=>licenseSource(row.subscription?.license_source)},
      {label:"Estado",render:row=>statusBadge(row.subscription?.status)},
      {label:"Desde",render:row=>dateTime(row.created_at)}
    ],platformSuperuser()?row=>[
      {label:"Editar",onClick:()=>platformTenantForm(row)},
      ...(row.subscription?[{label:row.subscription.license_source==="vip"?"Renovar VIP":"Conceder VIP",onClick:()=>vipForm({...row.subscription,tenant_name:row.name}).catch(handleError)}]:[])
    ]:null,
      empty("Nenhuma empresa","O primeiro cadastro aparecerá aqui."),{tenant:false}));
  }catch(error){if(token===state.renderId){box.replaceChildren(empty("Falha ao carregar",errorText(error),"!"));handleError(error);}}
}
function platformTenantForm(row) {
  const form=el("form","form-grid");
  const nameField=el("div","field"),nameLabel=el("label","","Nome da empresa"),name=el("input");
  name.id="platform-tenant-name";nameLabel.htmlFor=name.id;name.required=true;name.maxLength=160;name.value=row.name||"";
  nameField.append(nameLabel,name);
  const documentField=el("div","field"),documentLabel=el("label","","Documento"),documentNumber=el("input");
  documentNumber.id="platform-tenant-document";documentLabel.htmlFor=documentNumber.id;documentNumber.maxLength=30;
  documentNumber.value=row.document||"";documentField.append(documentLabel,documentNumber);
  const active=el("label","checkbox-line full"),check=el("input");check.type="checkbox";check.checked=row.active!==false;
  active.append(check,el("span","","Empresa ativa"));
  form.append(nameField,documentField,active);
  const message=formMessage(form),submit=formButtons(form,"Salvar empresa");
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!form.reportValidity())return;
    submit.disabled=true;
    try{
      await api.patch("/platform/tenants/"+row.id,{name:name.value.trim(),document:documentNumber.value.trim(),active:check.checked},{tenant:false});
      closeDialog();toast("Empresa atualizada.");renderRoute();
    }catch(error){handleError(error,message);submit.disabled=false;}
  });
  openDialog("Editar empresa","A desativação bloqueia o acesso dos membros à empresa.",form);
}
const planFields=[
  {key:"name",label:"Nome do plano",required:true,max:100},
  {key:"slug",label:"Identificador",required:true,max:60,placeholder:"ex.: essencial"},
  {key:"price_month_cents",label:"Preço mensal (R$)",type:"money",required:true,min:0},
  {key:"price_year_cents",label:"Preço anual (R$)",type:"money",required:true,min:0},
  {key:"terminal_limit",label:"Limite de terminais",type:"number",required:true,min:1},
  {key:"shop_limit",label:"Limite de lojas",type:"number",required:true,min:1},
  {key:"active",label:"Plano disponível para novas assinaturas",type:"checkbox",default:true,full:true},
  {key:"features",label:"Recursos (JSON)",type:"json",required:true,default:[],full:true,help:"Lista JSON de códigos de recursos. Ex.: [\"cloud_dashboard\", \"stock\"]."}
];
async function planForm(record) {
  const editing=!!record,fields=editing?planFields.filter(field=>field.key!=="slug"):planFields;
  const form=await formFor(fields,record||{});
  const message=formMessage(form),submit=formButtons(form,editing?"Salvar plano":"Criar plano");
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!form.reportValidity())return;
    submit.disabled=true;
    try {
      const payload=formPayload(form,fields);
      payload.terminal_limit=Number(payload.terminal_limit);
      payload.shop_limit=Number(payload.shop_limit);
      if(!Number.isSafeInteger(payload.terminal_limit)||!Number.isSafeInteger(payload.shop_limit)||payload.terminal_limit<1||payload.shop_limit<1)throw new Error("Os limites precisam ser inteiros maiores que zero.");
      if(!editing&&!/^[a-z0-9-]+$/.test(payload.slug))throw new Error("O identificador aceita letras minúsculas, números e hífen.");
      if(!Array.isArray(payload.features)||payload.features.some(feature=>typeof feature!=="string"))throw new Error("Recursos deve ser uma lista JSON de textos.");
      if(editing)await api.patch("/platform/plans/"+record.id,payload,{tenant:false});
      else await api.post("/platform/plans",payload,{tenant:false});
      closeDialog();toast("Plano salvo.");renderRoute();
    }catch(error){handleError(error,message);submit.disabled=false;}
  });
  openDialog(editing?"Editar plano":"Novo plano","Preços e limites aparecerão nas assinaturas.",form);
}
async function platformPlans() {
  const token=++state.renderId;
  const view=page("Planos","Preços e limites comerciais da plataforma.","Plataforma");
  if(platformEdit())view.actions.append(button("+ Novo plano",()=>planForm().catch(handleError)));
  const box=surface("Planos cadastrados");view.outer.append(box);box.append(loadingSurface());
  try {
    const response=await platformApi("/plans");
    if(token!==state.renderId)return;
    box.lastChild.remove();const rows=records(response);state.plans=rows;
    box.append(pagedContent(response,"/platform/plans",[
      {label:"Plano",render:row=>row.name},{label:"Mensal",render:row=>money(row.price_month_cents)},
      {label:"Anual",render:row=>money(row.price_year_cents)},
      {label:"Terminais",render:row=>row.terminal_limit},{label:"Lojas",render:row=>row.shop_limit},
      {label:"Estado",render:row=>statusBadge(row.active?"Ativo":"Inativo")}
    ],platformEdit()?row=>[{label:"Editar",onClick:()=>planForm(row).catch(handleError)}]:null,
      empty("Nenhum plano","Cadastre o primeiro plano comercial."),{tenant:false}));
  }catch(error){if(token===state.renderId){box.replaceChildren(empty("Falha ao carregar",errorText(error),"!"));handleError(error);}}
}
async function subscriptionEdit(row) {
  const plans=state.plans.length?state.plans:records(await platformApi("/plans"));
  const fields=[
    {key:"plan_id",label:"Plano",type:"select",required:true,choices:plans.filter(plan=>plan.active||plan.id===row.plan?.id).map(plan=>[plan.id,plan.name])},
    {key:"status",label:"Estado",type:"select",required:true,choices:[["trial","Teste"],["active","Ativa"],["past_due","Pagamento pendente"],["suspended","Suspensa"],["cancelled","Cancelada"]]},
    {key:"trial_ends_at",label:"Fim do teste",type:"date"},
    {key:"current_period_end",label:"Fim do período de acesso",type:"date",help:"Obrigatório para acesso ativo ou com pagamento pendente."}
  ];
  const form=await formFor(fields,{plan_id:row.plan?.id,status:row.status,trial_ends_at:row.trial_ends_at?.slice(0,10),current_period_end:row.current_period_end?.slice(0,10)});
  const message=formMessage(form),submit=formButtons(form,"Salvar assinatura");
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!form.reportValidity())return;
    submit.disabled=true;
    try {
      const payload=formPayload(form,fields);
      if(payload.trial_ends_at)payload.trial_ends_at=new Date(payload.trial_ends_at+"T12:00:00Z").toISOString();
      if(payload.current_period_end)payload.current_period_end=new Date(payload.current_period_end+"T12:00:00Z").toISOString();
      if(["active","past_due"].includes(payload.status)&&!payload.current_period_end&&!row.current_period_end)
        throw new Error("Informe o fim do período de acesso antes de ativar a assinatura.");
      await api.patch("/platform/subscriptions/"+row.id,payload,{tenant:false});
      closeDialog();toast("Assinatura atualizada.");renderRoute();
    }catch(error){handleError(error,message);submit.disabled=false;}
  });
  openDialog("Assinatura de "+row.tenant_name,"Este ajuste administrativo será identificado como ativação manual e não processa cobrança.",form);
}
async function vipForm(row) {
  const plans=records(await platformApi("/plans"));
  const choices=plans.filter(plan=>plan.active).map(plan=>[plan.id,`${plan.name} · ${plan.terminal_limit} terminal(is)`]);
  if(!choices.length)throw new Error("Cadastre um plano ativo antes de conceder VIP.");
  const selected=choices.some(([id])=>id===row.plan?.id)?row.plan.id:choices[0][0];
  const current=row.current_period_end?new Date(row.current_period_end):new Date();
  const end=new Date(Math.max(Date.now(),current.getTime()));end.setFullYear(end.getFullYear()+1);
  const fields=[
    {key:"plan_id",label:"Plano e limite de caixas",type:"select",required:true,choices},
    {key:"current_period_end",label:"Válido até",type:"date",required:true,help:"O acesso VIP expira nesta data."},
    {key:"reason",label:"Motivo da cortesia",max:240,full:true,placeholder:"Ex.: parceria comercial"}
  ];
  const form=await formFor(fields,{plan_id:selected,current_period_end:end.toISOString().slice(0,10)});
  form.prepend(info("Acesso VIP é uma cortesia para esta empresa. Ele não gera cobrança, usa os limites do plano escolhido e não altera o papel dos usuários. Se já houver assinatura recorrente, pause-a no Asaas antes; o sistema verificará o estado no provedor.",true));
  const message=formMessage(form),submit=formButtons(form,"Ativar VIP");
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!form.reportValidity())return;
    submit.disabled=true;
    try {
      const data=formPayload(form,fields);
      if(new Date(data.current_period_end+"T23:59:59Z")<=new Date())throw new Error("Escolha uma data futura.");
      await api.post("/platform/subscriptions/"+row.id+"/vip",{
        plan_id:data.plan_id,current_period_end:new Date(data.current_period_end+"T23:59:59Z").toISOString(),
        ...(data.reason?{reason:data.reason}:{})
      },{tenant:false});
      closeDialog();toast("Acesso VIP ativado para a empresa.");renderRoute();
    }catch(error){handleError(error,message);submit.disabled=false;}
  });
  openDialog((row.license_source==="vip"?"Renovar":"Conceder")+" VIP · "+row.tenant_name,"O acesso será registrado e poderá ser auditado.",form);
}
async function platformSubscriptions() {
  const token=++state.renderId;
  const view=page("Assinaturas","Plano e estado de cada empresa.","Plataforma");
  view.outer.append(info("A situação da assinatura não comprova recebimento financeiro. Confira cobranças no provedor quando essa integração estiver configurada.",true));
  const box=surface("Assinaturas");view.outer.append(box);box.append(loadingSurface());
  try {
    const [response,plans]=await Promise.all([platformApi("/subscriptions"),platformEdit()?platformApi("/plans"):Promise.resolve(null)]);
    if(token!==state.renderId)return;
    state.plans=records(plans);box.lastChild.remove();const rows=records(response);
    box.append(pagedContent(response,"/platform/subscriptions",[
      {label:"Empresa",render:row=>row.tenant_name},{label:"Plano",render:row=>row.plan?.name},
      {label:"Acesso",render:row=>licenseSource(row.license_source)},
      {label:"Estado",render:row=>statusBadge(row.status)},{label:"Fim do teste",render:row=>dateOnly(row.trial_ends_at)},
      {label:"Período até",render:row=>dateOnly(row.current_period_end)}
    ],platformEdit()?row=>[
      {label:"Editar",onClick:()=>subscriptionEdit(row).catch(handleError)},
      ...(platformSuperuser()?[{label:row.license_source==="vip"?"Renovar VIP":"Conceder VIP",onClick:()=>vipForm(row).catch(handleError)}]:[])
    ]:null,
      empty("Nenhuma assinatura","As empresas cadastradas aparecerão aqui."),{tenant:false}));
  }catch(error){if(token===state.renderId){box.replaceChildren(empty("Falha ao carregar",errorText(error),"!"));handleError(error);}}
}
async function platformDevices() {
  const token=++state.renderId;
  const view=page("Terminais","Dispositivos cadastrados pelas empresas.","Plataforma");
  const box=surface("Todos os terminais");view.outer.append(box);box.append(loadingSurface());
  try {
    const response=await platformApi("/terminals");
    if(token!==state.renderId)return;
    box.lastChild.remove();const rows=records(response);
    box.append(pagedContent(response,"/platform/terminals",[
      {label:"Empresa",render:row=>row.tenant_name},{label:"Loja",render:row=>row.shop_name},
      {label:"Terminal",render:row=>row.name},{label:"Dispositivo",render:row=>row.device_id},
      {label:"Versão",render:row=>row.version},{label:"Fila",render:row=>row.heartbeat_pending_events??0},{label:"Último contato",render:row=>dateTime(row.last_seen_at)},
      {label:"Estado",render:row=>statusBadge(row.revoked?"Revogado":"Ativo")}
    ],platformCan("platform_ops")?row=>row.revoked?[]:[{label:"Revogar",danger:true,onClick:()=>platformRevokeDevice(row)}]:null,
      empty("Nenhum terminal","Terminais pareados ou cadastrados aparecerão aqui."),{tenant:false}));
  }catch(error){if(token===state.renderId){box.replaceChildren(empty("Falha ao carregar",errorText(error),"!"));handleError(error);}}
}
function platformRevokeDevice(row) {
  const form=el("form","auth-form");
  form.append(info("O acesso do terminal será revogado para novas autorizações. Eventos offline anteriores ainda precisam de reconciliação.",true));
  const field=el("div","field"),label=el("label","","Motivo"),reason=el("textarea");
  reason.id="platform-revoke-reason";label.htmlFor=reason.id;reason.required=true;reason.maxLength=240;
  field.append(label,reason);form.append(field);
  const message=formMessage(form),submit=formButtons(form,"Revogar terminal");
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!form.reportValidity())return;
    submit.disabled=true;
    try{
      await api.patch("/platform/terminals/"+row.id,{revoked:true,reason:reason.value.trim()},{tenant:false});
      closeDialog();toast("Terminal revogado.");renderRoute();
    }catch(error){handleError(error,message);submit.disabled=false;}
  });
  openDialog("Revogar terminal de "+row.tenant_name,"Ação administrativa auditada.",form);
}

function integrationForm(provider, current, asPlatform=false) {
  const form=el("form","form-grid");
  const envField=el("div","field"),envLabel=el("label","","Ambiente"),env=el("select");
  env.id="integration-environment";envLabel.htmlFor=env.id;
  (asPlatform?[["sandbox","Testes"],["production","Produção"]]:[["homologation","Homologação"],["production","Produção"]]).forEach(([value,label])=>{
    const option=el("option","",label);option.value=value;env.append(option);
  });
  env.value=current.environment;
  envField.append(envLabel,env);
  const tokenField=el("div","field full"),tokenLabel=el("label","","Credencial do provedor"),token=el("input");
  token.id="integration-token";token.type="password";token.autocomplete="new-password";token.maxLength=1000;
  token.placeholder=current.configured?"Deixe em branco para manter a credencial":"Cole a credencial";
  tokenLabel.htmlFor=token.id;
  tokenField.append(tokenLabel,token,el("small","","O valor salvo não é exibido novamente."));
  const enabledField=el("label","checkbox-line"),enabled=el("input");
  enabled.type="checkbox";enabled.checked=!!current.enabled;
  enabledField.append(enabled,el("span","", "Integração habilitada"));
  form.append(envField,tokenField,enabledField);
  const message=formMessage(form),submit=formButtons(form,"Salvar integração");
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!form.reportValidity())return;
    submit.disabled=true;
    try {
      if(!current.configured&&!token.value.trim())throw new Error("Informe a credencial do provedor para configurar a integração.");
      const payload={environment:env.value,enabled:enabled.checked};
      if(token.value.trim())payload.token=token.value.trim();
      const options=asPlatform?{tenant:false}:{};
      await api.post("/ops/integrations/"+provider,payload,options);
      closeDialog();toast("Configuração salva.");renderRoute();
    }catch(error){handleError(error,message);submit.disabled=false;}
  });
  openDialog(asPlatform?"Asaas da plataforma":"Focus NFe da empresa","A credencial é armazenada no servidor; não será exibida no painel.",form);
}
async function downloadFiscalXml(row) {
  let response;
  try {
    response=await fetch("/api/v1/ops/fiscal/documents/"+row.id+"/xml",{
      credentials:"same-origin",headers:{"X-Tenant-ID":state.tenantId,Accept:"application/xml"}
    });
  }catch{throw new Error("Sem conexão para baixar o XML.");}
  if(!response.ok) {
    let body;
    try {body=await response.json();}catch{}
    throw new Error(body?.error||"Não foi possível baixar o XML.");
  }
  const blob=await response.blob();
  const url=URL.createObjectURL(blob);
  const link=el("a");link.href=url;link.download="nfce-"+row.id+".xml";
  document.body.append(link);link.click();link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1500);
}
function fiscalCancel(row) {
  const form=el("form","auth-form");
  form.append(info("A solicitação depende da resposta do provedor fiscal. Ela não altera a venda comercial automaticamente.",true));
  const field=el("div","field"),label=el("label","","Justificativa"),input=el("textarea");
  input.id="fiscal-cancel-reason";label.htmlFor=input.id;
  input.minLength=15;input.maxLength=255;input.required=true;
  field.append(label,input,el("small","","Entre 15 e 255 caracteres."));
  form.append(field);
  const message=formMessage(form),submit=formButtons(form,"Solicitar cancelamento");
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!form.reportValidity())return;
    submit.disabled=true;
    try {
      await api.post("/ops/fiscal/documents/"+row.id+"/cancel",{reason:input.value.trim()});
      closeDialog();toast("Solicitação enviada; acompanhe a tarefa.");renderRoute();
    }catch(error){handleError(error,message);submit.disabled=false;}
  });
  openDialog("Cancelar documento fiscal","Esta operação pode ter regras e prazos do provedor fiscal.",form);
}
function canReconcileJob(row) {
  return row.status==="needs_review" &&
    ["fiscal.submit","fiscal.cancel","fiscal.poll","fiscal.archive","billing.reconcile"].includes(row.kind);
}
function reconcileJob(row, asPlatform=false) {
  confirmAction("Consultar estado no provedor","A tarefa fará uma consulta ao provedor. Ela não reenviará a emissão fiscal nem criará nova cobrança.",async()=>{
    await api.post("/ops/jobs/"+row.id+"/reconcile",{},asPlatform?{tenant:false}:{});
    toast("Consulta enfileirada.");renderRoute();
  },"Consultar provedor");
}
function reconcileBillingJob(row) {
  const form=el("form","auth-form");
  form.append(info("A conciliação consulta dados existentes no Asaas. Ela não cria uma nova assinatura nem repete a cobrança.",true));
  const modeField=el("div","field"),modeLabel=el("label","","Como conciliar"),mode=el("select");
  mode.id="billing-reconcile-mode";modeLabel.htmlFor=mode.id;
  if(!row.result?.customer_id){
    const option=el("option","","Consultar cliente da solicitação");option.value="customer";mode.append(option);
  }
  const known=el("option","","Informar assinatura existente no Asaas");known.value="subscription";mode.append(known);
  modeField.append(modeLabel,mode);
  const referenceField=el("div","field"),referenceLabel=el("label","","ID da assinatura no Asaas"),reference=el("input");
  reference.id="billing-reconcile-subscription";referenceLabel.htmlFor=reference.id;
  reference.maxLength=160;reference.placeholder="Ex.: sub_...";
  referenceField.append(referenceLabel,reference);
  form.append(modeField,referenceField);
  function updateMode(){const required=mode.value==="subscription";referenceField.hidden=!required;reference.required=required;}
  mode.addEventListener("change",updateMode);updateMode();
  const message=formMessage(form),submit=formButtons(form,"Solicitar conciliação");
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!form.reportValidity())return;
    submit.disabled=true;
    try {
      const payload=mode.value==="subscription"
        ? {subscription_id:reference.value.trim()}
        : {recover_customer:true};
      await api.post("/ops/jobs/"+row.id+"/reconcile",payload,{tenant:false});
      closeDialog();toast("Conciliação enfileirada. Consulte o estado da tarefa.");renderRoute();
    }catch(error){handleError(error,message);submit.disabled=false;}
  });
  openDialog("Conciliar cobrança incerta","Verifique os dados diretamente no provedor antes de informar uma assinatura.",form);
}
async function integrationsPage() {
  const token=++state.renderId;
  const view=page("Integrações","Credenciais e tarefas de serviços externos.","Administração");
  view.outer.append(info("A emissão fiscal exige dados tributários corretos, certificado e validação operacional. Configurar a credencial por si só não homologa a emissão.",true));
  const content=el("div");view.outer.append(content);content.append(loadingSurface());
  try {
    const [integration,jobs,docs]=await Promise.all([
      api.get("/ops/integrations/focusnfe"),
      api.get("/ops/jobs"),
      api.get("/ops/fiscal/documents")
    ]);
    if(token!==state.renderId)return;
    const config=surface("Focus NFe","Configuração fiscal desta empresa.");
    config.append(detailList([
      ["Credencial",integration.configured?"Configurada":"Não configurada"],
      ["Ambiente",integration.environment==="production"?"Produção":"Homologação"],
      ["Estado",statusBadge(integration.enabled?"Ativo":"Inativo")]
    ]));
    config.append(button("Configurar",()=>integrationForm("focusnfe",integration)));
    const documents=surface("Documentos fiscais","Solicitações e retornos dos documentos desta empresa.");
    const docRows=records(docs);
    documents.append(docRows.length?table([
      {label:"Venda",render:row=>String(row.sale_id).slice(0,8)},
      {label:"Estado",render:row=>statusBadge(row.status)},
      {label:"Autorização",render:row=>row.authorization},
      {label:"Erro",render:row=>row.error_message},
      {label:"Data",render:row=>dateTime(row.created_at)}
    ],docRows,row=>[
      ...(row.xml_archived?[{label:"Baixar XML",onClick:()=>downloadFiscalXml(row).catch(handleError)}]:[]),
      ...(row.status==="authorized"?[{label:"Solicitar cancelamento",danger:true,onClick:()=>fiscalCancel(row)}]:[])
    ]):empty("Nenhum documento","As solicitações fiscais aparecerão aqui."));
    const tasks=surface("Tarefas de integração","Execuções recentes; pagamentos e documentos podem permanecer pendentes.");
    const jobRows=records(jobs);
    tasks.append(jobRows.length?table([
      {label:"Tipo",render:row=>row.kind},{label:"Estado",render:row=>statusBadge(row.status)},
      {label:"Tentativas",render:row=>row.attempts},{label:"Último erro",render:row=>row.last_error},
      {label:"Criada em",render:row=>dateTime(row.created_at)}
    ],jobRows,row=>canReconcileJob(row)?[{label:"Consultar",onClick:()=>reconcileJob(row)}]:[]):empty("Sem tarefas","Quando uma integração for solicitada, a tarefa aparecerá aqui."));
    content.replaceChildren(config,documents,tasks);
  }catch(error){if(token===state.renderId){content.replaceChildren(empty("Falha ao carregar",errorText(error),"!"));handleError(error);}}
}
async function platformServices() {
  const token=++state.renderId;
  const view=page("Serviços","Estado das integrações e do processamento da plataforma.","Plataforma");
  const content=el("div");view.outer.append(content);content.append(loadingSurface());
  try {
    const [health,jobs,asaas]=await Promise.all([
      api.get("/ops/health",{tenant:false}),
      api.get("/ops/jobs",{tenant:false}),
      platformSuperuser()?api.get("/ops/integrations/asaas",{tenant:false}):Promise.resolve(null)
    ]);
    if(token!==state.renderId)return;
    const cards=el("div","metric-grid");
    cards.append(
      metric("Tarefas na fila",String(health.pending_jobs??0)),
      metric("Pedem revisão",String(health.jobs_needing_review??0)),
      metric("Último backup",dateTime(health.last_backup_at),"Indicador do servidor"),
      metric("Backup externo",health.backup_offsite_configured?"Configurado":"Não configurado")
    );
    const integration=surface("Cobrança Asaas","Credencial da plataforma para processamento de assinaturas.");
    integration.append(asaas?detailList([
      ["Credencial",asaas.configured?"Configurada":"Não configurada"],
      ["Ambiente",asaas.environment==="production"?"Produção":"Testes"],
      ["Estado",statusBadge(asaas.enabled?"Ativo":"Inativo")]
    ]):info("A configuração da cobrança exige acesso de superusuário."));
    if(asaas)integration.append(button("Configurar Asaas",()=>integrationForm("asaas",asaas,true)));
    const tasks=surface("Tarefas recentes");const rows=records(jobs);
    tasks.append(rows.length?table([
      {label:"Tipo",render:row=>row.kind},{label:"Estado",render:row=>statusBadge(row.status)},
      {label:"Tentativas",render:row=>row.attempts},{label:"Erro",render:row=>row.last_error},
      {label:"Criada em",render:row=>dateTime(row.created_at)}
    ],rows,row=>{
      if(row.kind==="billing.subscribe"&&row.status==="needs_review"&&platformCan("platform_billing"))
        return [{label:"Conciliar cobrança",onClick:()=>reconcileBillingJob(row)}];
      if(canReconcileJob(row)&&platformCan("platform_ops","platform_billing"))
        return [{label:"Consultar",onClick:()=>reconcileJob(row,true)}];
      return [];
    }):empty("Sem tarefas","As tarefas aparecerão aqui."));
    content.replaceChildren(cards,integration,tasks);
  }catch(error){if(token===state.renderId){content.replaceChildren(empty("Falha ao carregar",errorText(error),"!"));handleError(error);}}
}

function requestPlatformBackup() {
  confirmAction("Executar backup agora", "O banco será exportado, validado e enviado ao destino Restic configurado. A venda e o banco ativo continuam disponíveis.", async () => {
    await api.post("/ops/backups", { action: "run" }, { tenant:false });
    toast("Backup solicitado. Atualize o painel para acompanhar o resultado.");
    renderRoute();
  }, "Executar backup");
}

function requestPlatformRestore(snapshot) {
  confirmAction("Validar restauração", `O snapshot ${snapshot.short_id || snapshot.id.slice(0, 8)} será restaurado em um banco temporário isolado. O banco ativo não será alterado.`, async () => {
    await api.post("/ops/backups", { action: "restore_test", snapshot_id: snapshot.id, confirm:true }, { tenant:false });
    toast("Restauração solicitada. O resultado aparecerá neste painel.");
    renderRoute();
  }, "Restaurar em ambiente isolado");
}

async function platformBackupsPage() {
  const token=++state.renderId;
  const view=page("Backups e restauração","Acompanhe cópias do SaaS, solicite uma cópia imediata e valide restaurações sem tocar no banco ativo.","Plataforma");
  view.actions.append(button("Atualizar",()=>renderRoute()),button("Executar backup agora",requestPlatformBackup,"primary"));
  view.outer.append(info("A restauração disponível nesta tela é uma validação em banco temporário. Ela confirma que o snapshot pode ser recuperado e preserva o banco de produção.",true));
  const content=el("div");view.outer.append(content);content.append(loadingSurface());
  try {
    const result=await api.get("/ops/backups",{tenant:false});
    if(token!==state.renderId)return;
    const restore=result.restore||{};
    const cards=el("div","metric-grid");
    cards.append(
      metric("Estado do backup",result.state||"—",result.pending_action?`Ação pendente: ${result.pending_action}`:"Verificação automática"),
      metric("Última cópia",dateTime(result.last_success_at),result.last_failure_at?`Última falha: ${dateTime(result.last_failure_at)}`:"Sem falha registrada"),
      metric("Destino externo",result.external_configured?"Configurado":"Não configurado","Restic criptografado"),
      metric("Última restauração",restore.status?String(restore.status):"Nenhuma",restore.completed_at?dateTime(Number(restore.completed_at)*1000):"Ainda não executada")
    );
    const overview=surface("Estado do serviço","O container de backup executa cópias periódicas e consome as solicitações desta tela.");
    overview.append(detailList([
      ["Periodicidade",`${Math.round((result.interval_seconds||3600)/60)} minutos`],
      ["Próxima ação",result.pending_action?"Aguardando o worker":"Agendada pelo serviço"],
      ["Mensagem da restauração",restore.message||"—"],
      ["Falha mais recente",result.last_failure||"Nenhuma falha registrada"]
    ]));
    const snapshots=surface("Snapshots disponíveis","Escolha uma cópia estável para validar a recuperação.");
    const rows=Array.isArray(result.snapshots)?result.snapshots:[];
    snapshots.append(rows.length?table([
      {label:"ID",render:row=>row.short_id||row.id.slice(0,8)},
      {label:"Data",render:row=>dateTime(row.time)},
      {label:"Origem",render:row=>row.hostname},
      {label:"Tags",render:row=>(row.tags||[]).join(", ")||"—"}
    ],rows,row=>[{label:"Restaurar",onClick:()=>requestPlatformRestore(row)}]):empty("Nenhum snapshot listado","O serviço ainda não publicou o inventário de cópias."));
    content.replaceChildren(cards,overview,snapshots);
  }catch(error){if(token===state.renderId){content.replaceChildren(empty("Falha ao carregar backups",errorText(error),"!"));handleError(error);}}
}

function billingForm(plans) {
  const form=el("form","form-grid");
  function selectField(id,label,options) {
    const field=el("div","field"),caption=el("label","",label),input=el("select");
    input.id=id;caption.htmlFor=id;
    options.forEach(([value,text])=>{const option=el("option","",text);option.value=value;input.append(option);});
    field.append(caption,input);form.append(field);return input;
  }
  function inputField(id,label,type="text") {
    const field=el("div","field"),caption=el("label","",label),input=el("input");
    input.id=id;input.type=type;input.required=true;caption.htmlFor=id;
    field.append(caption,input);form.append(field);return input;
  }
  const plan=selectField("billing-plan","Plano",plans.map(row=>[row.id,row.name+" · "+money(row.price_month_cents)+"/mês"]));
  const cycle=selectField("billing-cycle","Periodicidade",[["MONTHLY","Mensal"],["YEARLY","Anual"]]);
  const type=selectField("billing-type","Forma de cobrança",[["PIX","Pix"],["BOLETO","Boleto"]]);
  const name=inputField("billing-name","Nome do pagador");
  const documentNumber=inputField("billing-document","CPF ou CNPJ");documentNumber.maxLength=30;
  const email=inputField("billing-email","E-mail","email");
  form.append(info("A solicitação será enviada ao provedor quando configurado. Esta ação não confirma pagamento.",true));
  const message=formMessage(form),submit=formButtons(form,"Solicitar assinatura");
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!form.reportValidity())return;
    const payload={
      plan_id:plan.value,cycle:cycle.value,billing_type:type.value,
      name:name.value.trim(),document:documentNumber.value.trim(),email:email.value.trim()
    };
    payload.idempotency_key=idempotency(form,payload);
    submit.disabled=true;
    try {
      const result=await api.post("/ops/billing/subscribe",payload);
      closeDialog();toast("Solicitação criada: "+String(result.status||"pendente")+". Consulte o processamento em Integrações.");renderRoute();
    }catch(error){handleError(error,message);submit.disabled=false;}
  });
  openDialog("Solicitar assinatura","A assinatura dependerá da configuração e resposta do provedor.",form);
}
function releaseForm() {
  const fields=[
    {key:"component",label:"Componente",type:"select",required:true,choices:[["terminal","Terminal"],["cloud","Nuvem"]]},
    {key:"channel",label:"Canal",type:"select",required:true,choices:[["pilot","Piloto"],["stable","Estável"]]},
    {key:"version",label:"Versão",required:true,placeholder:"2.0.0",max:50},
    {key:"url",label:"URL HTTPS do pacote",type:"url",required:true,full:true,max:2000},
    {key:"sha256",label:"SHA-256 do arquivo",required:true,full:true,max:64},
    {key:"size_bytes",label:"Tamanho em bytes",type:"number",required:true,min:1},
    {key:"notes",label:"Notas da versão",type:"textarea",full:true,max:4000}
  ];
  formFor(fields).then(form=>{
    form.append(info("A publicação registra metadados de atualização. O terminal deve validar assinatura e checksum antes de instalar.",true));
    const message=formMessage(form),submit=formButtons(form,"Publicar versão");
    form.addEventListener("submit",async event=>{
      event.preventDefault();if(!form.reportValidity())return;
      submit.disabled=true;
      try {
        const body=formPayload(form,fields);
        body.size_bytes=Number(body.size_bytes);
        if(!/^\d+\.\d+\.\d+$/.test(body.version))throw new Error("Use versão no formato 2.0.0.");
        if(!/^[0-9a-f]{64}$/.test(body.sha256))throw new Error("O SHA-256 precisa ter 64 caracteres hexadecimais minúsculos.");
        if(!Number.isSafeInteger(body.size_bytes)||body.size_bytes<1)throw new Error("Tamanho inválido.");
        await api.post("/ops/releases",body,{tenant:false});
        closeDialog();toast("Versão publicada.");renderRoute();
      }catch(error){handleError(error,message);submit.disabled=false;}
    });
    openDialog("Publicar versão","Confira URL, tamanho e checksum antes de publicar.",form);
  }).catch(handleError);
}
async function releasesPage() {
  const token=++state.renderId;
  const view=page("Versões","Pacotes publicados para terminal e nuvem.","Plataforma");
  view.actions.append(button("+ Publicar versão",releaseForm));
  view.outer.append(info("A nuvem só muda quando o Dokploy recria os serviços a partir do Git. O terminal consulta o manifesto assinado, mas a instalação do pacote continua sendo uma ação manual do suporte.",true));
  const box=surface("Histórico de versões");view.outer.append(box);box.append(loadingSurface());
  try {
    const [result,releaseStatus]=await Promise.all([
      api.get("/ops/releases",{tenant:false}),
      api.get("/ops/releases/status",{tenant:false})
    ]);
    if(token!==state.renderId)return;
    box.lastChild.remove();
    const statusGrid=el("div","metric-grid");
    const cloud=releaseStatus.cloud||{},terminal=releaseStatus.terminal||{};
    statusGrid.append(
      metric("Nuvem em execução",cloud.running_version||"—",cloud.deployment_pending?"Há uma versão publicada aguardando deploy":"Versão em execução"),
      metric("Nuvem publicada",cloud.published_version||"—",cloud.published_version?"Metadado disponível":"Nenhuma versão cloud publicada"),
      metric("Terminal estável",terminal.published_version||"—",terminal.manifest_available?"Manifesto assinado disponível":"Nenhum pacote publicado")
    );
    view.outer.insertBefore(statusGrid,box);
    const rows=records(result);
    box.append(rows.length?table([
      {label:"Componente",render:row=>row.component==="cloud"?"Nuvem":"Terminal"},
      {label:"Canal",render:row=>row.channel==="pilot"?"Piloto":"Estável"},
      {label:"Versão",render:row=>row.version},
      {label:"Pacote",render:row=>{const a=el("a","",row.url);a.href=row.url;a.target="_blank";a.rel="noopener noreferrer";return a;}},
      {label:"Publicado",render:row=>dateTime(row.published_at)},
      {label:"Estado",render:row=>statusBadge(row.active?"Ativa":"Retirada")}
    ],rows,row=>row.active?[{label:"Retirar",danger:true,onClick:()=>confirmAction("Retirar versão","O manifesto deixará de oferecer esta versão em novas consultas.",async()=>{
      await api.post("/ops/releases/"+row.id+"/withdraw",{}, {tenant:false});
      toast("Versão retirada.");renderRoute();
    },"Retirar")}]:[]):empty("Nenhuma versão","Publique um pacote validado para iniciar o histórico."));
  }catch(error){if(token===state.renderId){box.replaceChildren(empty("Falha ao carregar",errorText(error),"!"));handleError(error);}}
}
async function invoicesPage() {
  const token=++state.renderId;
  const view=page("Cobranças","Cobranças registradas pelo provedor na plataforma.","Plataforma");
  const box=surface("Cobranças");view.outer.append(box);box.append(loadingSurface());
  try {
    const result=await platformApi("/invoices");
    if(token!==state.renderId)return;
    box.lastChild.remove();const rows=records(result);
    box.append(pagedContent(result,"/platform/invoices",[
      {label:"Empresa",render:row=>row.tenant_name},{label:"Valor",render:row=>money(row.amount_cents)},
      {label:"Vencimento",render:row=>dateOnly(row.due_date)},
      {label:"Estado",render:row=>statusBadge(row.status)},
      {label:"Provedor",render:row=>row.provider},
      {label:"Checkout",render:row=>{
        const url=httpsUrl(row.checkout_url);
        if(!url)return "—";
        const a=el("a","","Abrir");a.href=url;a.target="_blank";a.rel="noopener noreferrer";return a;
      }}
    ],null,empty("Nenhuma cobrança","Cobranças conciliadas aparecerão aqui."),{tenant:false}));
  }catch(error){if(token===state.renderId){box.replaceChildren(empty("Falha ao carregar",errorText(error),"!"));handleError(error);}}
}
async function platformAuditPage() {
  const token=++state.renderId;
  const view=page("Auditoria","Alterações administrativas de toda a plataforma.","Plataforma");
  const box=surface("Eventos recentes");view.outer.append(box);box.append(loadingSurface());
  try {
    const result=await platformApi("/audit");
    if(token!==state.renderId)return;
    box.lastChild.remove();const rows=records(result);
    box.append(pagedContent(result,"/platform/audit",[
      {label:"Data",render:row=>dateTime(row.created_at)},{label:"Empresa",render:row=>row.tenant_id},
      {label:"Quem",render:row=>row.actor_email},{label:"Ação",render:row=>row.action},
      {label:"Recurso",render:row=>row.subject_type},{label:"ID",render:row=>row.subject_id}
    ],null,empty("Sem eventos","As ações auditadas aparecerão aqui."),{tenant:false}));
  }catch(error){if(token===state.renderId){box.replaceChildren(empty("Falha ao carregar",errorText(error),"!"));handleError(error);}}
}
async function platformUserForm(record) {
  const editing=!!record;
  if (record?.protected) {
    toast("A conta principal do SaaS é protegida e não pode ser editada.", "error");
    return;
  }
  const fields=[
    {key:"name",label:"Nome",required:true,max:150},
    {key:"email",label:"E-mail",type:"email",required:true,max:254},
    {key:"password",label:editing?"Nova senha (opcional)":"Senha",type:"password",required:!editing,max:128,minLength:8,full:true,
      help:"8 a 128 caracteres, com maiúscula, minúscula, número e símbolo."}
  ];
  const form=await formFor(fields,record||{});
  const roles=el("fieldset","role-set full");
  roles.append(el("legend","","Papéis de plataforma"));
  const choices=[["platform_support","Atendimento"],["platform_billing","Cobrança e planos"],["platform_ops","Operação e versões"]];
  const checked=new Map();
  choices.forEach(([key,label])=>{
    const line=el("label","checkbox-line"),check=el("input");
    check.type="checkbox";check.value=key;check.checked=(record?.roles||[]).includes(key);
    checked.set(key,check);
    line.append(check,el("span","",label));roles.append(line);
  });
  form.append(roles);
  const active=el("label","checkbox-line full"),activeInput=el("input");
  activeInput.type="checkbox";activeInput.checked=record?!!record.active:true;
  active.append(activeInput,el("span","","Acesso ativo"));
  form.append(active);
  form.append(info(editing && !record.platform_access
    ? "Este usuário já pertence a uma empresa. Marque um ou mais papéis para promovê-lo também à administração SaaS."
    : "Os papéis selecionados dão acesso administrativo a todas as empresas da plataforma.", true));
  const message=formMessage(form),submit=formButtons(form,editing?"Salvar permissões":"Adicionar pessoa");
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!form.reportValidity())return;
    submit.disabled=true;
    try {
      const payload=formPayload(form,fields);
      payload.roles=choices.filter(([key])=>checked.get(key).checked).map(([key])=>key);
      payload.active=activeInput.checked;
      if(!editing&&!payload.roles.length)throw new Error("Selecione pelo menos um papel para criar o acesso SaaS.");
      if(payload.password&&!/^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z\d]).{8,128}$/.test(payload.password))
        throw new Error("A senha precisa ter 8 a 128 caracteres, com maiúscula, minúscula, número e símbolo.");
      if(editing)await api.patch("/platform/users/"+record.id,payload,{tenant:false});
      else await api.post("/platform/users",payload,{tenant:false});
      closeDialog();toast("Acesso da plataforma salvo.");renderRoute();
    }catch(error){handleError(error,message);submit.disabled=false;}
  });
  openDialog(editing
    ? (record.platform_access ? "Editar permissões SaaS" : "Promover usuário para o SaaS")
    : "Adicionar usuário do SaaS",
    editing ? "Você pode conceder, trocar ou remover o acesso administrativo sem apagar o usuário da empresa."
      : "Crie um login administrativo separado para a plataforma.", form);
}
async function platformUsersPage() {
  const token=++state.renderId;
  const view=page("Usuários e permissões","Crie acessos administrativos e defina exatamente o que cada pessoa pode fazer.","Plataforma");
  view.actions.append(button("+ Adicionar pessoa",()=>platformUserForm().catch(handleError)));
  view.outer.append(info("A lista mostra a conta principal e o login proprietário criado em cada contratação. Somente o proprietário ativo pode ser promovido ao SaaS.",true));
  const box=surface("Proprietários e acessos","O mesmo login que administra a empresa pode receber permissões de atendimento, cobrança ou operação na plataforma.");view.outer.append(box);box.append(loadingSurface());
  try {
    const result=await platformApi("/users");
    if(token!==state.renderId)return;
    box.lastChild.remove();const rows=records(result);
    box.append(pagedContent(result,"/platform/users",[
      {label:"Pessoa",render:row=>row.name},{label:"E-mail",render:row=>row.email},
      {label:"Empresa",render:row=>(row.memberships||[]).map(member=>`${member.tenant_name} · ${roleNames[member.role]||member.role}`).join(", ")},
      {label:"Papéis",render:row=>(row.roles||[]).map(role=>({
        platform_support:"Atendimento",platform_billing:"Cobrança",platform_ops:"Operação"
      })[role]||role).join(", ")},
      {label:"SaaS",render:row=>statusBadge(row.platform_access?"Ativo":"Inativo")},
      {label:"Conta",render:row=>statusBadge(row.active?"Ativo":"Inativo")}
    ],row=>row.protected
      ? [{label:"Protegida",onClick:()=>toast("A conta principal não pode ser alterada por esta tela.","error")}]
      : [{label:row.platform_access?"Editar permissões":"Promover para admin SaaS",onClick:()=>platformUserForm(row).catch(handleError)}],
      empty("Nenhum proprietário","A conta criada na contratação aparecerá aqui."),{tenant:false}));
  }catch(error){if(token===state.renderId){box.replaceChildren(empty("Falha ao carregar",errorText(error),"!"));handleError(error);}}
}

function showPlatformRecoveryCodes(codes) {
  const content = el("div");
  content.append(info("Guarde estes códigos em um local seguro. Cada código funciona uma única vez.", true));
  const list = el("code", "auth-recovery-codes");
  list.textContent = (codes || []).join("\n");
  content.append(list);
  content.append(el("p", "hint", "Se você perder o autenticador, use um destes códigos para entrar e depois gere uma nova lista."));
  content.append(el("div", "form-actions"));
  openDialog("Códigos de recuperação", "A lista não será exibida novamente depois que esta janela for fechada.", content);
}

async function startPlatformMfaEnrollment() {
  const setup = await api.post("/auth/mfa/enroll", {}, { tenant:false });
  const content = el("div");
  content.append(info("Abra Google Authenticator, Microsoft Authenticator ou outro aplicativo TOTP e adicione esta conta. Depois informe o código de seis dígitos.", false));
  const secret = el("code", "auth-secret", setup.secret);
  const uri = el("small", "auth-secret-uri", setup.otpauth_uri);
  const qr = el("img", "mfa-qr"); qr.src = setup.qr_data_uri || ""; qr.alt = "QR Code para configurar o autenticador"; qr.hidden = !setup.qr_data_uri;
  content.append(el("p", "field-label", "Escaneie no aplicativo autenticador"), qr, el("small", "mfa-qr-help", "Se não conseguir escanear, use a chave de configuração abaixo."), el("p", "field-label", "Chave de configuração"), secret, uri);
  const field = el("div", "field");
  const label = el("label", "", "Código do autenticador");
  const code = el("input");
  code.inputMode = "numeric"; code.autocomplete = "one-time-code"; code.maxLength = 8; code.placeholder = "000000"; code.required = true;
  label.htmlFor = "platform-mfa-code"; code.id = "platform-mfa-code"; field.append(label, code); content.append(field);
  const message = formMessage(content); message.hidden = true;
  const actions = el("div", "form-actions");
  actions.append(button("Cancelar", closeDialog, "ghost"));
  const confirm = button("Ativar MFA");
  confirm.addEventListener("click", async () => {
    if (!code.value.trim()) { message.textContent = "Informe o código do autenticador."; message.hidden = false; return; }
    confirm.disabled = true;
    try {
      const result = await api.post("/auth/mfa/setup/confirm", { code: code.value.trim() }, { tenant:false });
      closeDialog();
      renderRoute();
      showPlatformRecoveryCodes(result.recovery_codes);
      toast("MFA ativada para esta conta.");
    } catch (error) { handleError(error, message); confirm.disabled = false; }
  });
  actions.append(confirm); content.append(actions);
  openDialog("Ativar MFA da administração", "A proteção passa a ser exigida no próximo acesso.", content);
}

async function regeneratePlatformRecoveryCodes() {
  const content = el("div");
  content.append(info("Os códigos atuais deixarão de funcionar imediatamente. Gere uma nova lista somente se você perdeu ou suspeita que os códigos antigos foram expostos.", true));
  const message = formMessage(content); message.hidden = true;
  const actions = el("div", "form-actions");
  actions.append(button("Cancelar", closeDialog, "ghost"));
  const confirm = button("Gerar novos códigos", async () => {
    confirm.disabled = true;
    try {
      const result = await api.post("/auth/mfa/recovery-codes", {}, { tenant:false });
      closeDialog(); renderRoute(); showPlatformRecoveryCodes(result.recovery_codes);
    } catch (error) { handleError(error, message); confirm.disabled = false; }
  }, "danger");
  actions.append(confirm); content.append(actions);
  openDialog("Regenerar códigos de recuperação", "Confirme a troca dos códigos atuais.", content);
}

async function platformSecurityPage() {
  const token = ++state.renderId;
  const view = page("Segurança e MFA", "Proteja o acesso à administração SaaS e controle os códigos de recuperação.", "Plataforma");
  const box = surface("Autenticação multifator"); view.outer.append(box); box.append(loadingSurface());
  try {
    const status = await api.get("/auth/mfa/status", { tenant:false });
    if (token !== state.renderId) return;
    box.lastChild.remove();
    const cards = el("div", "metric-grid");
    cards.append(metric("MFA", status.enabled ? "Ativa" : "Não configurada", status.required ? "Obrigatória em produção" : "Ativação opcional neste ambiente"));
    cards.append(metric("Códigos restantes", String(status.recovery_codes_remaining || 0), status.enabled ? "Códigos de uso único" : "Disponíveis após ativação"));
    const detail = surface("Estado da conta", status.enabled
      ? "A senha sozinha não libera a administração. O próximo acesso exigirá o autenticador ou um código de recuperação."
      : "Ative a MFA agora para proteger esta conta. Em produção, a ativação é obrigatória no primeiro acesso.");
    detail.append(detailList([
      ["Obrigatoriedade", status.required ? "Produção: obrigatória" : "Local: opcional"],
      ["Ativação", status.enabled ? dateTime(status.confirmed_at) : "Ainda não ativada"],
      ["Recuperação", status.enabled ? `${status.recovery_codes_remaining} códigos disponíveis` : "Será criada ao ativar"]
    ]));
    const actions = el("div", "form-actions");
    if (!status.enabled) actions.append(button("Ativar MFA", () => startPlatformMfaEnrollment().catch(handleError), "primary"));
    else actions.append(button("Gerar novos códigos", () => regeneratePlatformRecoveryCodes(), "secondary"));
    detail.append(actions);
    box.append(cards, detail);
  } catch (error) { if (token === state.renderId) { box.replaceChildren(empty("Falha ao carregar", errorText(error), "!")); handleError(error); } }
}

const labelPresets = [
  { width_mm: 30, height_mm: 20, use: "gôndola pequena, bijuteria" },
  { width_mm: 40, height_mm: 25, use: "preço de gôndola" },
  { width_mm: 40, height_mm: 30, use: "produto pequeno" },
  { width_mm: 40, height_mm: 40, use: "frios, padaria" },
  { width_mm: 50, height_mm: 25, use: "código de barras compacto" },
  { width_mm: 50, height_mm: 30, use: "produto com código de barras" },
  { width_mm: 60, height_mm: 40, use: "balança, hortifrúti" },
  { width_mm: 100, height_mm: 50, use: "caixa, prateleira" },
  { width_mm: 100, height_mm: 100, use: "volume, estoque" },
  { width_mm: 100, height_mm: 150, use: "envio, Correios e marketplaces" }
];
const labelSensorNames = { gap: "Espaço entre etiquetas (mais comum)", mark: "Marca preta no verso", continuous: "Papel contínuo, sem divisão" };
function selectNode(id, labelText, options, value) {
  const field = el("div", "field"), label = el("label", "", labelText), select = el("select");
  label.htmlFor = select.id = id;
  options.forEach(([optionValue, text]) => { const option = el("option", "", text); option.value = optionValue; select.append(option); });
  if (value !== undefined) select.value = String(value);
  field.append(label, select);
  return { field, select };
}
function numberNode(id, labelText, min, max, value) {
  const field = el("div", "field"), label = el("label", "", labelText), input = el("input");
  label.htmlFor = input.id = id; input.type = "number"; input.min = String(min); input.max = String(max); input.step = "0.1"; input.required = true; input.value = String(value);
  field.append(label, input);
  return { field, input };
}
function testLabelPrintDocument(size) {
  const font = Math.max(6, Math.min(14, size.height_mm / 4));
  return `<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>Etiqueta de teste · ${labelSizeText(size)}</title><style>
    html,body{margin:0;padding:0;background:#fff}
    @page{size:${size.width_mm}mm ${size.height_mm}mm;margin:0}
    .test-label{box-sizing:border-box;width:${size.width_mm}mm;height:${size.height_mm}mm;border:.6mm solid #000;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1mm;font-family:Arial,sans-serif;color:#000;overflow:hidden}
    .test-label strong{font-size:${font}pt}.test-label span{font-size:${(font * .7).toFixed(1)}pt}
  </style></head><body><div class="test-label"><strong>TESTE PDV</strong><span>${labelSizeText(size)}</span></div></body></html>`;
}
async function printersPage() {
  const token = ++state.renderId;
  const view = page("Impressoras", "Escolha o tamanho da etiqueta, imprima um teste e calibre a impressora de etiquetas.", "Administração");
  view.outer.append(info("As etiquetas são impressas por este computador, pela janela de impressão do navegador. A impressora de etiquetas precisa estar instalada no sistema (Windows, Linux ou macOS) com o driver do fabricante."));
  const labelBox = surface("Etiqueta da loja", "Usada em Produtos → Imprimir etiquetas.");
  const testBox = surface("Testar e calibrar", "Calibre sempre que trocar o rolo ou quando a impressora puxar etiquetas em branco.");
  view.outer.append(labelBox, testBox);
  labelBox.append(loadingSurface());
  try {
    const settings = await api.get("/label-settings");
    if (token !== state.renderId) return;
    const label = { ...defaultLabelSize, ...settings };

    const form = el("form", "form-grid");
    const presetKey = size => `${size.width_mm}x${size.height_mm}`;
    const preset = selectNode("label-preset", "Tamanho da etiqueta",
      [...labelPresets.map(size => [presetKey(size), `${labelSizeText(size)} · ${size.use}`]), ["custom", "Outro tamanho…"]]);
    preset.select.value = labelPresets.some(size => presetKey(size) === presetKey(label)) ? presetKey(label) : "custom";
    const width = numberNode("label-width", "Largura (mm)", 15, 108, label.width_mm);
    const height = numberNode("label-height", "Altura (mm)", 10, 200, label.height_mm);
    const sensor = selectNode("label-sensor", "Tipo de papel", Object.entries(labelSensorNames), label.sensor);
    const gap = selectNode("label-gap", "Espaço entre etiquetas", [0, 1, 2, 3, 4, 5, 6, 8, 10].map(mm => [String(mm), mm + " mm"]), label.gap_mm);
    if (![...gap.select.options].some(option => option.value === String(label.gap_mm))) gap.select.value = "2";
    const smallWarning = info("Em etiquetas com menos de 40 mm de largura o código de barras fica pequeno e alguns leitores podem não reconhecer.", true);
    const currentLabel = () => ({ width_mm: Number(width.input.value), height_mm: Number(height.input.value),
      gap_mm: sensor.select.value === "continuous" ? 0 : Number(gap.select.value), sensor: sensor.select.value });
    const driverHint = el("p", "label-print-help");
    const syncFields = () => {
      const custom = preset.select.value === "custom";
      width.input.disabled = height.input.disabled = !custom;
      if (!custom) { const [w, h] = preset.select.value.split("x").map(Number); width.input.value = w; height.input.value = h; }
      gap.select.disabled = sensor.select.value === "continuous";
      smallWarning.hidden = Number(width.input.value) >= 40;
      const size = currentLabel();
      driverHint.textContent = "No driver da impressora, configure o papel " + labelSizeText(size) + " e o tipo " +
        (size.sensor === "gap" ? "etiqueta com espaço de " + size.gap_mm + " mm" : size.sensor === "mark" ? "marca preta" : "contínuo") +
        ". Na janela de impressão do navegador, use escala 100%, margens nenhuma e desative cabeçalhos e rodapés.";
    };
    [preset.select, sensor.select, gap.select].forEach(node => node.addEventListener("change", syncFields));
    [width.input, height.input].forEach(node => node.addEventListener("input", syncFields));
    syncFields();
    form.append(preset.field, sensor.field, width.field, height.field, gap.field);
    const labelMessage = formMessage(form);
    const labelActions = el("div", "form-actions"), save = el("button", "button", "Salvar tamanho"); save.type = "submit";
    labelActions.append(save); form.append(labelActions);
    labelBox.replaceChildren(labelBox.firstChild, labelBox.children[1], smallWarning, form);
    form.addEventListener("submit", async event => {
      event.preventDefault(); if (!form.reportValidity()) return;
      save.disabled = true;
      try { await api.patch("/label-settings", currentLabel()); labelMessage.hidden = true; toast("Tamanho da etiqueta salvo."); }
      catch (error) { handleError(error, labelMessage); }
      finally { save.disabled = false; }
    });

    const steps = el("ol", "label-calibration-steps");
    ["Coloque o rolo com o lado térmico (o que escurece ao riscar com a unha) virado para cima, passando pelo sensor.",
     "Desligue a impressora.",
     "Segure o botão FEED e ligue a impressora.",
     "Solte o FEED quando a luz piscar ou o papel começar a avançar. A impressora mede o espaço entre etiquetas sozinha e para no início de uma etiqueta.",
     "Com a luz estável (verde ou azul), clique em Imprimir etiqueta de teste. Deve sair uma única etiqueta, dentro da borda."
    ].forEach(text => steps.append(el("li", "", text)));
    const testActions = el("div", "connection-actions");
    testActions.append(button("Imprimir etiqueta de teste", () => {
      if (!form.reportValidity()) return;
      printLabelDocument(testLabelPrintDocument(currentLabel()));
    }));
    testBox.append(steps, testActions, driverHint,
      info("Luz vermelha piscando ou várias etiquetas em branco: o sensor perdeu o espaço entre etiquetas. Repita a calibração e confira se o tamanho e o espaço no driver são os mesmos desta tela.", true));
  } catch (error) {
    if (token === state.renderId) { labelBox.replaceChildren(empty("Falha ao carregar", errorText(error), "!")); testBox.replaceChildren(); handleError(error); }
  }
}

const tenantRoutes = {
  dashboard:dashboardPage,sales:salesPage,cash:cashPage,commissions:commissionsPage,reports:reportsPage,products:()=>resourcePage("products"),
  categories:()=>resourcePage("categories"),stock:stockPage,
  customers:()=>resourcePage("customers"),suppliers:()=>resourcePage("suppliers"),
  finance:financePage,shops:()=>resourcePage("shops"),company:companyPage,users:usersPage,
  terminals:terminalsPage,printers:printersPage,subscription:subscriptionPage,integrations:integrationsPage,audit:auditPage,sync_conflicts:syncConflictsPage
};
const platformRoutes = {
  overview:platformOverview,tenants:platformTenants,plans:platformPlans,
  subscriptions:platformSubscriptions,devices:platformDevices,services:platformServices,
  releases:releasesPage,invoices:invoicesPage,platform_audit:platformAuditPage,security:platformSecurityPage,backups:platformBackupsPage,
  platform_users:platformUsersPage
};
function activeSection() {
  const id=decodeURIComponent(location.hash.slice(1));
  const navigation=platform?platformNav.filter(item=>item.superuserOnly?platformSuperuser():platformCan(...item.roles)):tenantNav.filter(item=>item.roles.includes(state.role)&&(!item.feature||hasFeature(item.feature)));
  return navigation.some(item=>item.id===id)?id:(navigation[0]?.id || (platform?"overview":"dashboard"));
}
function renderNav() {
  const nav=$("#sidebar-nav");nav.replaceChildren();
  const current=activeSection();
  const links=platform?platformNav.filter(item=>item.superuserOnly?platformSuperuser():platformCan(...item.roles)):tenantNav.filter(item=>item.roles.includes(state.role)&&(!item.feature||hasFeature(item.feature)));
  let group="";
  links.forEach(item=>{
    if(item.group!==group){group=item.group;nav.append(el("div","nav-group",group));}
    const link=el("a","nav-item"+(item.id===current?" active":""),item.icon+"  "+item.label);
    link.href="#"+item.id;
    if(item.id===current)link.setAttribute("aria-current","page");
    link.addEventListener("click",()=>setSidebar(false));
    nav.append(link);
  });
}
function renderRoute() {
  closeDialog();
  renderNav();
  const id=activeSection();
  const routes=platform?platformRoutes:tenantRoutes;
  Promise.resolve(routes[id]()).catch(error=>{
    page("Falha ao abrir seção",errorText(error),"Painel").outer.append(empty("Não foi possível abrir a seção",errorText(error),"!"));
    handleError(error);
  });
}
window.addEventListener("hashchange",renderRoute);
$("#tenant-selector").addEventListener("change",async event=>{
  const id=event.currentTarget.value;
  const membership=state.memberships.find(item=>String(item.tenant_id)===String(id));
  if(!membership)return;
  state.tenantId=id;state.role=membership.role;setTenant(id);
  $("#sidebar-role").textContent=roleNames[state.role]||state.role;
  try { localStorage.setItem("pdv2-tenant",id); } catch {}
  state.shops=[];state.features=[];state.subscription=null;
  try { await Promise.all([loadShops(),loadSubscription()]); }
  catch(error) { handleError(error); }
  renderRoute();
});
async function init() {
  try {
    await getCsrf();
    const session=await api.get("/auth/me",{tenant:false});
    state.user=session.user;
    state.memberships=session.memberships || [];
    state.platformRoles=state.user?.platform_roles||[];
    if(!state.user)throw new Error("Faça login para continuar.");
    $("#sidebar-user").textContent=state.user.name||state.user.email||"Conta";
    $("#avatar").textContent=(state.user.name||state.user.email||"P").trim()[0].toUpperCase();
    renderProfileMenu();
    $("#today").textContent=new Intl.DateTimeFormat("pt-BR",{dateStyle:"full"}).format(new Date());
    if(platform) {
      if(!state.user.is_staff || !platformCan("platform_support","platform_billing","platform_ops")) {
        $("#loading").replaceChildren(empty("Sem acesso à plataforma","Esta conta não possui um papel de administração na plataforma.","!"));return;
      }
      $("#tenant-selector-wrap").hidden=true;
      $("#sidebar-role").textContent=state.user.is_superuser?"Superusuário":"Equipe da plataforma";
    } else {
      if(!state.memberships.length) {
        if(platformCan("platform_support","platform_billing","platform_ops")){location.replace("/platform/");return;}
        $("#loading").replaceChildren(empty("Nenhuma empresa associada","Esta conta não tem acesso a uma empresa. Solicite um convite ao administrador.","!"));
        return;
      }
      let saved;
      try {saved=new URLSearchParams(location.search).get("tenant") || localStorage.getItem("pdv2-tenant");}catch{}
      const current=state.memberships.find(row=>String(row.tenant_id)===String(saved))||state.memberships[0];
      state.tenantId=current.tenant_id;state.role=current.role;setTenant(state.tenantId);
      const select=$("#tenant-selector");
      state.memberships.forEach(membership=>{
        const option=el("option","",membership.tenant_name);
        option.value=membership.tenant_id;select.append(option);
      });
      select.value=state.tenantId;
      $("#sidebar-role").textContent=roleNames[state.role]||state.role;
      try {await Promise.all([loadShops(),loadSubscription()]);}catch(error){handleError(error);}
    }
    $("#console").hidden=false;
    $("#loading").hidden=true;
    renderRoute();
  }catch(error){
    if(error?.status===401){location.replace("/login/?next="+encodeURIComponent(location.pathname));return;}
    $("#loading").replaceChildren();
    const box=empty("Não foi possível abrir o painel",errorText(error),"!");
    const retry=button("Tentar novamente",()=>location.reload());
    box.append(retry);
    $("#loading").append(box);
  }
}
init();
