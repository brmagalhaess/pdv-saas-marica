"""Fase 2: "Trocar de computador", token só-envio, restore paginado, terminal.config
e chave da cópia de segurança."""
import hashlib
import json
import os
import re
import time
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from shared.licensing import generate_keypair
from . import views_terminal
from .models import (AuditEvent, CashSession, Customer, FinanceEntry, Membership, Plan, Product, Sale, Shop,
                     Subscription, SyncConflict, SyncEvent, Tenant, Terminal)

DAY_MS = 86400 * 1000
CODE_SHAPE = re.compile(r"[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}(-[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}){2}")


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Fase2Base(TestCase):
    def setUp(self):
        private, public = generate_keypair()
        env = patch.dict(os.environ, {"PDV_LICENSE_PRIVATE_KEY": private, "PDV_LICENSE_PUBLIC_KEY": public,
                                      "PDV_LICENSE_PRIVATE_KEY_FILE": "", "PDV_LICENSE_PUBLIC_KEY_FILE": ""})
        env.start()
        self.addCleanup(env.stop)
        User = get_user_model()
        self.owner = User.objects.create_user(username="dona@example.test", email="dona@example.test",
                                              password="Synthetic-42!")
        self.admin = User.objects.create_user(username="adm@example.test", email="adm@example.test",
                                              password="Synthetic-42!")
        self.cashier = User.objects.create_user(username="caixa@example.test", email="caixa@example.test",
                                                password="Synthetic-42!")
        self.tenant = Tenant.objects.create(name="Mercado Troca")
        self.member = Membership.objects.create(tenant=self.tenant, user=self.owner, name="Dona", role="owner")
        Membership.objects.create(tenant=self.tenant, user=self.admin, name="Adm", role="admin")
        Membership.objects.create(tenant=self.tenant, user=self.cashier, name="Caixa", role="cashier")
        self.shop = Shop.objects.create(tenant=self.tenant, name="Matriz")
        self.sub = Subscription.objects.create(tenant=self.tenant, plan=Plan.objects.get(slug="management"),
                                               status="trial", trial_ends_at=timezone.now() + timedelta(days=14))
        self.product = Product.objects.create(tenant=self.tenant, sku="ARROZ", name="Arroz", price_cents=1000)
        self.customer = Customer.objects.create(tenant=self.tenant, name="Dona Rosa", credit_limit_cents=100000)
        self.token = "token-antigo-" + uuid.uuid4().hex
        self.terminal = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa 01",
                                                device_id="pc-antigo", token_hash=sha(self.token))
        self.identity = {"tenant_id": str(self.tenant.id), "shop_id": str(self.shop.id),
                         "terminal_id": str(self.terminal.id)}
        self.now = int(time.time() * 1000)
        self.session_id = str(uuid.uuid4())

    # Helpers -----------------------------------------------------------------

    def panel(self, verb, path, body=None, user=None, tenant=None):
        self.client.force_login(user or self.owner)
        return self.client.generic(verb, path, data=json.dumps(body) if body is not None else None,
                                   content_type="application/json", HTTP_X_TENANT_ID=str((tenant or self.tenant).id))

    def term(self, verb, path, body=None, token=None):
        return self.client.generic(verb, path, data=json.dumps(body) if body is not None else None,
                                   content_type="application/json",
                                   HTTP_AUTHORIZATION=f"Bearer {token or self.token}")

    def event(self, kind, aggregate, data, identity=None):
        return {"event_id": uuid.uuid4().hex, "type": kind, "aggregate_id": aggregate,
                "identity": identity or self.identity, "data": data}

    def sync(self, *events, token=None):
        response = self.term("POST", "/api/v1/terminal/sync", {"events": list(events)}, token=token)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["accepted_event_ids"], [event["event_id"] for event in events])
        return body

    def sale(self, created_at=None, session=None, fiado=False, qty=1000, identity=None, product=None, member=None):
        product = product or self.product
        total = product.price_cents * qty // 1000
        item = {"product_id": str(product.id), "name": product.name, "sku": product.sku, "ncm": "", "unit": "UN",
                "quantity_milli": qty, "unit_price_cents": product.price_cents, "gross_cents": total,
                "discount_cents": 0, "line_total_cents": total, "stock_controlled": True}
        data = {"request_id": uuid.uuid4().hex, "cash_session_id": session or self.session_id,
                "created_at": created_at or self.now, "customer_id": str(self.customer.id) if fiado else None,
                "customer_name": "", "customer_document": "", "operator_id": str((member or self.member).id),
                "seller_id": None, "gross_cents": total, "promo_discount_cents": 0, "discount_cents": 0,
                "total_cents": total, "change_cents": 0, "items": [item],
                "payments": [{"method": "credit_account" if fiado else "cash", "amount_cents": total}]}
        return self.event("sale.completed", str(uuid.uuid4()), data, identity)

    def opened(self, session=None, created_at=None, opening=10000):
        return self.event("cash.opened", session or self.session_id,
                          {"opening_cents": opening, "operator_id": str(self.member.id),
                           "created_at": created_at or self.now})

    def closed(self, session, created_at):
        return self.event("cash.closed", session, {"counted_cents": 10000, "expected_cents": 10000,
                                                   "difference_cents": 0, "created_at": created_at})

    def withdrawn(self, cents=5000):
        return self.event("cash.withdrawn", str(uuid.uuid4()), {
            "cash_session_id": self.session_id, "amount_cents": cents, "reason": "Sangria",
            "operator_id": str(self.member.id), "created_at": self.now + 500})

    def receipt(self, cents=500, created_at=None, session=None):
        return self.event("credit.received", str(uuid.uuid4()), {
            "customer_id": str(self.customer.id), "amount_cents": cents, "method": "cash",
            "operator_id": str(self.member.id), "cash_session_id": session or self.session_id,
            "created_at": created_at or self.now + 5})

    def config(self, updated_at, target="EPSON TM-T20", mirror=True, aggregate=None, **extra):
        data = {"version": 1, "print_settings": [{"kind": "receipt", "driver": "windows_raw", "target": target,
                                                  "format": "escpos80"}],
                "mirror": {"enabled": mirror}, "app_version": "2.0.0", "updated_at": updated_at}
        data.update(extra)
        return self.event("terminal.config", aggregate or str(self.terminal.id), data)

    def transfer(self):
        response = self.panel("POST", f"/api/v1/terminals/{self.terminal.id}/transfer", {})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def pair(self, code, device="pc-novo", license_key=None):
        return self.client.generic("POST", "/api/v1/terminal/pair", content_type="application/json",
                                   data=json.dumps({"license_key": license_key or self.sub.license_key, "code": code,
                                                    "device_id": device, "version": "2.0.0"}))

    def swap(self):
        """Old computer replaced: returns the token of the new one."""
        response = self.pair(self.transfer()["recovery_code"])
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["token"]

    def restore(self, cursor="", limit=None, token=None):
        query = f"?cursor={cursor}" + (f"&limit={limit}" if limit is not None else "")
        return self.term("GET", "/api/v1/terminal/restore" + query, token=token)

    def restore_all(self, limit=2, token=None):
        cursor, records, pages = "", [], []
        while True:
            response = self.restore(cursor, limit, token)
            self.assertEqual(response.status_code, 200, response.content)
            body = response.json()
            self.assertLessEqual(len(body["records"]), limit)
            records += body["records"]
            pages.append(body)
            if body["done"]:
                self.assertIsNone(body["next_cursor"])
                return records, body, pages
            self.assertIsNone(body["totals"])
            cursor = body["next_cursor"]


class TrocarDeComputadorTests(Fase2Base):
    def test_transfer_issues_recovery_code_for_the_same_terminal(self):
        self.sync(self.opened(), self.sale())
        Terminal.objects.filter(pk=self.terminal.pk).update(heartbeat_pending_events=3)
        body = self.transfer()
        self.assertRegex(body["recovery_code"], CODE_SHAPE)
        self.assertEqual(body["id"], str(self.terminal.id))
        self.assertEqual(body["license_key"], self.sub.license_key)
        self.assertEqual((body["pending_events"], body["open_cash_session"], body["send_only_days"],
                          body["history_days"], body["paired"]), (3, True, 31, 90, True))
        self.assertEqual((body["transferred_at"], body["send_only_until"], body["previous_device_id"],
                          body["mirror_enabled"]), (None, None, "", None))
        row = Terminal.objects.get(pk=self.terminal.pk)
        self.assertEqual(row.pairing_code_hash, sha(body["recovery_code"]))
        self.assertEqual(row.token_hash, sha(self.token))  # nothing changes for the old computer yet
        minutes = (row.pairing_expires_at - timezone.now()).total_seconds() / 60
        self.assertTrue(29 < minutes <= 30)
        self.assertEqual(self.term("POST", "/api/v1/terminal/lease", {}).status_code, 200)
        audit = AuditEvent.objects.get(action="terminal.transfer_started")
        self.assertNotIn(body["recovery_code"], json.dumps(audit.detail))

    def test_transfer_requires_owner_or_admin_and_a_paired_terminal(self):
        path = f"/api/v1/terminals/{self.terminal.id}/transfer"
        self.assertEqual(self.panel("POST", path, {}, user=self.cashier).status_code, 403)
        self.assertEqual(self.panel("POST", path, {}, user=self.admin).status_code, 200)
        self.assertEqual(self.panel("GET", path).status_code, 405)
        self.assertEqual(self.panel("POST", f"/api/v1/terminals/{uuid.uuid4()}/transfer", {}).status_code, 404)
        fresh = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa 02")
        response = self.panel("POST", f"/api/v1/terminals/{fresh.id}/transfer", {})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "terminal_not_paired"))
        Terminal.objects.filter(pk=self.terminal.pk).update(revoked=True)
        response = self.panel("POST", path, {})
        self.assertEqual((response.status_code, response.json()), (409, {"error": "Terminal revogado"}))

    def test_transfer_is_isolated_between_companies(self):
        other = Tenant.objects.create(name="Mercado Vizinho")
        Subscription.objects.create(tenant=other, plan=Plan.objects.get(slug="management"), status="trial",
                                    trial_ends_at=timezone.now() + timedelta(days=14))
        Membership.objects.create(tenant=other, user=self.admin, name="Adm", role="owner")
        response = self.panel("POST", f"/api/v1/terminals/{self.terminal.id}/transfer", {}, user=self.admin,
                              tenant=other)
        self.assertEqual(response.status_code, 404)
        self.assertIsNone(Terminal.objects.get(pk=self.terminal.pk).pairing_code_hash)

    def test_pair_accepts_the_code_typed_in_lowercase_without_hyphens(self):
        self.sync(self.opened(), self.sale())
        code = self.transfer()["recovery_code"]
        typed = " " + code.replace("-", "").lower() + " "
        self.assertEqual(self.pair(typed, license_key="PDV2-ERRADA").status_code, 401)
        response = self.pair(typed)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["terminal_id"], str(self.terminal.id))
        self.assertIs(body["recovery"], True)
        self.assertEqual(body["history_days"], 90)
        self.assertEqual(body["capabilities"], ["restore", "send-only", "terminal-config", "mirror-key"])
        self.assertRegex(body["mirror_key"]["key_id"], r"^[0-9a-f]{16}$")
        self.assertEqual(len(body["mirror_key"]["key"]), 44)
        row = Terminal.objects.get(pk=self.terminal.pk)
        self.assertEqual((row.device_id, row.previous_device_id, row.previous_token_hash),
                         ("pc-novo", "pc-antigo", sha(self.token)))
        self.assertEqual(row.token_hash, sha(body["token"]))
        days = (row.previous_token_expires_at - timezone.now()).total_seconds() / 86400
        self.assertTrue(30.99 < days <= 31)  # the plan's 30 offline days plus one to upload
        self.assertEqual(body["terminal"]["previous_device_id"], "pc-antigo")
        self.assertIsNotNone(body["terminal"]["send_only_until"])
        audit = AuditEvent.objects.get(action="terminal.transferred")
        self.assertEqual((audit.detail["device_id"], audit.detail["previous_device_id"]), ("pc-novo", "pc-antigo"))
        self.assertNotIn(body["token"], json.dumps(audit.detail))
        self.assertEqual(self.pair(code).status_code, 401)  # single use

    def test_ordinary_pairing_code_and_first_pairing_are_unchanged(self):
        code = "codigo-comum-1234"
        fresh = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa 02",
                                        pairing_code_hash=sha(code),
                                        pairing_expires_at=timezone.now() + timedelta(minutes=10))
        self.assertEqual(self.pair(code.upper(), device="pc-2").status_code, 401)
        body = self.pair(code, device="pc-2").json()
        self.assertIs(body["recovery"], False)
        row = Terminal.objects.get(pk=fresh.pk)
        self.assertEqual((row.previous_token_hash, row.transferred_at, row.previous_device_id), (None, None, ""))

    def test_pairing_again_on_the_same_computer_keeps_no_send_only_token(self):
        response = self.pair(self.transfer()["recovery_code"], device="pc-antigo")
        self.assertEqual(response.status_code, 200, response.content)
        row = Terminal.objects.get(pk=self.terminal.pk)
        self.assertEqual((row.previous_token_hash, row.transferred_at), (None, None))
        self.assertEqual(self.term("POST", "/api/v1/terminal/sync", {"events": [self.opened()]}).status_code, 401)


class TokenSoEnvioTests(Fase2Base):
    def test_old_token_only_sends_its_queue(self):
        self.sync(self.opened())
        new_token = self.swap()
        seen = Terminal.objects.get(pk=self.terminal.pk).last_seen_at
        late = self.sale()
        body = self.sync(late)  # old computer still uploading
        self.assertIs(body["send_only"], True)
        deadline = Terminal.objects.get(pk=self.terminal.pk).previous_token_expires_at
        self.assertEqual(body["send_only_until"], deadline.isoformat())
        self.assertTrue(Sale.objects.filter(pk=late["aggregate_id"]).exists())
        self.assertEqual(Terminal.objects.get(pk=self.terminal.pk).last_seen_at, seen)  # not presence
        for verb, path, payload in (("POST", "/api/v1/terminal/lease", {}),
                                    ("GET", "/api/v1/terminal/bootstrap", None),
                                    ("POST", "/api/v1/terminal/heartbeat", {}),
                                    ("GET", "/api/v1/terminal/restore", None)):
            response = self.term(verb, path, payload)
            self.assertEqual(response.status_code, 403, path)
            refused = response.json()
            self.assertEqual(refused["code"], "terminal_transferred", path)
            self.assertEqual(refused["error"], "Este caixa foi transferido para outro computador.")
            self.assertEqual(refused["send_only_until"], deadline.isoformat())
            self.assertIsNotNone(refused["transferred_at"])
        current = self.sync(self.sale(), token=new_token)
        self.assertIs(current["send_only"], False)
        self.assertNotIn("send_only_until", current)
        self.assertEqual(self.term("POST", "/api/v1/terminal/lease", {}, token=new_token).status_code, 200)
        self.assertEqual(self.term("POST", "/api/v1/terminal/sync", {"events": [self.sale()]},
                                   token="x" * 48).status_code, 401)

    def test_duplicate_resend_from_both_computers_is_applied_once(self):
        self.sync(self.opened())
        sale = self.sale()
        self.sync(sale)
        new_token = self.swap()
        conflicts = SyncConflict.objects.count()
        self.sync(sale)                    # old computer never got the acknowledgement
        self.sync(sale, token=new_token)   # and the replica re-sent it from the new one
        self.assertEqual(Sale.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(SyncEvent.objects.filter(event_id=sale["event_id"]).count(), 1)
        self.assertEqual(SyncConflict.objects.count(), conflicts)  # not judged a second time
        changed = json.loads(json.dumps(sale))
        changed["data"]["customer_name"] = "Outro"
        body = self.sync(changed)
        self.assertEqual([row["event_id"] for row in body["quarantined"]], [sale["event_id"]])
        self.assertEqual(Sale.objects.filter(tenant=self.tenant).count(), 1)

    def test_send_only_window_covers_the_lease_the_old_computer_may_hold(self):
        # The old computer can keep selling offline for the plan's offline days:
        # what it sells until then must still have a way up.
        plan = Plan.objects.get(pk=self.sub.plan_id)
        for offline, expected in ((1, 7), (7, 8), (30, 31), (45, 46)):
            Plan.objects.filter(pk=plan.pk).update(offline_days=offline)
            self.assertEqual(self.transfer()["send_only_days"], expected)
        self.swap()
        row = Terminal.objects.get(pk=self.terminal.pk)
        days = (row.previous_token_expires_at - row.transferred_at).total_seconds() / 86400
        self.assertEqual(days, 46)
        # Day 40 of a 45-day plan: the sale made offline on the old computer still goes up.
        late = self.sale()
        with patch("django.utils.timezone.now", return_value=row.transferred_at + timedelta(days=40)):
            response = self.term("POST", "/api/v1/terminal/sync", {"events": [late]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIs(response.json()["send_only"], True)
        self.assertTrue(Sale.objects.filter(pk=late["aggregate_id"]).exists())

    def test_send_only_expires_after_its_deadline(self):
        self.swap()
        Terminal.objects.filter(pk=self.terminal.pk).update(
            previous_token_expires_at=timezone.now() - timedelta(seconds=1))
        response = self.term("POST", "/api/v1/terminal/sync", {"events": [self.opened()]})
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual((body["code"], body["send_only_until"]), ("terminal_transferred", None))
        self.assertIsNotNone(body["transferred_at"])
        self.assertEqual(SyncEvent.objects.count(), 0)
        listed = self.panel("GET", f"/api/v1/terminals/{self.terminal.id}").json()
        self.assertIsNone(listed["send_only_until"])
        self.assertEqual(listed["previous_device_id"], "pc-antigo")

    def test_panel_can_end_send_only_early(self):
        new_token = self.swap()
        path = f"/api/v1/terminals/{self.terminal.id}"
        self.assertIsNotNone(self.panel("GET", path).json()["send_only_until"])
        self.assertEqual(self.panel("PATCH", path, {"end_send_only": "sim"}).status_code, 400)
        self.assertEqual(self.panel("PATCH", path, {"end_send_only": True}, user=self.cashier).status_code, 403)
        response = self.panel("PATCH", path, {"end_send_only": True})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()["send_only_until"])
        self.assertEqual(AuditEvent.objects.filter(action="terminal.send_only_ended").count(), 1)
        refused = self.term("POST", "/api/v1/terminal/sync", {"events": [self.opened()]})
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "terminal_transferred"))
        self.assertEqual(self.term("POST", "/api/v1/terminal/sync", {"events": [self.opened()]},
                                   token=new_token).status_code, 200)
        self.panel("PATCH", path, {"end_send_only": True})  # nothing left to end
        self.assertEqual(AuditEvent.objects.filter(action="terminal.send_only_ended").count(), 1)

    def test_revoked_terminal_refuses_both_tokens(self):
        new_token = self.swap()
        Terminal.objects.filter(pk=self.terminal.pk).update(revoked=True)
        for token in (self.token, new_token):
            response = self.term("POST", "/api/v1/terminal/sync", {"events": [self.opened()]}, token=token)
            self.assertEqual((response.status_code, response.json()), (401, {"error": "Terminal não autenticado"}))


class RestoreTests(Fase2Base):
    def history(self):
        """90-day window, fiado, open cash session, returns and receipts."""
        old = self.now - 120 * DAY_MS
        old_session = str(uuid.uuid4())
        self.old_sale = self.sale(created_at=old, session=old_session)
        self.old_fiado = self.sale(created_at=old + 1000, session=old_session, fiado=True, qty=5000)
        self.old_in_open = self.sale(created_at=old + 2000)  # clock was wrong; belongs to the open session
        self.recent = [self.sale(created_at=self.now - day * DAY_MS) for day in (80, 40, 3, 0)]
        self.voided_sale = self.sale()
        self.returned_sale = self.sale(qty=2000)
        self.cash_return = self.event("sale.returned", str(uuid.uuid4()), {
            "sale_id": self.returned_sale["aggregate_id"], "refund_method": "cash", "reason": "Defeito",
            "operator_id": str(self.member.id), "cash_session_id": self.session_id, "created_at": self.now + 900,
            "items": [{"product_id": str(self.product.id), "quantity_milli": 1000, "amount_cents": 1000}],
            "total_cents": 1000})
        self.receipts = [self.receipt(700), self.receipt(300, created_at=old + 5000, session=old_session)]
        self.sync(self.opened(old_session, old), self.old_sale, self.old_fiado, self.closed(old_session, old + 9000),
                  self.opened(), self.withdrawn(5000), self.withdrawn(2500), self.old_in_open, *self.recent,
                  self.voided_sale, self.returned_sale, self.cash_return, *self.receipts,
                  self.event("sale.voided", self.voided_sale["aggregate_id"],
                             {"sale_id": self.voided_sale["aggregate_id"], "reason": "Cliente desistiu",
                              "created_at": self.now + 10}),
                  self.config(self.now))
        self.assertEqual(SyncConflict.objects.filter(reason="rejected_event").count(), 0)
        self.assertTrue(FinanceEntry.objects.filter(sale_id=self.old_fiado["aggregate_id"], status="open").exists())
        response = self.panel("POST", f"/api/v1/sales/{self.recent[0]['aggregate_id']}/return", {
            "items": [{"product_id": str(self.product.id), "quantity_milli": 1000}],
            "refund_method": "external", "reason": "Pix devolvido"})
        self.assertEqual(response.status_code, 201, response.content)

    def test_paged_restore_brings_window_fiado_open_cash_and_matching_totals(self):
        self.history()
        records, last, pages = self.restore_all(limit=2)
        self.assertGreater(len(pages), 5)
        self.assertEqual([page["progress"]["position"] for page in pages][-1], len(records))
        self.assertEqual(pages[0]["progress"]["total"], len(records))
        self.assertEqual(last["history_days"], 90)
        self.assertAlmostEqual(last["cutoff"], self.now - 90 * DAY_MS, delta=120000)
        self.assertAlmostEqual(last["server_time"], time.time(), delta=120)
        kinds = [record["kind"] for record in records]
        self.assertEqual(kinds, sorted(kinds, key=["config", "cash_session", "sale", "credit_receipt"].index))

        config = records[0]
        self.assertEqual(config["data"]["print_settings"][0]["target"], "EPSON TM-T20")

        sessions = [record for record in records if record["kind"] == "cash_session"]
        self.assertEqual([row["id"] for row in sessions], [self.session_id])  # the closed one is older than 90 days
        session = sessions[0]
        self.assertEqual((session["opening_cents"], session["closed_at"], session["counted_cents"],
                          session["operator_id"], session["incomplete"], session["opened_at"]),
                         (10000, None, None, str(self.member.id), False, self.now))
        self.assertEqual(sorted(row["amount_cents"] for row in session["movements"]), [2500, 5000])
        self.assertEqual({row["kind"] for row in session["movements"]}, {"withdrawn"})
        self.assertEqual(session["movements"][0]["created_at"], self.now + 500)

        sales = {record["id"]: record for record in records if record["kind"] == "sale"}
        self.assertNotIn(self.old_sale["aggregate_id"], sales)             # outside the 90 days, paid
        self.assertIn(self.old_fiado["aggregate_id"], sales)               # old, but still owed
        self.assertIn(self.old_in_open["aggregate_id"], sales)             # old date, open cash session
        self.assertEqual(len(sales), 8)
        for sent in (self.old_fiado, self.old_in_open, *self.recent, self.voided_sale, self.returned_sale):
            record = sales[sent["aggregate_id"]]
            self.assertEqual(record["data"], sent["data"])                 # the fact, exactly as sold
            self.assertEqual(record["event_id"], sent["event_id"])
        voided = sales[self.voided_sale["aggregate_id"]]
        self.assertEqual((voided["status"], voided["void_reason"]), ("voided", "Cliente desistiu"))
        self.assertIsInstance(voided["voided_at"], int)
        kept = sales[self.recent[1]["aggregate_id"]]
        self.assertEqual((kept["status"], kept["voided_at"], kept["void_reason"], kept["returns"]),
                         ("completed", None, "", []))
        at_counter = sales[self.returned_sale["aggregate_id"]]["returns"]
        self.assertEqual(len(at_counter), 1)
        self.assertEqual({key: at_counter[0][key] for key in ("id", "refund_method", "total_cents", "reason", "origin",
                                                              "cash_session_id", "created_at", "operator_id")},
                         {"id": self.cash_return["aggregate_id"], "refund_method": "cash", "total_cents": 1000,
                          "reason": "Defeito", "origin": "terminal", "cash_session_id": self.session_id,
                          "created_at": self.now + 900, "operator_id": str(self.member.id)})
        self.assertEqual(at_counter[0]["items"], [{"product_id": str(self.product.id), "quantity_milli": 1000,
                                                    "amount_cents": 1000}])
        from_panel = sales[self.recent[0]["aggregate_id"]]["returns"]
        self.assertEqual([(row["origin"], row["refund_method"], row["cash_session_id"], row["total_cents"])
                          for row in from_panel], [("panel", "external", "", 1000)])

        receipts = [record for record in records if record["kind"] == "credit_receipt"]
        self.assertEqual([(row["id"], row["amount_cents"], row["method"], row["customer_id"], row["cash_session_id"])
                          for row in receipts],
                         [(self.receipts[0]["aggregate_id"], 700, "cash", str(self.customer.id), self.session_id)])

        returns = [row for record in sales.values() for row in record["returns"]]
        self.assertEqual(last["totals"], {
            "sales": {"count": len(sales), "total_cents": sum(row["data"]["total_cents"] for row in sales.values()),
                      "voided_count": 1},
            "returns": {"count": 2, "total_cents": sum(row["total_cents"] for row in returns)},
            "credit_receipts": {"count": 1, "total_cents": 700},
            "cash_sessions": {"count": 1, "open_count": 1, "stale_open_omitted": 0},
            "cash_movements": {"count": 2, "total_cents": 7500},
            "config": {"count": 1}})
        self.assertEqual(last["totals"]["sales"]["total_cents"], 13000)
        # Restore reads; it never creates events, conflicts or stock effects.
        self.assertEqual(AuditEvent.objects.filter(action="terminal.restore_started").count(), 1)
        finished = AuditEvent.objects.get(action="terminal.restore_finished")
        self.assertEqual(finished.detail, last["totals"])

    def test_same_cursor_returns_the_same_page_and_resumes_after_a_drop(self):
        self.history()
        first = self.restore("", 3).json()
        second = self.restore(first["next_cursor"], 3).json()
        again = self.restore(first["next_cursor"], 3).json()   # connection dropped: the terminal asks again
        self.assertEqual(again["records"], second["records"])
        self.assertEqual(again["next_cursor"], second["next_cursor"])
        self.assertEqual(again["progress"], second["progress"])
        self.assertLessEqual(len(second["next_cursor"]), 2000)
        # Resuming from the middle ends with the same totals as a single big page.
        records, cursor = first["records"] + second["records"], second["next_cursor"]
        while cursor:
            body = self.restore(cursor, 3).json()
            records += body["records"]
            cursor = body["next_cursor"]
        whole = self.restore("", 500).json()
        self.assertTrue(whole["done"])
        self.assertEqual(records, whole["records"])
        self.assertEqual(body["totals"], whole["totals"])
        self.assertEqual(len({record.get("id", "config") for record in records}), len(records))  # nothing twice

    def test_page_closes_early_by_size_but_always_carries_one_record(self):
        self.history()
        with patch.object(views_terminal, "RESTORE_PAGE_BYTES", 10):
            records, last, pages = self.restore_all(limit=500)
        self.assertEqual([len(page["records"]) for page in pages], [1] * len(records))
        self.assertEqual(records, self.restore("", 500).json()["records"])

    def test_stale_open_sessions_are_omitted_and_counted(self):
        stale = str(uuid.uuid4())
        self.sync(self.opened(stale, self.now - 2 * DAY_MS), self.sale(session=stale), self.opened())
        records, last, _ = self.restore_all(limit=10)
        self.assertEqual([row["id"] for row in records if row["kind"] == "cash_session"], [self.session_id])
        self.assertEqual(last["totals"]["cash_sessions"], {"count": 1, "open_count": 1, "stale_open_omitted": 1})
        self.assertEqual(last["totals"]["sales"]["count"], 1)  # its sale is in the window all the same

    def test_old_session_left_open_is_not_restored_as_open_after_newer_closed_ones(self):
        # The closing of an old session never arrived; the terminal went on
        # opening and closing others. It admits one open session, so the old
        # one cannot really be open: the new computer must not be born with it.
        forgotten, later = str(uuid.uuid4()), str(uuid.uuid4())
        long_ago, yesterday = self.now - 200 * DAY_MS, self.now - DAY_MS
        old_sale = self.sale(created_at=long_ago + 1000, session=forgotten)
        self.sync(self.opened(forgotten, long_ago), old_sale,
                  self.opened(later, yesterday), self.sale(created_at=yesterday + 1000, session=later),
                  self.closed(later, yesterday + 9000))
        self.assertTrue(CashSession.objects.filter(pk=forgotten, closed_at__isnull=True).exists())
        self.assertIs(self.transfer()["open_cash_session"], False)
        records, last, _ = self.restore_all(limit=10)
        sessions = [row for row in records if row["kind"] == "cash_session"]
        self.assertEqual([(row["id"], row["closed_at"] is None) for row in sessions], [(later, False)])
        self.assertEqual(last["totals"]["cash_sessions"], {"count": 1, "open_count": 0, "stale_open_omitted": 1})
        sales = [row["id"] for row in records if row["kind"] == "sale"]
        self.assertNotIn(old_sale["aggregate_id"], sales)  # not pulled in as "sale of the open session"
        self.assertEqual(len(sales), 1)

    def test_sale_without_its_original_event_is_rebuilt_with_the_same_keys(self):
        sale = self.sale()
        self.sync(self.opened(), sale)
        SyncEvent.objects.filter(event_id=sale["event_id"]).delete()
        record = next(row for row in self.restore("", 50).json()["records"] if row["kind"] == "sale")
        self.assertIsNone(record["event_id"])
        self.assertEqual(set(record["data"]), set(sale["data"]))
        self.assertEqual({key: record["data"][key] for key in ("request_id", "cash_session_id", "created_at",
                                                               "total_cents", "payments", "operator_id")},
                         {key: sale["data"][key] for key in ("request_id", "cash_session_id", "created_at",
                                                             "total_cents", "payments", "operator_id")})
        self.assertEqual(record["data"]["items"][0]["quantity_milli"], 1000)

    def test_empty_terminal_and_invalid_requests(self):
        body = self.restore().json()
        self.assertEqual((body["records"], body["done"], body["next_cursor"]), ([], True, None))
        self.assertEqual(body["progress"], {"position": 0, "total": 0})
        self.assertEqual(body["totals"], {
            "sales": {"count": 0, "total_cents": 0, "voided_count": 0}, "returns": {"count": 0, "total_cents": 0},
            "credit_receipts": {"count": 0, "total_cents": 0},
            "cash_sessions": {"count": 0, "open_count": 0, "stale_open_omitted": 0},
            "cash_movements": {"count": 0, "total_cents": 0}, "config": {"count": 0}})
        for limit in ("0", "501", "abc"):
            response = self.restore("", limit)
            self.assertEqual((response.status_code, response.json()), (400, {"error": "Paginação inválida"}))
        for cursor in ("lixo", "e30", "x" * 2001):
            response = self.restore(cursor)
            self.assertEqual((response.status_code, response.json()),
                             (400, {"error": "Cursor inválido", "code": "invalid_cursor"}), cursor)
        self.assertEqual(self.term("POST", "/api/v1/terminal/restore", {}).status_code, 405)
        self.assertEqual(self.restore(token="y" * 48).status_code, 401)
        self.assertEqual(self.restore().headers["Cache-Control"], "private, no-store")

    def test_cursor_older_than_the_window_is_refused(self):
        self.sync(self.opened(), self.sale(), self.sale())
        cursor = self.restore("", 1).json()["next_cursor"]
        self.assertEqual(self.restore(cursor, 1).status_code, 200)
        with patch("core.restore_logic.timezone.now", return_value=timezone.now() + timedelta(days=2)):
            response = self.restore(cursor, 1)
        self.assertEqual((response.status_code, response.json()["code"]), (400, "invalid_cursor"))

    def test_restore_is_isolated_between_terminals_and_companies(self):
        self.history()
        other = Tenant.objects.create(name="Mercado Vizinho")
        other_shop = Shop.objects.create(tenant=other, name="Filial")
        Subscription.objects.create(tenant=other, plan=Plan.objects.get(slug="management"), status="trial",
                                    trial_ends_at=timezone.now() + timedelta(days=14))
        other_member = Membership.objects.create(tenant=other, user=self.admin, name="Vizinho", role="owner")
        other_product = Product.objects.create(tenant=other, sku="FEIJAO", name="Feijão", price_cents=700)
        other_token = "token-vizinho-" + uuid.uuid4().hex
        other_terminal = Terminal.objects.create(tenant=other, shop=other_shop, name="Caixa V", device_id="pc-v",
                                                 token_hash=sha(other_token))
        identity = {"tenant_id": str(other.id), "shop_id": str(other_shop.id), "terminal_id": str(other_terminal.id)}
        theirs = self.sale(identity=identity, product=other_product, member=other_member,
                           session=str(uuid.uuid4()))
        self.sync(theirs, token=other_token)
        sibling_token = "token-irmao-" + uuid.uuid4().hex
        sibling = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa 02", device_id="pc-2",
                                          token_hash=sha(sibling_token))

        records, last, _ = self.restore_all(limit=50, token=other_token)
        self.assertEqual([(row["kind"], row["id"]) for row in records], [("sale", theirs["aggregate_id"])])
        self.assertEqual(last["totals"]["sales"], {"count": 1, "total_cents": 700, "voided_count": 0})
        mine, _, _ = self.restore_all(limit=50)
        self.assertNotIn(theirs["aggregate_id"], [row.get("id") for row in mine])
        self.assertEqual(self.restore_all(limit=50, token=sibling_token)[0], [])  # same company, another terminal

        cursor = self.restore("", 2).json()["next_cursor"]
        for token in (other_token, sibling_token):
            response = self.restore(cursor, 2, token=token)
            self.assertEqual((response.status_code, response.json()["code"]), (400, "invalid_cursor"))
        # Even a forged cursor naming this terminal cannot reach another terminal's cash session.
        import base64
        forged = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        forged.update(t=str(sibling.id), open=[self.session_id], sec="config", key=[])
        text = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
        self.assertEqual(self.restore(text, 50, token=sibling_token).json()["records"], [])

    def test_restore_does_not_depend_on_the_subscription(self):
        self.sync(self.opened(), self.sale())
        Subscription.objects.filter(pk=self.sub.pk).update(status="suspended")
        self.assertEqual(self.term("POST", "/api/v1/terminal/lease", {}).status_code, 403)
        body = self.restore().json()
        self.assertEqual(body["totals"]["sales"]["count"], 1)

    def test_sales_received_after_the_restore_started_do_not_shift_the_pages(self):
        self.sync(self.opened(), self.sale(), self.sale(), self.sale())
        first = self.restore("", 2).json()
        late = self.sale()
        self.sync(late)  # e.g. the old computer, still send-only
        records = list(first["records"])
        cursor = first["next_cursor"]
        while cursor:
            body = self.restore(cursor, 2).json()
            records += body["records"]
            cursor = body["next_cursor"]
        self.assertNotIn(late["aggregate_id"], [row["id"] for row in records])
        self.assertEqual(body["totals"]["sales"]["count"], 3)


class TerminalConfigTests(Fase2Base):
    def test_newest_configuration_wins_and_returns_on_restore(self):
        newest = self.config(self.now, target="EPSON TM-T20", mirror=True)
        older = self.config(self.now - 5000, target="Impressora velha", mirror=False)
        body = self.sync(newest, older)
        self.assertEqual((body["conflicts"], body["quarantined"]), ([], []))
        row = Terminal.objects.get(pk=self.terminal.pk)
        self.assertEqual(row.config, newest["data"])
        self.assertEqual(SyncEvent.objects.filter(event_type="terminal.config").count(), 2)
        self.assertIs(self.panel("GET", f"/api/v1/terminals/{self.terminal.id}").json()["mirror_enabled"], True)
        later = self.config(self.now + 5000, target="Nova", mirror=False)
        self.sync(later)
        self.sync(newest)  # resend of an applied event does not roll the configuration back
        self.assertEqual(Terminal.objects.get(pk=self.terminal.pk).config, later["data"])
        self.assertIs(self.panel("GET", f"/api/v1/terminals/{self.terminal.id}").json()["mirror_enabled"], False)
        restored = self.restore().json()
        self.assertEqual(restored["records"], [{"kind": "config", "data": later["data"]}])
        self.assertEqual(restored["totals"]["config"], {"count": 1})

    def test_optional_fields_and_empty_printers(self):
        event = self.event("terminal.config", str(self.terminal.id),
                           {"version": 1, "print_settings": [], "updated_at": self.now})
        self.assertEqual(self.sync(event)["quarantined"], [])
        self.assertEqual(Terminal.objects.get(pk=self.terminal.pk).config, event["data"])
        self.assertIsNone(self.panel("GET", f"/api/v1/terminals/{self.terminal.id}").json()["mirror_enabled"])

    def test_malformed_configuration_is_quarantined_and_ignored(self):
        good = self.config(self.now)
        self.sync(good)
        printer = good["data"]["print_settings"][0]
        broken = [
            self.config(self.now + 1, aggregate=str(uuid.uuid4())),                     # another terminal
            self.config(self.now + 2, token="segredo"),                                 # unknown key
            self.config(self.now + 3, version=2),
            self.config(self.now + 4, mirror={"enabled": True, "directory": "E:\\"}),
            self.config(self.now + 5, print_settings=[{**printer, "driver": "usb"}]),
            self.config(self.now + 6, print_settings=[{**printer, "target": "x" * 261}]),
            self.config(self.now + 7, print_settings=[printer] * 5),
            self.config(self.now + 8, app_version="v" * 81),
            self.config("ontem"),
        ]
        body = self.sync(*broken)
        self.assertEqual([row["event_id"] for row in body["quarantined"]], [event["event_id"] for event in broken])
        self.assertEqual(Terminal.objects.get(pk=self.terminal.pk).config, good["data"])


class ChaveDaCopiaTests(Fase2Base):
    def test_key_is_created_once_and_delivered_to_the_terminal(self):
        self.assertEqual(self.panel("GET", "/api/v1/backup-key").json(),
                         {"exists": False, "key_id": "", "revealed_at": None, "can_reveal": True})
        first = self.term("POST", "/api/v1/terminal/lease", {}).json()
        second = self.term("POST", "/api/v1/terminal/lease", {}).json()
        self.assertEqual(first["capabilities"], ["restore", "send-only", "terminal-config", "mirror-key"])
        key = first["mirror_key"]
        self.assertEqual(second["mirror_key"], key)
        self.assertEqual(key["key_id"], sha(key["key"])[:16])
        self.assertEqual(len(key["key"]), 44)
        paired = self.pair(self.transfer()["recovery_code"]).json()
        self.assertEqual(paired["mirror_key"], key)
        bootstrap = self.term("GET", "/api/v1/terminal/bootstrap", token=paired["token"]).json()
        self.assertEqual(bootstrap["capabilities"], first["capabilities"])
        self.assertNotIn("mirror_key", bootstrap)
        tenant = Tenant.objects.get(pk=self.tenant.pk)
        self.assertNotIn(key["key"], tenant.backup_key_encrypted)  # stored encrypted
        self.assertEqual(tenant.backup_key_id, key["key_id"])
        created = AuditEvent.objects.filter(action="backup_key.created")
        self.assertEqual([row.detail for row in created], [{"key_id": key["key_id"]}])
        # Another company gets another key.
        other = Tenant.objects.create(name="Mercado Vizinho")
        from .backup_key import tenant_backup_key
        self.assertNotEqual(tenant_backup_key(other)["key"], key["key"])

    def test_owner_sees_the_key_only_once(self):
        key = self.term("POST", "/api/v1/terminal/lease", {}).json()["mirror_key"]
        self.assertEqual(self.panel("GET", "/api/v1/backup-key", user=self.admin).json(),
                         {"exists": True, "key_id": key["key_id"], "revealed_at": None, "can_reveal": False})
        self.assertEqual(self.panel("GET", "/api/v1/backup-key", user=self.cashier).status_code, 403)
        self.assertEqual(self.panel("POST", "/api/v1/backup-key/reveal", {"confirm": True},
                                    user=self.admin).status_code, 403)
        self.assertEqual(self.panel("POST", "/api/v1/backup-key/reveal", {"confirm": False}).status_code, 400)
        self.assertEqual(self.panel("POST", "/api/v1/backup-key/reveal", {}).status_code, 400)
        response = self.panel("POST", "/api/v1/backup-key/reveal", {"confirm": True})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body["key_id"], body["key"]), (key["key_id"], key["key"]))
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        again = self.panel("POST", "/api/v1/backup-key/reveal", {"confirm": True})
        self.assertEqual((again.status_code, again.json()["code"]), (409, "backup_key_already_revealed"))
        self.assertNotIn("key", again.json())
        status = self.panel("GET", "/api/v1/backup-key").json()
        self.assertEqual((status["exists"], status["can_reveal"], status["revealed_at"]),
                         (True, False, body["revealed_at"]))
        for row in AuditEvent.objects.filter(action__startswith="backup_key."):
            self.assertNotIn(key["key"], json.dumps(row.detail))
        self.assertEqual(AuditEvent.objects.filter(action="backup_key.revealed").count(), 1)

    def test_reveal_creates_the_key_when_no_terminal_asked_yet(self):
        body = self.panel("POST", "/api/v1/backup-key/reveal", {"confirm": True}).json()
        self.assertEqual(self.term("POST", "/api/v1/terminal/lease", {}).json()["mirror_key"],
                         {"key_id": body["key_id"], "key": body["key"]})

    def test_missing_server_key_never_breaks_pair_or_lease(self):
        code = self.transfer()["recovery_code"]
        with patch.dict(os.environ, {"PDV_ENCRYPTION_KEY": "", "PDV_ENCRYPTION_KEY_FILE": ""}), \
                self.assertLogs("core.backup_key", level="ERROR"):
            lease = self.term("POST", "/api/v1/terminal/lease", {})
            self.assertEqual(lease.status_code, 200)
            self.assertIsNone(lease.json()["mirror_key"])
            paired = self.pair(code)
            self.assertEqual(paired.status_code, 200, paired.content)
            self.assertIsNone(paired.json()["mirror_key"])
            response = self.panel("POST", "/api/v1/backup-key/reveal", {"confirm": True})
            self.assertEqual((response.status_code, response.json()["code"]), (503, "backup_key_unavailable"))
        self.assertEqual(Tenant.objects.get(pk=self.tenant.pk).backup_key_encrypted, "")
        self.assertIsNone(Tenant.objects.get(pk=self.tenant.pk).backup_key_revealed_at)

    def test_unreadable_server_key_file_never_breaks_pair_or_lease(self):
        # The key file is missing (or unreadable): that raises OSError, not ValueError.
        for existing_key in (False, True):
            code = self.transfer()["recovery_code"]
            missing = os.path.join(os.path.dirname(__file__), "nao-existe", "chave-do-servidor")
            with patch.dict(os.environ, {"PDV_ENCRYPTION_KEY_FILE": missing}), \
                    self.assertLogs("core.backup_key", level="ERROR") as logs:
                lease = self.term("POST", "/api/v1/terminal/lease", {})
                self.assertEqual(lease.status_code, 200, lease.content)
                self.assertIsNone(lease.json()["mirror_key"])
                paired = self.pair(code, device=f"pc-novo-{existing_key}")
                self.assertEqual(paired.status_code, 200, paired.content)  # the new computer gets its token
                self.assertIsNone(paired.json()["mirror_key"])
                self.token = paired.json()["token"]
                response = self.panel("POST", "/api/v1/backup-key/reveal", {"confirm": True})
                self.assertEqual((response.status_code, response.json()["code"]), (503, "backup_key_unavailable"))
            self.assertNotIn(missing, "\n".join(logs.output))
            self.assertIn("FileNotFoundError", "\n".join(logs.output))
            # With the server key back, the same company key is delivered.
            key = self.term("POST", "/api/v1/terminal/lease", {}).json()["mirror_key"]
            self.assertEqual(len(key["key"]), 44)
