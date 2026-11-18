const API_ROOT = "/api/v1";
export const APP_VERSION = "2.1.0";
export const VENDOR = {
  name: "CrownTech",
  site: "crowntech.com.br",
  url: "https://crowntech.com.br",
  signature: "PDV2 · um produto CrownTech",
  footer: "PDV2 · um produto CrownTech · crowntech.com.br"
};
let csrfToken = "";
let activeTenant = "";

export class ApiError extends Error {
  constructor(message, status, details) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

function cookie(name) {
  const entry = document.cookie.split("; ").find(part => part.startsWith(name + "="));
  return entry ? decodeURIComponent(entry.slice(name.length + 1)) : "";
}

function errorMessage(value, status) {
  if (typeof value === "string" && value.trim()) return value;
  if (value && typeof value === "object") {
    for (const key of ["detail", "error", "message"]) {
      if (typeof value[key] === "string" && value[key].trim()) return value[key];
    }
    const errors = value.errors || value;
    for (const [field, messages] of Object.entries(errors)) {
      if (Array.isArray(messages) && messages.length) return field + ": " + messages.join(", ");
      if (typeof messages === "string" && messages.trim()) return field + ": " + messages;
    }
  }
  if (status === 401) return "Sua sessão terminou. Entre novamente.";
  if (status === 403) return "Você não tem permissão para esta ação.";
  if (status === 404) return "O recurso solicitado não foi encontrado.";
  if (status >= 500) return "O servidor apresentou uma falha. Tente novamente.";
  return "Não foi possível concluir a solicitação.";
}

export function setTenant(id) { activeTenant = id || ""; }
export function getTenant() { return activeTenant; }

export async function getCsrf() {
  try {
    const response = await fetch(API_ROOT + "/csrf", { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (!response.ok) return cookie("csrftoken");
    const body = await response.json();
    csrfToken = body.csrfToken || body.csrf_token || cookie("csrftoken");
    return csrfToken;
  } catch {
    return cookie("csrftoken");
  }
}

export async function request(path, { method = "GET", body, tenant, signal } = {}) {
  const mutation = !["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase());
  if (mutation && !csrfToken) await getCsrf();
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (mutation) headers["X-CSRFToken"] = csrfToken || cookie("csrftoken");
  if (tenant !== false && (tenant || activeTenant)) headers["X-Tenant-ID"] = tenant || activeTenant;
  let response;
  try {
    response = await fetch(API_ROOT + path, {
      method,
      credentials: "same-origin",
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError("Sem conexão com o servidor. Confira sua rede e tente novamente.", 0);
  }
  const raw = await response.text();
  let result = null;
  if (raw) {
    try { result = JSON.parse(raw); }
    catch {
      if (!response.ok) throw new ApiError(errorMessage(null, response.status), response.status);
      throw new ApiError("O servidor enviou uma resposta inválida.", response.status);
    }
  }
  if (!response.ok) throw new ApiError(errorMessage(result, response.status), response.status, result);
  return result;
}

export const api = {
  get: (path, options) => request(path, { ...options, method: "GET" }),
  post: (path, body, options) => request(path, { ...options, method: "POST", body }),
  patch: (path, body, options) => request(path, { ...options, method: "PATCH", body }),
  put: (path, body, options) => request(path, { ...options, method: "PUT", body }),
  delete: (path, options) => request(path, { ...options, method: "DELETE" })
};

export function records(result, key = "results") {
  if (Array.isArray(result)) return result;
  if (Array.isArray(result?.[key])) return result[key];
  if (Array.isArray(result?.items)) return result.items;
  return [];
}

export function money(cents) {
  const amount = Number(cents);
  if (!Number.isFinite(amount)) return "—";
  return new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" }).format(amount / 100);
}

export function dateTime(value) {
  if (!value) return "—";
  const parsed = typeof value === "number" ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value);
  return Number.isNaN(parsed.getTime()) ? "—" : new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short" }).format(parsed);
}

export function dateOnly(value) {
  if (!value) return "—";
  const parsed = new Date(String(value).length === 10 ? value + "T12:00:00" : value);
  return Number.isNaN(parsed.getTime()) ? "—" : new Intl.DateTimeFormat("pt-BR", { dateStyle: "short" }).format(parsed);
}

export function quantity(milli, unit = "UN") {
  const n = Number(milli);
  if (!Number.isFinite(n)) return "—";
  // Never round to whole units: 0.350 kg must not read as "0".
  const text = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 3 }).format(n / 1000);
  return unit ? text + " " + unit : text;
}

export function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

export function errorText(error) {
  return error instanceof Error ? error.message : "Ocorreu uma falha inesperada.";
}

export function editableMoney(cents) {
  const value = Number(cents);
  return Number.isFinite(value) ? (value / 100).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "";
}

export function parseMoneyInput(raw) {
  let value = String(raw ?? "").trim().replace(/R\$|\s/g, "");
  if (!value) return NaN;
  if (value.includes(",")) value = value.replace(/\./g, "").replace(",", ".");
  else if (!/^\d+\.\d{1,2}$/.test(value)) value = value.replace(/\./g, "");
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.round(parsed * 100) : NaN;
}

// Spreadsheet and ERP exports: a comma means Brazilian format; a single dot is
// the decimal separator whatever the number of places ("2.3456" is 2,3456).
export function parseImportDecimal(raw) {
  let value = String(raw ?? "").trim().replace(/R\$|\s/g, "");
  if (value.includes(",")) value = value.replace(/\./g, "").replace(",", ".");
  else if ((value.match(/\./g) || []).length > 1) value = value.replace(/\./g, "");
  return /^\d+(\.\d+)?$/.test(value) ? Number(value) : NaN;
}

export function scaledInteger(value, factor) {
  return Number.isFinite(value) ? Math.round(Number((value * factor).toFixed(4))) : NaN;
}

const NUMERIC_FIELDS = ["money", "milli", "percent"];

function clearedValue(field) {
  if (NUMERIC_FIELDS.includes(field.type) || field.type === "number") return field.default ?? null;
  return field.type === "source" || field.type === "select" ? null : "";
}

// With `editing`, an emptied optional field is sent explicitly (""/null/default):
// dropping the key would make it impossible to clear a value through PATCH.
// A `sendIfChanged` field is left out of an edit while it still equals the stored
// value: the server revalidates whatever it receives, legacy values included.
export function formPayload(form, fields, { editing = false, record = null } = {}) {
  const payload = {};
  for (const field of fields) {
    if (field.generated) continue;
    const input = form.elements.namedItem(field.key);
    if (!input) continue;
    const raw = field.type === "checkbox" ? "" : String(input.value ?? "").trim();
    if (editing && record && field.sendIfChanged && raw === String(record[field.key] ?? "").trim()) continue;
    const numeric = NUMERIC_FIELDS.includes(field.type);
    let value;
    if (field.type === "checkbox") value = input.checked;
    else if (numeric && raw === "") value = "";
    else if (field.type === "money") value = parseMoneyInput(raw);
    else if (field.type === "milli") value = Math.round(Number(raw.replace(",", ".")) * 1000);
    else if (field.type === "percent") value = Math.round(Number(raw.replace(",", ".")) * 100);
    else if (field.type === "json") {
      try { value = JSON.parse(raw || "{}"); }
      catch { throw new Error(field.label + ": JSON inválido."); }
    } else value = raw;
    if (value === "" || value === null) {
      if (!field.required) {
        if (editing) payload[field.key] = clearedValue(field);
        continue;
      }
      if (numeric) throw new Error(field.label + ": informe um número válido.");
    }
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

export function listPath(endpoint, params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === "" || value === null || value === undefined || value === false) continue;
    query.set(key, value === true ? "1" : String(value));
  }
  const text = query.toString();
  return text ? endpoint + (endpoint.includes("?") ? "&" : "?") + text : endpoint;
}

export function sortByLabel(rows, labelOf = row => row.name) {
  return [...rows].sort((a, b) => String(labelOf(a) ?? "").localeCompare(String(labelOf(b) ?? ""), "pt-BR", { sensitivity: "base" }));
}

export function taxIdValue(raw) {
  return String(raw ?? "").toUpperCase().replace(/[.\-/\s]/g, "");
}

export function validCpf(value) {
  const digits = String(value ?? "");
  if (!/^\d{11}$/.test(digits) || /^(\d)\1+$/.test(digits)) return false;
  const check = length => {
    let sum = 0;
    for (let index = 0; index < length; index++) sum += Number(digits[index]) * (length + 1 - index);
    return (sum * 10) % 11 % 10;
  };
  return check(9) === Number(digits[9]) && check(10) === Number(digits[10]);
}

// Also accepts the alphanumeric CNPJ (12 letters/digits + 2 check digits):
// each character is worth its ASCII code minus 48, as defined by the Receita Federal.
export function validCnpj(value) {
  const code = String(value ?? "").toUpperCase();
  if (!/^[0-9A-Z]{12}\d{2}$/.test(code) || /^(.)\1+$/.test(code)) return false;
  const check = length => {
    let sum = 0, weight = length - 7;
    for (let index = 0; index < length; index++) {
      sum += (code.charCodeAt(index) - 48) * weight;
      weight = weight === 2 ? 9 : weight - 1;
    }
    const rest = sum % 11;
    return rest < 2 ? 0 : 11 - rest;
  };
  return check(12) === Number(code[12]) && check(13) === Number(code[13]);
}

export function validTaxId(raw) {
  const value = taxIdValue(raw);
  return validCpf(value) || validCnpj(value);
}

export const conflictReasons = {
  rejected_event: "Evento recusado pela nuvem (em revisão)",
  discount_over_limit: "Desconto acima do limite atual",
  unit_changed: "Unidade do produto alterada",
  stock_control_changed: "Controle de estoque alterado",
  product_inactive: "Produto desativado",
  payment_method_inactive: "Forma de pagamento desativada",
  promotion_changed: "Promoção alterada ou encerrada",
  cash_expected_negative: "Caixa fechado com esperado negativo",
  cash_closed_twice: "Caixa fechado duas vezes (dois computadores)",
  void_already_reversed: "Cancelamento no caixa de venda já revertida no painel",
  void_after_return: "Cancelamento no caixa após devolução",
  fiscal_cancel_required: "Cancelar a NFC-e da venda estornada no caixa",
  return_on_reversed_sale: "Devolução no caixa de venda já revertida no painel",
  customer_inactive: "Fiado recebido de cliente desativado",
  negative_stock: "Estoque negativo",
  return_exceeds_sold: "Devolução acima do vendido"
};

export function conflictReasonText(row) {
  const message = row?.detail?.message;
  return conflictReasons[row?.reason] || (typeof message === "string" && message.trim()) || row?.reason || "—";
}

const filled = value => typeof value === "string" && value.trim() ? value.trim() : "";

// Only stock conflicts carry the central balance; the other reasons list the
// product (and the quantity involved, when the server sent one).
export function conflictItemLines(row) {
  const items = Array.isArray(row?.detail?.items) ? row.detail.items : [];
  return items.filter(item => item && typeof item === "object").map(item => {
    const name = filled(item.product_name) || "Produto " + String(item.product_id ?? "").slice(0, 8);
    if (item.central_balance_milli !== undefined) return name + ": saldo central " + quantity(item.central_balance_milli, "");
    const amount = item.quantity_milli ?? item.delta_milli;
    return amount === undefined ? name : name + ": " + quantity(amount, "");
  });
}

function payloadText(payload) {
  if (payload === undefined || payload === null) return "";
  let value = payload;
  // A payload the database refused is kept as escaped JSON text.
  if (typeof value?.unserializable === "string" && Object.keys(value).length === 1) {
    try { value = JSON.parse(value.unserializable); } catch { return value.unserializable; }
  }
  try { return typeof value === "string" ? value : JSON.stringify(value, null, 2); }
  catch { return String(value); }
}

export function conflictView(row) {
  const detail = row?.detail && typeof row.detail === "object" ? row.detail : {};
  const lines = conflictItemLines(row);
  const balances = (Array.isArray(detail.items) ? detail.items : []).some(item => item?.central_balance_milli !== undefined);
  const rejected = row?.reason === "rejected_event";
  const error = rejected ? filled(detail.error) : "";
  const message = filled(detail.message);
  return {
    message, lines, error,
    type: rejected ? filled(detail.type) || filled(row?.event_type) : "",
    payload: rejected ? payloadText(detail.payload) : "",
    summary: balances ? lines.join(" · ") : error || message || lines.join(" · ") || "Ver detalhes"
  };
}

// Cashiers register customers but never set the fiado limit nor deactivate;
// finance edits only the limit (the API answers 403 to anything else).
export function customerFormFields(fields, role, editing = false) {
  if (role === "cashier") return fields.filter(field => !["credit_limit_cents", "active"].includes(field.key));
  if (role === "finance") return editing ? fields.filter(field => field.key === "credit_limit_cents") : [];
  return fields;
}

export function productSaveErrorText(error) {
  const message = error instanceof Error ? error.message : "";
  if (error?.status === 409 && /trocar de unidade/i.test(message)) {
    return "Este produto já tem vendas ou movimentações de estoque, então a unidade não pode ser trocada. "
      + "Cadastre um novo produto com a unidade desejada e desative este.";
  }
  if (error?.status === 409 && /custo calculado pelas entradas/i.test(message)) {
    return "Este produto já teve entrada de mercadoria: o custo médio é calculado pelas entradas e não pode ser editado aqui.";
  }
  return message || "Ocorreu uma falha inesperada.";
}

// The server version (auth/me) wins; the bundled constant covers older APIs.
export function displayVersion(session) {
  const value = session?.version;
  const text = typeof value === "string" ? value.trim().replace(/^v/i, "") : "";
  return text && text.length <= 40 ? text : APP_VERSION;
}

// "Trocar de computador" (Terminais) ------------------------------------------

function shortDay(value, year = false) {
  const parsed = new Date(value);
  if (!value || Number.isNaN(parsed.getTime())) return "";
  return new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit", ...(year ? { year: "numeric" } : {}) }).format(parsed);
}

// The replaced computer may still send its sales queue until this date.
export function sendOnlyActive(row, now = Date.now()) {
  const until = row?.send_only_until ? new Date(row.send_only_until).getTime() : NaN;
  return !row?.revoked && Number.isFinite(until) && until > now;
}

export function terminalStateText(row, now = Date.now()) {
  if (row?.revoked) return { badge: "Revogado", note: "" };
  if (!row?.paired) return { badge: "Aguardando", note: "" };
  return { badge: "Pareado", note: sendOnlyActive(row, now) ? "Antigo enviando até " + shortDay(row.send_only_until) : "" };
}

export function mirrorText(row) {
  return row?.mirror_enabled === true ? "Ligada" : row?.mirror_enabled === false ? "Desligada" : "—";
}

export function canTransferTerminal(row) {
  return Boolean(row && row.paired && !row.revoked);
}

// Caixas da loja (fase 3) --------------------------------------------------------
// "Caixa principal" is the shop's server computer (the terminal marked as hub);
// "caixas adicionais" are the other computers linked to it. Every helper accepts
// answers of an older cloud, which do not carry these fields.

export const MAIN_STATION_ID = "server";
export const hubConfirmText = "Os caixas adicionais ligados ao principal atual deixam de funcionar até serem ligados ao novo.";

function activeStations(row) {
  return (Array.isArray(row?.stations) ? row.stations : [])
    .filter(item => item && typeof item === "object" && !item.revoked && typeof item.id === "string");
}

export function stationCountText(count) {
  const total = Math.max(0, Math.trunc(Number(count) || 0));
  return "Principal + " + total + (total === 1 ? " adicional" : " adicionais");
}

// Column "Caixas" of Terminais: text and the names of the additional registers.
export function terminalStationsView(row) {
  if (!row || row.revoked || row.hub !== true) return { text: "—", names: [] };
  const names = activeStations(row).map(item => String(item.name || "").trim() || "Caixa sem nome");
  const used = Number.isSafeInteger(row.stations_used) ? row.stations_used : names.length;
  const limit = Number.isSafeInteger(row.max_stations) ? row.max_stations : null;
  return { text: stationCountText(used) + (limit === null ? "" : " (plano: até " + limit + ")"), names };
}

export function canSetHub(row) {
  return Boolean(row && row.paired && !row.revoked && row.hub === false);
}

export function stationLimitText(license) {
  const limit = license?.max_stations;
  return Number.isSafeInteger(limit) && limit >= 0 ? "Caixas adicionais incluídos por loja: " + limit : "";
}

// Column "Caixa" of Caixas and of a sale: the main register carries no name of its own.
export function stationLabel(row) {
  const name = typeof row?.station_name === "string" ? row.station_name.trim() : "";
  if (name) return name;
  const id = typeof row?.station_id === "string" ? row.station_id : "";
  return !id || id === MAIN_STATION_ID ? "Caixa principal" : "Caixa adicional";
}

// Choices of the "Caixa" filter: every register, the main one, then the additional
// ones the terminals reported and the ones seen in the rows already loaded.
// Each shop names its own registers ("Caixa 2", "Caixa 3"…): when the same name shows up
// for different registers, the terminal they belong to is added to tell them apart.
export function stationFilterOptions(terminals = [], rows = []) {
  const found = [], seen = new Set(["", MAIN_STATION_ID]);
  const add = (id, name, place) => {
    if (typeof id !== "string" || seen.has(id)) return;
    seen.add(id);
    found.push([id, String(name || "").trim() || "Caixa adicional", String(place || "").trim()]);
  };
  (Array.isArray(terminals) ? terminals : []).forEach(terminal =>
    activeStations(terminal).forEach(item => add(item.id, item.name, terminal?.name)));
  (Array.isArray(rows) ? rows : []).forEach(row => add(row?.station_id, row?.station_name, row?.terminal_name));
  const count = new Map();
  found.forEach(([, label]) => count.set(label, (count.get(label) || 0) + 1));
  return [["", "Todos os caixas"], [MAIN_STATION_ID, "Caixa principal"],
    ...found.map(([id, label, place]) => [id, count.get(label) > 1 && place ? label + " · " + place : label])];
}

export const transferConfirmText = "Use quando o computador do caixa quebrou ou foi trocado. As vendas dos últimos 90 dias e o fiado em aberto voltam no computador novo. Se o antigo ainda liga, feche o caixa nele e espere a fila zerar antes.";

// Plain-language warnings shown with the recovery code.
export function transferWarnings(result) {
  // The deadline is the cloud's (license offline of the plan plus one day, never under 7): no number is guessed here.
  const warnings = [], pending = Number(result?.pending_events) || 0, days = Number(result?.send_only_days) || 0;
  const window = days > 0 ? "ele ainda consegue enviar por " + days + " dias depois da troca" : "ele ainda consegue enviar por um tempo depois da troca";
  if (result?.open_cash_session) warnings.push("O caixa está aberto no computador antigo. Se ele ainda liga, feche o caixa nele antes de continuar.");
  if (pending > 0) warnings.push((pending === 1 ? "Falta 1 registro" : "Faltam " + pending + " registros") +
    " para o computador antigo enviar. Deixe-o ligado e com internet: " + window + ". Se ele não liga mais, use a cópia de segurança no computador novo.");
  // The last heartbeat may say "fila 0" for a computer that kept selling offline afterwards.
  else if (days > 0) warnings.push("Depois da troca, o computador antigo ainda consegue enviar o que vendeu por " + days +
    " dias. Se ele ficou sem internet, ligue-o à rede dentro desse prazo.");
  return warnings;
}

export function transferSteps(result) {
  const days = Number(result?.history_days) || 90;
  return [
    "No computador novo, instale e abra o PDV2.",
    "Clique em \"Recuperar minha loja\" e informe a chave da licença e o código de recuperação.",
    "Espere a barra \"Trazendo seus dados\" terminar. As vendas dos últimos " + days + " dias e o fiado em aberto voltam sozinhos."
  ];
}

// Company key of the second-disk copy: shown once, to the owner only.
export function backupKeyView(status, role) {
  const id = String(status?.key_id || "").slice(0, 8);
  if (status?.revealed_at) return { canReveal: false, text: "Chave já exibida em " + shortDay(status.revealed_at, true) + (id ? " · identificação " + id + "…" : "") +
    " Se você não anotou, a recuperação da loja continua funcionando normalmente." };
  if (status?.can_reveal && role === "owner") return { canReveal: true, text: "Você não precisa desta chave para trocar de computador: o caixa novo recebe sozinho pela internet. Ela serve só para o suporte abrir a cópia do pendrive em um caso extremo, e só pode ser mostrada uma vez." };
  return { canReveal: false, text: "Só o proprietário da empresa pode ver a chave, uma única vez." + (id ? " Identificação " + id + "…" : "") };
}

// PDV2 Gestão (mobile app) access released in /platform. Same texts as the server and the apps.
export const appPermissionNames = { sales: "Vendas", cash: "Caixa", stock: "Estoque", credit: "Fiado", terminals: "Terminais e conflitos" };
export const appPermissionOrder = ["sales", "cash", "stock", "credit", "terminals"];

export function appPermissionText(permissions) {
  const list = Array.isArray(permissions) ? permissions : [];
  const known = appPermissionOrder.filter(key => list.includes(key));
  if (!known.length) return "—";
  if (known.length === appPermissionOrder.length) return "Todas";
  return known.map(key => appPermissionNames[key]).join(", ");
}

export function appAccessStatusText(status) {
  return { active: "Ativo", expired: "Expirado", revoked: "Revogado" }[status] || "—";
}

export function appAccessShopsText(row) {
  if (!row || row.scope === "platform" || row.all_shops) return "Todas";
  const names = (row.shops || []).map(shop => shop?.name).filter(Boolean);
  return names.length ? names.join(", ") : "Todas";
}

export function appAccessCompanyText(row) {
  if (!row) return "—";
  return row.scope === "platform" ? "Equipe CrownTech" : (row.tenant?.name || "—");
}

// ---------------------------------------------------------------- fiscal (docs/FISCAL.md)

// Product fiscal overrides typed in the form. Keys of `previous` that the form does not show are
// kept; an emptied field is dropped (the product then follows its tax profile again).
export function fiscalOverridesPayload(form, fields, previous = {}) {
  const shown = new Set(fields.map(field => field.key));
  const result = {};
  for (const [key, value] of Object.entries(previous || {})) if (!shown.has(key)) result[key] = value;
  for (const field of fields) {
    const input = form.elements.namedItem(field.key);
    if (!input) { if (previous && field.key in previous) result[field.key] = previous[field.key]; continue; }
    if (field.type === "checkbox") { result[field.key] = !!input.checked; continue; }
    const raw = String(input.value ?? "").trim();
    if (!raw) continue;
    if (field.integer) {
      const number = Number(raw);
      if (!Number.isInteger(number)) throw new Error(field.label + ": informe um número inteiro.");
      result[field.key] = number;
    } else result[field.key] = field.rate ? raw.replace(",", ".") : raw;
  }
  return result;
}

export function sameFiscalFields(left = {}, right = {}) {
  const canonical = value => JSON.stringify(Object.keys(value || {}).sort().map(key => [key, String(value[key])]));
  return canonical(left) === canonical(right);
}

// Badge of the product list: "Pronto para nota" or the first thing that is missing ("Falta NCM").
export function fiscalBadge(fiscal) {
  if (!fiscal) return { label: "Sem dados fiscais", tone: "neutral", detail: "" };
  const warnings = (fiscal.warnings || []).join(" · ");
  if (fiscal.ready) return { label: "Pronto para nota", tone: "", detail: warnings };
  const missing = fiscal.missing || [];
  const first = String(missing[0] || "Dados fiscais incompletos").split(":")[0];
  return { label: missing.length > 1 ? `${first} (+${missing.length - 1})` : first, tone: "warn",
           detail: missing.join(" · ") };
}

// "CSOSN 102 · CFOP 5102" for profile lists.
export function fiscalCodeSummary(fields = {}) {
  const parts = [];
  if (fields.icms_csosn) parts.push("CSOSN " + fields.icms_csosn);
  if (fields.icms_cst) parts.push("CST " + fields.icms_cst);
  if (fields.cfop) parts.push("CFOP " + fields.cfop);
  if (fields.icms_rate) parts.push("ICMS " + String(fields.icms_rate).replace(".", ",") + "%");
  return parts.join(" · ") || "—";
}

export function contributionSummary(fields = {}) {
  const part = (label, cst, rate) => cst ? `${label} ${cst}${rate ? " · " + String(rate).replace(".", ",") + "%" : ""}` : "";
  return [part("PIS", fields.pis_cst, fields.pis_rate), part("COFINS", fields.cofins_cst, fields.cofins_rate)].filter(Boolean).join(" / ") || "—";
}

export const CRT_OPTIONS = [
  [1, "1 · Simples Nacional"],
  [2, "2 · Simples Nacional — excesso de sublimite"],
  [3, "3 · Regime Normal (Lucro Presumido ou Real)"],
  [4, "4 · MEI"]
];
export const isSimplesCrt = crt => Number(crt) === 1 || Number(crt) === 4;
