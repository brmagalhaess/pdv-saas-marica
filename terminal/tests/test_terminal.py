import copy
import json
import os
import sqlite3
import tempfile
import time
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import ANY, patch

from shared.licensing import generate_keypair, payload_digest, sign_lease
from shared.releases import sign_release
from shared.security import hash_pin
from terminal.app import create_app
from terminal.printers import label_bytes, receipt_bytes, send_bytes
from terminal.store import Store, TerminalError
from terminal.sync import check_release, pair, print_once, refresh_bootstrap, sync_once


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "terminal.sqlite3"
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name,
                               "DB_PATH": self.path, "START_WORKER": False})
        self.store = self.app.extensions["terminal_store"]
        self.client = self.app.test_client()
        self.tenant_id, self.shop_id, self.terminal_id = (str(uuid.uuid4()) for _ in range(3))
        self.cashier_id, self.manager_id = (str(uuid.uuid4()) for _ in range(2))
        self.product_id, self.unit_product_id = (str(uuid.uuid4()) for _ in range(2))
        self.private, self.public = generate_keypair()
        self.operators = [
            {"id": self.cashier_id, "name": "Ana", "role": "cashier", "pin_hash": hash_pin("123987")},
            {"id": self.manager_id, "name": "Bruno", "role": "manager", "pin_hash": hash_pin("654321")},
        ]
        self.products = [
            {"id": self.product_id, "sku": "QUEIJO", "barcode": "7890000000001", "name": "Queijo",
             "unit": "KG", "scale_code": "12345", "category": "Frios", "price_cents": 1299,
             "quantity_milli": 5000, "active": True},
            {"id": self.unit_product_id, "sku": "ARROZ", "barcode": "7890000000002", "name": "Arroz",
             "unit": "UN", "category": "Mercearia", "price_cents": 700, "quantity_milli": 3000, "active": True},
        ]
        now = int(time.time())
        self.claims = {"schema": 1, "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                       "terminal_id": self.terminal_id, "device_id": self.store.identity()["device_id"],
                       "issued_at": now - 5, "expires_at": now + 3600, "features": ["pdv"],
                       "status": "active", "operators_hash": payload_digest(self.operators)}
        self.lease = sign_lease(self.claims, self.private)
        self.store.set_pairing({"token": "t" * 48, "tenant_id": self.tenant_id,
                                "shop_id": self.shop_id, "terminal_id": self.terminal_id,
                                "lease": self.lease, "public_key": self.public}, "http://127.0.0.1:8000")
        self.bootstrap = {"products": self.products, "operators": self.operators,
                          "lease": self.lease, "public_key": self.public}
        self.store.apply_bootstrap(self.bootstrap)
        self.cashier = self.store.login(self.cashier_id, "123987")
        self.manager = self.store.login(self.manager_id, "654321")

    def csrf(self):
        return self.client.get("/api/session").json["csrf"]

    def login(self, operator_id=None, pin="123987"):
        token = self.csrf()
        response = self.client.post("/api/login", json={"operator_id": operator_id or self.cashier_id, "pin": pin},
                                    headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json["csrf"]

    def test_auth_csrf_origin_host_and_pin(self):
        self.assertIn(b"Frente de caixa", self.client.get("/").data)
        self.assertEqual(self.client.post("/api/cash/open", json={"opening_cents": 0}).status_code, 403)
        csrf = self.csrf()
        self.assertEqual(self.client.post("/api/cash/open", json={"opening_cents": 0},
                                          headers={"X-CSRF-Token": csrf}).status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"operator_id": self.cashier_id, "pin": "wrong"},
                                          headers={"X-CSRF-Token": csrf}).status_code, 401)
        self.login()
        self.assertEqual(self.client.post("/api/cash/open", json={"opening_cents": 0},
                                          headers={"X-CSRF-Token": "wrong"}).status_code, 403)
        self.assertEqual(self.client.post("/api/cash/open", json={"opening_cents": 0},
                                          headers={"X-CSRF-Token": self.csrf(),
                                                   "Origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/session", headers={"Host": "evil.example"}).status_code, 403)

    def test_discount_pin_is_authorized_before_sale(self):
        self.login()
        csrf = self.csrf()
        invalid = self.client.post("/api/discount/authorize",
                                   json={"pin": "000000", "gross_cents": 1299, "discount_cents": 100},
                                   headers={"X-CSRF-Token": csrf})
        self.assertEqual(invalid.status_code, 403, invalid.json)
        valid = self.client.post("/api/discount/authorize",
                                 json={"pin": "654321", "gross_cents": 1299, "discount_cents": 100},
                                 headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(valid.status_code, 200, valid.json)
        self.assertEqual(valid.json, {"authorized": True})

    def test_item_removal_requires_manager_pin_and_signed_hours_are_visible(self):
        with self.assertRaisesRegex(TerminalError, "PIN"):
            self.store.authorize_item_removal(self.cashier, "000000")
        self.assertTrue(self.store.authorize_item_removal(self.cashier, "654321")["authorized"])
        self.assertTrue(self.store.authorize_item_removal(self.manager)["authorized"])
        claims = dict(self.claims, business_hours={"mon": {"enabled": True, "open": "08:00", "close": "18:00"}})
        lease = sign_lease(claims, self.private)
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "lease", json.dumps(lease))
        self.assertTrue(self.store.business_hours_status(datetime(2026, 10, 5, 10, 0))["open"])
        self.assertFalse(self.store.business_hours_status(datetime(2026, 10, 5, 20, 0))["open"])

    def test_license_key_and_one_time_pairing_do_not_store_full_key(self):
        fresh = Store(Path(self.temp.name) / "fresh.sqlite3")
        claims = dict(self.claims, device_id=fresh.identity()["device_id"])
        response = {"token": "x" * 48, "tenant_id": self.tenant_id,
                    "shop_id": self.shop_id, "terminal_id": self.terminal_id,
                    "tenant": {"name": "Mercearia Aurora"},
                    "shop": {"name": "Centro"},
                    "terminal": {"name": "Caixa 1"},
                    "license_hint": "7B2C", "lease": sign_lease(claims, self.private),
                    "public_key": self.public}
        with self.assertRaisesRegex(TerminalError, "Chave da licença"):
            pair(fresh, "http://127.0.0.1:8000", "", "codigo-temporario")
        with patch("terminal.sync._request", return_value=response) as send, \
             patch("terminal.sync.refresh_bootstrap", return_value={"products": 0, "operators": 0}):
            pair(fresh, "http://127.0.0.1:8000", " PDV2-LICENSE-7B2C ", " codigo-temporario ")
        self.assertEqual(send.call_args.kwargs["json"]["license_key"], "PDV2-LICENSE-7B2C")
        self.assertEqual(send.call_args.kwargs["json"]["code"], "codigo-temporario")
        self.assertNotIn("license_key", fresh.meta())
        self.assertEqual(fresh.connection_info()["license_hint"], "7B2C")
        self.assertEqual(fresh.connection_info()["tenant_name"], "Mercearia Aurora")
        with self.assertRaisesRegex(TerminalError, "já está pareado"):
            pair(fresh, "http://127.0.0.1:8000", "PDV2-LICENSE-7B2C", "outro-codigo")

    def test_clean_pairing_can_be_corrected_without_exposing_old_company_cache(self):
        fresh = Store(Path(self.temp.name) / "clean-switch.sqlite3")
        old_claims = dict(self.claims, device_id=fresh.identity()["device_id"])
        old_lease = sign_lease(old_claims, self.private)
        fresh.set_pairing({"token": "a" * 48, "tenant_id": self.tenant_id,
                           "shop_id": self.shop_id, "terminal_id": self.terminal_id,
                           "tenant": {"name": "Empresa Antiga"}, "lease": old_lease,
                           "public_key": self.public}, "http://127.0.0.1:8000")
        fresh.apply_bootstrap(dict(self.bootstrap, lease=old_lease))
        self.assertTrue(fresh.export_data()["products"])

        self.assertEqual(fresh.unpair(), {"paired": False})
        self.assertFalse(fresh.connection_info()["paired"])
        self.assertEqual(fresh.operators(), [])
        self.assertEqual(fresh.export_data()["products"], [])
        self.assertEqual(fresh.sales(), [])

        new_tenant, new_shop, new_terminal, new_product, new_operator = (str(uuid.uuid4()) for _ in range(5))
        new_operators = [{"id": new_operator, "name": "Pessoa Nova", "role": "cashier",
                          "pin_hash": hash_pin("831947")}]
        new_claims = dict(old_claims, tenant_id=new_tenant, shop_id=new_shop,
                          terminal_id=new_terminal, operators_hash=payload_digest(new_operators))
        new_lease = sign_lease(new_claims, self.private)
        fresh.set_pairing({"token": "b" * 48, "tenant_id": new_tenant,
                           "shop_id": new_shop, "terminal_id": new_terminal,
                           "tenant": {"name": "Empresa Nova"}, "lease": new_lease,
                           "public_key": self.public}, "http://127.0.0.1:8000")
        fresh.apply_bootstrap({"products": [{"id": new_product, "sku": "NOVO", "barcode": "7890000000099",
            "name": "Produto Novo", "unit": "UN", "price_cents": 100,
            "quantity_milli": 1000, "active": True}], "operators": new_operators,
            "lease": new_lease, "public_key": self.public})
        exported = json.dumps(fresh.export_data(), ensure_ascii=False)
        self.assertIn("Produto Novo", exported)
        self.assertNotIn("Empresa Antiga", exported)
        self.assertNotIn("Queijo", exported)
        self.assertNotIn(self.terminal_id, exported)
        self.assertEqual([row["name"] for row in fresh.operators()], ["Pessoa Nova"])

    def test_business_history_blocks_unpair_and_repair_without_erasing_data(self):
        self.store.open_cash(self.cashier_id, 0)
        sale = self.store.sale(self.cashier, {"request_id": "old-company-sale",
            "items": [{"product_id": self.unit_product_id, "quantity_milli": 1000}],
            "payments": [{"method": "cash", "amount_cents": 700}],
            "customer_name": "Cliente antigo"})
        self.store.close_cash(self.cashier_id, 700, "654321")
        self.store.ack_events([event["event_id"] for event in self.store.pending_events()])
        response = self.client.post("/api/unpair", json={"confirm": True},
                                    headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(response.status_code, 409, response.json)
        self.assertIn("diretório de dados", response.json["error"])
        self.assertEqual(self.store.identity()["terminal_id"], self.terminal_id)
        self.assertEqual(self.store.sale_detail(sale["id"])["customer_name"], "Cliente antigo")
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM meta WHERE key IN ('terminal_id', 'token')")
        with patch("terminal.sync._request") as send, self.assertRaisesRegex(TerminalError, "histórico"):
            pair(self.store, "http://127.0.0.1:8000", "PDV2-EXAMPLE-KEY", "codigo-temporario")
        send.assert_not_called()

    def test_new_profile_preserves_database_and_is_selected_after_restart(self):
        old_path = self.store.path
        old_device = self.store.identity()["device_id"]
        response = self.client.post("/api/new-profile", json={"confirm": True},
                                    headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(old_path.exists())
        self.assertTrue(list((old_path.parent / "backups").glob("terminal-*.sqlite3")))
        active = self.app.extensions["terminal_store"]
        self.assertNotEqual(active.path, old_path)
        self.assertFalse(active.connection_info()["paired"])
        self.assertNotEqual(active.identity()["device_id"], old_device)
        self.assertTrue((Path(self.temp.name) / "active-profile.json").exists())
        restarted = create_app({"TESTING": True, "DATA_DIR": self.temp.name,
                                "DB_PATH": old_path, "START_WORKER": False})
        self.assertEqual(restarted.extensions["terminal_store"].path, active.path)
        self.assertFalse(restarted.extensions["terminal_store"].connection_info()["paired"])

    def test_preserved_backup_blocks_switch_even_if_audit_was_lost(self):
        backup = self.store.backup_now()
        self.assertTrue(Path(backup["directory"], backup["name"]).exists())
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM audit WHERE action='terminal.backup'")
        with self.assertRaisesRegex(TerminalError, "histórico"):
            self.store.unpair()
        self.assertEqual(self.store.identity()["terminal_id"], self.terminal_id)

    def test_offline_operator_login_after_restart(self):
        reopened = Store(self.path)
        self.assertEqual({o["name"] for o in reopened.operators()}, {"Ana", "Bruno"})
        self.assertEqual(reopened.login(self.cashier_id, "123987")["role"], "cashier")
        self.assertEqual(reopened.login(self.manager_id, "654321")["role"], "manager")

    def test_http_terminal_stays_operational_when_cloud_is_unreachable(self):
        """The local checkout must not call the cloud in its critical path."""
        self.login()
        csrf = self.csrf()
        with patch("terminal.sync._request", side_effect=TerminalError("nuvem indisponível", 503)) as remote:
            products = self.client.get("/api/products")
            self.assertEqual(products.status_code, 200, products.json)
            opened = self.client.post("/api/cash/open", json={"opening_cents": 1000},
                                      headers={"X-CSRF-Token": csrf})
            self.assertEqual(opened.status_code, 200, opened.json)
            sale = self.client.post("/api/sales", json={
                "request_id": "offline-http-sale",
                "items": [{"product_id": self.unit_product_id, "quantity_milli": 1000}],
                "discount_cents": 0,
                "payments": [{"method": "cash", "amount_cents": 700}],
            }, headers={"X-CSRF-Token": self.csrf()})
            self.assertEqual(sale.status_code, 200, sale.json)
            self.assertEqual(self.client.get("/api/session").status_code, 200)
            remote.assert_not_called()

    def test_weight_product_keeps_scale_plu_in_local_catalog(self):
        product = next(row for row in self.store.products() if row["id"] == self.product_id)
        self.assertEqual(product["unit"], "KG")
        self.assertEqual(product["scale_code"], "12345")

    def test_read_only_operator_can_enter_without_cash_permissions(self):
        viewer_id = str(uuid.uuid4())
        operators = [*self.operators,
                     {"id": viewer_id, "name": "Consulta", "role": "viewer",
                      "pin_hash": hash_pin("321987")}]
        claims = dict(self.claims, operators_hash=payload_digest(operators))
        self.store.apply_bootstrap(dict(self.bootstrap, operators=operators,
                                        lease=sign_lease(claims, self.private)))
        self.login(viewer_id, "321987")
        self.assertEqual(self.client.get("/api/products").status_code, 200)
        self.assertEqual(self.client.get("/api/sales").status_code, 200)
        self.assertEqual(self.client.get("/api/sync").status_code, 200)
        self.assertEqual(self.client.get("/api/carts/suspended").status_code, 403)
        self.assertEqual(self.client.post("/api/cash/open", json={"opening_cents": 0},
                                          headers={"X-CSRF-Token": self.csrf()}).status_code, 403)

    def test_login_lockout_and_pin_rotation_invalidates_cookie(self):
        for _ in range(5):
            with self.assertRaises(TerminalError):
                self.store.login(self.cashier_id, "000000")
        with self.assertRaisesRegex(TerminalError, "Muitas tentativas"):
            self.store.login(self.cashier_id, "123987")
        self.login(self.manager_id, "654321")
        changed_operators = copy.deepcopy(self.operators)
        changed_operators[1]["pin_hash"] = hash_pin("987654")
        changed_claims = dict(self.claims, operators_hash=payload_digest(changed_operators))
        changed = dict(self.bootstrap, operators=changed_operators,
                       lease=sign_lease(changed_claims, self.private))
        self.store.apply_bootstrap(changed)
        self.assertEqual(self.client.get("/api/sales").status_code, 401)

    def test_fractional_sale_split_money_idempotence_and_restart(self):
        self.store.open_cash(self.cashier_id, 10000)
        request = {"request_id": "sale-one", "items": [{"product_id": self.product_id, "quantity_milli": 1250}],
                   "discount_cents": 24, "payments": [{"method": "cash", "amount_cents": 500},
                                                          {"method": "pix", "amount_cents": 1200}],
                   "customer_name": "Cliente A"}
        sale = self.store.sale(self.cashier, request)
        self.assertEqual(sale["gross_cents"], 1624)
        self.assertEqual(sale["total_cents"], 1600)
        self.assertEqual(sale["change_cents"], 100)
        self.assertEqual(self.store.sale(self.cashier, request)["id"], sale["id"])
        changed = copy.deepcopy(request)
        changed["items"][0]["quantity_milli"] = 1500
        with self.assertRaisesRegex(TerminalError, "outra venda"):
            self.store.sale(self.cashier, changed)
        reopened = Store(self.path)
        self.assertEqual(next(x for x in reopened.products() if x["id"] == self.product_id)["stock_milli"], 3750)
        self.assertEqual(len(reopened.sales()), 1)
        self.assertEqual(len(reopened.pending_events()), 2)
        close = reopened.close_cash(self.cashier_id, 10400, "654321")
        self.assertEqual(close["expected_cents"], 10400)

    def test_sale_event_carries_operator_and_optional_seller(self):
        self.store.open_cash(self.cashier_id, 0)
        request = {"request_id": "credited-sale", "seller_id": self.manager_id,
                   "items": [{"product_id": self.unit_product_id, "quantity_milli": 1000}],
                   "payments": [{"method": "cash", "amount_cents": 700}]}
        sale = self.store.sale(self.cashier, request)
        self.assertEqual((sale["operator_name"], sale["seller_id"], sale["seller_name"]),
                         ("Ana", self.manager_id, "Bruno"))
        self.assertIn(b"Vendedor: Bruno", receipt_bytes(sale))
        with self.assertRaisesRegex(TerminalError, "outra venda"):
            self.store.sale(self.cashier, dict(request, seller_id=None))
        plain = self.store.sale(self.cashier, {"request_id": "plain-sale",
            "items": [{"product_id": self.unit_product_id, "quantity_milli": 1000}],
            "payments": [{"method": "cash", "amount_cents": 700}]})
        self.assertIsNone(plain["seller_id"])
        with self.assertRaisesRegex(TerminalError, "Vendedor não encontrado"):
            self.store.sale(self.cashier, {"request_id": "ghost-seller", "seller_id": str(uuid.uuid4()),
                "items": [{"product_id": self.unit_product_id, "quantity_milli": 1000}],
                "payments": [{"method": "cash", "amount_cents": 700}]})
        events = [e["data"] for e in self.store.pending_events() if e["type"] == "sale.completed"]
        self.assertEqual([(e["operator_id"], e["seller_id"]) for e in events],
                         [(self.cashier_id, self.manager_id), (self.cashier_id, None)])
        self.assertEqual(next(x for x in self.store.products() if x["id"] == self.unit_product_id)["stock_milli"], 1000)

    def test_cash_close_event_names_closer_and_supervisor(self):
        self.store.open_cash(self.cashier_id, 0)
        self.store.close_cash(self.cashier_id, 0, "654321")
        closed = [e["data"] for e in self.store.pending_events() if e["type"] == "cash.closed"]
        self.assertEqual([(e["operator_id"], e["supervisor_id"]) for e in closed], [(self.cashier_id, self.manager_id)])

    def test_fiado_limit_store_credit_and_partial_returns(self):
        customer_id = str(uuid.uuid4())
        self.store.apply_bootstrap(dict(self.bootstrap, customers=[
            {"id": customer_id, "name": "Dona Rosa", "document": "", "credit_limit_cents": 1500,
             "open_credit_cents": 100, "store_credit_cents": 0}]))
        self.store.open_cash(self.cashier_id, 5000)
        rice = lambda qty: [{"product_id": self.unit_product_id, "quantity_milli": qty * 1000}]
        fiado = self.store.sale(self.cashier, {"request_id": "fiado-1", "customer_id": customer_id,
                                               "items": rice(2), "payments": [{"method": "credit_account", "amount_cents": 1400}]})
        self.assertEqual(fiado["customer_name"], "Dona Rosa")
        self.assertEqual(self.store.sale(self.cashier, {"request_id": "fiado-1", "customer_id": customer_id,
            "items": rice(2), "payments": [{"method": "credit_account", "amount_cents": 1400}]})["id"], fiado["id"])
        with self.assertRaisesRegex(TerminalError, "Limite de fiado excedido"):
            self.store.sale(self.cashier, {"request_id": "fiado-2", "customer_id": customer_id,
                                           "items": rice(1), "payments": [{"method": "credit_account", "amount_cents": 700}]})
        with self.assertRaisesRegex(TerminalError, "exige cliente"):
            self.store.sale(self.cashier, {"request_id": "fiado-3", "items": rice(1),
                                           "payments": [{"method": "credit_account", "amount_cents": 700}]})
        with self.assertRaisesRegex(TerminalError, "PIN de gerente"):
            self.store.return_sale(self.cashier, fiado["id"], {"return_id": "r0", "refund_method": "account",
                                   "reason": "Avaria", "items": [{"product_id": self.unit_product_id, "quantity_milli": 1000}]})
        abated = self.store.return_sale(self.cashier, fiado["id"], {"return_id": "r1", "refund_method": "account",
            "reason": "Avaria", "manager_pin": "654321", "items": [{"product_id": self.unit_product_id, "quantity_milli": 1000}]})
        self.assertEqual(abated["total_cents"], 700)
        self.assertEqual(self.store.returnable(fiado["id"])["items"][0]["available_milli"], 1000)
        with self.assertRaisesRegex(TerminalError, "não pode ser cancelada"):
            self.store.void_sale(self.manager, fiado["id"], "Teste")
        # Return in store credit, then pay a new sale with it (an exchange).
        self.store.return_sale(self.manager, fiado["id"], {"return_id": "r2", "refund_method": "store_credit",
            "reason": "Troca", "items": [{"product_id": self.unit_product_id, "quantity_milli": 1000}]})
        with self.assertRaisesRegex(TerminalError, "disponível para devolução"):
            self.store.return_sale(self.manager, fiado["id"], {"return_id": "r3", "refund_method": "cash",
                "reason": "x", "items": [{"product_id": self.unit_product_id, "quantity_milli": 1000}]})
        swap = self.store.sale(self.cashier, {"request_id": "swap", "customer_id": customer_id, "items": rice(1),
                                              "payments": [{"method": "store_credit", "amount_cents": 700}]})
        with self.assertRaisesRegex(TerminalError, "Crédito na loja insuficiente"):
            self.store.sale(self.cashier, {"request_id": "swap-2", "customer_id": customer_id, "items": rice(1),
                                           "payments": [{"method": "store_credit", "amount_cents": 700}]})
        cash_sale = self.store.sale(self.cashier, {"request_id": "cash", "items": rice(1),
                                                   "payments": [{"method": "cash", "amount_cents": 700}]})
        self.store.return_sale(self.manager, cash_sale["id"], {"return_id": "r4", "refund_method": "cash",
            "reason": "Desistiu", "items": [{"product_id": self.unit_product_id, "quantity_milli": 1000}]})
        summary = self.store.cash_summary()
        self.assertEqual((summary["categories"]["cash_refunds_cents"], summary["expected_cents"]), (700, 5000))
        customer = self.store.customers("Rosa")[0]
        self.assertEqual((customer["open_credit_cents"], customer["store_credit_cents"], customer["available_credit_cents"]),
                         (800, 0, 700))
        self.assertEqual(next(p for p in self.store.products() if p["id"] == self.unit_product_id)["stock_milli"], 2000)
        returns = [e["data"] for e in self.store.pending_events() if e["type"] == "sale.returned"]
        self.assertEqual([(r["refund_method"], r["total_cents"]) for r in returns], [("account", 700), ("store_credit", 700), ("cash", 700)])
        # A catalog refresh keeps unsynced effects on top of the cloud balance.
        self.store.apply_bootstrap(dict(self.bootstrap, customers=[
            {"id": customer_id, "name": "Dona Rosa", "document": "", "credit_limit_cents": 1500,
             "open_credit_cents": 100, "store_credit_cents": 0}]))
        refreshed = self.store.customers()[0]
        self.assertEqual((refreshed["open_credit_cents"], refreshed["store_credit_cents"]), (800, 0))
        self.assertTrue(swap["id"])

    def test_payment_config_disables_methods_and_adds_custom(self):
        voucher = str(uuid.uuid4())
        config = [{"id": "c", "kind": "cash", "name": "Dinheiro", "active": True},
                  {"id": "p", "kind": "pix", "name": "Pix", "active": False},
                  {"id": voucher, "kind": "other", "name": "Vale-refeição", "active": True}]
        self.store.apply_bootstrap(dict(self.bootstrap, payment_methods=config))
        self.assertEqual([m["name"] for m in self.store.payment_methods()], ["Dinheiro", "Pix", "Vale-refeição"])
        self.store.open_cash(self.cashier_id, 0)
        item = [{"product_id": self.unit_product_id, "quantity_milli": 1000}]
        with self.assertRaisesRegex(TerminalError, "desativada pela empresa"):
            self.store.sale(self.cashier, {"request_id": "pix", "items": item, "payments": [{"method": "pix", "amount_cents": 700}]})
        with self.assertRaisesRegex(TerminalError, "desconhecida"):
            self.store.sale(self.cashier, {"request_id": "x", "items": item,
                "payments": [{"method": "other", "payment_method_id": str(uuid.uuid4()), "amount_cents": 700}]})
        request = {"request_id": "vale", "items": item,
                   "payments": [{"method": "other", "payment_method_id": voucher, "amount_cents": 700}]}
        sale = self.store.sale(self.cashier, request)
        self.assertEqual(self.store.sale(self.cashier, request)["id"], sale["id"])
        self.assertIn(b"Vale-refei", receipt_bytes(sale))
        event = [e["data"] for e in self.store.pending_events() if e["type"] == "sale.completed"][0]
        self.assertEqual(event["payments"], [{"method": "other", "payment_method_id": voucher, "amount_cents": 700}])

    def test_bootstrap_keeps_variation_grid_fields(self):
        group = str(uuid.uuid4())
        shirt = {"id": str(uuid.uuid4()), "sku": "P1", "barcode": "2000000000015", "name": "Camiseta - Azul / M",
                 "unit": "UN", "category": "Moda", "price_cents": 4990, "quantity_milli": 2000, "active": True,
                 "group_id": group, "group_name": "Camiseta", "variant": "Azul / M"}
        self.store.apply_bootstrap(dict(self.bootstrap, products=self.products + [shirt]))
        row = next(p for p in self.store.products() if p["id"] == shirt["id"])
        self.assertEqual((row["group_id"], row["group_name"], row["variant"]), (group, "Camiseta", "Azul / M"))
        self.store.open_cash(self.cashier_id, 0)
        sale = self.store.sale(self.cashier, {"request_id": "grade", "items": [{"product_id": shirt["id"], "quantity_milli": 1000}],
                                              "payments": [{"method": "cash", "amount_cents": 4990}]})
        self.assertEqual(sale["items"][0]["name"], "Camiseta - Azul / M")

    def test_receive_fiado_at_counter_enters_drawer_and_prints_vouchers(self):
        customer_id = str(uuid.uuid4())
        self.store.apply_bootstrap(dict(self.bootstrap, customers=[{"id": customer_id, "name": "Seu Zé", "document": "",
            "credit_limit_cents": 5000, "open_credit_cents": 1500, "store_credit_cents": 0}]))
        self.store.open_cash(self.cashier_id, 1000)
        with self.assertRaisesRegex(TerminalError, "maior que o fiado"):
            self.store.receive_credit(self.cashier, {"receipt_id": "r0", "customer_id": customer_id, "amount_cents": 1600, "method": "cash"})
        result = self.store.receive_credit(self.cashier, {"receipt_id": "r1", "customer_id": customer_id,
                                                          "amount_cents": 1000, "method": "cash"})
        self.assertEqual(result["open_credit_cents"], 500)
        self.assertIn("RECEBIMENTO DE FIADO", json.dumps(result["receipt"]))
        self.assertTrue(self.store.receive_credit(self.cashier, {"receipt_id": "r1", "customer_id": customer_id,
                                                                 "amount_cents": 1000, "method": "cash"})["replayed"])
        self.store.receive_credit(self.cashier, {"receipt_id": "r2", "customer_id": customer_id, "amount_cents": 200, "method": "pix"})
        summary = self.store.cash_summary()
        self.assertEqual((summary["categories"]["credit_received_cash_cents"], summary["categories"]["credit_received_other_cents"],
                          summary["expected_cents"]), (1000, 200, 2000))
        self.assertEqual(self.store.customers("Zé")[0]["open_credit_cents"], 300)
        # Refreshing the catalog keeps the unsynced payments on top of the cloud balance.
        self.store.apply_bootstrap(dict(self.bootstrap, customers=[{"id": customer_id, "name": "Seu Zé", "document": "",
            "credit_limit_cents": 5000, "open_credit_cents": 1500, "store_credit_cents": 0}]))
        self.assertEqual(self.store.customers("Zé")[0]["open_credit_cents"], 300)
        events = [e["data"]["amount_cents"] for e in self.store.pending_events() if e["type"] == "credit.received"]
        self.assertEqual(events, [1000, 200])
        sale = self.store.sale(self.cashier, {"request_id": "v", "items": [{"product_id": self.unit_product_id, "quantity_milli": 1000}],
                                              "payments": [{"method": "cash", "amount_cents": 700}]})
        back = self.store.return_sale(self.manager, sale["id"], {"return_id": "d1", "refund_method": "cash", "reason": "Avaria",
                                      "items": [{"product_id": self.unit_product_id, "quantity_milli": 1000}]})
        printed = json.dumps(back["receipt"], ensure_ascii=False)
        self.assertIn("DEVOLUÇÃO", printed)
        self.assertIn("Assinatura do cliente", printed)

    def test_promotions_priced_offline_and_manual_discount_over_promo_price(self):
        rules = [{"id": str(uuid.uuid4()), "name": "Leve 3 pague 2 Arroz", "kind": "buy_x_pay_y",
                  "product_id": self.unit_product_id, "buy_quantity": 3, "pay_quantity": 2, "active": True}]
        self.store.apply_bootstrap(dict(self.bootstrap, promotions=rules))
        quote = self.store.quote([{"product_id": self.unit_product_id, "quantity_milli": 3000}])
        self.assertEqual((quote["gross_cents"], quote["promo_discount_cents"]), (2100, 700))
        self.assertEqual(quote["lines"][0]["promotion_label"], "Leve 3 pague 2")
        self.store.open_cash(self.cashier_id, 0)
        with self.assertRaisesRegex(TerminalError, "PIN de gerente"):
            self.store.sale(self.cashier, {"request_id": "promo-2", "discount_cents": 150,
                "items": [{"product_id": self.unit_product_id, "quantity_milli": 3000}],
                "payments": [{"method": "cash", "amount_cents": 1250}]})
        # 10% manual discount (cashier limit) applies on the 14.00 promotional price, not on 21.00.
        sale = self.store.sale(self.cashier, {"request_id": "promo", "discount_cents": 140,
            "items": [{"product_id": self.unit_product_id, "quantity_milli": 3000}],
            "payments": [{"method": "cash", "amount_cents": 1260}]})
        self.assertEqual((sale["gross_cents"], sale["promo_discount_cents"], sale["discount_cents"], sale["total_cents"]),
                         (2100, 700, 140, 1260))
        self.assertEqual(sale["items"][0]["line_total_cents"], 1260)
        self.assertIn("Promoção: Leve 3 pague 2 Arroz".encode("cp850"), receipt_bytes(sale))
        item = [e for e in self.store.pending_events() if e["type"] == "sale.completed"][0]["data"]["items"][0]
        self.assertEqual((item["promo_discount_cents"], item["promotion_id"], item["promotion_name"]),
                         (700, rules[0]["id"], "Leve 3 pague 2 Arroz"))
        self.assertNotIn("promotion_label", item)  # screen-only field never reaches the cloud

    def test_company_production_product_sells_without_stock_and_does_not_reduce_balance(self):
        production_id = str(uuid.uuid4())
        production = {"id": production_id, "sku": "PAO-KG", "barcode": "7890000000099",
                       "name": "Pão francês", "unit": "KG", "category": "Padaria",
                       "price_cents": 1290, "quantity_milli": 0, "stock_controlled": False,
                       "active": True}
        self.store.apply_bootstrap(dict(self.bootstrap, products=self.products + [production]))
        self.store.open_cash(self.cashier_id, 0)
        sale = self.store.sale(self.cashier, {"request_id": "production-sale",
            "items": [{"product_id": production_id, "quantity_milli": 350}],
            "payments": [{"method": "cash", "amount_cents": 452}]})
        self.assertEqual(sale["total_cents"], 452)
        self.assertFalse(sale["items"][0]["stock_controlled"])
        self.assertEqual(next(row for row in self.store.products() if row["id"] == production_id)["stock_milli"], 0)

    def test_cash_close_requires_supervisor_pin_before_mutating_session(self):
        self.store.open_cash(self.cashier_id, 10000)
        with self.assertRaisesRegex(TerminalError, "PIN"):
            self.store.close_cash(self.cashier_id, 10000, "000000")
        self.assertIsNotNone(self.store.cash_state())
        closed = self.store.close_cash(self.cashier_id, 10000, "654321")
        self.assertEqual(closed["difference_cents"], 0)
        self.assertIsNone(self.store.cash_state())

    def test_cash_summary_separates_channels_and_reports_shortage(self):
        self.store.open_cash(self.cashier_id, 10000)
        self.store.sale(self.cashier, {"request_id": "channels-one",
            "items": [{"product_id": self.unit_product_id, "quantity_milli": 1000}],
            "payments": [{"method": "cash", "amount_cents": 200},
                          {"method": "pix", "amount_cents": 100},
                          {"method": "card_credit", "amount_cents": 200},
                          {"method": "card_debit", "amount_cents": 200}]})
        summary = self.store.cash_summary()
        self.assertEqual(summary["categories"]["cash_sales_net_cents"], 200)
        self.assertEqual(summary["categories"]["pix_cents"], 100)
        self.assertEqual(summary["categories"]["card_credit_cents"], 200)
        self.assertEqual(summary["categories"]["card_debit_cents"], 200)
        self.assertEqual(summary["expected_cents"], 10200)
        self.assertEqual(summary["total_channels_cents"], 700)
        closed = self.store.close_cash(self.cashier_id, 10150, "654321")
        self.assertEqual(closed["difference_cents"], -50)
        self.assertEqual(closed["counted_cents"], 10150)

    def test_backup_preserves_committed_sale_and_pending_outbox(self):
        self.store.open_cash(self.cashier_id, 0)
        sale = self.store.sale(self.cashier, {"request_id": "backup-sale",
            "items": [{"product_id": self.unit_product_id, "quantity_milli": 1000}],
            "payments": [{"method": "cash", "amount_cents": 700}]})
        copy = self.store.backup_now()
        backup_path = Path(copy["directory"]) / copy["name"]
        self.assertTrue(backup_path.is_file())
        with sqlite3.connect(backup_path) as recovered:
            self.assertEqual(recovered.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(recovered.execute("SELECT id FROM sales").fetchone()[0], sale["id"])
            self.assertEqual(recovered.execute("SELECT COUNT(*) FROM outbox WHERE acked_at IS NULL").fetchone()[0], 2)
        self.assertEqual(self.store.backup_status()["copies"][0]["name"], copy["name"])
        self.login(self.manager_id, "654321")
        self.assertEqual(self.client.get("/api/backups").status_code, 200)

    def test_discount_override_void_and_stock_restore_once(self):
        self.store.open_cash(self.cashier_id, 0)
        request = {"request_id": "discounted", "items": [{"product_id": self.unit_product_id,
                                                             "quantity_milli": 1000}],
                   "discount_cents": 140, "payments": [{"method": "cash", "amount_cents": 560}]}
        with self.assertRaisesRegex(TerminalError, "PIN"):
            self.store.sale(self.cashier, request)
        request["manager_pin"] = "654321"
        sale = self.store.sale(self.cashier, request)
        self.assertEqual(sale["total_cents"], 560)
        self.assertEqual(next(x for x in self.store.products() if x["id"] == self.unit_product_id)["stock_milli"], 2000)
        with self.assertRaisesRegex(TerminalError, "PIN"):
            self.store.void_sale(self.cashier, sale["id"], "Erro do operador")
        self.store.void_sale(self.cashier, sale["id"], "Erro do operador", "654321")
        self.store.void_sale(self.manager, sale["id"], "Mesmo motivo")
        self.assertEqual(next(x for x in self.store.products() if x["id"] == self.unit_product_id)["stock_milli"], 3000)
        self.assertEqual(sum(e["type"] == "sale.voided" for e in self.store.pending_events()), 1)

    def test_admin_discount_ceiling_applies_even_with_manager_pin(self):
        self.store.open_cash(self.cashier_id, 0)
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "discount_max_bps", "2000")
        above_limit = {"request_id": "discount-over-limit", "items": [{"product_id": self.unit_product_id,
                       "quantity_milli": 1000}], "discount_cents": 210,
                       "payments": [{"method": "cash", "amount_cents": 490}], "manager_pin": "654321"}
        with self.assertRaisesRegex(TerminalError, "limite configurado"):
            self.store.sale(self.cashier, above_limit)
        within_limit = dict(above_limit, request_id="discount-within-limit", discount_cents=140,
                            payments=[{"method": "cash", "amount_cents": 560}])
        sale = self.store.sale(self.cashier, within_limit)
        self.assertEqual(sale["total_cents"], 560)

    def test_discount_allocation_never_creates_negative_line(self):
        cheap = [{"id": str(uuid.uuid4()), "sku": f"CENT-{i}", "barcode": f"789000000001{i}",
                  "name": f"Produto {i}", "unit": "UN", "category": "Teste",
                  "price_cents": 1, "quantity_milli": 1000, "active": True} for i in range(3)]
        self.store.apply_bootstrap(dict(self.bootstrap, products=self.products + cheap))
        self.store.open_cash(self.cashier_id, 0)
        sale = self.store.sale(self.cashier, {"request_id": "cent-discount",
            "items": [{"product_id": item["id"], "quantity_milli": 1000} for item in cheap],
            "discount_cents": 2, "manager_pin": "654321",
            "payments": [{"method": "cash", "amount_cents": 1}]})
        self.assertEqual(sum(line["discount_cents"] for line in sale["items"]), 2)
        self.assertEqual(sum(line["line_total_cents"] for line in sale["items"]), 1)
        self.assertTrue(all(line["line_total_cents"] >= 0 for line in sale["items"]))

    def test_bootstrap_never_overwrites_stock_and_rejects_pin_tampering(self):
        self.store.open_cash(self.cashier_id, 0)
        self.store.sale(self.cashier, {"request_id": "stock-sale", "items": [{"product_id": self.unit_product_id,
                      "quantity_milli": 1000}], "payments": [{"method": "cash", "amount_cents": 700}]})
        self.store.apply_bootstrap(self.bootstrap)
        self.assertEqual(next(x for x in self.store.products() if x["id"] == self.unit_product_id)["stock_milli"], 2000)
        tampered = copy.deepcopy(self.bootstrap)
        tampered["operators"][0]["pin_hash"] = hash_pin("111111")
        with self.assertRaisesRegex(TerminalError, "operadores"):
            self.store.apply_bootstrap(tampered)
        self.assertEqual(self.store.login(self.cashier_id, "123987")["id"], self.cashier_id)

    def test_bootstrap_refreshes_cloud_stock_when_no_local_events_are_pending(self):
        updated = copy.deepcopy(self.bootstrap)
        updated["products"] = [self.products[0], dict(self.products[1], quantity_milli=4321)]
        self.store.apply_bootstrap(updated)
        product = next(row for row in self.store.products() if row["id"] == self.unit_product_id)
        self.assertEqual(product["stock_milli"], 4321)

    def test_catalog_accepts_blank_and_moved_barcodes_and_deactivates_removed_product(self):
        third = dict(self.products[1], id=str(uuid.uuid4()), sku="NOVO", barcode="")
        blank = [dict(self.products[0], barcode=""), dict(self.products[1], barcode=""), third]
        self.store.apply_bootstrap(dict(self.bootstrap, products=blank))
        self.assertEqual(len(self.store.products()), 3)
        moved = [dict(blank[0], barcode=""), dict(blank[1], barcode=self.products[0]["barcode"])]
        self.store.apply_bootstrap(dict(self.bootstrap, products=moved))
        self.assertEqual(len(self.store.products()), 2)
        self.assertEqual(self.store.products(self.products[0]["barcode"])[0]["id"], self.unit_product_id)
        self.assertEqual(next(p for p in self.store.export_data()["products"] if p["id"] == third["id"])["active"], 0)

    def test_stock_adjustment_respects_unit_precision(self):
        with self.assertRaisesRegex(TerminalError, "unidades inteiras"):
            self.store.adjust_stock(self.manager, self.unit_product_id, 500, "Contagem")
        self.assertEqual(self.store.adjust_stock(self.manager, self.unit_product_id, -1000,
                                                 "Contagem")["stock_milli"], 2000)
        self.assertEqual(self.store.adjust_stock(self.manager, self.product_id, 125,
                                                 "Contagem")["stock_milli"], 5125)

    def test_expired_and_clock_rollback_block_new_sale_but_close_and_export_work(self):
        self.store.open_cash(self.cashier_id, 0)
        expired = dict(self.claims, issued_at=int(time.time()) - 3600,
                       expires_at=int(time.time()) - 1)
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "lease", json.dumps(sign_lease(expired, self.private)))
        self.assertEqual(self.store.lease_status()["state"], "expired")
        with self.assertRaises(TerminalError):
            self.store.sale(self.cashier, {"request_id": "expired", "items": [], "payments": []})
        self.assertEqual(self.store.close_cash(self.cashier_id, 0, "654321")["expected_cents"], 0)
        self.login()
        self.assertEqual(self.client.get("/api/export").status_code, 403)  # cashier is not manager
        self.login(self.manager_id, "654321")
        self.assertEqual(self.client.get("/api/export").status_code, 200)
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "last_seen", str(int(time.time()) + 1000))
        self.assertEqual(self.store.lease_status()["state"], "clock_rollback")

    def test_print_formats_spool_uncertain_and_explicit_retry(self):
        sale = {"id": "sale-1", "customer_name": "Maria", "items": [{"name": "Queijo",
                "quantity_milli": 1250, "unit": "KG", "unit_price_cents": 1299, "line_total_cents": 1624}],
                "gross_cents": 1624, "discount_cents": 0, "total_cents": 1624,
                "payments": [{"method": "cash", "amount_cents": 1700}], "change_cents": 76}
        self.assertIn("NÃO É DOCUMENTO FISCAL".encode("cp850"), receipt_bytes(sale, 58))
        self.assertTrue(label_bytes({"id": "x", "name": "Arroz", "barcode": "789", "price_cents": 700}, "zpl").startswith(b"^XA"))
        self.assertTrue(label_bytes({"id": "x", "name": "Arroz", "barcode": "789", "price_cents": 700}, "epl").startswith(b"N\n"))
        with self.assertRaisesRegex(ValueError, "TM-T20"):
            label_bytes({"id": "x", "name": "Arroz", "barcode": "789", "price_cents": 700}, "escpos50x30")
        self.assertIn(b'"ArrozP4"', label_bytes({"id": "x", "name": 'Arroz"\\\nP4',
                                                "barcode": "789", "price_cents": 700}, "epl"))
        self.assertEqual(self.store.save_printer("receipt", "cups", "TM-T20", "escpos80")["driver"], "cups")
        directory = Path(self.temp.name) / "print-output"
        self.store.save_printer("receipt", "file", str(directory), "escpos58")
        self.store.open_cash(self.cashier_id, 0)
        self.store.sale(self.cashier, {"request_id": "print-1", "items": [{"product_id": self.unit_product_id,
                        "quantity_milli": 1000}], "payments": [{"method": "cash", "amount_cents": 700}]})
        result = print_once(self.store)
        self.assertEqual(result["status"], "printed")
        self.assertEqual(len(list(directory.glob("*.bin"))), 1)
        # Receipt configuration alone must never direct labels to the TM-T20.
        with self.assertRaisesRegex(TerminalError, "impressora de etiquetas"):
            self.store.queue_label(self.unit_product_id, 3)
        self.store.save_printer("label", "file", str(directory), "zpl")
        label_job = self.store.queue_label(self.unit_product_id, 3)
        self.assertEqual(label_job["quantity"], 3)
        label_work = self.store.begin_print()
        self.assertEqual(label_work["id"], label_job["id"])
        self.assertEqual(label_work["kind"], "label")
        self.assertEqual(label_work["payload"].count(b"^XA"), 3)
        self.assertNotIn(b"\x1dV", label_work["payload"])
        self.store.finish_print(label_work["id"])
        self.store.save_printer("label", "tcp", "127.0.0.1:1", "zpl")
        job = self.store.queue_label(self.unit_product_id)
        result = print_once(self.store)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(print_once(self.store), None)
        self.store.retry_print(self.manager_id, job["id"])
        self.assertEqual(self.store.print_jobs()[0]["status"], "queued")

    def test_legacy_escpos_label_settings_and_jobs_are_blocked_but_receipts_continue(self):
        with self.assertRaisesRegex(TerminalError, "TM-T20"):
            self.store.save_printer("label", "cups", "EPSON_TM_T20", "escpos50x30")
        with self.store.transaction() as conn:
            # Simulate an installation that saved this setting before the fix.
            conn.execute("INSERT INTO print_settings VALUES (?,?,?,?)",
                         ("label", "cups", "EPSON_TM_T20", "escpos50x30"))
            setting = {"driver": "file", "target": str(Path(self.temp.name) / "print")}
            blocked = self.store._queue_print(conn, "label", b"\x1b@old-label\x1dV\x00", setting)
            receipt = self.store._queue_print(conn, "receipt", b"\x1b@receipt\x1dV\x00", setting)
        with self.assertRaisesRegex(TerminalError, "TM-T20"):
            self.store.queue_label(self.unit_product_id)
        self.login()
        response = self.client.post("/api/print/labels", json={"product_id": self.unit_product_id, "quantity": 2},
                                    headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(response.status_code, 410)
        self.assertIn("painel da empresa", response.json["error"])
        work = self.store.begin_print()
        self.assertEqual(work["id"], receipt)
        preserved = next(job for job in self.store.print_jobs() if job["id"] == blocked)
        self.assertEqual(preserved["status"], "blocked")
        self.assertEqual(preserved["attempts"], 0)
        with self.assertRaisesRegex(TerminalError, "TM-T20"):
            self.store.retry_print(self.manager_id, blocked)
        self.assertEqual(len(self.store.print_jobs()), 2)

    def test_label_quantity_rejects_invalid_input_without_queueing(self):
        self.store.save_printer("label", "file", str(Path(self.temp.name) / "print"), "zpl")
        for quantity in (0, -1, 1000, 1.5, True, "3", None):
            with self.subTest(quantity=quantity):
                with self.assertRaisesRegex(TerminalError, "quantidade|Quantidade"):
                    self.store.queue_label(self.unit_product_id, quantity)
        self.assertEqual(self.store.print_jobs(), [])

    def test_sale_preview_auto_print_and_reprint_keep_the_same_issuer(self):
        issuer = {"trade_name": "Mercado Maricá", "document": "12345678000199"}
        self.store.apply_bootstrap(dict(self.bootstrap, issuer=issuer))
        self.store.save_printer("receipt", "file", str(Path(self.temp.name) / "print"), "escpos80")
        self.store.open_cash(self.cashier_id, 0)
        request = {"request_id": "receipt-identity", "items": [{"product_id": self.unit_product_id,
                   "quantity_milli": 1000}], "payments": [{"method": "cash", "amount_cents": 1000}]}
        sale = self.store.sale(self.cashier, request)
        self.assertEqual(sale["receipt"]["columns"], 48)
        self.assertEqual(sale["issuer"], issuer)
        self.assertEqual(sale["operator_name"], "Ana")
        auto_job = self.store.begin_print()
        self.assertEqual(auto_job["payload"], receipt_bytes(sale, 80))
        self.store.finish_print(auto_job["id"])
        self.store.apply_bootstrap(dict(self.bootstrap, issuer={"trade_name": "Nome alterado"}))
        detail = self.store.sale_detail(sale["id"])
        self.assertEqual(detail["issuer"], issuer)
        self.assertEqual(detail["receipt"], sale["receipt"])
        retry = self.store.sale(self.cashier, request)
        self.assertEqual(retry["receipt"], sale["receipt"])
        self.assertEqual(len(self.store.print_jobs()), 1)  # request retry cannot print twice
        self.store.reprint(sale["id"], self.cashier_id)
        reprint = self.store.begin_print()
        self.assertEqual(reprint["payload"], auto_job["payload"])
        cancelled = self.store.void_sale(self.manager, sale["id"], "Teste de cancelamento")
        self.assertIn(b"VENDA CANCELADA", receipt_bytes(cancelled))
        self.assertEqual(self.store.sale_detail(sale["id"])["receipt"], cancelled["receipt"])

    @patch.dict(os.environ, {"PDV_PRINT_BRIDGE_URL": ""})
    @patch("terminal.printers.shutil.which", return_value="/usr/bin/lp")
    @patch("terminal.printers.subprocess.run")
    def test_cups_transport_sends_raw_escpos_to_native_queue(self, run, _which):
        run.return_value.returncode = 0
        self.assertEqual(send_bytes("cups", "TM-T20", b"ESC/POS", "job-1"), "TM-T20")
        run.assert_called_once_with(["lp", "-d", "TM-T20", "-o", "raw"], input=b"ESC/POS",
                                    stdout=ANY, stderr=ANY,
                                    timeout=20, check=False)

    def test_cloud_ack_order_retry_and_bootstrap_pagination(self):
        self.store.open_cash(self.cashier_id, 0)
        self.store.sale(self.cashier, {"request_id": "sync-sale", "items": [{"product_id": self.unit_product_id,
                        "quantity_milli": 1000}], "payments": [{"method": "cash", "amount_cents": 700}]})
        events = self.store.pending_events()
        ids = [e["event_id"] for e in events]
        with patch("terminal.sync._request", return_value={"accepted_event_ids": ids[::-1]}):
            with self.assertRaisesRegex(TerminalError, "lote completo"):
                sync_once(self.store)
        self.assertEqual(len(self.store.pending_events()), 2)
        with patch("terminal.sync._request", side_effect=TerminalError("Resposta perdida", 503)):
            with self.assertRaises(TerminalError):
                sync_once(self.store)
        self.assertEqual(len(self.store.pending_events()), 2)
        with patch("terminal.sync._request", return_value={"accepted_event_ids": ids}) as sender:
            self.assertEqual(sync_once(self.store), {"sent": 2, "remaining": 0})
            self.assertEqual(sender.call_args.kwargs["json"]["events"], events)
        self.assertEqual(sync_once(self.store), {"sent": 0, "remaining": 0})

        new_product = dict(self.products[0], id=str(uuid.uuid4()), sku="NOVO", barcode="7899999999999")
        first = dict(self.bootstrap, products=[self.products[0]], next_cursor="1")
        # The cloud response reflects the sale that was acknowledged above.
        second = dict(self.bootstrap, products=[dict(self.products[1], quantity_milli=2000), new_product], next_cursor=None)
        with patch("terminal.sync._request", side_effect=[first, second,
                {"release": None, "public_key": self.public}]) as request:
            result = refresh_bootstrap(self.store)
        self.assertEqual(result["products"], 3)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(next(x for x in self.store.products() if x["id"] == self.unit_product_id)["stock_milli"], 2000)

    def test_signed_release_notice_never_downloads_package(self):
        payload = {"schema": 1, "purpose": "pdv2-release", "component": "terminal",
                   "channel": "stable", "version": "2.0.1",
                   "url": "https://updates.example.test/terminal.zip", "sha256": "a" * 64,
                   "size_bytes": 1200, "notes": "Correções", "published_at": "2026-09-29T00:00:00Z"}
        envelope = sign_release(payload, self.private)
        with patch("terminal.sync._request", return_value={"release": envelope, "public_key": self.public}) as request:
            self.assertEqual(check_release(self.store)["version"], "2.0.1")
            self.assertEqual(request.call_count, 1)
        self.assertEqual(self.store.sync_status()["release"]["url"], payload["url"])
        bad = copy.deepcopy(envelope)
        bad["payload"]["version"] = "2.0.2"
        with patch("terminal.sync._request", return_value={"release": bad, "public_key": self.public}):
            with self.assertRaisesRegex(TerminalError, "assinatura válida"):
                check_release(self.store)
        with patch("terminal.sync._request", return_value={"release": None, "public_key": self.public}):
            self.assertIsNone(check_release(self.store))
        self.assertIsNone(self.store.sync_status()["release"])


if __name__ == "__main__":
    unittest.main()
