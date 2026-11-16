"""Contrato da fase 2 de ponta a ponta: nuvem Django de verdade, computador antigo e computador novo.

Só roda depois da união das frentes: precisa da rota `terminal/restore` da nuvem (frente A).
"""

import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "cloud"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pdvcloud.settings")
os.environ.setdefault("PDV_ENV", "test")
os.environ.setdefault("PDV_DEBUG", "1")

import django
django.setup()

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connections
from django.test import Client
from django.utils import timezone

from core import views_terminal
from core.models import (CashSession, Customer, Membership, Plan, Product, Sale, SaleReturn, Shop, StockMovement,
                         Subscription, Tenant, Terminal)
from shared.licensing import generate_keypair
from shared.security import hash_pin
from terminal import sync
from terminal.app import create_app
from terminal.store import RESTORE_BLOCKED, TerminalError

RECOVERY_CODE = "K7QD-M2XP-9HTW"  # canonical form: what the cloud hashes on "Trocar de computador"


@unittest.skipUnless(hasattr(views_terminal, "restore"), "a nuvem ainda não tem a rota terminal/restore (frente A)")
class RestoreContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.old_database = settings.DATABASES["default"]["NAME"]
        connections.close_all()
        settings.DATABASES["default"]["NAME"] = str(Path(cls.temp.name) / "cloud.sqlite3")
        connections.databases["default"]["NAME"] = settings.DATABASES["default"]["NAME"]
        call_command("migrate", interactive=False, verbosity=0)
        cls.private, cls.public = generate_keypair()
        cls.old_private = os.environ.get("PDV_LICENSE_PRIVATE_KEY")
        cls.old_public = os.environ.get("PDV_LICENSE_PUBLIC_KEY")
        os.environ["PDV_LICENSE_PRIVATE_KEY"] = cls.private
        os.environ["PDV_LICENSE_PUBLIC_KEY"] = cls.public

    @classmethod
    def tearDownClass(cls):
        connections.close_all()
        settings.DATABASES["default"]["NAME"] = cls.old_database
        connections.databases["default"]["NAME"] = cls.old_database
        for name, old in (("PDV_LICENSE_PRIVATE_KEY", cls.old_private), ("PDV_LICENSE_PUBLIC_KEY", cls.old_public)):
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        cls.temp.cleanup()

    def cloud(self):
        """sync._request against the Django test client, raising exactly what the real transport raises."""
        client = Client()

        def request(method, url, **kwargs):
            parsed = urlparse(url)
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
            headers = kwargs.get("headers") or {}
            data = json.dumps(kwargs["json"]) if "json" in kwargs else None
            response = client.generic(method, path, data=data, content_type="application/json",
                                      HTTP_AUTHORIZATION=headers.get("Authorization", ""),
                                      HTTP_X_PDV_CAPABILITIES=headers.get("X-PDV-Capabilities", ""))
            return sync._json_response(types.SimpleNamespace(
                ok=response.status_code < 400, status_code=response.status_code, json=response.json))
        return patch.object(sync, "_request", side_effect=request)

    def computer(self, name):
        directory = Path(self.temp.name) / name
        app = create_app({"TESTING": True, "DATA_DIR": str(directory), "DB_PATH": directory / "terminal.sqlite3",
                          "START_WORKER": False})
        return app.extensions["terminal_store"]

    def rows(self, store, table):
        with store.connection() as conn:
            return sorted((dict(row) for row in conn.execute(f"SELECT * FROM {table}")), key=lambda row: row["id"])

    def test_change_of_computer_restores_the_shop_and_the_old_one_only_sends(self):
        user = get_user_model().objects.create_user(username="troca@example.test", password="Strong-42!",
                                                    email="troca@example.test")
        tenant = Tenant.objects.create(name="Mercearia da troca")
        member = Membership.objects.create(tenant=tenant, user=user, name="Gerente", role="manager",
                                           pin_hash=hash_pin("543210"))
        shop = Shop.objects.create(tenant=tenant, name="Loja principal")
        subscription = Subscription.objects.create(tenant=tenant, plan=Plan.objects.get(slug="essential"),
                                                   status="trial", trial_ends_at=timezone.now() + timedelta(days=14))
        product = Product.objects.create(tenant=tenant, sku="QUEIJO", name="Queijo", unit="KG", price_cents=1299)
        StockMovement.objects.create(tenant=tenant, shop=shop, product=product, delta_milli=50000,
                                     source="manual", reason="Carga inicial")
        customer = Customer.objects.create(tenant=tenant, name="Dona Rosa", credit_limit_cents=50000)
        first_code = "codigo-do-primeiro-pc"
        terminal = Terminal.objects.create(tenant=tenant, shop=shop, name="Caixa 1",
                                           pairing_code_hash=hashlib.sha256(first_code.encode()).hexdigest(),
                                           pairing_expires_at=timezone.now() + timedelta(minutes=10))

        # ----- the old computer works for a day -----
        old = self.computer("antigo")
        with self.cloud():
            sync.pair(old, "http://127.0.0.1:8000", subscription.license_key, first_code)
        self.assertFalse(old.restore_status()["active"])  # first pairing: nothing to recover
        self.assertIn("restore", json.loads(old.meta()["cloud_capabilities"]))
        operator = old.login(str(member.id), "543210")
        printer_target = str(Path(self.temp.name) / "cupons")
        old.save_printer("receipt", "file", printer_target, "escpos80", operator["id"])
        old.open_cash(operator["id"], 10000)

        def sell(store, request_id, quantity, payments, customer_id=None):
            request = {"request_id": request_id, "items": [{"product_id": str(product.id),
                                                             "quantity_milli": quantity}], "payments": payments}
            if customer_id:
                request["customer_id"] = customer_id
            return store.sale(operator, request)

        cash_sale = sell(old, "troca-1", 1000, [{"method": "cash", "amount_cents": 1299}])
        voided = sell(old, "troca-2", 2000, [{"method": "cash", "amount_cents": 2598}])
        old.void_sale(operator, voided["id"], "Cliente desistiu")
        fiado = sell(old, "troca-3", 2000, [{"method": "credit_account", "amount_cents": 2598}], str(customer.id))
        old.return_sale(operator, fiado["id"], {"return_id": str(uuid.uuid4()), "refund_method": "account",
                        "reason": "Avaria", "items": [{"product_id": str(product.id), "quantity_milli": 1000}]})
        old.cash_movement(operator["id"], "withdrawn", 500, "Sangria")
        with self.cloud():
            self.assertEqual(sync.sync_once(old)["remaining"], 0)
        # One more sale that the old computer had not sent when it was replaced.
        late = sell(old, "troca-atrasada", 1000, [{"method": "cash", "amount_cents": 1299}])
        self.assertEqual(Sale.objects.filter(terminal=terminal).count(), 3)

        # ----- "Trocar de computador": same Terminal, a recovery code, typed carelessly on the new computer -----
        terminal.refresh_from_db()
        terminal.pairing_code_hash = hashlib.sha256(RECOVERY_CODE.encode()).hexdigest()
        terminal.pairing_expires_at = timezone.now() + timedelta(minutes=30)
        terminal.save(update_fields=["pairing_code_hash", "pairing_expires_at"])
        new = self.computer("novo")
        with self.cloud():
            sync.pair(new, "http://127.0.0.1:8000", subscription.license_key, " k7qdm2xp9htw ")
        self.assertEqual(new.identity()["terminal_id"], str(terminal.id))
        self.assertNotEqual(new.identity()["device_id"], old.identity()["device_id"])
        status = new.restore_status()
        self.assertEqual((status["active"], status["state"]), (True, "pending"))
        with self.assertRaises(TerminalError) as caught:
            new.open_cash(operator["id"], 0)
        self.assertEqual(str(caught.exception), RESTORE_BLOCKED)
        balances = {"products": self.rows(new, "products"), "customers": self.rows(new, "customers")}

        with self.cloud():
            status = sync.restore_step(new)
        self.assertEqual((status["state"], status["error"], status["percent"]), ("awaiting_backup", "", 100))
        counts = status["counts"]
        self.assertEqual(counts["sales"], Sale.objects.filter(terminal=terminal).count())
        self.assertEqual(counts["returns"], SaleReturn.objects.filter(sale__terminal=terminal).count())
        self.assertEqual(counts["cash_sessions"], CashSession.objects.filter(terminal=terminal).count())
        self.assertEqual((counts["cash_movements"], counts["cash_movements_cents"], counts["config"]), (1, 500, 1))
        self.assertEqual(counts["sales_cents"], 1299 + 2598 + 2598)
        self.assertEqual(new.restore_skip_backup()["state"], "done")

        # The new computer holds what the cloud had, and nothing was generated or recalculated on the way.
        self.assertEqual([row["id"] for row in self.rows(new, "sales")],
                         sorted([cash_sale["id"], voided["id"], fiado["id"]]))
        self.assertNotIn(late["id"], [row["id"] for row in self.rows(new, "sales")])
        self.assertEqual(new.sale_detail(voided["id"])["status"], "voided")
        self.assertEqual(new.sale_detail(fiado["id"])["items"], old.sale_detail(fiado["id"])["items"])
        self.assertEqual(new.sale_detail(fiado["id"])["payments"], old.sale_detail(fiado["id"])["payments"])
        for table in ("cash_sessions", "cash_movements", "sale_returns"):
            self.assertEqual(self.rows(new, table), self.rows(old, table), table)
        self.assertEqual(new.printer_settings(),
                         [{"kind": "receipt", "driver": "file", "target": printer_target, "format": "escpos80"}])
        self.assertEqual(new.pending_events(), [])
        self.assertEqual({"products": self.rows(new, "products"), "customers": self.rows(new, "customers")}, balances)
        self.assertEqual(new.customers("Rosa")[0]["open_credit_cents"], 1299)
        key = json.loads(new.meta().get("mirror_key") or "null")
        if key is not None:  # the cloud only issues it with PDV_ENCRYPTION_KEY configured
            self.assertEqual((len(key["key"]), len(key["key_id"])), (44, 16))

        # The cashier that was open came back open, and the shop sells again with the new token.
        self.assertEqual(new.cash_state()["id"], old.cash_state()["id"])
        resumed = sell(new, "troca-novo-pc", 1000, [{"method": "cash", "amount_cents": 1299}])
        with self.cloud():
            self.assertEqual(sync.sync_once(new)["remaining"], 0)
            sync.heartbeat_once(new)
        self.assertFalse(new.is_transferred())
        self.assertTrue(Sale.objects.filter(pk=resumed["id"]).exists())

        # ----- the old computer: refused everywhere except to deliver its queue -----
        with self.cloud():
            with self.assertRaises(TerminalError) as refused:
                sync.heartbeat_once(old)
        self.assertEqual(refused.exception.remote_code, "terminal_transferred")
        self.assertEqual(old.lease_status()["state"], "transferred")
        with self.assertRaisesRegex(TerminalError, "transferido"):
            sell(old, "troca-proibida", 1000, [{"method": "cash", "amount_cents": 1299}])
        with self.cloud():
            self.assertEqual(sync.sync_once(old)["remaining"], 0)  # send-only: the late sale still goes up
        self.assertTrue(Sale.objects.filter(pk=late["id"]).exists())
        self.assertIsNotNone(old.lease_status()["send_only_until"])
        terminal.refresh_from_db()
        self.assertEqual(terminal.previous_device_id, old.identity()["device_id"])
        self.assertEqual(terminal.config["print_settings"][0]["target"], printer_target)


if __name__ == "__main__":
    unittest.main()
