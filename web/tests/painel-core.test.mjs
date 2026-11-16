// Run with: node --test web/tests/painel-core.test.mjs
import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import { test } from "node:test";

const read = path => readFile(new URL(path, import.meta.url), "utf8");
// Loaded through a data: URL so the browser module runs as ESM on any Node version.
const core = await import("data:text/javascript;base64," + Buffer.from(await read("../assets/core.js")).toString("base64"));
const { formPayload, quantity, validCpf, validCnpj, validTaxId, taxIdValue, conflictReasonText,
  listPath, sortByLabel, parseImportDecimal, scaledInteger, parseMoneyInput, conflictView, customerFormFields,
  productSaveErrorText, displayVersion, sendOnlyActive, terminalStateText, mirrorText, canTransferTerminal,
  transferConfirmText, transferWarnings, transferSteps, backupKeyView } = core;

function fakeForm(values) {
  const inputs = Object.fromEntries(Object.entries(values).map(([key, value]) =>
    [key, typeof value === "boolean" ? { checked: value } : { value }]));
  return { elements: { namedItem: key => inputs[key] || null } };
}

const promotionFields = [
  { key: "name", label: "Nome da promoção", required: true },
  { key: "kind", label: "Tipo", type: "select", required: true },
  { key: "product_id", label: "Produto", type: "source" },
  { key: "price_cents", label: "Preço promocional por unidade (R$)", type: "money", min: 0 },
  { key: "min_quantity_milli", label: "Quantidade mínima (atacado)", type: "milli", min: 0 },
  { key: "buy_quantity", label: "Leve (X)", type: "number" },
  { key: "pay_quantity", label: "Pague (Y)", type: "number" },
  { key: "active", label: "Promoção ativa", type: "checkbox", default: true }
];

test("promoção Leve X pague Y: preço oculto e vazio não derruba o envio", () => {
  const form = fakeForm({ name: "Leve 3 pague 2", kind: "buy_x_pay_y", product_id: "p1", price_cents: "",
    min_quantity_milli: "", buy_quantity: "3", pay_quantity: "2", active: true });
  const data = formPayload(form, promotionFields);
  assert.equal(data.kind, "buy_x_pay_y");
  assert.equal(data.buy_quantity, "3");
  assert.equal(data.pay_quantity, "2");
  assert.ok(!("price_cents" in data));
  assert.ok(!("min_quantity_milli" in data));
});

test("campo monetário obrigatório vazio continua sendo recusado", () => {
  const fields = [{ key: "price_cents", label: "Preço de venda (R$)", type: "money", required: true }];
  assert.throws(() => formPayload(fakeForm({ price_cents: "" }), fields), /Preço de venda \(R\$\): informe um número válido/);
  assert.throws(() => formPayload(fakeForm({ price_cents: "abc" }), fields), /informe um número válido/);
  assert.deepEqual(formPayload(fakeForm({ price_cents: "12,50" }), fields), { price_cents: 1250 });
});

test("grade de variações: preço opcional vazio na edição não vira NaN", () => {
  const fields = [{ key: "name", label: "Nome", required: true },
    { key: "price_cents", label: "Preço (novas variações)", type: "money", required: false, min: 0 }];
  assert.deepEqual(formPayload(fakeForm({ name: "Camiseta", price_cents: "" }), fields), { name: "Camiseta" });
  assert.deepEqual(formPayload(fakeForm({ name: "Camiseta", price_cents: "" }), fields, { editing: true }),
    { name: "Camiseta", price_cents: null });
});

test("edição envia campos opcionais esvaziados para o servidor poder limpá-los", () => {
  const fields = [
    { key: "name", label: "Nome", required: true },
    { key: "sku", label: "SKU", generated: true },
    { key: "ncm", label: "NCM" },
    { key: "category_id", label: "Categoria", type: "source" },
    { key: "commission_bps", label: "Comissão", type: "percent" },
    { key: "credit_limit_cents", label: "Limite de fiado", type: "money", default: 0 },
    { key: "stock_minimum_milli", label: "Estoque mínimo", type: "milli", default: 0 },
    { key: "settlement_days", label: "Prazo", type: "number", default: 0 },
    { key: "address", label: "Endereço", type: "textarea" },
    { key: "active", label: "Ativo", type: "checkbox" }
  ];
  const form = fakeForm({ name: "Arroz", sku: "P1", ncm: "  ", category_id: "", commission_bps: "", credit_limit_cents: "",
    stock_minimum_milli: "", settlement_days: "", address: "", active: true });
  assert.deepEqual(formPayload(form, fields, { editing: true }), {
    name: "Arroz", ncm: "", category_id: null, commission_bps: null, credit_limit_cents: 0,
    stock_minimum_milli: 0, settlement_days: 0, address: "", active: true
  });
  // On create the empty optional fields stay out, so the server applies its defaults.
  assert.deepEqual(formPayload(form, fields), { name: "Arroz", active: true });
});

test("valores preenchidos são convertidos como antes", () => {
  const fields = [{ key: "commission_bps", label: "Comissão", type: "percent" },
    { key: "stock_minimum_milli", label: "Mínimo", type: "milli" }, { key: "price_cents", label: "Preço", type: "money" }];
  assert.deepEqual(formPayload(fakeForm({ commission_bps: "2,5", stock_minimum_milli: "1.5", price_cents: "1.234,56" }), fields),
    { commission_bps: 250, stock_minimum_milli: 1500, price_cents: 123456 });
  assert.throws(() => formPayload(fakeForm({ commission_bps: "120", stock_minimum_milli: "1", price_cents: "1" }), fields),
    /percentual entre 0 e 100/);
  assert.equal(parseMoneyInput("R$ 10"), 1000);
});

test("quantidades em kg e litro não são arredondadas", () => {
  assert.equal(quantity(-350, "KG"), "-0,35 KG");
  assert.equal(quantity(1350, "L"), "1,35 L");
  // Unit unknown (API did not send it): still the real figure, never "0 UN".
  assert.equal(quantity(-350), "-0,35 UN");
  assert.equal(quantity(-350, ""), "-0,35");
  assert.equal(quantity(5000, "UN"), "5 UN");
  assert.equal(quantity("x", "KG"), "—");
});

test("CPF e CNPJ são validados pelos dígitos verificadores", () => {
  assert.ok(validCpf("52998224725"));
  assert.ok(!validCpf("52998224724"));
  assert.ok(!validCpf("11111111111"));
  assert.ok(!validCpf("5299822472"));
  assert.ok(validCnpj("11222333000181"));
  assert.ok(!validCnpj("11222333000180"));
  assert.ok(!validCnpj("00000000000000"));
  assert.ok(!validCnpj("1122233300018"));
  assert.ok(validCnpj("12ABC34501DE35"));
  assert.ok(validTaxId("529.982.247-25"));
  assert.ok(validTaxId("11.222.333/0001-81"));
  assert.ok(validTaxId("12.abc.345/01de-35"));
  assert.ok(!validTaxId("11.222.333/0001-8"));
  assert.ok(!validTaxId(""));
  assert.equal(taxIdValue(" 11.222.333/0001-81 "), "11222333000181");
});

test("conflitos offline têm rótulo em português e caem no texto do detalhe quando desconhecidos", () => {
  const labels = {
    rejected_event: "Evento recusado pela nuvem (em revisão)",
    discount_over_limit: "Desconto acima do limite atual",
    unit_changed: "Unidade do produto alterada",
    stock_control_changed: "Controle de estoque alterado",
    product_inactive: "Produto desativado",
    payment_method_inactive: "Forma de pagamento desativada",
    promotion_changed: "Promoção alterada ou encerrada",
    cash_expected_negative: "Caixa fechado com esperado negativo",
    void_already_reversed: "Cancelamento no caixa de venda já revertida no painel",
    void_after_return: "Cancelamento no caixa após devolução",
    fiscal_cancel_required: "Cancelar a NFC-e da venda estornada no caixa",
    return_on_reversed_sale: "Devolução no caixa de venda já revertida no painel",
    customer_inactive: "Fiado recebido de cliente desativado",
    negative_stock: "Estoque negativo",
    return_exceeds_sold: "Devolução acima do vendido"
  };
  assert.deepEqual(Object.keys(core.conflictReasons).sort(), Object.keys(labels).sort());
  for (const [reason, label] of Object.entries(labels)) assert.equal(conflictReasonText({ reason, detail: { message: "x" } }), label);
  assert.equal(conflictReasonText({ reason: "motivo_novo", detail: { message: "Texto vindo do servidor" } }), "Texto vindo do servidor");
  assert.equal(conflictReasonText({ reason: "motivo_novo", detail: {} }), "motivo_novo");
  assert.equal(conflictReasonText({}), "—");
});

test("conflitos offline: saldo central só no estoque; demais motivos mostram a mensagem", () => {
  const stock = conflictView({ reason: "negative_stock", detail: { message: "Venda deixou o saldo negativo.",
    items: [{ product_id: "p1", product_name: "Queijo", requested_milli: 500, central_balance_milli: -350 },
      { product_id: "p2", product_name: "Arroz", central_balance_milli: -2000 }] } });
  assert.equal(stock.summary, "Queijo: saldo central -0,35 · Arroz: saldo central -2");
  assert.deepEqual(stock.lines, ["Queijo: saldo central -0,35", "Arroz: saldo central -2"]);
  assert.equal(stock.payload, "");

  const discount = conflictView({ reason: "discount_over_limit", detail: { message: "Desconto de 15% acima do limite de 10%.", sale_id: "s1" } });
  assert.equal(discount.summary, "Desconto de 15% acima do limite de 10%.");
  assert.deepEqual(discount.lines, []);

  // Items without a central balance never render as "(0)".
  const inactive = conflictView({ reason: "product_inactive", detail: { message: "Ajuste para produto desativado.",
    items: [{ product_id: "p1", product_name: "Pão", delta_milli: -1500 }] } });
  assert.equal(inactive.summary, "Ajuste para produto desativado.");
  assert.deepEqual(inactive.lines, ["Pão: -1,5"]);
  const reversed = conflictView({ reason: "return_on_reversed_sale", detail: { message: "Devolução de venda já revertida.",
    items: [{ product_id: "abcdef123456", quantity_milli: 1000, amount_cents: 500 }] } });
  assert.deepEqual(reversed.lines, ["Produto abcdef12: 1"]);

  assert.equal(conflictView({ reason: "cash_expected_negative", detail: {} }).summary, "Ver detalhes");
  assert.equal(conflictView({}).summary, "Ver detalhes");
  assert.equal(conflictView({ reason: "x", detail: { items: "quebrado", message: 7 } }).summary, "Ver detalhes");
});

test("conflitos offline: evento recusado mostra erro, tipo e payload formatado", () => {
  const payload = { event_id: "e1", payload: { total_cents: 1250 } };
  const view = conflictView({ reason: "rejected_event", event_type: "sale.completed", detail: {
    message: "Evento do caixa recusado pela nuvem: Produto não encontrado", type: "sale.completed",
    error: "Produto não encontrado", status: 404, payload } });
  assert.equal(view.summary, "Produto não encontrado");
  assert.equal(view.error, "Produto não encontrado");
  assert.equal(view.type, "sale.completed");
  assert.equal(view.payload, JSON.stringify(payload, null, 2));
  assert.ok(view.payload.split("\n").length > 3);
  // Payload the database refused is stored as escaped JSON text.
  const escaped = conflictView({ reason: "rejected_event", event_type: "cash.closed",
    detail: { error: "Texto inválido", payload: { unserializable: JSON.stringify({ a: 1 }) } } });
  assert.equal(escaped.payload, JSON.stringify({ a: 1 }, null, 2));
  assert.equal(escaped.type, "cash.closed");
  assert.equal(conflictView({ reason: "rejected_event", detail: { payload: { unserializable: "não é json" } } }).payload, "não é json");
  // Error, type and payload belong to rejected events only.
  const other = conflictView({ reason: "unit_changed", detail: { message: "m", error: "e", type: "t", payload: { a: 1 } } });
  assert.deepEqual([other.error, other.type, other.payload], ["", "", ""]);
});

test("clientes: caixa não vê limite nem desativação; financeiro edita só o limite", () => {
  const fields = [{ key: "name" }, { key: "document" }, { key: "email" }, { key: "phone" },
    { key: "credit_limit_cents", type: "money", default: 0 }, { key: "active", type: "checkbox" }];
  const keys = (role, editing) => customerFormFields(fields, role, editing).map(field => field.key);
  assert.deepEqual(keys("cashier", false), ["name", "document", "email", "phone"]);
  assert.deepEqual(keys("cashier", true), ["name", "document", "email", "phone"]);
  assert.deepEqual(keys("finance", true), ["credit_limit_cents"]);
  assert.deepEqual(keys("finance", false), []);
  for (const role of ["owner", "admin", "manager"]) assert.equal(keys(role, true).length, fields.length);
  // What the form posts for each role stays inside what the API accepts.
  const form = fakeForm({ name: "Ana", document: "", email: "", phone: "", credit_limit_cents: "150,00", active: true });
  assert.deepEqual(formPayload(form, customerFormFields(fields, "finance", true), { editing: true }), { credit_limit_cents: 15000 });
  const cashier = formPayload(form, customerFormFields(fields, "cashier", true), { editing: true });
  assert.ok(!("credit_limit_cents" in cashier) && !("active" in cashier));
});

test("produto: código de barras e custo editáveis; 409 de unidade tem mensagem clara", async () => {
  const app = await read("../assets/app.js");
  const barcode = app.split("\n").find(line => line.includes('{ key: "barcode",'));
  assert.ok(!barcode.includes("generated"));
  assert.ok(!/bloquead/.test(barcode));
  assert.match(barcode, /Deixe vazio para o sistema gerar/);
  const pattern = new RegExp("^(?:" + barcode.match(/pattern: "([^"]+)"/)[1] + ")$");
  for (const code of ["78912345", "789123456789", "7891234567895", "17891234567892"]) assert.ok(pattern.test(code), code);
  for (const code of ["1234567", "123456789", "7891234567895A", "178912345678921"]) assert.ok(!pattern.test(code), code);
  assert.ok(app.includes('{ key: "cost_cents", label: "Custo (R$)", type: "money"'));
  // Empty barcode is sent on edit so the server can generate the internal code; empty cost falls back to zero.
  const fields = [{ key: "name", required: true }, { key: "barcode", max: 14 }, { key: "cost_cents", type: "money", default: 0 }];
  assert.deepEqual(formPayload(fakeForm({ name: "Arroz", barcode: "", cost_cents: "" }), fields), { name: "Arroz" });
  assert.deepEqual(formPayload(fakeForm({ name: "Arroz", barcode: "", cost_cents: "" }), fields, { editing: true }),
    { name: "Arroz", barcode: "", cost_cents: 0 });
  assert.deepEqual(formPayload(fakeForm({ name: "Arroz", barcode: " 7891234567895 ", cost_cents: "3,20" }), fields),
    { name: "Arroz", barcode: "7891234567895", cost_cents: 320 });

  const unit = new core.ApiError("Produto com vendas ou movimentações não pode trocar de unidade; cadastre um novo produto", 409);
  assert.match(productSaveErrorText(unit), /unidade não pode ser trocada\. Cadastre um novo produto/);
  const cost = new core.ApiError("Produto com entrada de mercadoria tem o custo calculado pelas entradas", 409);
  assert.match(productSaveErrorText(cost), /custo médio é calculado pelas entradas/);
  const clash = new core.ApiError("Código de barras já cadastrado em outro produto", 409);
  assert.equal(productSaveErrorText(clash), "Código de barras já cadastrado em outro produto");
  assert.ok(app.includes("saveErrorText: productSaveErrorText"));
});

test("versão exibida vem do servidor quando /auth/me a envia", () => {
  assert.equal(displayVersion({ version: "2.1.0" }), "2.1.0");
  assert.equal(displayVersion({ version: " v2.1.0+abc123 " }), "2.1.0+abc123");
  for (const session of [{}, null, { version: "" }, { version: "   " }, { version: 3 }, { version: "x".repeat(41) }]) {
    assert.equal(displayVersion(session), core.APP_VERSION);
  }
});

test("estoque: nomes vêm da API e a busca extra só roda para linhas sem eles", async () => {
  const app = await read("../assets/app.js");
  assert.ok(app.includes('movements.filter(row => !row.product_name).map(row => stockName("/products", row.product_id))'));
  assert.ok(app.includes('filter(row => !row.supplier_name).map(row => stockName("/suppliers", row.supplier_id))'));
  assert.ok(app.includes("row.product_name || productOf(row).name"));
  assert.ok(app.includes("quantity(row.delta_milli, row.unit || productOf(row).unit"));
});

test("service worker guarda os ativos de marca e o painel não é indexado", async () => {
  const [html, sw, manifest] = await Promise.all([read("../app.html"), read("../sw.js"), read("../manifest.webmanifest")]);
  const shell = sw.slice(sw.indexOf("const SHELL"), sw.indexOf("];", sw.indexOf("const SHELL")));
  const assets = [...shell.matchAll(/"(\/assets\/[^"]+)"/g)].map(match => match[1]);
  for (const name of ["pdv2-simbolo.svg", "pdv2-por-crowntech.svg", "pdv2-por-crowntech-negativo.svg",
    "pdv2-por-crowntech-empilhado.svg", "icon-maskable-192.png", "icon-maskable-512.png"]) {
    assert.ok(assets.includes("/assets/" + name), name);
  }
  // cache.addAll rejects the whole install if a single file is missing.
  for (const asset of assets) await assert.doesNotReject(access(new URL("../" + asset.slice(1), import.meta.url)), asset);
  for (const icon of JSON.parse(manifest).icons) assert.ok(assets.includes(icon.src), icon.src);
  assert.equal(new Set(assets).size, assets.length);
  assert.ok(!sw.includes('"2.0.0-painel.1"'));
  assert.ok(html.includes('<link rel="icon" type="image/svg+xml" href="/assets/pdv2-simbolo.svg">'));
  assert.ok(html.includes('<meta name="robots" content="noindex">'));
  assert.ok(html.includes("<title>Painel · PDV2 por CrownTech</title>"));
  assert.ok(html.includes("<small>" + core.VENDOR.signature + "</small>"));
});

test("listas pedem inativos e busca no servidor; seletores ordenam por nome", () => {
  assert.equal(listPath("/customers", { q: "", include_inactive: false }), "/customers");
  assert.equal(listPath("/customers", { q: "joão silva", include_inactive: true }), "/customers?q=jo%C3%A3o+silva&include_inactive=1");
  assert.equal(listPath("/products", { q: "arroz", limit: 100 }), "/products?q=arroz&limit=100");
  assert.equal(listPath("/transfers?limit=50", { cursor: "50" }), "/transfers?limit=50&cursor=50");
  const rows = [{ name: "banana" }, { name: "Água" }, { name: "abacaxi" }, { name: "Arroz" }];
  assert.deepEqual(sortByLabel(rows).map(row => row.name), ["abacaxi", "Água", "Arroz", "banana"]);
  assert.equal(rows[0].name, "banana");
});

test("importação CSV: ponto sozinho é separador decimal, com qualquer número de casas", () => {
  assert.equal(scaledInteger(parseImportDecimal("2.3456"), 100), 235);
  assert.equal(scaledInteger(parseImportDecimal("12.345"), 100), 1235);
  assert.equal(scaledInteger(parseImportDecimal("12,50"), 100), 1250);
  assert.equal(scaledInteger(parseImportDecimal("1.234,56"), 100), 123456);
  assert.equal(scaledInteger(parseImportDecimal("R$ 7"), 100), 700);
  assert.equal(scaledInteger(parseImportDecimal("1.500"), 1000), 1500);
  assert.equal(scaledInteger(parseImportDecimal("1.234.567"), 1), 1234567);
  assert.ok(Number.isNaN(scaledInteger(parseImportDecimal("-3"), 100)));
  assert.ok(Number.isNaN(scaledInteger(parseImportDecimal(""), 100)));
  assert.ok(Number.isNaN(scaledInteger(parseImportDecimal("abc"), 100)));
});

test("marca e versão: rodapé da barra lateral, Sobre e cache do service worker versionado", async () => {
  const [html, sw, version] = await Promise.all([read("../app.html"), read("../sw.js"), read("../../VERSION")]);
  assert.equal(core.APP_VERSION, version.trim());
  assert.equal(core.VENDOR.signature, "PDV2 · um produto CrownTech");
  assert.equal(core.VENDOR.footer, "PDV2 · um produto CrownTech · crowntech.com.br");
  assert.match(html, /PDV2 · um produto <b>CrownTech<\/b>/);
  assert.match(html, /por CrownTech/);
  assert.match(html, /id="app-version"/);
  assert.match(html, /id="about-button"/);
  assert.ok(sw.includes(`const VERSION = "${core.APP_VERSION}`));
  assert.match(sw, /const CACHE = "pdv2-shell-" \+ VERSION/);
  assert.match(sw, /cache: "no-cache"/);
});

test("edição de produto não reenvia o código de barras inalterado (legado não-GTIN)", () => {
  const fields = [
    { key: "name", label: "Nome", required: true },
    { key: "barcode", label: "Código de barras (EAN)", sendIfChanged: true },
    { key: "price_cents", label: "Preço de venda (R$)", type: "money", required: true }
  ];
  const record = { id: "p1", name: "Arroz branco 1 kg", barcode: "7891000000001", price_cents: 899 };
  const unchanged = formPayload(fakeForm({ name: "Arroz branco 1 kg", barcode: "7891000000001", price_cents: "9,49" }), fields, { editing: true, record });
  assert.deepEqual(unchanged, { name: "Arroz branco 1 kg", price_cents: 949 });
  assert.ok(!("barcode" in unchanged));
  // A new value is sent, and clearing it still asks the server for an internal code.
  assert.equal(formPayload(fakeForm({ name: "Arroz", barcode: "7894900011517", price_cents: "9,49" }), fields, { editing: true, record }).barcode, "7894900011517");
  assert.equal(formPayload(fakeForm({ name: "Arroz", barcode: "", price_cents: "9,49" }), fields, { editing: true, record }).barcode, "");
  // A product without a stored code keeps the empty field out of the PATCH.
  assert.ok(!("barcode" in formPayload(fakeForm({ name: "Arroz", barcode: "", price_cents: "9,49" }), fields, { editing: true, record: { ...record, barcode: null } })));
  // On create nothing changes: a typed code goes, an empty one stays out.
  assert.equal(formPayload(fakeForm({ name: "Arroz", barcode: "7894900011517", price_cents: "9,49" }), fields).barcode, "7894900011517");
  assert.ok(!("barcode" in formPayload(fakeForm({ name: "Arroz", barcode: "", price_cents: "9,49" }), fields)));
});

test("formulário de produto marca o código de barras como enviado só quando muda", async () => {
  const app = await readFile(new URL("../assets/app.js", import.meta.url), "utf8");
  assert.match(app, /\{ key: "barcode",[^}]*sendIfChanged: true/);
  assert.match(app, /formPayload\(form, fields, \{ editing, record \}\)/);
});

test("terminais: estado mostra o computador antigo enviando só dentro do prazo", () => {
  const now = Date.parse("2026-11-16T12:00:00Z");
  const row = { paired: true, revoked: false, send_only_until: "2026-11-23T12:00:00+00:00" };
  assert.equal(sendOnlyActive(row, now), true);
  assert.deepEqual(terminalStateText(row, now), { badge: "Pareado", note: "Antigo enviando até 23/11" });
  // Deadline passed, never transferred, or revoked: just the usual badge.
  assert.equal(sendOnlyActive(row, Date.parse("2026-11-23T12:00:01Z")), false);
  assert.deepEqual(terminalStateText({ paired: true, send_only_until: null }, now), { badge: "Pareado", note: "" });
  assert.deepEqual(terminalStateText({ ...row, revoked: true }, now), { badge: "Revogado", note: "" });
  assert.equal(sendOnlyActive({ ...row, revoked: true }, now), false);
  assert.deepEqual(terminalStateText({ paired: false }, now), { badge: "Aguardando", note: "" });
  assert.equal(sendOnlyActive({ send_only_until: "ontem" }, now), false);
  assert.equal(sendOnlyActive(null, now), false);
});

test("terminais: coluna da cópia em 2º disco e quem pode trocar de computador", () => {
  assert.equal(mirrorText({ mirror_enabled: true }), "Ligada");
  assert.equal(mirrorText({ mirror_enabled: false }), "Desligada");
  assert.equal(mirrorText({ mirror_enabled: null }), "—");
  assert.equal(mirrorText({}), "—");
  assert.equal(canTransferTerminal({ paired: true, revoked: false }), true);
  assert.equal(canTransferTerminal({ paired: false, revoked: false }), false);
  assert.equal(canTransferTerminal({ paired: true, revoked: true }), false);
  assert.equal(canTransferTerminal(null), false);
});

test("trocar de computador: avisos e passos em linguagem simples", () => {
  assert.match(transferConfirmText, /quebrou ou foi trocado/);
  assert.match(transferConfirmText, /últimos 90 dias e o fiado em aberto/);
  assert.deepEqual(transferWarnings({ pending_events: 0, open_cash_session: false }), []);
  const both = transferWarnings({ pending_events: 3, open_cash_session: true, send_only_days: 7 });
  assert.equal(both.length, 2);
  assert.match(both[0], /caixa está aberto no computador antigo/);
  assert.match(both[1], /^Faltam 3 registros .* por 7 dias/);
  assert.match(transferWarnings({ pending_events: 1 })[0], /^Falta 1 registro /);
  const steps = transferSteps({ history_days: 90 });
  assert.equal(steps.length, 3);
  assert.match(steps[1], /Recuperar minha loja/);
  assert.match(steps[2], /últimos 90 dias/);
  assert.match(transferSteps({})[2], /últimos 90 dias/);
});

test("chave da cópia de segurança: só o proprietário vê, uma única vez", () => {
  const fresh = { exists: true, key_id: "a1b2c3d4e5f60718", revealed_at: null, can_reveal: true };
  assert.equal(backupKeyView(fresh, "owner").canReveal, true);
  assert.equal(backupKeyView({ ...fresh, can_reveal: false }, "admin").canReveal, false);
  assert.equal(backupKeyView(fresh, "admin").canReveal, false);
  assert.match(backupKeyView(fresh, "admin").text, /Só o proprietário/);
  const shown = backupKeyView({ ...fresh, revealed_at: "2026-11-16T12:00:00+00:00", can_reveal: false }, "owner");
  assert.equal(shown.canReveal, false);
  assert.equal(shown.text, "Chave já exibida em 16/11/2026 · identificação a1b2c3d4…");
  assert.equal(backupKeyView({ exists: false, key_id: "", revealed_at: null, can_reveal: true }, "owner").canReveal, true);
});

test("painel de terminais usa a troca de computador e a chave da cópia", async () => {
  const app = await readFile(new URL("../assets/app.js", import.meta.url), "utf8");
  assert.match(app, /"\/terminals\/" \+ row\.id \+ "\/transfer"/);
  assert.match(app, /label:"Trocar de computador"/);
  assert.match(app, /end_send_only:true/);
  assert.match(app, /label:"Cópia em 2º disco"/);
  assert.match(app, /"\/backup-key\/reveal", \{ confirm: true \}/);
  assert.match(app, /Mostrar a chave \(uma única vez\)/);
});
