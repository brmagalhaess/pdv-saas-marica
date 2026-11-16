// Run with: node --test web/tests/painel-core.test.mjs
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const read = path => readFile(new URL(path, import.meta.url), "utf8");
// Loaded through a data: URL so the browser module runs as ESM on any Node version.
const core = await import("data:text/javascript;base64," + Buffer.from(await read("../assets/core.js")).toString("base64"));
const { formPayload, quantity, validCpf, validCnpj, validTaxId, taxIdValue, conflictReasonText,
  listPath, sortByLabel, parseImportDecimal, scaledInteger, parseMoneyInput } = core;

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
    rejected_event: "Evento recusado (em revisão)",
    discount_over_limit: "Desconto acima do limite atual",
    unit_changed: "Unidade do produto mudou",
    fiscal_cancel_required: "Cancelar a NFC-e na SEFAZ",
    sale_already_reversed: "Venda já cancelada no painel",
    return_on_reversed_sale: "Devolução de venda cancelada",
    negative_stock: "Estoque negativo",
    return_exceeds_sold: "Devolução no caixa acima do disponível"
  };
  for (const [reason, label] of Object.entries(labels)) assert.equal(conflictReasonText({ reason, detail: { message: "x" } }), label);
  assert.equal(conflictReasonText({ reason: "motivo_novo", detail: { message: "Texto vindo do servidor" } }), "Texto vindo do servidor");
  assert.equal(conflictReasonText({ reason: "motivo_novo", detail: {} }), "motivo_novo");
  assert.equal(conflictReasonText({}), "—");
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
