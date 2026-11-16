"""Fase 2 de ponta a ponta, sem dublê: nuvem Django de verdade, terminal de verdade e terminal/mirror.py de verdade.

Réplica em segundo disco -> perda do banco -> "Recuperar minha loja" -> importação da fila sem duplicar,
mais os caminhos de volta ("Desistir da recuperação") e a segunda troca de computador.
"""

import json
import os
import shutil
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

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connections
from django.test import Client
from django.utils import timezone

from core.models import (AuditEvent, Membership, Plan, Product, Sale, Shop, StockMovement, Subscription, SyncConflict,
                         SyncEvent, Tenant, Terminal, TerminalFormerToken)
from shared.licensing import generate_keypair
from shared.security import hash_pin
from terminal import mirror, sync
from terminal.app import create_app
from terminal.store import TerminalError

CLOUD_URL = "http://127.0.0.1:8000"
PIN = "543210"


class RealPhaseTwoCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.old_database = settings.DATABASES["default"]["NAME"]
        connections.close_all()
        settings.DATABASES["default"]["NAME"] = str(Path(cls.temp.name) / "cloud.sqlite3")
        connections.databases["default"]["NAME"] = settings.DATABASES["default"]["NAME"]
        call_command("migrate", interactive=False, verbosity=0)
        private, public = generate_keypair()
        cls.environment = patch.dict(os.environ, {
            "PDV_LICENSE_PRIVATE_KEY": private, "PDV_LICENSE_PUBLIC_KEY": public,
            "PDV_LICENSE_PRIVATE_KEY_FILE": "", "PDV_LICENSE_PUBLIC_KEY_FILE": "",
            # With the server key the cloud hands the company's replica key to the terminal.
            "PDV_ENCRYPTION_KEY": Fernet.generate_key().decode(), "PDV_ENCRYPTION_KEY_FILE": "",
            "PDV_ENV": "test"})
        cls.environment.start()

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()
        connections.close_all()
        settings.DATABASES["default"]["NAME"] = cls.old_database
        connections.databases["default"]["NAME"] = cls.old_database
        cls.temp.cleanup()

    def setUp(self):
        for registry in (mirror._SNAPSHOT_REQUESTS, mirror._SNAPSHOT_RETRY_AT, mirror._NEXT_DUE,
                         mirror._JOURNAL_CACHE):
            registry.clear()
        self.folder = Path(tempfile.mkdtemp(dir=self.temp.name))
        tag = uuid.uuid4().hex[:8]
        self.user = get_user_model().objects.create_user(username=f"dona-{tag}@example.test", password="Strong-42!",
                                                         email=f"dona-{tag}@example.test")
        self.tenant = Tenant.objects.create(name=f"Mercearia {tag}")
        self.member = Membership.objects.create(tenant=self.tenant, user=self.user, name="Dona", role="owner",
                                                pin_hash=hash_pin(PIN))
        self.shop = Shop.objects.create(tenant=self.tenant, name="Loja principal")
        self.subscription = Subscription.objects.create(
            tenant=self.tenant, plan=Plan.objects.get(slug="essential"), status="trial",
            trial_ends_at=timezone.now() + timedelta(days=14))
        self.product = Product.objects.create(tenant=self.tenant, sku="CAFE", name="Café", price_cents=1500)
        StockMovement.objects.create(tenant=self.tenant, shop=self.shop, product=self.product, delta_milli=90000,
                                     source="manual", reason="Carga inicial")
        self.panel = Client()
        self.panel.force_login(self.user)
        created = self.panel_call("POST", "/api/v1/terminals", {"shop_id": str(self.shop.id), "name": "Caixa 1"})
        self.terminal = Terminal.objects.get(pk=created["id"])
        self.first_code = created["pairing_code"]
        self.sold = []

    # ----- helpers -----
    def panel_call(self, verb, path, body=None, expect=(200, 201)):
        response = self.panel.generic(verb, path, data=json.dumps(body if body is not None else {}),
                                      content_type="application/json", HTTP_X_TENANT_ID=str(self.tenant.id))
        self.assertIn(response.status_code, expect, response.content)
        return response.json()

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

    def offline(self):
        return patch.object(sync, "_request", side_effect=TerminalError(
            "Sem conexão com o servidor; dados locais preservados", 503))

    def computer(self, name):
        """A whole terminal (Flask application and its database) in its own folder."""
        directory = self.folder / name
        app = create_app({"TESTING": True, "DATA_DIR": str(directory), "START_WORKER": False})
        client = app.test_client()
        return types.SimpleNamespace(app=app, client=client, store=app.extensions["terminal_store"],
                                     directory=directory)

    def post(self, computer, path, payload=None):
        csrf = computer.client.get("/api/session").json["csrf"]
        return computer.client.post(path, json=payload or {}, headers={"X-CSRF-Token": csrf})

    def first_computer(self, name="antigo"):
        computer = self.computer(name)
        with self.cloud():
            sync.pair(computer.store, CLOUD_URL, self.subscription.license_key, self.first_code)
        self.assertFalse(computer.store.restore_status()["active"])
        computer.operator = computer.store.login(str(self.member.id), PIN)
        return computer

    def sell(self, computer, quantity=1):
        operator = getattr(computer, "operator", None) or computer.store.login(str(self.member.id), PIN)
        sale = computer.store.sale(operator, {
            "request_id": "venda-" + uuid.uuid4().hex,
            "items": [{"product_id": str(self.product.id), "quantity_milli": quantity * 1000}],
            "payments": [{"method": "cash", "amount_cents": quantity * 1500}]})
        self.sold.append(sale["id"])
        return sale

    def recover_on(self, name):
        """"Trocar de computador" in the panel and "Recuperar minha loja" on a new computer."""
        transfer = self.panel_call("POST", f"/api/v1/terminals/{self.terminal.id}/transfer")
        computer = self.computer(name)
        with self.cloud():
            answer = self.post(computer, "/api/restore/start", {
                "cloud_url": CLOUD_URL, "license_key": transfer["license_key"],
                "code": transfer["recovery_code"].replace("-", "").lower()})
        self.assertEqual(answer.status_code, 200, answer.json)
        return computer, answer.json

    def step(self, computer, until):
        with self.cloud():
            for _ in range(30):
                restore = self.post(computer, "/api/restore/step").json["restore"]
                if restore["state"] == until:
                    return restore
        self.fail(f"a recuperação não chegou a {until}: {restore}")

    def cloud_sales(self):
        return sorted(str(pk) for pk in Sale.objects.filter(terminal=self.terminal).values_list("pk", flat=True))

    def local_sales(self, computer):
        with computer.store.connection() as conn:
            return sorted(row["id"] for row in conn.execute("SELECT id FROM sales"))


class ReplicaRecoveryTests(RealPhaseTwoCase):
    def test_replica_database_lost_and_queue_imported_without_duplicates(self):
        old = self.first_computer()
        pendrive = self.folder / "pendrive"
        pendrive.mkdir()
        self.assertIsNotNone(json.loads(old.store.meta()["mirror_key"]))  # delivered by the real cloud
        self.assertEqual(mirror.configure(old.store, str(pendrive), allow_same_volume=True)["state"], "ok")

        old.store.open_cash(old.operator["id"], 10000)
        self.sell(old), self.sell(old, 2)
        with self.cloud():
            self.assertEqual(sync.sync_once(old.store)["remaining"], 0)
        self.assertEqual(len(self.cloud_sales()), 2)

        # The internet drops; three more sales stay only on this computer and on the pendrive.
        stranded = [self.sell(old)["id"], self.sell(old, 3)["id"], self.sell(old)["id"]]
        waiting = old.store.pending_events(50)
        self.assertEqual([event["type"] for event in waiting], ["sale.completed"] * 3)
        # One of them did reach the cloud, but the confirmation never came back.
        with self.cloud():
            sync._request("POST", CLOUD_URL + "/api/v1/terminal/sync", headers=sync._auth_headers(old.store.meta()),
                          json={"events": waiting[:1]})
        self.assertEqual(len(old.store.pending_events(50)), 3)
        status = mirror.tick(old.store)
        self.assertEqual((status["state"], status["pending_events"]), ("ok", 0))
        old_device, old_sales = old.store.identity()["device_id"], self.local_sales(old)
        with old.store.connection() as conn:
            journal = [json.loads(row["payload"]) for row in conn.execute("SELECT payload FROM outbox ORDER BY seq")]
        self.assertEqual(journal[3:], waiting)

        # The computer breaks: its database is gone for good.
        shutil.rmtree(old.directory)

        new, started = self.recover_on("novo")
        self.assertEqual((started["paired"], started["recovery"]), (True, True))
        self.assertEqual(new.store.identity()["terminal_id"], str(self.terminal.id))
        restore = self.step(new, "awaiting_backup")
        self.assertEqual(restore["counts"]["sales"], 3)      # only what the cloud had
        self.assertEqual(len(self.local_sales(new)), 3)

        found = new.client.get("/api/restore/backups?directory=" + str(pendrive)).json
        self.assertEqual(found["supported"], True)
        self.assertEqual([(copy["replica_id"], copy["matches"], copy["current_device"], copy["directory"])
                          for copy in found["candidates"]], [(old_device, True, False, str(pendrive))])

        answer = self.post(new, "/api/restore/import-backup", {"directory": str(pendrive), "replica_id": old_device})
        self.assertEqual(answer.status_code, 200, answer.json)
        body = answer.json
        # The diary cannot know what the cloud confirmed after the last copy of the database: every
        # event written since then comes back (the opening, the two delivered sales and the three
        # stranded ones) and the cloud tells them apart by event_id.
        self.assertEqual((body["imported"], body["already_present"], body["foreign"], body["invalid"],
                          body["warnings"], body["restore"]["state"]), (6, 0, 0, 0, [], "importing"))
        # The queue came back byte for byte: same ids, same order.
        self.assertEqual(new.store.pending_events(50), journal)
        # Reading the same copy again brings nothing twice.
        again = self.post(new, "/api/restore/import-backup", {"directory": str(pendrive), "replica_id": old_device})
        self.assertEqual((again.status_code, again.json["imported"], again.json["already_present"]), (200, 0, 6))
        self.assertEqual(len(new.store.pending_events(50)), 6)
        events_before, conflicts_before = (SyncEvent.objects.filter(terminal=self.terminal).count(),
                                           SyncConflict.objects.filter(terminal=self.terminal).count())
        self.assertEqual(events_before, 4)  # opening, two sales and the one whose confirmation was lost

        restore = self.step(new, "done")
        self.assertEqual((restore["pass"], restore["active"], restore["counts"]["sales"]), (2, False, 5))
        self.assertEqual(new.store.pending_events(50), [])
        # Nothing lost and nothing doubled, in the cloud and on the new computer.
        self.assertEqual(self.cloud_sales(), sorted(self.sold))
        self.assertEqual(self.local_sales(new), old_sales)
        for event in journal:
            self.assertEqual(SyncEvent.objects.filter(event_id=event["event_id"]).count(), 1)
        self.assertEqual(SyncEvent.objects.filter(terminal=self.terminal).count(), 6)
        self.assertEqual(SyncConflict.objects.filter(terminal=self.terminal).count(), conflicts_before)
        self.assertEqual(sum(Sale.objects.filter(terminal=self.terminal).values_list("total_cents", flat=True)),
                         8 * 1500)
        for sale_id in stranded:
            self.assertEqual(new.store.sale_detail(sale_id)["status"], "completed")
        # The shop sells again on the new computer, in the cashier that was open.
        self.assertIsNotNone(new.store.cash_state())
        resumed = self.sell(new)
        with self.cloud():
            self.assertEqual(sync.sync_once(new.store)["remaining"], 0)
        self.assertTrue(Sale.objects.filter(pk=resumed["id"]).exists())

    def test_cashier_that_never_synchronized_recovers_from_a_damaged_copy_and_a_second_disk(self):
        old = self.first_computer()
        pendrive, spare = self.folder / "pendrive", self.folder / "segundo-pendrive"
        pendrive.mkdir()
        self.assertEqual(mirror.configure(old.store, str(pendrive), allow_same_volume=True)["state"], "ok")
        # Paired and then always without internet: nothing ever reached the cloud.
        old.store.open_cash(old.operator["id"], 0)
        for _ in range(3):
            self.sell(old)
        self.assertEqual(mirror.tick(old.store)["pending_events"], 0)
        self.assertEqual(SyncEvent.objects.filter(terminal=self.terminal).count(), 0)
        waiting = old.store.pending_events(50)
        old_device = old.store.identity()["device_id"]
        shutil.copytree(pendrive, spare)  # the shop kept a second copy
        journal = sorted(pendrive.rglob("journal-*" + mirror.JOURNAL_SUFFIX))[0]
        lines = journal.read_bytes().split(b"\n")
        damaged = bytearray(lines[1])
        damaged[len(damaged) // 2] ^= 0x01  # one flipped bit in the first sale
        lines[1] = bytes(damaged)
        journal.write_bytes(b"\n".join(lines))
        shutil.rmtree(old.directory)

        new, started = self.recover_on("novo")
        self.assertIs(started["recovery"], True)  # a replaced computer always starts in recovery
        restore = self.step(new, "awaiting_backup")
        self.assertEqual((restore["counts"]["sales"], restore["percent"]), (0, 100))

        first = self.post(new, "/api/restore/import-backup", {"directory": str(pendrive), "replica_id": old_device})
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(first.json["warnings"], [mirror.MSG_BAD_LINE])   # said, not swallowed
        imported = first.json["imported"]
        self.assertEqual((imported, first.json["restore"]["state"]), (len(waiting) - 1, "importing"))
        # The second disk still has the sale the first one lost, and it can be read now.
        listed = new.client.get("/api/restore/backups?directory=" + str(spare))
        self.assertEqual(listed.status_code, 200, listed.json)
        second = self.post(new, "/api/restore/import-backup", {"directory": str(spare), "replica_id": old_device})
        self.assertEqual(second.status_code, 200, second.json)
        self.assertEqual((second.json["imported"], second.json["already_present"], second.json["warnings"]),
                         (1, imported, []))
        self.assertEqual(new.store.restore_status()["import"]["imported"], len(waiting))
        self.assertEqual(sorted(event["event_id"] for event in new.store.pending_events(50)),
                         sorted(event["event_id"] for event in waiting))

        restore = self.step(new, "done")
        self.assertEqual(restore["counts"]["sales"], 3)
        self.assertEqual(self.cloud_sales(), sorted(self.sold))
        self.assertEqual(self.local_sales(new), sorted(self.sold))
        actions = [row["action"] for row in new.store.audit()]
        self.assertIn("restore.mirror_warnings", actions)

    def test_database_erased_on_the_same_computer_does_not_offer_its_own_empty_copy_first(self):
        old = self.first_computer()
        pendrive = self.folder / "pendrive"
        pendrive.mkdir()
        self.assertEqual(mirror.configure(old.store, str(pendrive), allow_same_volume=True)["state"], "ok")
        old.store.open_cash(old.operator["id"], 0)
        self.sell(old), self.sell(old)
        mirror.tick(old.store)
        old_device = old.store.identity()["device_id"]
        # The database is erased but the folder (and mirror.json, still switched on) stays.
        for path in old.directory.glob("terminal.sqlite3*"):
            path.unlink()

        new, _started = self.recover_on("antigo")
        self.assertNotEqual(new.store.identity()["device_id"], old_device)
        self.assertTrue(mirror.is_enabled(new.store))
        # While the recovery runs the new database writes no copy of its own...
        mirror.tick(new.store)
        self.step(new, "awaiting_backup")
        mirror.tick(new.store)
        found = new.client.get("/api/restore/backups?directory=" + str(pendrive)).json["candidates"]
        self.assertEqual([copy["replica_id"] for copy in found], [old_device])
        answer = self.post(new, "/api/restore/import-backup", {"directory": str(pendrive), "replica_id": old_device})
        self.assertEqual((answer.status_code, answer.json["imported"]), (200, 3))  # cash.opened and two sales
        self.step(new, "done")
        self.assertEqual(self.cloud_sales(), sorted(self.sold))
        # ...and afterwards it protects the shop again; the old copy still comes first in the list.
        self.assertEqual(mirror.tick(new.store)["state"], "ok")
        listed = mirror.candidates(new.store.identity(), str(pendrive))
        self.assertEqual([(copy["replica_id"] == old_device, copy["current_device"]) for copy in listed],
                         [(True, False), (False, True)])


class GiveUpAndSecondSwapTests(RealPhaseTwoCase):
    def test_giving_up_the_recovery_gives_the_cashier_back_to_the_computer_that_works(self):
        old = self.first_computer()
        old.store.open_cash(old.operator["id"], 0)
        self.sell(old)
        with self.cloud():
            sync.sync_once(old.store)
        # "Trocar de computador" was clicked on the wrong cashier.
        new, _started = self.recover_on("engano")
        wrong_device = new.store.identity()["device_id"]
        with self.cloud():
            with self.assertRaises(TerminalError):
                sync.heartbeat_once(old.store)
        self.assertEqual((old.store.lease_status()["state"], old.store.lease_status()["can_sell"]),
                         ("transferred", False))

        with self.cloud():
            answer = self.post(new, "/api/restore/abandon", {"confirm": True, "manager_pin": PIN})
        self.assertEqual(answer.status_code, 200, answer.json)
        self.assertEqual((answer.json["paired"], answer.json["released"]), (False, True))
        self.assertNotEqual(new.store.identity()["device_id"], wrong_device)
        self.terminal.refresh_from_db()
        self.assertEqual((self.terminal.device_id, self.terminal.transferred_at, self.terminal.previous_device_id),
                         (old.store.identity()["device_id"], None, ""))
        self.assertTrue(AuditEvent.objects.filter(tenant=self.tenant, action="terminal.transfer_undone").exists())

        # The right computer notices by itself (the worker asks now and then) and sells again.
        with self.cloud():
            sync.Worker(old.store).run_once()
        status = old.store.lease_status()
        self.assertEqual((status["state"], status["can_sell"]), ("active", True))
        sale = self.sell(old)
        with self.cloud():
            self.assertEqual(sync.sync_once(old.store)["remaining"], 0)
        self.assertTrue(Sale.objects.filter(pk=sale["id"]).exists())

        # And the computer that gave up can now recover the cashier that really broke.
        Plan.objects.filter(pk=self.subscription.plan_id).update(terminal_limit=5)
        other = self.panel_call("POST", "/api/v1/terminals", {"shop_id": str(self.shop.id), "name": "Caixa 2"})
        with self.cloud():
            paired = self.post(new, "/api/pair", {"cloud_url": CLOUD_URL,
                                                  "license_key": self.subscription.license_key,
                                                  "code": other["pairing_code"]})
        self.assertEqual(paired.status_code, 200, paired.json)

    def test_second_swap_after_giving_up_offline_keeps_the_old_computer_sending(self):
        old = self.first_computer()
        old.store.open_cash(old.operator["id"], 0)
        late = [self.sell(old)["id"], self.sell(old)["id"]]  # in the queue, the computer is offline
        middle, _started = self.recover_on("meio")
        with self.offline():  # gives up without internet: the cloud never hears about it
            answer = self.post(middle, "/api/restore/abandon", {"confirm": True, "manager_pin": PIN})
        self.assertEqual((answer.status_code, answer.json["released"]), (200, False))
        new, _started = self.recover_on("novo")
        self.step(new, "awaiting_backup")
        self.assertEqual(self.post(new, "/api/restore/skip-backup").status_code, 200)
        self.assertEqual(TerminalFormerToken.objects.filter(terminal=self.terminal).count(), 2)

        # The real old computer comes back online days later.
        with self.cloud():
            errors = sync.Worker(old.store).run_once()
        self.assertTrue(old.store.is_transferred(), errors)
        status = old.store.lease_status()
        self.assertEqual((status["state"], status["can_sell"], status["send_only_closed"]),
                         ("transferred", False, False))
        self.assertIsNotNone(status["send_only_until"])
        with self.assertRaisesRegex(TerminalError, "transferido"):
            self.sell(old)
        self.sold.pop()
        with self.cloud():
            sync.Worker(old.store).run_once()
        self.assertEqual(old.store.pending_events(50), [])
        self.assertEqual(self.cloud_sales(), sorted(late))

        # "Encerrar envio" in the panel: the old computer stops insisting and says so.
        self.assertEqual(self.post(old, "/api/login", {"operator_id": str(self.member.id), "pin": PIN}).status_code,
                         200)
        with old.store.transaction() as conn:
            old.store._event(conn, "stock.adjusted", str(uuid.uuid4()),
                             {"product_id": str(self.product.id), "delta_milli": 1000, "reason": "Contagem",
                              "created_at": 1})
        self.panel_call("PATCH", f"/api/v1/terminals/{self.terminal.id}", {"end_send_only": True})
        with self.cloud():
            with self.assertRaises(TerminalError):
                sync.sync_once(old.store)
        status = old.client.get("/api/session").json["license"]
        self.assertEqual((status["send_only_closed"], status["send_only_until"], status["pending_events"]),
                         (True, None, 1))
        self.assertIn("prazo para este computador enviar terminou", status["reason"])
        with patch.object(sync, "_request", side_effect=AssertionError("não deve reenviar")) as transport:
            worker = sync.Worker(old.store)
            worker.transfer_probe_at = __import__("time").monotonic()
            self.assertEqual(worker.run_once(), [])
        transport.assert_not_called()
        self.assertEqual(len(old.store.pending_events(50)), 1)  # kept, never dropped


if __name__ == "__main__":
    unittest.main()
