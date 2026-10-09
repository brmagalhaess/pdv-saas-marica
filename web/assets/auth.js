import { api, errorText, getCsrf } from "./core.js";

const $ = selector => document.querySelector(selector);
const message = $("#auth-message");
const params = new URLSearchParams(location.search);
let mode = params.get("mode") === "register" ? "register" : "login";
const safeNext = params.get("next");
let mfaMode = "";
let mfaCompletedResult = null;
let accountChoiceResult = null;

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
  $("#panel-mfa").hidden = true;
  $("#panel-account-choice").hidden = true;
  $(".auth-tabs").hidden = false;
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

async function showMfa(setupRequired) {
  mfaMode = setupRequired ? "setup" : "verify";
  $(".auth-tabs").hidden = true;
  $("#panel-login").hidden = true;
  $("#panel-register").hidden = true;
  $("#panel-mfa").hidden = false;
  $("#auth-title").textContent = setupRequired ? "Ative a MFA da administração" : "Confirme sua identidade";
  $("#auth-description").textContent = setupRequired
    ? "Use um aplicativo autenticador para proteger o acesso ao painel SaaS."
    : "Informe o código de seis dígitos do seu aplicativo autenticador.";
  $("#mfa-setup-copy").hidden = !setupRequired;
  $("#mfa-recovery-list").hidden = true;
  $("#mfa-continue").hidden = true;
  $("#mfa-form").hidden = false;
  $("#mfa-code").value = "";
  $("#mfa-recovery").value = "";
  if (setupRequired) {
    const setup = await api.get("/auth/mfa/setup", { tenant: false });
    $("#mfa-secret").textContent = setup.secret;
    $("#mfa-uri").textContent = setup.otpauth_uri;
  }
  $("#mfa-code").focus();
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

function hasMultipleAccountTypes(result) {
  const user = result?.user || {};
  const platformAccess = user.is_superuser || (user.platform_roles || []).length > 0;
  return !safeNext && platformAccess && (result?.memberships || []).length > 0;
}

function continueAfterAuth(result) {
  if (hasMultipleAccountTypes(result)) {
    showAccountChoice(result);
    return;
  }
  location.assign(destination(result));
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'\"]/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  }[character]));
}

function showAccountChoice(result) {
  accountChoiceResult = result;
  const user = result?.user || {};
  const memberships = result?.memberships || [];
  const list = $("#account-choice-list");
  list.replaceChildren();
  $(".auth-tabs").hidden = true;
  $("#panel-login").hidden = true;
  $("#panel-register").hidden = true;
  $("#panel-mfa").hidden = true;
  $("#panel-account-choice").hidden = false;
  $("#auth-title").textContent = "Escolha sua área de acesso";
  $("#auth-description").textContent = `${user.name || user.email || "Sua conta"} tem acesso à plataforma e a uma empresa.`;
  const platformButton = document.createElement("a");
  platformButton.className = "account-choice-card";
  platformButton.href = "/platform/";
  platformButton.innerHTML = '<span class="account-choice-icon" aria-hidden="true">⌘</span><span><strong>Administração SaaS</strong><small>Planos, licenças, usuários, versões e suporte da plataforma.</small></span><span class="account-choice-arrow" aria-hidden="true">→</span>';
  list.append(platformButton);
  memberships.forEach(membership => {
    const link = document.createElement("a");
    link.className = "account-choice-card";
    link.href = "/app/?tenant=" + encodeURIComponent(membership.tenant_id);
    link.innerHTML = `<span class="account-choice-icon" aria-hidden="true">⌂</span><span><strong>${escapeHtml(membership.tenant_name || "Painel da empresa")}</strong><small>Painel da empresa · ${escapeHtml(membership.role || "acesso")}</small></span><span class="account-choice-arrow" aria-hidden="true">→</span>`;
    list.append(link);
  });
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
    if (result?.mfa_setup_required || result?.mfa_required) {
      busy(form, false);
      await showMfa(Boolean(result.mfa_setup_required));
      return;
    }
    continueAfterAuth(result);
  } catch (error) {
    showMessage(errorText(error));
    busy(form, false);
  }
});

$("#mfa-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  showMessage("");
  busy(form, true);
  try {
    const data = Object.fromEntries(new FormData(form));
    if (mfaMode === "setup" && !data.code.trim()) {
      showMessage("Informe o código exibido no aplicativo autenticador.");
      busy(form, false);
      return;
    }
    if (mfaMode === "verify" && !data.code.trim() && !data.recovery_code.trim()) {
      showMessage("Informe o código do autenticador ou um código de recuperação.");
      busy(form, false);
      return;
    }
    const result = mfaMode === "setup"
      ? await api.post("/auth/mfa/setup/confirm", { code: data.code }, { tenant: false })
      : await api.post("/auth/mfa/verify", { code: data.code, recovery_code: data.recovery_code.trim() }, { tenant: false });
    if (mfaMode === "setup" && Array.isArray(result.recovery_codes)) {
      mfaCompletedResult = result;
      $("#mfa-recovery-codes").textContent = result.recovery_codes.join("\n");
      $("#mfa-recovery-list").hidden = false;
      $("#mfa-continue").hidden = false;
      $("#mfa-form").hidden = true;
      showMessage("MFA ativada. Guarde os códigos de recuperação antes de continuar.", "success");
      return;
    }
    continueAfterAuth(result);
  } catch (error) {
    showMessage(errorText(error));
    busy(form, false);
  }
});

$("#mfa-continue").addEventListener("click", () => {
  if (mfaCompletedResult) continueAfterAuth(mfaCompletedResult);
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
  if (result?.user) {
    if (hasMultipleAccountTypes(result)) showAccountChoice(result);
    else location.replace(destination(result));
  }
}).catch(error => {
  if (error?.status !== 401) showMessage(errorText(error));
});
