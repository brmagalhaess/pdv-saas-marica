"""Cross-stack contract: Django cloud endpoints and Flask/SQLite terminal."""

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
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

from core.models import Membership, Plan, Product, Sale, Shop, StockMovement, Subscription, SyncEvent, Tenant, Terminal
from shared.licensing import generate_keypair
from shared.security import hash_pin
from terminal.app import create_app
from terminal.store import TerminalError
from terminal.sync import pair, sync_once


class CloudContractTests(unittest.TestCase):
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
        for name, old in (("PDV_LICENSE_PRIVATE_KEY", cls.old_private),
                          ("PDV_LICENSE_PUBLIC_KEY", cls.old_public)):
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        cls.temp.cleanup()

    def test_pair_bootstrap_sale_lost_ack_void_and_central_stock(self):
        User = get_user_model()
        user = User.objects.create_user(username="contract@example.test", password="Strong-42!",
                                        email="contract@example.test")
        tenant = Tenant.objects.create(name="Mercearia contrato")
        Membership.objects.create(tenant=tenant, user=user, name="Gerente",
                                  role="manager", pin_hash=hash_pin("543210"))
        shop = Shop.objects.create(tenant=tenant, name="Loja principal")
        plan = Plan.objects.get(slug="essential")
        Subscription.objects.create(tenant=tenant, plan=plan, status="trial",
                                    trial_ends_at=timezone.now() + timedelta(days=14))
        product = Product.objects.create(tenant=tenant, sku="QUEIJO", name="Queijo",
                                         unit="KG", price_cents=1299)
        StockMovement.objects.create(tenant=tenant, shop=shop, product=product,
                                     delta_milli=5000, source="manual", reason="Carga inicial")
        code = "codigo-contrato-123"
        cloud_terminal = Terminal.objects.create(tenant=tenant, shop=shop, name="Caixa 1",
            pairing_code_hash=hashlib.sha256(code.encode()).hexdigest(),
            pairing_expires_at=timezone.now() + timedelta(minutes=10))
        client = Client()

        def django_request(method, url, **kwargs):
            parsed = urlparse(url)
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
            headers = kwargs.get("headers") or {}
            data = json.dumps(kwargs["json"]) if "json" in kwargs else None
            response = client.generic(method, path, data=data, content_type="application/json",
                HTTP_AUTHORIZATION=headers.get("Authorization", ""))
            result = response.json()
            if response.status_code >= 400:
                raise TerminalError(f"Cloud HTTP {response.status_code}: {result.get('error')}", 502)
            return result

        local_path = Path(self.temp.name) / "terminal.sqlite3"
        app = create_app({"TESTING": True, "DATA_DIR": self.temp.name,
                          "DB_PATH": local_path, "START_WORKER": False})
        store = app.extensions["terminal_store"]
        with patch("terminal.sync._request", side_effect=django_request):
            bootstrap = pair(store, "http://127.0.0.1:8000", code)
        self.assertEqual(bootstrap["products"], 1)
        self.assertEqual(store.identity()["terminal_id"], str(cloud_terminal.id))
        self.assertTrue(store.lease_status()["can_sell"])
        operator = store.login(str(Membership.objects.get(tenant=tenant).id), "543210")
        store.open_cash(operator["id"], 0)
        sale = store.sale(operator, {"request_id": "contract-sale", "items": [{"product_id": str(product.id),
                          "quantity_milli": 1250}], "payments": [{"method": "cash", "amount_cents": 1624}]})
        self.assertEqual(sale["total_cents"], 1624)
        self.assertEqual(len(store.pending_events()), 2)

        def lost_ack(method, url, **kwargs):
            response = django_request(method, url, **kwargs)
            if url.endswith("/terminal/sync"):
                raise TerminalError("Resposta perdida após commit", 503)
            return response

        with patch("terminal.sync._request", side_effect=lost_ack):
            with self.assertRaisesRegex(TerminalError, "Resposta perdida"):
                sync_once(store)
        self.assertEqual(len(store.pending_events()), 2)
        self.assertEqual(Sale.objects.filter(tenant=tenant).count(), 1)
        with patch("terminal.sync._request", side_effect=django_request):
            self.assertEqual(sync_once(store)["remaining"], 0)
        self.assertEqual(Sale.objects.filter(tenant=tenant).count(), 1)
        self.assertEqual(StockMovement.objects.filter(tenant=tenant, source="sale").count(), 1)
        self.assertEqual(StockMovement.objects.filter(tenant=tenant, product=product).aggregate(
            total=django.db.models.Sum("delta_milli"))["total"], 3750)
        store.void_sale(operator, sale["id"], "Cliente desistiu")
        with patch("terminal.sync._request", side_effect=django_request):
            self.assertEqual(sync_once(store)["remaining"], 0)
        self.assertEqual(Sale.objects.get(pk=sale["id"]).status, "cancelled")
        self.assertEqual(StockMovement.objects.filter(tenant=tenant, source="void").count(), 1)
        self.assertEqual(StockMovement.objects.filter(tenant=tenant, product=product).aggregate(
            total=django.db.models.Sum("delta_milli"))["total"], 5000)
        self.assertEqual(SyncEvent.objects.filter(tenant=tenant).count(), 3)


if __name__ == "__main__":
    unittest.main()
