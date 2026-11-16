const API_ROOT = "/api/v1";
export const APP_VERSION = "2.0.0";
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
export function formPayload(form, fields, { editing = false } = {}) {
  const payload = {};
  for (const field of fields) {
    if (field.generated) continue;
    const input = form.elements.namedItem(field.key);
    if (!input) continue;
    const raw = field.type === "checkbox" ? "" : String(input.value ?? "").trim();
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
  negative_stock: "Estoque negativo",
  return_exceeds_sold: "Devolução no caixa acima do disponível",
  rejected_event: "Evento recusado (em revisão)",
  discount_over_limit: "Desconto acima do limite atual",
  unit_changed: "Unidade do produto mudou",
  fiscal_cancel_required: "Cancelar a NFC-e na SEFAZ",
  sale_already_reversed: "Venda já cancelada no painel",
  return_on_reversed_sale: "Devolução de venda cancelada"
};

export function conflictReasonText(row) {
  const message = row?.detail?.message;
  return conflictReasons[row?.reason] || (typeof message === "string" && message.trim()) || row?.reason || "—";
}
