"""Fase 3: caixas adicionais (estações). Limite na licença, etiqueta `station` nos
eventos, caixa principal da loja (hub), terminal.config com `stations` e restore
com um caixa aberto por estação."""
import base64
import io
import json
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from shared.licensing import verify_lease
from . import views_terminal
from .api_common import digest
from .models import (AuditEvent, CashSession, Membership, Plan, Sale, Shop, Subscription, SyncConflict, SyncEvent,
                     Terminal)
from .station_logic import effective_hub_id, station_of, stations_allowed, valid_stations
from .tests_fase2_recuperacao import DAY_MS, Fase2Base, sha


class Fase3Base(Fase2Base):
    def setUp(self):
        super().setUp()
        Plan.objects.filter(slug="management").update(max_stations=2)
        self.manager = get_user_model().objects.create_user(username="ger@example.test", email="ger@example.test",
                                                            password="Synthetic-42!")
        Membership.objects.create(tenant=self.tenant, user=self.manager, name="Gerente", role="manager")

    def assertClean(self, body, message=None):
        """Nothing quarantined and no divergence (the helper product has no stock in the cloud)."""
        self.assertEqual(body["quarantined"], [], message)
        self.assertEqual([row["reason"] for row in body["conflicts"] if row["reason"] != "negative_stock"], [], message)

    def second_terminal(self, name="Caixa 02", shop=None):
        token = "token-segundo-" + uuid.uuid4().hex
        row = Terminal.objects.create(tenant=self.tenant, shop=shop or self.shop, name=name,
                                      device_id="pc-" + uuid.uuid4().hex[:8], token_hash=sha(token))
        return row, token

    def claims(self, token=None, terminal=None):
        response = self.term("POST", "/api/v1/terminal/lease", {}, token=token)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        return verify_lease(body["lease"], body["public_key"],
                            expected={"terminal_id": (terminal or self.terminal).id})

    def station(self, event, station_id, name):
        event["station"] = {"id": station_id, "name": name}
        return event

    def panel_terminal(self, terminal=None):
        return self.panel("GET", f"/api/v1/terminals/{(terminal or self.terminal).id}").json()


class MigracaoFase3Tests(TransactionTestCase):
    def test_migrations_apply_revert_and_never_overwrite_a_chosen_amount(self):
        before, schema = [("core", "0038_terminal_former_tokens")], [("core", "0039_fase3_estacoes")]
        after = [("core", "0040_fase3_plan_max_stations")]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        try:
            with connection.cursor() as cursor:
                columns = [column.name for column in connection.introspection.get_table_description(cursor, "core_plan")]
            self.assertNotIn("max_stations", columns)
            executor = MigrationExecutor(connection)
            executor.migrate(schema)
            PlanOld = executor.loader.project_state(schema).apps.get_model("core", "Plan")
            for slug, amount in (("essential", 0), ("management", 7), ("advanced", 0), ("sob-medida", 0)):
                PlanOld.objects.update_or_create(slug=slug, defaults={"name": slug, "max_stations": amount})
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            PlanNew = executor.loader.project_state(after).apps.get_model("core", "Plan")
            self.assertEqual(dict(PlanNew.objects.filter(slug__in=("essential", "management", "advanced", "sob-medida"))
                                  .values_list("slug", "max_stations")),
                             {"essential": 1, "management": 7, "advanced": 4, "sob-medida": 0})
            executor = MigrationExecutor(connection)
            executor.migrate(before)  # reverts 0040 and 0039
            with connection.cursor() as cursor:
                names = {table: [column.name for column in connection.introspection.get_table_description(cursor, table)]
                         for table in ("core_plan", "core_shop", "core_sale", "core_cashsession")}
            self.assertNotIn("max_stations", names["core_plan"])
            self.assertNotIn("hub_terminal_id", names["core_shop"])
            self.assertNotIn("station_id", names["core_sale"])
            self.assertNotIn("station_id", names["core_cashsession"])
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())

    def test_no_pending_model_change(self):
        call_command("makemigrations", "--check", "--dry-run", stdout=io.StringIO(), stderr=io.StringIO())


class LicencaEstacoesTests(Fase3Base):
    def test_hub_gets_the_plan_amount_and_the_second_terminal_gets_zero(self):
        self.assertIn("stations", views_terminal.CLOUD_CAPABILITIES)
        other, other_token = self.second_terminal()
        hub = self.claims()
        self.assertEqual((hub["max_stations"], hub["hub"]), (2, True))
        second = self.claims(other_token, other)
        self.assertEqual((second["max_stations"], second["hub"]), (0, False))
        self.assertEqual(stations_allowed(self.terminal, self.sub.plan), 2)
        self.assertEqual(stations_allowed(other, self.sub.plan), 0)

    def test_changing_the_plan_changes_the_claim_on_the_next_renewal(self):
        self.assertEqual(self.claims()["max_stations"], 2)
        Plan.objects.filter(pk=self.sub.plan_id).update(max_stations=5)
        self.assertEqual(self.claims()["max_stations"], 5)
        self.sub.plan = Plan.objects.get(slug="essential")
        self.sub.save()
        Plan.objects.filter(slug="essential").update(max_stations=0)
        self.assertEqual(self.claims()["max_stations"], 0)

    def test_pair_and_bootstrap_carry_the_claims_and_the_capability(self):
        response = self.term("GET", "/api/v1/terminal/bootstrap")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertIn("stations", body["capabilities"])
        claims = verify_lease(body["lease"], body["public_key"])
        self.assertEqual((claims["max_stations"], claims["hub"]), (2, True))
        self.assertEqual((body["terminal"]["hub"], body["terminal"]["max_stations"]), (True, 2))
        paired = self.pair(self.transfer()["recovery_code"]).json()
        self.assertIn("stations", paired["capabilities"])
        self.assertEqual(verify_lease(paired["lease"], paired["public_key"])["max_stations"], 2)

    def test_each_shop_has_its_own_hub(self):
        branch = Shop.objects.create(tenant=self.tenant, name="Filial")
        other, other_token = self.second_terminal("Caixa filial", branch)
        self.assertEqual((self.claims()["hub"], self.claims(other_token, other)["hub"]), (True, True))
        self.assertEqual(self.claims(other_token, other)["max_stations"], 2)


class CaixaPrincipalTests(Fase3Base):
    def test_single_terminal_is_the_hub_without_anyone_choosing(self):
        self.assertIsNone(Shop.objects.get(pk=self.shop.pk).hub_terminal_id)
        self.assertEqual(effective_hub_id(self.shop), self.terminal.id)
        data = self.panel_terminal()
        self.assertEqual((data["hub"], data["max_stations"], data["stations"], data["stations_used"]),
                         (True, 2, [], 0))

    def test_unpaired_or_revoked_terminal_is_never_the_default_hub(self):
        waiting = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Aguardando")
        Terminal.objects.filter(pk=waiting.pk).update(created_at=timezone.now() - timedelta(days=30))
        self.assertEqual(effective_hub_id(self.shop), self.terminal.id)
        self.assertIs(self.panel_terminal(waiting)["hub"], False)

    def test_patch_hub_switches_and_audits(self):
        other, other_token = self.second_terminal()
        response = self.panel("PATCH", f"/api/v1/terminals/{other.id}", {"hub": True})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()["hub"], response.json()["max_stations"]), (True, 2))
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).hub_terminal_id, other.id)
        self.assertEqual((self.panel_terminal()["hub"], self.panel_terminal()["max_stations"]), (False, 0))
        audit = AuditEvent.objects.get(action="terminal.hub_set")
        self.assertEqual((audit.subject_id, audit.detail["previous_terminal_id"], audit.actor_id),
                         (str(other.id), str(self.terminal.id), self.owner.id))
        self.assertEqual((self.claims()["max_stations"], self.claims()["hub"]), (0, False))
        self.assertEqual(self.claims(other_token, other)["max_stations"], 2)
        # Choosing the current hub again changes nothing and is not audited twice.
        self.assertEqual(self.panel("PATCH", f"/api/v1/terminals/{other.id}", {"hub": True}).status_code, 200)
        self.assertEqual(AuditEvent.objects.filter(action="terminal.hub_set").count(), 1)

    def test_patch_hub_validation_and_roles(self):
        other, _ = self.second_terminal()
        path = f"/api/v1/terminals/{other.id}"
        for invalid in (False, "sim", 1, None):
            self.assertEqual(self.panel("PATCH", path, {"hub": invalid}).status_code, 400, invalid)
        for user in (self.manager, self.cashier):
            self.assertEqual(self.panel("PATCH", path, {"hub": True}, user=user).status_code, 403)
        self.assertIsNone(Shop.objects.get(pk=self.shop.pk).hub_terminal_id)
        self.assertEqual(self.panel("PATCH", path, {"hub": True}, user=self.admin).status_code, 200)
        Terminal.objects.filter(pk=self.terminal.pk).update(revoked=True)
        response = self.panel("PATCH", f"/api/v1/terminals/{self.terminal.id}", {"hub": True})
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).hub_terminal_id, other.id)

    def test_revoked_terminal_stops_being_the_hub(self):
        other, other_token = self.second_terminal()
        self.panel("PATCH", f"/api/v1/terminals/{other.id}", {"hub": True})
        response = self.panel("PATCH", f"/api/v1/terminals/{other.id}", {"revoked": True})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIs(response.json()["hub"], False)
        self.assertIsNone(Shop.objects.get(pk=self.shop.pk).hub_terminal_id)
        self.assertEqual((self.claims()["max_stations"], self.claims()["hub"]), (2, True))
        # Even without clearing the pointer, a revoked hub does not count.
        Shop.objects.filter(pk=self.shop.pk).update(hub_terminal=other)
        self.assertEqual(effective_hub_id(Shop.objects.get(pk=self.shop.pk)), self.terminal.id)

    def test_hub_is_isolated_between_companies(self):
        from .models import Tenant
        foreign = Tenant.objects.create(name="Outra")
        foreign_shop = Shop.objects.create(tenant=foreign, name="Loja")
        theirs = Terminal.objects.create(tenant=foreign, shop=foreign_shop, name="Deles", device_id="pc-x",
                                         token_hash=sha("token-deles-" + "x" * 30))
        self.assertEqual(self.panel("PATCH", f"/api/v1/terminals/{theirs.id}", {"hub": True}).status_code, 404)
        self.assertIsNone(Shop.objects.get(pk=foreign_shop.pk).hub_terminal_id)


class EtiquetaDeEstacaoTests(Fase3Base):
    def test_station_of_reads_only_a_well_formed_label(self):
        self.assertEqual(station_of({"station": {"id": "3f2a_b-9", "name": "Caixa 2"}}), ("3f2a_b-9", "Caixa 2"))
        self.assertEqual(station_of({"station": {"id": "server", "name": ""}}), ("server", ""))
        for broken in (None, "caixa 2", 7, [], {}, {"id": "ok"}, {"id": "a b", "name": "x"}, {"id": "", "name": "x"},
                       {"id": "x" * 65, "name": "x"}, {"id": "ok", "name": "x" * 81}, {"id": "ok", "name": 3},
                       {"id": 5, "name": "x"}, {"id": "ok", "name": "x", "token": "segredo"}):
            self.assertEqual(station_of({"station": broken}), ("", ""), broken)
        self.assertEqual(station_of({}), ("", ""))

    def test_cash_opened_and_sale_keep_the_station(self):
        opened = self.station(self.opened(), "3f2a9c", "Caixa 2")
        sale = self.station(self.sale(), "3f2a9c", "Caixa 2")
        body = self.sync(opened, sale)
        self.assertClean(body)
        session = CashSession.objects.get(pk=self.session_id)
        self.assertEqual((session.station_id, session.station_name), ("3f2a9c", "Caixa 2"))
        row = Sale.objects.get(pk=sale["aggregate_id"])
        self.assertEqual((row.station_id, row.station_name), ("3f2a9c", "Caixa 2"))
        # The label is outside "data": the content hash of the sale does not cover it.
        self.assertEqual(row.content_hash, digest(sale["data"]))
        self.assertEqual(row.content_hash, digest({key: value for key, value in sale.items() if key != "station"}["data"]))
        self.assertEqual(SyncEvent.objects.get(event_id=sale["event_id"]).payload["station"],
                         {"id": "3f2a9c", "name": "Caixa 2"})
        listed = self.panel("GET", "/api/v1/sales").json()["results"][0]
        self.assertEqual((listed["station_id"], listed["station_name"]), ("3f2a9c", "Caixa 2"))
        detail = self.panel("GET", f"/api/v1/sales/{sale['aggregate_id']}").json()
        self.assertEqual(detail["station_name"], "Caixa 2")

    def test_events_without_station_are_accepted_with_empty_fields(self):
        sale = self.sale()
        body = self.sync(self.opened(), sale)
        self.assertClean(body)
        session, row = CashSession.objects.get(pk=self.session_id), Sale.objects.get(pk=sale["aggregate_id"])
        self.assertEqual((session.station_id, session.station_name, row.station_id, row.station_name), ("", "", "", ""))

    def test_malformed_station_is_accepted_with_empty_fields_and_no_conflict(self):
        for index, broken in enumerate(("caixa 2", None, 3, [], {"id": "a b", "name": "x"}, {"id": "ok"},
                                        {"id": "ok", "name": "x" * 81}, {"id": "ok", "name": "x", "extra": 1})):
            session = str(uuid.uuid4())
            opened, sale = self.opened(session, self.now + index), self.sale(session=session)
            opened["station"], sale["station"] = broken, broken
            body = self.sync(opened, sale)
            self.assertClean(body, broken)
            row = Sale.objects.get(pk=sale["aggregate_id"])
            self.assertEqual((CashSession.objects.get(pk=session).station_id, row.station_id, row.station_name),
                             ("", "", ""), broken)
        self.assertEqual(SyncConflict.objects.exclude(reason="negative_stock").count(), 0)

    def test_identical_resend_is_recognized(self):
        opened = self.station(self.opened(), "3f2a9c", "Caixa 2")
        sale = self.station(self.sale(), "3f2a9c", "Caixa 2")
        self.sync(opened, sale)
        body = self.sync(opened, sale)
        self.assertClean(body)
        self.assertEqual((Sale.objects.count(), CashSession.objects.count(), SyncEvent.objects.count()), (1, 1, 2))

    def test_station_is_accepted_on_every_event_type(self):
        sale = self.station(self.sale(), "c2", "Caixa 2")
        events = [self.station(self.opened(), "c2", "Caixa 2"), sale,
                  self.station(self.withdrawn(), "c2", "Caixa 2"), self.station(self.receipt(), "c2", "Caixa 2"),
                  self.station(self.event("sale.voided", sale["aggregate_id"],
                                          {"sale_id": sale["aggregate_id"], "reason": "Erro", "created_at": self.now + 9}),
                               "c2", "Caixa 2"),
                  self.station(self.event("stock.adjusted", str(uuid.uuid4()),
                                          {"product_id": str(self.product.id), "delta_milli": 1000, "reason": "Contagem",
                                           "created_at": self.now}), "server", "Caixa principal"),
                  self.station(self.closed(self.session_id, self.now + 5000), "c2", "Caixa 2"),
                  self.station(self.config(self.now), "server", "Caixa principal")]
        self.assertEqual(self.sync(*events)["quarantined"], [])

    def test_unknown_top_level_key_is_still_quarantined(self):
        strange = self.opened()
        strange["caixa"] = {"id": "c2", "name": "Caixa 2"}
        body = self.sync(strange)
        self.assertEqual([row["event_id"] for row in body["quarantined"]], [strange["event_id"]])
        self.assertFalse(CashSession.objects.exists())


class VariosCaixasTests(Fase3Base):
    def open_three(self):
        self.sessions = {name: str(uuid.uuid4()) for name in ("server", "c2", "c3")}
        labels = {"server": "Caixa principal", "c2": "Caixa 2", "c3": "Caixa 3"}
        body = self.sync(*[self.station(self.opened(session, self.now + index), station, labels[station])
                           for index, (station, session) in enumerate(self.sessions.items())])
        self.assertClean(body)

    def close(self, session, created_at, counted, expected, station=None):
        event = self.event("cash.closed", session, {"counted_cents": counted, "expected_cents": expected,
                                                    "difference_cents": counted - expected, "created_at": created_at})
        return self.station(event, station, "Caixa") if station else event

    def test_three_registers_stay_open_together_and_close_one_by_one(self):
        self.open_three()
        self.assertEqual(CashSession.objects.filter(terminal=self.terminal, closed_at__isnull=True).count(), 3)
        self.sync(self.close(self.sessions["c2"], self.now + 5000, 10000, 10000, "c2"))
        still_open = set(CashSession.objects.filter(closed_at__isnull=True).values_list("station_id", flat=True))
        self.assertEqual(still_open, {"server", "c3"})
        closed = CashSession.objects.get(pk=self.sessions["c2"])
        self.assertEqual((closed.station_id, closed.station_name, closed.counted_cents), ("c2", "Caixa 2", 10000))

    def test_closing_twice_is_still_a_conflict(self):
        self.open_three()
        self.assertEqual(self.sync(self.close(self.sessions["c2"], self.now + 5000, 12345, 11450, "c2"))["conflicts"], [])
        body = self.sync(self.close(self.sessions["c2"], self.now + 9000, 999, 11800, "server"))
        self.assertEqual([row["reason"] for row in body["conflicts"]], ["cash_closed_twice"])
        self.assertEqual(CashSession.objects.get(pk=self.sessions["c2"]).counted_cents, 999)
        self.assertEqual(CashSession.objects.filter(closed_at__isnull=True).count(), 2)

    def test_cash_sessions_api_shows_and_filters_by_station(self):
        self.open_three()
        old = str(uuid.uuid4())  # sent by a terminal older than "stations": no label
        self.sync(self.opened(old, self.now - DAY_MS), self.closed(old, self.now - DAY_MS + 9000))
        rows = self.panel("GET", "/api/v1/cash-sessions").json()["results"]
        self.assertEqual({(row["station_id"], row["station_name"]) for row in rows},
                         {("server", "Caixa principal"), ("c2", "Caixa 2"), ("c3", "Caixa 3"), ("", "")})
        only = self.panel("GET", "/api/v1/cash-sessions?station_id=c2").json()["results"]
        self.assertEqual([row["id"] for row in only], [self.sessions["c2"]])
        main = self.panel("GET", "/api/v1/cash-sessions?station_id=server").json()["results"]
        self.assertEqual({row["id"] for row in main}, {self.sessions["server"], old})
        both = self.panel("GET", "/api/v1/cash-sessions?station_id=c3&status=open").json()["results"]
        self.assertEqual([row["id"] for row in both], [self.sessions["c3"]])
        self.assertEqual(self.panel("GET", "/api/v1/cash-sessions?station_id=nenhum").json()["results"], [])
        self.assertEqual(self.panel("GET", "/api/v1/cash-sessions?station_id=a%20b").status_code, 400)
        detail = self.panel("GET", f"/api/v1/cash-sessions/{self.sessions['c2']}").json()
        self.assertEqual((detail["station_id"], detail["station_name"]), ("c2", "Caixa 2"))


class ConfigComEstacoesTests(Fase3Base):
    STATIONS = [{"id": "3f2a9c", "name": "Caixa 2", "revoked": False},
                {"id": "77aa01", "name": "Caixa 3", "revoked": True}]

    def test_valid_list_is_stored_shown_and_sets_the_hub_when_null(self):
        other, other_token = self.second_terminal()
        Terminal.objects.filter(pk=other.pk).update(created_at=timezone.now() - timedelta(days=30))
        self.assertEqual(effective_hub_id(self.shop), other.id)  # the oldest one, nobody chose
        event = self.config(self.now, stations=self.STATIONS)
        self.assertEqual(self.sync(event)["quarantined"], [])
        self.assertEqual(Terminal.objects.get(pk=self.terminal.pk).config["stations"], self.STATIONS)
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).hub_terminal_id, self.terminal.id)
        audit = AuditEvent.objects.get(action="terminal.hub_set")
        self.assertEqual((audit.subject_id, audit.detail["source"]), (str(self.terminal.id), "terminal.config"))
        data = self.panel_terminal()
        self.assertEqual((data["hub"], data["max_stations"], data["stations"], data["stations_used"]),
                         (True, 2, self.STATIONS, 1))
        self.assertIs(self.panel_terminal(other)["hub"], False)
        listed = {row["id"]: row for row in self.panel("GET", "/api/v1/terminals").json()["results"]}
        self.assertEqual(listed[str(self.terminal.id)]["stations_used"], 1)

    def test_a_hub_already_chosen_is_not_replaced_by_a_configuration(self):
        other, other_token = self.second_terminal()
        self.panel("PATCH", f"/api/v1/terminals/{other.id}", {"hub": True})
        self.sync(self.config(self.now, stations=self.STATIONS))
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).hub_terminal_id, other.id)
        self.assertEqual(AuditEvent.objects.filter(action="terminal.hub_set").count(), 1)

    def test_configuration_without_stations_or_with_an_empty_list_sets_no_hub(self):
        self.sync(self.config(self.now), self.config(self.now + 1, stations=[]))
        self.assertIsNone(Shop.objects.get(pk=self.shop.pk).hub_terminal_id)
        self.assertEqual(self.panel_terminal()["stations"], [])

    def test_malformed_list_is_quarantined(self):
        good = self.config(self.now, stations=self.STATIONS)
        self.sync(good)
        row = self.STATIONS[0]
        broken = [self.config(self.now + 1, stations={"id": "x"}),
                  self.config(self.now + 2, stations=[{"id": "3f2a9c", "name": "Caixa 2"}]),
                  self.config(self.now + 3, stations=[{**row, "token": "segredo"}]),
                  self.config(self.now + 4, stations=[{**row, "id": "a b"}]),
                  self.config(self.now + 5, stations=[{**row, "name": "x" * 81}]),
                  self.config(self.now + 6, stations=[{**row, "revoked": 0}]),
                  self.config(self.now + 7, stations=["3f2a9c"]),
                  self.config(self.now + 8, stations=[{**row, "id": f"c{n}"} for n in range(65)])]
        body = self.sync(*broken)
        self.assertEqual([item["event_id"] for item in body["quarantined"]], [event["event_id"] for event in broken])
        self.assertEqual(Terminal.objects.get(pk=self.terminal.pk).config, good["data"])
        self.assertTrue(valid_stations([{**row, "id": f"c{n}"} for n in range(64)]))

    def test_send_only_token_changes_neither_the_configuration_nor_the_hub(self):
        new_token = self.swap()
        stale = self.config(self.now + 5000, stations=self.STATIONS)
        body = self.sync(stale)  # old computer, send-only
        self.assertEqual((body["send_only"], body["quarantined"]), (True, []))
        self.assertEqual(Terminal.objects.get(pk=self.terminal.pk).config, {})
        self.assertIsNone(Shop.objects.get(pk=self.shop.pk).hub_terminal_id)
        self.assertEqual(self.sync(self.config(self.now, stations=self.STATIONS[:1]), token=new_token)["quarantined"], [])
        self.assertEqual(self.panel_terminal()["stations"], self.STATIONS[:1])

    def test_stored_configuration_with_a_broken_list_never_breaks_the_panel(self):
        Terminal.objects.filter(pk=self.terminal.pk).update(config={"stations": "quebrado"})
        self.assertEqual(self.panel_terminal()["stations"], [])


class RestoreComEstacoesTests(Fase3Base):
    def sessions(self, records):
        return [row for row in records if row["kind"] == "cash_session"]

    def test_two_open_registers_come_back_with_their_station(self):
        main, second, stale = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        sale_main = self.station(self.sale(session=main), "server", "Caixa principal")
        sale_second = self.station(self.sale(session=second, qty=2000), "c2", "Caixa 2")
        old_sale = self.station(self.sale(created_at=self.now - 200 * DAY_MS, session=stale), "c2", "Caixa 2")
        self.sync(self.station(self.opened(stale, self.now - 200 * DAY_MS), "c2", "Caixa 2"), old_sale,
                  self.station(self.opened(main, self.now - 1000), "server", "Caixa principal"),
                  self.station(self.opened(second, self.now - 500), "c2", "Caixa 2"), sale_main, sale_second)
        self.assertIs(self.transfer()["open_cash_session"], True)
        records, last, pages = self.restore_all(limit=2)
        self.assertGreater(len(pages), 1)
        found = self.sessions(records)
        self.assertEqual([(row["id"], row["station_id"], row["station_name"], row["closed_at"]) for row in found],
                         [(main, "server", "Caixa principal", None), (second, "c2", "Caixa 2", None)])
        # The older open session of "c2" was closed at the counter: stale, and its old sale stays out.
        self.assertEqual(last["totals"]["cash_sessions"], {"count": 2, "open_count": 2, "stale_open_omitted": 1})
        sales = {row["id"]: row for row in records if row["kind"] == "sale"}
        self.assertEqual(set(sales), {sale_main["aggregate_id"], sale_second["aggregate_id"]})
        self.assertEqual(sales[sale_second["aggregate_id"]]["station_id"], "c2")
        self.assertEqual(sales[sale_main["aggregate_id"]]["station_id"], "server")
        self.assertNotIn("station_id", sales[sale_second["aggregate_id"]]["data"])
        self.assertEqual(sales[sale_second["aggregate_id"]]["data"], sale_second["data"])
        self.assertEqual(last["totals"]["sales"], {"count": 2, "total_cents": 3000, "voided_count": 0})
        self.assertEqual(last["progress"]["position"], len(records))
        self.assertEqual(records, self.restore("", 500).json()["records"])

    def test_unlabelled_sessions_belong_to_the_main_register(self):
        before, after = str(uuid.uuid4()), str(uuid.uuid4())
        # Opened before the terminal knew "stations" and never closed in the cloud;
        # the same computer later opened another one, now labelled "server".
        self.sync(self.opened(before, self.now - 3 * DAY_MS),
                  self.station(self.opened(after, self.now), "server", "Caixa principal"))
        records, last, _ = self.restore_all(limit=10)
        self.assertEqual([row["id"] for row in self.sessions(records)], [after])
        self.assertEqual(last["totals"]["cash_sessions"]["stale_open_omitted"], 1)

    def test_old_terminal_with_one_open_session_is_unchanged(self):
        self.sync(self.opened(), self.sale())
        records, last, _ = self.restore_all(limit=10)
        (session,) = self.sessions(records)
        self.assertEqual((session["id"], session["station_id"], session["station_name"]), (self.session_id, "", ""))
        self.assertEqual([row["station_id"] for row in records if row["kind"] == "sale"], [""])
        self.assertEqual(last["totals"]["cash_sessions"], {"count": 1, "open_count": 1, "stale_open_omitted": 0})

    def test_cursor_issued_before_this_version_stays_valid(self):
        self.sync(self.opened(), *[self.sale(created_at=self.now + n) for n in range(4)])
        first = self.restore("", 2).json()
        cursor = first["next_cursor"]
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        self.assertEqual(len(data["open"]), 1)
        data["open"] = [str(uuid.UUID(value)) for value in data["open"]]  # as the previous version wrote it
        old = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
        response = self.restore(old, 2)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["records"], self.restore(cursor, 2).json()["records"])

    def test_cursor_carries_many_open_registers_but_not_an_absurd_amount(self):
        events = [self.station(self.opened(str(uuid.uuid4()), self.now + n), f"c{n}", f"Caixa {n}") for n in range(50)]
        self.sync(*events[:50])
        self.sync(*[self.station(self.sale(session=event["aggregate_id"]), event["station"]["id"], "Caixa")
                    for event in events[:3]])
        records, last, pages = self.restore_all(limit=20)
        self.assertEqual(last["totals"]["cash_sessions"], {"count": 50, "open_count": 50, "stale_open_omitted": 0})
        self.assertLess(max(len(page["next_cursor"] or "") for page in pages), 4000)
        cursor = pages[0]["next_cursor"]
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        data["open"] = [uuid.uuid4().hex for _ in range(65)]
        forged = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
        self.assertEqual(self.restore(forged, 2).status_code, 400)


class PlanosEAssinaturaTests(Fase3Base):
    def setUp(self):
        super().setUp()
        self.root = get_user_model().objects.create_superuser(username="root-f3@example.test",
                                                              email="root-f3@example.test", password="Root-42!")
        self.body = {"slug": "estacoes", "name": "Estações", "price_month_cents": 1000, "price_year_cents": 10000,
                     "terminal_limit": 1, "shop_limit": 1, "features": []}

    def platform(self, verb, path, body=None):
        self.client.force_login(self.root)
        return self.client.generic(verb, path, data=json.dumps(body) if body is not None else None,
                                   content_type="application/json")

    def test_plan_api_exposes_and_validates_max_stations(self):
        created = self.platform("POST", "/api/v1/platform/plans", self.body)
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["max_stations"], 0)
        path = "/api/v1/platform/plans/" + created.json()["id"]
        for invalid in (-1, 51, "2", 2.5, True, None):
            self.assertEqual(self.platform("PATCH", path, {"max_stations": invalid}).status_code, 400, invalid)
            self.assertEqual(self.platform("POST", "/api/v1/platform/plans",
                                           {**self.body, "slug": "outro", "max_stations": invalid}).status_code, 400)
        for valid in (0, 50, 3):
            response = self.platform("PATCH", path, {"max_stations": valid})
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()["max_stations"], valid)
        self.assertEqual(self.platform("GET", path).json()["max_stations"], 3)
        listed = self.platform("GET", "/api/v1/platform/plans").json()
        self.assertTrue(all("max_stations" in row for row in listed["results"]))
        other = self.platform("POST", "/api/v1/platform/plans", {**self.body, "slug": "quatro", "max_stations": 4})
        self.assertEqual(other.json()["max_stations"], 4)

    def test_model_validator_rejects_out_of_range(self):
        with self.assertRaises(ValidationError):
            Plan(slug="x", name="X", max_stations=51, features=["stock"]).full_clean()
        Plan(slug="y", name="Y", max_stations=50, features=["stock"]).full_clean()

    def test_subscription_shows_the_limit(self):
        body = self.panel("GET", "/api/v1/subscription").json()
        self.assertEqual((body["license"]["max_stations"], body["plan"]["max_stations"]), (2, 2))
        self.assertEqual(self.panel("GET", "/api/v1/subscription", user=self.cashier).json()["license"]["max_stations"], 2)
