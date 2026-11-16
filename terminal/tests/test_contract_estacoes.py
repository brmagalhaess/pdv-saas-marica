"""Cross-stack contract of phase 3: what an additional cashier records reaches the cloud tagged
with its station, and the license says how many additional cashiers the shop may have.

Skipped until the cloud announces the "stations" capability (it is deployed first)."""

import hashlib
import json
import os
import sys
import tempfile
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

from core.models import CashSession, Membership, Plan, Product, Sale, Shop, StockMovement, Subscription, Tenant, Terminal
from core.views_terminal import CLOUD_CAPABILITIES
from shared.licensing import generate_keypair
from shared.security import hash_pin
from terminal.app import create_app
from terminal.store import TerminalError
from terminal.sync import pair, sync_once


@unittest.skipUnless("stations" in CLOUD_CAPABILITIES, "a nuvem deste checkout ainda não anuncia 'stations'")
class StationsContractTests(unittest.TestCase):
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

    def test_station_sale_and_cashier_reach_the_cloud_with_their_station(self):
        user = get_user_model().objects.create_user(username="estacoes@example.test", password="Strong-42!",
                                                    email="estacoes@example.test")
        tenant = Tenant.objects.create(name="Mercado das estações")
        Membership.objects.create(tenant=tenant, user=user, name="Gerente", role="manager",
                                  pin_hash=hash_pin("543210"))
        shop = Shop.objects.create(tenant=tenant, name="Loja principal")
        plan = Plan.objects.get(slug="essential")
        Plan.objects.filter(pk=plan.pk).update(max_stations=2)
        subscription = Subscription.objects.create(tenant=tenant, plan=plan, status="trial",
                                                   trial_ends_at=timezone.now() + timedelta(days=14))
        product = Product.objects.create(tenant=tenant, sku="CAFE", name="Café", unit="UN", price_cents=1500)
        StockMovement.objects.create(tenant=tenant, shop=shop, product=product, delta_milli=10000,
                                     source="manual", reason="Carga inicial")
        code = "codigo-estacoes-123"
        cloud_terminal = Terminal.objects.create(
            tenant=tenant, shop=shop, name="Caixa 1", pairing_code_hash=hashlib.sha256(code.encode()).hexdigest(),
            pairing_expires_at=timezone.now() + timedelta(minutes=10))
        client = Client()

        def django_request(method, url, **kwargs):
            parsed = urlparse(url)
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
            headers = kwargs.get("headers") or {}
            data = json.dumps(kwargs["json"]) if "json" in kwargs else None
            response = client.generic(method, path, data=data, content_type="application/json",
                                      HTTP_AUTHORIZATION=headers.get("Authorization", ""),
                                      HTTP_X_PDV_CAPABILITIES=headers.get("X-PDV-Capabilities", ""))
            result = response.json()
            if response.status_code >= 400:
                raise TerminalError(f"Cloud HTTP {response.status_code}: {result.get('error')}", 502)
            return result

        app = create_app({"TESTING": True, "DATA_DIR": self.temp.name,
                          "DB_PATH": Path(self.temp.name) / "terminal.sqlite3", "START_WORKER": False})
        store = app.extensions["terminal_store"]
        with patch("terminal.sync._request", side_effect=django_request):
            pair(store, "http://127.0.0.1:8000", subscription.license_key, code)
        self.assertEqual(store.identity()["terminal_id"], str(cloud_terminal.id))

        # The license carries the number of additional cashiers of the plan.
        self.assertIn("stations", json.loads(store.meta()["cloud_capabilities"]))
        claims = json.loads(store.meta()["lease"])["payload"]
        self.assertEqual(claims["max_stations"], 2)
        self.assertEqual(store.lease_status()["max_stations"], 2)
        self.assertEqual(store.station_limit(), {"limit": 2, "used": 0, "available": 2})

        operator = store.login(str(Membership.objects.get(tenant=tenant).id), "543210")
        linked = store.enroll_station(store.create_station_code(operator["id"])["code"], name="Caixa 2")
        main_cash = store.open_cash(operator["id"], 0)
        with store.station(linked["station_id"]):
            station_cash = store.open_cash(operator["id"], 500)
            sale = store.sale(operator, {"request_id": "contrato-estacao",
                                         "items": [{"product_id": str(product.id), "quantity_milli": 2000}],
                                         "payments": [{"method": "cash", "amount_cents": 3000}]})
        main_sale = store.sale(operator, {"request_id": "contrato-principal",
                                          "items": [{"product_id": str(product.id), "quantity_milli": 1000}],
                                          "payments": [{"method": "cash", "amount_cents": 1500}]})
        with patch("terminal.sync._request", side_effect=django_request):
            for _ in range(5):
                result = sync_once(store)
                self.assertFalse(result.get("quarantined"), result)
                if not result["remaining"]:
                    break
        self.assertEqual(store.sync_status()["pending"], 0)

        cloud_cash = CashSession.objects.get(pk=station_cash["id"])
        self.assertEqual((cloud_cash.station_id, cloud_cash.station_name), (linked["station_id"], "Caixa 2"))
        cloud_sale = Sale.objects.get(pk=sale["id"])
        self.assertEqual((cloud_sale.station_id, cloud_sale.station_name, cloud_sale.total_cents),
                         (linked["station_id"], "Caixa 2", 3000))
        self.assertEqual(CashSession.objects.get(pk=main_cash["id"]).station_id, "server")
        self.assertEqual(Sale.objects.get(pk=main_sale["id"]).station_name, "Caixa principal")


if __name__ == "__main__":
    unittest.main()
