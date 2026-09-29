const API_ROOT = "/api/v1";
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
  return new Intl.NumberFormat("pt-BR", { maximumFractionDigits: unit === "UN" ? 0 : 3 }).format(n / 1000) + " " + unit;
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
