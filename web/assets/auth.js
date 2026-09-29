import { api, errorText, getCsrf } from "./core.js";

const $ = selector => document.querySelector(selector);
const message = $("#auth-message");
const params = new URLSearchParams(location.search);
let mode = params.get("mode") === "register" ? "register" : "login";
const safeNext = params.get("next");

function showMessage(text, type = "error") {
  message.textContent = text;
  message.className = "auth-message feedback " + type;
  message.hidden = !text;
  message.setAttribute("role", type === "error" ? "alert" : "status");
  message.setAttribute("aria-live", type === "error" ? "assertive" : "polite");
}

function setMode(next, focus = false) {
  mode = next;
  const login = next === "login";
  $("#tab-login").setAttribute("aria-selected", String(login));
  $("#tab-register").setAttribute("aria-selected", String(!login));
  $("#tab-login").tabIndex = login ? 0 : -1;
  $("#tab-register").tabIndex = login ? -1 : 0;
  $("#panel-login").hidden = !login;
  $("#panel-register").hidden = login;
  $("#auth-title").textContent = login ? "Boas-vindas ao PDV2" : "Seu espaço começa aqui";
  $("#auth-description").textContent = login
    ? "Acesse seu espaço de gestão ou crie uma conta para testar as funções disponíveis."
    : "Crie uma conta de teste para conhecer a gestão de lojas, produtos, estoque e equipe.";
  showMessage("");
  if (focus) (login ? $("#login-email") : $("#company")).focus();
  const url = new URL(location.href);
  if (login) url.searchParams.delete("mode");
  else url.searchParams.set("mode", "register");
  history.replaceState({}, "", url);
}

function passwordIsValid(value) {
  return value.length >= 8 && value.length <= 128 &&
    /[A-Z]/.test(value) && /[a-z]/.test(value) &&
    /\d/.test(value) && /[^A-Za-z0-9]/.test(value);
}

function busy(form, value) {
  form.setAttribute("aria-busy", String(value));
  const button = form.querySelector('button[type="submit"]');
  button.disabled = value;
}

function destination(result) {
  const user = result?.user || {};
  const memberships = result?.memberships || [];
  const platformAccess = user.is_superuser || (user.platform_roles || []).length > 0;
  if (safeNext === "/app/" && memberships.length) return safeNext;
  if (safeNext === "/platform/" && platformAccess) return safeNext;
  return platformAccess && !memberships.length ? "/platform/" : "/app/";
}

$("#tab-login").addEventListener("click", () => setMode("login", true));
$("#tab-register").addEventListener("click", () => setMode("register", true));
$(".auth-tabs").addEventListener("keydown", event => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const next = event.key === "Home" ? "login" : event.key === "End" ? "register"
    : mode === "login" ? "register" : "login";
  setMode(next);
  $("#tab-" + next).focus();
});

document.querySelectorAll("[data-toggle-password]").forEach(button => {
  button.addEventListener("click", () => {
    const input = document.getElementById(button.dataset.togglePassword);
    const visible = input.type === "password";
    input.type = visible ? "text" : "password";
    button.textContent = visible ? "Ocultar senha" : "Mostrar senha";
    button.setAttribute("aria-pressed", String(visible));
    input.focus();
  });
});

$("#login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  showMessage("");
  busy(form, true);
  try {
    const data = Object.fromEntries(new FormData(form));
    const result = await api.post("/auth/login", { email: data.email.trim(), password: data.password }, { tenant: false });
    location.assign(destination(result));
  } catch (error) {
    showMessage(errorText(error));
    busy(form, false);
  }
});

$("#register-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const data = Object.fromEntries(new FormData(form));
  if (!passwordIsValid(data.password)) {
    showMessage("A senha precisa ter de 8 a 128 caracteres, com maiúscula, minúscula, número e símbolo.");
    return;
  }
  showMessage("");
  busy(form, true);
  try {
    await api.post("/auth/register", {
      company: data.company.trim(),
      name: data.name.trim(),
      email: data.email.trim(),
      password: data.password
    }, { tenant: false });
    location.assign("/app/");
  } catch (error) {
    showMessage(errorText(error));
    busy(form, false);
  }
});

setMode(mode);
getCsrf();
api.get("/auth/me", { tenant: false }).then(result => {
  if (result?.user) location.replace(destination(result));
}).catch(error => {
  if (error?.status !== 401) showMessage(errorText(error));
});
