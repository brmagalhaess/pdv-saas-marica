"""Caixa por turno de operador: pausas e totais do fechamento chegam do terminal e aparecem no painel.

Terminais com a capacidade "cash_shifts" mandam cash.paused / cash.resumed e um cash.closed com
paused_ms e totals. Terminais antigos continuam mandando o cash.closed de antes, que segue aceito.
"""
import hashlib
import json
import time
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .models import CashPause, CashSession, Membership, Plan, Shop, Subscription, SyncConflict, Tenant, Terminal


class TurnosCaixaSyncTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="dona@turnos.test", email="dona@turnos.test",
                                             password="Synthetic-42!", first_name="Dona")
        cls.tenant = Tenant.objects.create(name="Mercado Turno")
        cls.member = Membership.objects.create(tenant=cls.tenant, user=cls.owner, name="Dona", role="owner")
        cashier_user = User.objects.create_user(username="ana@turnos.test", email="ana@turnos.test",
                                                password="Synthetic-42!", first_name="Ana")
        cls.cashier = Membership.objects.create(tenant=cls.tenant, user=cashier_user, name="Ana", role="cashier")
        cls.shop = Shop.objects.create(tenant=cls.tenant, name="Matriz")
        Subscription.objects.create(tenant=cls.tenant, plan=Plan.objects.get(slug="management"), status="trial",
                                    trial_ends_at=timezone.now() + timedelta(days=14))
        cls.token = "terminal-token-" + uuid.uuid4().hex
        cls.terminal = Terminal.objects.create(tenant=cls.tenant, shop=cls.shop, name="Caixa 1", device_id="turno-device",
                                               token_hash=hashlib.sha256(cls.token.encode()).hexdigest())
        cls.identity = {"tenant_id": str(cls.tenant.id), "shop_id": str(cls.shop.id), "terminal_id": str(cls.terminal.id)}

    def setUp(self):
        self.now = int(time.time() * 1000) - 3_600_000
        self.session_id = str(uuid.uuid4())

    def event(self, kind, aggregate, data):
        return {"event_id": uuid.uuid4().hex, "type": kind, "aggregate_id": aggregate,
                "identity": self.identity, "data": data}

    def sync(self, *events):
        response = self.client.generic("POST", "/api/v1/terminal/sync", data=json.dumps({"events": list(events)}),
                                       content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["accepted_event_ids"], [event["event_id"] for event in events])
        return body

    def panel(self, path):
        self.client.force_login(self.owner)
        response = self.client.get(path, HTTP_X_TENANT_ID=str(self.tenant.id))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def opened(self):
        return self.event("cash.opened", self.session_id, {"opening_cents": 15000, "operator_id": str(self.cashier.id),
                                                          "created_at": self.now})

    def paused(self, pause_id, at, reason="Almoço"):
        return self.event("cash.paused", pause_id, {"cash_session_id": self.session_id, "operator_id": str(self.cashier.id),
                                                    "reason": reason, "created_at": at})

    def resumed(self, pause_id, at, who=None):
        return self.event("cash.resumed", pause_id, {"cash_session_id": self.session_id,
                                                     "operator_id": str(who or self.cashier.id), "created_at": at})

    def closed(self, at, **extra):
        data = {"counted_cents": 15000, "expected_cents": 15500, "difference_cents": -500, "created_at": at,
                "operator_id": str(self.cashier.id), "supervisor_id": str(self.member.id)}
        data.update(extra)
        return self.event("cash.closed", self.session_id, data)

    def test_cloud_announces_the_capability(self):
        from .views_terminal import CLOUD_CAPABILITIES
        self.assertIn("cash_shifts", CLOUD_CAPABILITIES)

    def test_pause_resume_and_closing_totals_are_recorded_and_shown(self):
        pause = str(uuid.uuid4())
        totals = {"sales_count": 3, "gross_cents": 9000, "discount_cents": 500, "net_cents": 8500, "returns_cents": 0,
                  "voided_count": 1, "supplied_cents": 0, "withdrawn_cents": 1000,
                  "by_method": {"cash": 2000, "pix": 3000, "credit_account": 1500, "store_credit": 1000,
                                "other:" + str(uuid.uuid4()): 1000}}
        body = self.sync(self.opened(), self.paused(pause, self.now + 60_000),
                         self.resumed(pause, self.now + 15 * 60_000, who=self.member.id),
                         self.closed(self.now + 3_600_000, shift_operator_id=str(self.cashier.id),
                                     paused_ms=14 * 60_000, totals=totals))
        self.assertEqual(body["quarantined"], [])
        session = CashSession.objects.get(pk=self.session_id)
        self.assertEqual((session.paused_ms, session.closing_totals), (14 * 60_000, totals))
        self.assertEqual(session.operator, self.cashier)
        row = CashPause.objects.get(pk=pause)
        self.assertEqual((row.reason, row.operator, row.resumed_by), ("Almoço", self.cashier, self.member))
        self.assertEqual(int((row.ended_at - row.started_at).total_seconds()), 14 * 60)
        listing = self.panel("/api/v1/cash-sessions")["results"][0]
        self.assertEqual((listing["operator_name"], listing["pauses_count"], listing["paused_ms"], listing["paused_now"]),
                         ("Ana", 1, 14 * 60_000, False))
        self.assertEqual(listing["difference_cents"], -500)
        detail = self.panel(f"/api/v1/cash-sessions/{self.session_id}")
        self.assertEqual([(p["reason"], p["operator_name"], p["resumed_by_name"]) for p in detail["pauses"]],
                         [("Almoço", "Ana", "Dona")])
        self.assertEqual(detail["closing_totals"]["by_method"]["credit_account"], 1500)

    def test_same_events_again_change_nothing(self):
        pause = str(uuid.uuid4())
        events = [self.opened(), self.paused(pause, self.now + 1000), self.resumed(pause, self.now + 2000)]
        self.sync(*events)
        self.sync(*events)
        self.assertEqual(CashPause.objects.filter(session_id=self.session_id).count(), 1)

    def test_open_pause_is_shown_and_ends_with_the_closing(self):
        pause = str(uuid.uuid4())
        self.sync(self.opened(), self.paused(pause, self.now + 1000))
        listing = self.panel("/api/v1/cash-sessions")["results"][0]
        self.assertTrue(listing["paused_now"])
        self.sync(self.closed(self.now + 50_000))
        self.assertIsNotNone(CashPause.objects.get(pk=pause).ended_at)
        self.assertFalse(self.panel("/api/v1/cash-sessions")["results"][0]["paused_now"])

    def test_resumption_that_arrives_without_its_pause_is_kept(self):
        pause = str(uuid.uuid4())
        self.sync(self.opened(), self.resumed(pause, self.now + 9000))
        row = CashPause.objects.get(pk=pause)
        self.assertEqual(row.started_at, row.ended_at)
        self.sync(self.paused(pause, self.now + 3000))
        row.refresh_from_db()
        self.assertEqual(int((row.ended_at - row.started_at).total_seconds()), 6)

    def test_closing_of_an_older_terminal_is_still_accepted(self):
        body = self.sync(self.opened(), self.closed(self.now + 1000))
        self.assertEqual(body["quarantined"], [])
        session = CashSession.objects.get(pk=self.session_id)
        self.assertEqual((session.paused_ms, session.closing_totals, session.difference_cents), (0, {}, -500))

    def test_malformed_shift_data_goes_to_quarantine_alone(self):
        bad_totals = self.closed(self.now + 1000, totals={"by_method": {"cash": "muito"}})
        bad_pause = self.event("cash.paused", str(uuid.uuid4()), {"cash_session_id": self.session_id,
                                                                  "operator_id": str(self.cashier.id),
                                                                  "reason": "x" * 500, "created_at": self.now})
        body = self.sync(self.opened(), bad_pause, bad_totals)
        self.assertEqual({row["event_id"] for row in body["quarantined"]},
                         {bad_pause["event_id"], bad_totals["event_id"]})
        self.assertEqual(SyncConflict.objects.filter(reason="rejected_event").count(), 2)
        self.assertIsNone(CashSession.objects.get(pk=self.session_id).closed_at)

    def test_pause_cannot_move_to_another_shift(self):
        pause = str(uuid.uuid4())
        self.sync(self.opened(), self.paused(pause, self.now + 1000))
        other_session = str(uuid.uuid4())
        replay = self.event("cash.opened", other_session, {"opening_cents": 0, "operator_id": str(self.cashier.id),
                                                          "created_at": self.now})
        moved = self.event("cash.resumed", pause, {"cash_session_id": other_session,
                                                   "operator_id": str(self.cashier.id), "created_at": self.now + 5})
        body = self.sync(replay, moved)
        self.assertEqual([row["event_id"] for row in body["quarantined"]], [moved["event_id"]])
