"""Caixa Rápido: rotas novas do terminal, página servida e regras puras da tela (via node)."""
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from shared.licensing import generate_keypair, payload_digest, sign_lease
from shared.security import hash_pin
from terminal.app import create_app

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "terminal" / "static"
TEMPLATE = ROOT / "terminal" / "templates" / "index.html"
BRAND_FOOTER = "PDV2 · um produto CrownTech · crowntech.com.br"


class CaixaRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name,
                               "DB_PATH": Path(self.temp.name) / "terminal.sqlite3", "START_WORKER": False})
        self.store = self.app.extensions["terminal_store"]
        self.client = self.app.test_client()
        tenant_id, shop_id, terminal_id = (str(uuid.uuid4()) for _ in range(3))
        self.cashier_id, self.manager_id = (str(uuid.uuid4()) for _ in range(2))
        private, public = generate_keypair()
        operators = [
            {"id": self.cashier_id, "name": "Ana", "role": "cashier", "pin_hash": hash_pin("123987")},
            {"id": self.manager_id, "name": "Bruno", "role": "manager", "pin_hash": hash_pin("654321")},
        ]
        # 200 products named so the weighed one sorts after the 150-row page of /api/products.
        self.products = [{"id": str(uuid.uuid4()), "sku": f"SKU{index:03d}", "barcode": f"789000000{index:04d}",
                          "name": f"Produto {index:03d}", "unit": "UN", "category": "Mercearia",
                          "price_cents": 100 + index, "quantity_milli": 10000, "active": True}
                         for index in range(200)]
        self.weighed = {"id": str(uuid.uuid4()), "sku": "TOMATE", "barcode": None, "name": "Tomate italiano",
                        "unit": "KG", "scale_code": "54321", "category": "Hortifruti", "price_cents": 899,
                        "quantity_milli": 20000, "active": True}
        self.unit_with_code = {"id": str(uuid.uuid4()), "sku": "UNCODE", "barcode": None, "name": "Zzz unidade",
                               "unit": "UN", "scale_code": "11111", "category": "Mercearia", "price_cents": 500,
                               "quantity_milli": 5000, "active": True}
        self.products += [self.weighed, self.unit_with_code]
        now = int(time.time())
        claims = {"schema": 1, "tenant_id": tenant_id, "shop_id": shop_id, "terminal_id": terminal_id,
                  "device_id": self.store.identity()["device_id"], "issued_at": now - 5, "expires_at": now + 3600,
                  "features": ["pdv"], "status": "active", "operators_hash": payload_digest(operators)}
        lease = sign_lease(claims, private)
        self.store.set_pairing({"token": "t" * 48, "tenant_id": tenant_id, "shop_id": shop_id,
                                "terminal_id": terminal_id, "lease": lease, "public_key": public},
                               "http://127.0.0.1:8000")
        self.store.apply_bootstrap({"products": self.products, "operators": operators,
                                    "lease": lease, "public_key": public})

    def csrf(self):
        return self.client.get("/api/session").json["csrf"]

    def login(self, operator_id=None, pin="123987"):
        response = self.client.post("/api/login", json={"operator_id": operator_id or self.cashier_id, "pin": pin},
                                    headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json["csrf"]

    def post(self, path, payload):
        return self.client.post(path, json=payload, headers={"X-CSRF-Token": self.csrf()})

    def test_scale_label_resolves_beyond_the_first_150_products(self):
        self.assertEqual(self.client.get("/api/products/scale/54321").status_code, 401)
        self.login()
        listed = self.client.get("/api/products").json["products"]
        self.assertEqual(len(listed), 150)
        self.assertNotIn(self.weighed["id"], [row["id"] for row in listed])
        found = self.client.get("/api/products/scale/54321")
        self.assertEqual(found.status_code, 200, found.json)
        self.assertEqual(found.json["product"]["id"], self.weighed["id"])
        self.assertEqual(found.json["product"]["unit"], "KG")
        # Only weighed products answer to a scale code, and unknown codes are a clean 404.
        self.assertEqual(self.client.get("/api/products/scale/11111").status_code, 404)
        self.assertEqual(self.client.get("/api/products/scale/99999").status_code, 404)

    def test_batch_returns_current_price_and_stock_for_cart_products(self):
        ids = [self.products[0]["id"], self.weighed["id"], "desconhecido"]
        self.assertEqual(self.client.get("/api/products/batch?ids=" + ",".join(ids)).status_code, 401)
        self.login()
        rows = self.client.get("/api/products/batch?ids=" + ",".join(ids)).json["products"]
        self.assertEqual({row["id"] for row in rows}, set(ids[:2]))
        self.assertEqual(self.client.get("/api/products/batch").json, {"products": []})
        with self.store.transaction() as conn:
            conn.execute("UPDATE products SET price_cents=777 WHERE id=?", (ids[0],))
        fresh = self.client.get("/api/products/batch?ids=" + ids[0]).json["products"][0]
        self.assertEqual(fresh["price_cents"], 777)

    def test_cash_flow_open_sale_with_change_withdrawal_and_close(self):
        self.login()
        opened = self.post("/api/cash/open", {"opening_cents": 10000})
        self.assertEqual(opened.status_code, 200, opened.json)
        product = self.products[0]
        sale = self.post("/api/sales", {"request_id": str(uuid.uuid4()),
                                        "items": [{"product_id": product["id"], "quantity_milli": 3000}],
                                        "discount_cents": 0, "customer_name": "", "customer_document": "",
                                        "customer_id": None, "seller_id": None, "manager_pin": "",
                                        "payments": [{"method": "cash", "amount_cents": 1000}]})
        self.assertEqual(sale.status_code, 200, sale.json)
        self.assertEqual(sale.json["total_cents"], 300)
        self.assertEqual(sale.json["change_cents"], 700)
        movement = self.post("/api/cash/movement", {"kind": "withdrawn", "amount_cents": 5000, "reason": "Cofre"})
        self.assertEqual(movement.status_code, 200, movement.json)
        self.assertEqual(self.post("/api/cash/close", {"counted_cents": 5300, "manager_pin": "000000"}).status_code, 403)
        closed = self.post("/api/cash/close", {"counted_cents": 5300, "manager_pin": "654321"})
        self.assertEqual(closed.status_code, 200, closed.json)
        self.assertEqual(closed.json["expected_cents"], 5300)
        self.assertEqual(closed.json["difference_cents"], 0)
        self.assertIsNone(self.client.get("/api/session").json["cash"])


class CaixaPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        app = create_app({"TESTING": True, "DATA_DIR": cls.temp.name,
                          "DB_PATH": Path(cls.temp.name) / "terminal.sqlite3", "START_WORKER": False})
        cls.client = app.test_client()
        cls.html = cls.client.get("/").get_data(as_text=True)
        cls.js = (STATIC / "terminal.js").read_text(encoding="utf-8")
        cls.css = (STATIC / "terminal.css").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_page_has_brand_sale_screen_and_key_ruler(self):
        self.assertIn("Frente de caixa", self.html)
        self.assertRegex(self.html, r'class="lockup-name">PDV2</span><span class="lockup-by">por CrownTech</span>')
        self.assertEqual(self.html.count(BRAND_FOOTER), 4)  # pairing, recovery, login/lock and the "Sobre" card
        for element_id in ("statusBar", "productSearch", "lastItem", "cartItems", "cartTotal", "paymentMethods",
                           "payCash", "changeValue", "donePanel", "keyBar"):
            self.assertIn(f'id="{element_id}"', self.html)
        # Sangria/suprimento used to live in a hidden proxy nobody could reach.
        self.assertNotIn("cashStateProxy", self.html)

    def test_every_asset_is_served_locally(self):
        sources = re.findall(r'(?:src|href)="([^"]+)"', self.html)
        self.assertTrue(sources)
        for source in sources:
            # "/station/" is the local page of an additional cashier (phase 3), also served on 127.0.0.1.
            self.assertTrue(source.startswith(("/static/", "/api/", "/station/", "#")), source)
        for name in ("terminal.css", "terminal.js", "terminal-core.js", "theme.js"):
            response = self.client.get("/static/" + name)
            self.assertEqual(response.status_code, 200, name)
            response.close()
            text = (STATIC / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"https?://", name)
            self.assertNotIn("@import", text)
        self.assertEqual(re.findall(r"https?://(?!www\.w3\.org/2000/svg)[^\"'\s]+", self.html), [])

    def test_themes_and_tokens_are_declared(self):
        for token in ("--bg:", "--surface:", "--ink:", "--muted:", "--brand:", "--ok:", "--warn:", "--danger:"):
            self.assertIn(token, self.css)
        self.assertIn(':root[data-theme="dark"]', self.css)
        self.assertIn(':root[data-theme="contrast"]', self.css)
        self.assertIn("tabular-nums", self.css)
        self.assertIn("prefers-reduced-motion", self.css)

    def test_dialogs_use_one_single_column_component(self):
        # Return, fiado and variation dialogs inherited a two-column grid and rendered broken.
        self.assertNotIn("grid-template-columns:auto 1fr", self.css)
        self.assertRegex(self.css, r"\.modal\{[^}]*grid-template-columns:minmax\(0,1fr\)")
        self.assertNotIn("confirm-dialog", self.js)

    def test_manager_pin_is_never_kept_in_page_state(self):
        self.assertEqual(self.js.count("managerPin"), 0)
        state_literal = self.js[self.js.index("const state = {"):self.js.index("const role = ")]
        self.assertNotRegex(state_literal.lower(), r"\bpin\b|manager")
        self.assertNotRegex(self.js, r"state\.[A-Za-z.]*[Pp]in\s*=[^=]")

    def test_sale_flow_never_opens_the_system_print_dialog_by_itself(self):
        # Cashiers get 403 from /api/print/discover; relying on it printed twice on CUPS.
        body = self.js[self.js.index("async function printAfterSale"):self.js.index("function dismissDone")]
        self.assertNotIn("/api/print/discover", body)
        self.assertNotIn("window.print", body)
        self.assertEqual(self.js.count("/api/print/discover"), 1)  # only the printers tab, for managers

    def test_login_select_keeps_the_chosen_operator(self):
        body = self.js[self.js.index("function renderOperatorSelect"):self.js.index("function applySession")]
        self.assertIn("C.keepSelection(previous", body)
        self.assertIn("dataset.signature", body)


@unittest.skipUnless(shutil.which("node"), "node não está disponível")
class CaixaCoreTests(unittest.TestCase):
    """terminal-core.js has no DOM: run it in node and assert on the JSON it prints."""

    @classmethod
    def setUpClass(cls):
        script = """
const C = require(process.argv[1]);
const gate = C.createWeightGate(2);
const now = 1000000000;
const out = {
  shortcuts: C.SHORTCUTS.map(s => [s.cap || s.key, s.label]),
  scan: [C.parseScanInput('3*7891234567890'), C.parseScanInput('1,250 * banana'), C.parseScanInput('7891234567890'), C.parseScanInput('3*')],
  scaleValid: C.scaleLabel('2543210125005'),
  scaleBadDigit: C.scaleLabel('2543210125001'),
  scaleQty: C.scaleLabelQuantity({scaleCode: '54321', payload: 1250}, {unit: 'KG', price_cents: 899}, 'weight'),
  scaleQtyUnit: C.scaleLabelQuantity({scaleCode: '54321', payload: 1250}, {unit: 'UN', price_cents: 899}, 'weight'),
  targetUnit: C.scaleTarget([{id: 'refri', unit: 'UN', quantity_milli: 1000}], 'refri'),
  targetNotWaiting: C.scaleTarget([{id: 'banana', unit: 'KG', quantity_milli: 1200}], null),
  targetWaiting: C.scaleTarget([{id: 'banana', unit: 'KG', quantity_milli: 0}, {id: 'refri', unit: 'UN', quantity_milli: 1000}], 'banana'),
  gate: [gate.push(1200), gate.push(1180), gate.push(1195), gate.push(1195)],
  keep: [C.keepSelection('carla', ['ana', 'bruno', 'carla']), C.keepSelection('saiu', ['ana', 'bruno']), C.keepSelection('', ['ana', 'bruno'], 'bruno')],
  staleError: C.syncIndicator({pending: 0, last_sync_error: 'timeout antigo', last_heartbeat_at: now - 20000, last_heartbeat_error: ''}, now),
  heartbeatDown: C.syncIndicator({pending: 2, last_heartbeat_error: 'sem rede', last_heartbeat_at: now - 400000}, now),
  silent: C.syncIndicator({pending: 0, last_sync_at: now - 600000}, now),
  neverSeen: C.syncIndicator({pending: 0}, now),
  review: C.syncIndicator({pending: 1, quarantined_count: 3, last_heartbeat_at: now - 1000}, now),
  tones: [
    C.statusTone({loggedIn: true, sells: true, canSell: false, cashOpen: true, indicator: {state: 'online', pending: 0, review: 0}, pendingForMs: 0}),
    C.statusTone({loggedIn: true, sells: true, canSell: true, cashOpen: false, indicator: {state: 'online', pending: 0, review: 0}, pendingForMs: 0}),
    C.statusTone({loggedIn: true, sells: true, canSell: true, cashOpen: true, indicator: {state: 'offline', pending: 4, review: 0}, pendingForMs: 0}),
    C.statusTone({loggedIn: true, sells: true, canSell: true, cashOpen: true, indicator: {state: 'online', pending: 1, review: 0}, pendingForMs: 5000}),
    C.statusTone({loggedIn: true, sells: true, canSell: true, cashOpen: true, indicator: {state: 'online', pending: 1, review: 0}, pendingForMs: 40000}),
    C.statusTone({loggedIn: true, sells: true, canSell: true, cashOpen: true, indicator: {state: 'online', pending: 0, review: 2}, pendingForMs: 0}),
  ],
  pay: C.paymentOptions([{id: 'pix', kind: 'pix', name: 'Pix', active: true}, {id: 'cash', kind: 'cash', name: 'Dinheiro', active: true},
    {id: 'card_debit', kind: 'card_debit', name: 'Débito', active: false}, {id: 'v1', kind: 'other', name: 'Vale', active: true},
    {id: 'v2', kind: 'other', name: 'Convênio', active: true}]).map(o => [o.key, o.method, o.label]),
  discount: [C.discountCents({mode: 'percent', value: 10}, 1000), C.discountCents({mode: 'value', value: 250}, 1000), C.discountCents({mode: 'value', value: 5000}, 1000),
    C.discountNeedsPin('cashier', 100, 1000), C.discountNeedsPin('cashier', 101, 1000), C.discountNeedsPin('manager', 500, 1000)],
  receipt: C.receiptBlocksMarkup({blocks: [{kind: 'text', align: 'center', small: true, lines: ['PDV2 - crowntech.com.br']}, {kind: 'text', lines: ['<b>Total</b>']}]}, C.receiptDimensions({})),
  money: [C.parseMoney('1.234,56'), C.parseMoney('50'), C.moneyPlain(4139)],
};
console.log(JSON.stringify(out));
"""
        result = subprocess.run(["node", "-e", script, str(STATIC / "terminal-core.js")],
                                capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.out = json.loads(result.stdout)

    def test_one_shortcut_map_covers_f1_to_f12_and_esc(self):
        self.assertEqual(self.out["shortcuts"], [
            ["F1", "Ajuda"], ["F2", "Buscar"], ["F3", "Quantidade"], ["F4", "Cliente/CPF"], ["F5", "Desconto"],
            ["F6", "Em espera"], ["F7", "Sangria/Suprimento"], ["F8", "Cancelar item"], ["F9", "Reimprimir"],
            ["F10", "Pagar"], ["F11", "Foco"], ["F12", "Caixa"], ["Esc", "Voltar"], ["Pause", "Pausar caixa"]])

    def test_scan_field_accepts_quantity_times_code(self):
        self.assertEqual(self.out["scan"][0], {"quantityMilli": 3000, "code": "7891234567890"})
        self.assertEqual(self.out["scan"][1], {"quantityMilli": 1250, "code": "banana"})
        self.assertEqual(self.out["scan"][2], {"quantityMilli": None, "code": "7891234567890"})
        self.assertEqual(self.out["scan"][3], {"quantityMilli": 3000, "code": ""})

    def test_scale_label_is_decoded_only_with_a_valid_check_digit(self):
        self.assertEqual(self.out["scaleValid"], {"scaleCode": "54321", "payload": 1250})
        self.assertIsNone(self.out["scaleBadDigit"])
        self.assertEqual(self.out["scaleQty"], 1250)
        self.assertIsNone(self.out["scaleQtyUnit"])

    def test_scale_weight_only_reaches_a_weighed_item_waiting_for_it(self):
        self.assertIsNone(self.out["targetUnit"])  # was: last touched item, even one sold by unit
        self.assertIsNone(self.out["targetNotWaiting"])  # was: any KG line, so a weight was never locked
        self.assertEqual(self.out["targetWaiting"]["id"], "banana")
        self.assertEqual(self.out["gate"], [None, None, None, 1195])  # unstable readings are not captured

    def test_rebuilt_operator_select_keeps_the_choice(self):
        self.assertEqual(self.out["keep"], ["carla", "ana", "bruno"])

    def test_offline_indicator_follows_the_heartbeat_not_a_stale_sync_error(self):
        self.assertEqual(self.out["staleError"]["state"], "online")  # was shown as "Offline" forever
        self.assertEqual(self.out["heartbeatDown"]["state"], "offline")
        self.assertEqual(self.out["heartbeatDown"]["queueLabel"], "Fila 2")
        self.assertEqual(self.out["silent"]["state"], "offline")
        self.assertEqual(self.out["neverSeen"]["state"], "local")
        self.assertEqual(self.out["review"]["reviewLabel"], "3 em revisão")
        self.assertEqual(self.out["staleError"]["reviewLabel"], "")

    def test_status_bar_tone(self):
        self.assertEqual(self.out["tones"], ["danger", "danger", "warn", "ok", "warn", "warn"])

    def test_payment_digits_are_fixed_and_company_forms_follow(self):
        self.assertEqual(self.out["pay"], [["1", "cash", "Dinheiro"], ["2", "pix", "Pix"],
                                           ["7", "other", "Vale"], ["8", "other", "Convênio"]])

    def test_discount_amount_and_role_limit(self):
        self.assertEqual(self.out["discount"], [100, 250, 999, False, True, False])

    def test_receipt_preview_renders_small_blocks_and_escapes(self):
        self.assertIn('class="receipt-text receipt-text-center receipt-text-small"', self.out["receipt"])
        self.assertIn("PDV2 - crowntech.com.br", self.out["receipt"])
        self.assertIn("&lt;b&gt;Total&lt;/b&gt;", self.out["receipt"])
        self.assertEqual(self.out["money"], [123456, 5000, "41,39"])


class DemoTerminalTests(unittest.TestCase):
    def test_demo_seed_is_fictitious_paired_and_complete(self):
        spec = importlib.util.spec_from_file_location("demo_terminal", ROOT / "scripts" / "demo_terminal.py")
        demo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(demo)
        with tempfile.TemporaryDirectory() as data_dir:
            app = create_app({"TESTING": True, "DATA_DIR": data_dir,
                              "DB_PATH": Path(data_dir) / "terminal.sqlite3", "START_WORKER": False})
            store = app.extensions["terminal_store"]
            products = demo.seed(store)
            self.assertTrue(18 <= len(products) <= 24)
            self.assertEqual(len({product["category"] for product in products}), 4)
            self.assertTrue(any(product["unit"] == "KG" for product in products))
            self.assertEqual(len([product for product in products if product.get("group_id")]), 3)
            self.assertTrue(store.connection_info()["paired"])
            self.assertTrue(store.lease_status()["can_sell"])
            customers = store.customers()
            self.assertEqual(len(customers), 2)
            self.assertTrue(any(customer["credit_limit_cents"] > 0 for customer in customers))
            self.assertTrue(any(method["kind"] == "other" for method in store.payment_methods()))
            operators = {operator["role"]: operator for operator in store.operators()}
            self.assertEqual(set(operators), {"cashier", "manager"})
            self.assertEqual(store.login(operators["cashier"]["id"], demo.CASHIER_PIN)["role"], "cashier")
            self.assertEqual(store.login(operators["manager"]["id"], demo.MANAGER_PIN)["role"], "manager")
            water = next(product for product in products if product["name"].startswith("Água mineral"))
            quote = store.quote([{"product_id": water["id"], "quantity_milli": 3000}])
            self.assertEqual(quote["promo_discount_cents"], water["price_cents"])  # leve 3 pague 2


if __name__ == "__main__":
    unittest.main()
