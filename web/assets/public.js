// Account deletion form of /excluir-conta/ (the CSP forbids inline scripts).
const form = document.getElementById("deletion-form");
const message = document.getElementById("deletion-message");

function csrfToken() {
  const item = document.cookie.split(";").map(part => part.trim()).find(part => part.startsWith("csrftoken="));
  return item ? decodeURIComponent(item.slice("csrftoken=".length)) : "";
}

function show(text, tone) {
  message.textContent = text;
  message.className = "public-message " + tone;
}

form?.addEventListener("submit", async event => {
  event.preventDefault();
  const email = form.elements.namedItem("email").value.trim();
  const note = form.elements.namedItem("note").value.trim();
  if (!email || !form.elements.namedItem("email").checkValidity()) {
    show("Informe o e-mail da sua conta.", "error");
    return;
  }
  if (!window.confirm("Pedir a exclusão da conta " + email + "? O login será desativado.")) return;
  const submit = form.querySelector("button[type=submit]");
  submit.disabled = true;
  try {
    const response = await fetch("/api/v1/public/account-deletion", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      body: JSON.stringify(note ? { email, note } : { email })
    });
    const data = await response.json().catch(() => ({}));
    if (response.status === 202) {
      show(data.message || "Recebemos seu pedido.", "ok");
      form.reset();
    } else if (response.status === 429) {
      show("Muitos pedidos deste endereço. Tente de novo mais tarde.", "error");
    } else {
      show(data.error || "Não foi possível enviar. Tente de novo.", "error");
    }
  } catch {
    show("Sem internet ou servidor fora do ar. Confira a conexão e tente de novo.", "error");
  } finally {
    submit.disabled = false;
  }
});
