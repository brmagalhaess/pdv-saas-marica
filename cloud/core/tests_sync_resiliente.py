"""The register queue never stalls: per-event quarantine and offline facts kept as sold.

Contract of POST /api/v1/terminal/sync: every received event comes back in
`accepted_event_ids`; the ones that could not be applied are also listed in
`quarantined` and stored as SyncConflict(reason="rejected_event").
"""
import hashlib
import json
import time
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from . import commission_logic
from .models import (CashSession, Commission, CreditReceipt, Customer, CustomerCredit, FinanceEntry, FiscalDocument,
                     Membership, PaymentMethod, Plan, Product, Promotion, Sale, SaleItem, SaleReturn, Shop,
                     StockMovement, Subscription, SyncConflict, SyncEvent, Tenant, Terminal)
from .payment_methods import ensure as ensure_payment_methods
from .sale_logic import persist_sale
from .stock_balance import balance


class SyncResilienteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="dona@example.test", email="dona@example.test",
                                             password="Synthetic-42!", first_name="Dona")
        cls.tenant = Tenant.objects.create(name="Mercado Fila")
        cls.other_tenant = Tenant.objects.create(name="Mercado Vizinho")
        cls.member = Membership.objects.create(tenant=cls.tenant, user=cls.owner, name="Dona", role="owner")
        cls.shop = Shop.objects.create(tenant=cls.tenant, name="Matriz")
        Subscription.objects.create(tenant=cls.tenant, plan=Plan.objects.get(slug="management"), status="trial",
                                    trial_ends_at=timezone.now() + timedelta(days=14))
        cls.product = Product.objects.create(tenant=cls.tenant, sku="ARROZ", name="Arroz", price_cents=1000)
        cls.foreign_product = Product.objects.create(tenant=cls.other_tenant, sku="FEIJAO", name="Feijão",
                                                     price_cents=1000)
        cls.token = "terminal-token-" + uuid.uuid4().hex
        cls.terminal = Terminal.objects.create(tenant=cls.tenant, shop=cls.shop, name="Caixa 1", device_id="fila-device",
                                               token_hash=hashlib.sha256(cls.token.encode()).hexdigest())
        cls.identity = {"tenant_id": str(cls.tenant.id), "shop_id": str(cls.shop.id), "terminal_id": str(cls.terminal.id)}

    def setUp(self):
        self.now = int(time.time() * 1000)
        self.session_id = uuid.uuid4().hex

    # Helpers -----------------------------------------------------------------

    def post(self, events, token=None):
        return self.client.generic("POST", "/api/v1/terminal/sync", data=json.dumps({"events": events}),
                                   content_type="application/json",
                                   HTTP_AUTHORIZATION=f"Bearer {token or self.token}")

    def sync(self, *events):
        """Send a batch that must be acknowledged in full, whatever each event's fate."""
        response = self.post(list(events))
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["accepted_event_ids"], [event["event_id"] for event in events])
        return body

    def panel(self, verb, path, body=None):
        self.client.force_login(self.owner)
        return self.client.generic(verb, path, data=json.dumps(body) if body is not None else None,
                                   content_type="application/json", HTTP_X_TENANT_ID=str(self.tenant.id))

    def event(self, kind, aggregate, data):
        return {"event_id": uuid.uuid4().hex, "type": kind, "aggregate_id": aggregate,
                "identity": self.identity, "data": data}

    def sale(self, qty=1000, discount=0, payments=None, product=None, unit="UN", created_at=None, **extra):
        product = product or self.product
        gross = product.price_cents * qty // 1000
        total = gross - discount
        item = {"product_id": str(product.id), "name": product.name, "unit": unit, "quantity_milli": qty,
                "unit_price_cents": product.price_cents, "gross_cents": gross, "discount_cents": discount,
                "line_total_cents": total, "stock_controlled": True}
        item.update(extra.pop("item", {}))
        data = {"request_id": uuid.uuid4().hex, "cash_session_id": self.session_id,
                "created_at": created_at or self.now, "customer_id": None, "customer_name": "",
                "customer_document": "", "operator_id": str(self.member.id), "gross_cents": gross,
                "discount_cents": discount, "total_cents": total, "change_cents": 0, "items": [item],
                "payments": payments or [{"method": "cash", "amount_cents": total}]}
        data.update(extra)
        return self.event("sale.completed", str(uuid.uuid4()), data)

    def voided(self, sale, reason="Cliente desistiu"):
        return self.event("sale.voided", sale["aggregate_id"],
                          {"sale_id": sale["aggregate_id"], "reason": reason, "created_at": self.now + 10})

    def returned(self, sale, qty=1000, refund="cash"):
        amount = self.product.price_cents * qty // 1000
        return self.event("sale.returned", str(uuid.uuid4()), {
            "sale_id": sale["aggregate_id"], "refund_method": refund, "reason": "Produto avariado",
            "operator_id": str(self.member.id), "cash_session_id": self.session_id, "created_at": self.now + 10,
            "items": [{"product_id": str(self.product.id), "quantity_milli": qty, "amount_cents": amount}],
            "total_cents": amount})

    def receipt(self, customer_id, cents=500):
        return self.event("credit.received", str(uuid.uuid4()), {
            "customer_id": str(customer_id), "amount_cents": cents, "method": "cash",
            "operator_id": str(self.member.id), "cash_session_id": self.session_id, "created_at": self.now + 5})

    def adjustment(self, product_id, delta=2000):
        return self.event("stock.adjusted", str(uuid.uuid4()), {
            "product_id": str(product_id), "delta_milli": delta, "reason": "Contagem", "created_at": self.now})

    def opened(self):
        return self.event("cash.opened", self.session_id, {"opening_cents": 0, "operator_id": str(self.member.id),
                                                          "created_at": self.now})

    def reasons(self, event):
        return sorted(SyncConflict.objects.filter(event_id=event["event_id"]).values_list("reason", flat=True))

    # Quarantine protocol -----------------------------------------------------

    def test_rejected_event_in_the_middle_is_quarantined_and_the_rest_is_applied(self):
        broken = self.sale()
        broken["data"]["total_cents"] += 1  # totals do not add up: integrity error
        first, last = self.sale(), self.sale()
        orphan = self.adjustment(uuid.uuid4())
        body = self.sync(self.opened(), first, broken, orphan, last)

        self.assertEqual([row["event_id"] for row in body["quarantined"]], [broken["event_id"], orphan["event_id"]])
        self.assertEqual({row["reason"] for row in body["quarantined"]}, {"rejected_event"})
        self.assertEqual(body["quarantined"][0]["message"], "Totais da venda não conferem")
        self.assertTrue(Sale.objects.filter(pk=first["aggregate_id"]).exists())
        self.assertTrue(Sale.objects.filter(pk=last["aggregate_id"]).exists())
        self.assertFalse(Sale.objects.filter(pk=broken["aggregate_id"]).exists())
        self.assertTrue(CashSession.objects.filter(pk=uuid.UUID(self.session_id)).exists())
        # The raw event is kept and the administrator gets the payload, the error and the type.
        self.assertEqual(SyncEvent.objects.count(), 5)
        self.assertEqual(SyncEvent.objects.get(pk=broken["event_id"]).payload, broken)
        conflict = SyncConflict.objects.get(event_id=broken["event_id"])
        self.assertEqual((conflict.reason, conflict.status, conflict.event_type), ("rejected_event", "open", "sale.completed"))
        self.assertEqual(conflict.detail["payload"], broken)
        self.assertEqual((conflict.detail["error"], conflict.detail["type"], conflict.detail["status"]),
                         ("Totais da venda não conferem", "sale.completed", 400))
        # The terminal already has the payload: it is not echoed back.
        echoed = next(c for c in body["conflicts"] if c["event_id"] == broken["event_id"])
        self.assertNotIn("payload", echoed["detail"])
        listing = self.panel("GET", "/api/v1/sync/conflicts?status=open").json()["results"]
        row = next(r for r in listing if r["event_id"] == broken["event_id"])
        self.assertEqual((row["reason"], row["terminal_name"], row["detail"]["payload"]), ("rejected_event", "Caixa 1", broken))
        self.assertIn("Totais da venda não conferem", row["detail"]["message"])

    def test_resend_of_quarantined_event_is_acknowledged_without_reprocessing(self):
        orphan = self.voided(self.sale())  # the sale itself never reached the cloud
        first = self.sync(orphan)
        again = self.sync(orphan)
        for body in (first, again):
            self.assertEqual([row["event_id"] for row in body["quarantined"]], [orphan["event_id"]])
            self.assertEqual(body["quarantined"][0]["message"], "Venda original do estorno não encontrada")
        self.assertEqual(SyncConflict.objects.filter(event_id=orphan["event_id"]).count(), 1)
        self.assertEqual(SyncEvent.objects.count(), 1)

    def test_only_authentication_body_and_foreign_identity_refuse_the_whole_request(self):
        good = self.sale()
        stranger = self.sale()
        stranger["identity"] = {**self.identity, "terminal_id": str(uuid.uuid4())}
        self.assertEqual(self.post([good, stranger]).status_code, 403)
        self.assertEqual(self.post([]).status_code, 400)
        self.assertEqual(self.post([self.sale() for _ in range(51)]).status_code, 400)
        self.assertEqual(self.post({"not": "a list"}).status_code, 400)
        self.assertEqual(self.post(["not an event"]).status_code, 400)
        self.assertEqual(self.post([good], token="x" * 40).status_code, 401)
        self.assertEqual((SyncEvent.objects.count(), Sale.objects.count(), SyncConflict.objects.count()), (0, 0, 0))
        Terminal.objects.filter(pk=self.terminal.pk).update(revoked=True)
        self.assertEqual(self.post([good]).status_code, 401)

    def test_same_event_id_with_other_content_is_quarantined_and_keeps_the_original(self):
        original = self.sale()
        self.sync(original)
        forged = self.sale()
        forged["event_id"] = original["event_id"]
        body = self.sync(forged)
        self.assertEqual(body["quarantined"][0]["message"], "Evento já recebido com conteúdo diferente")
        self.assertTrue(Sale.objects.filter(pk=original["aggregate_id"]).exists())
        self.assertFalse(Sale.objects.filter(pk=forged["aggregate_id"]).exists())
        self.assertEqual(SyncEvent.objects.get(pk=original["event_id"]).payload, original)
        rejected = SyncConflict.objects.get(event_id=original["event_id"], reason="rejected_event")
        self.assertEqual(rejected.detail["payload"], forged)

    def test_malformed_events_are_quarantined_one_by_one(self):
        unknown = self.event("sale.teleported", str(uuid.uuid4()), {})
        bad_id = self.sale()
        bad_id["aggregate_id"] = "not-a-uuid"
        foreign = self.sale(product=self.foreign_product)
        cash = self.event("cash.opened", "not-a-uuid", {"opening_cents": 0, "operator_id": str(self.member.id),
                                                       "created_at": self.now})
        good = self.sale()
        body = self.sync(unknown, bad_id, foreign, cash, good)
        self.assertEqual([row["event_id"] for row in body["quarantined"]],
                         [unknown["event_id"], bad_id["event_id"], foreign["event_id"], cash["event_id"]])
        self.assertEqual(list(Sale.objects.values_list("pk", flat=True)), [uuid.UUID(good["aggregate_id"])])

    def test_unexpected_failure_quarantines_only_that_event(self):
        opened, good = self.opened(), self.sale()
        with patch("core.views_terminal.apply_cash_event", side_effect=RuntimeError("boom")), \
                self.assertLogs("core.views_terminal", level="ERROR"):
            body = self.sync(opened, good)
        self.assertEqual([row["event_id"] for row in body["quarantined"]], [opened["event_id"]])
        self.assertTrue(Sale.objects.filter(pk=good["aggregate_id"]).exists())
        self.assertEqual(SyncConflict.objects.get(event_id=opened["event_id"]).detail["status"], 500)

    def test_event_that_cannot_be_stored_as_json_is_still_quarantined(self):
        poisoned = self.sale(item={"stock_controlled": float("nan")})  # passes validation, refused by the database
        invalid = self.opened()
        invalid["data"]["opening_cents"] = float("inf")
        good = self.sale()
        with self.assertLogs("core.views_terminal", level="ERROR"):
            first = self.sync(poisoned, invalid, good)
        again = self.sync(poisoned, invalid, good)  # resend after a lost acknowledgement
        for body in (first, again):
            self.assertEqual([row["event_id"] for row in body["quarantined"]], [poisoned["event_id"], invalid["event_id"]])
        self.assertTrue(Sale.objects.filter(pk=good["aggregate_id"]).exists())
        self.assertFalse(Sale.objects.filter(pk=poisoned["aggregate_id"]).exists())
        self.assertEqual(SyncEvent.objects.count(), 3)
        self.assertIn("unserializable", SyncConflict.objects.get(event_id=invalid["event_id"]).detail["payload"])

    # 1. Resend is recognized before any validation ---------------------------

    def test_resend_of_accepted_sale_is_not_judged_again_after_rules_change(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(max_discount_bps=2000)
        sale = self.sale(qty=2000, discount=300)  # 15% with a 20% limit
        self.assertEqual(self.sync(sale)["conflicts"][0]["reason"], "negative_stock")
        Tenant.objects.filter(pk=self.tenant.pk).update(max_discount_bps=1000)
        Product.objects.filter(pk=self.product.pk).update(unit="KG", active=False)
        body = self.sync(sale)  # the acknowledgement was lost and the terminal sends it again
        self.assertEqual(body["quarantined"], [])
        self.assertEqual([c["reason"] for c in body["conflicts"]], ["negative_stock"])
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(self.reasons(sale), ["negative_stock"])

    def test_persist_sale_recognizes_a_stored_sale_before_validating(self):
        sale = self.sale()
        stored = persist_sale(self.terminal, sale)
        with patch("core.sale_logic.validate_sale", side_effect=AssertionError("revalidated")) as validate:
            self.assertEqual(persist_sale(self.terminal, sale).pk, stored.pk)
        validate.assert_not_called()

    # 2. Configuration changed after the offline sale -------------------------

    def test_discount_above_a_limit_lowered_later_is_accepted_with_conflict(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(max_discount_bps=1000)
        StockMovement.objects.create(tenant=self.tenant, shop=self.shop, product=self.product, delta_milli=5000,
                                     source="adjustment", reason="Entrada", idempotency_key="seed-discount")
        sale, later = self.sale(qty=2000, discount=300), self.sale()
        body = self.sync(sale, later)
        self.assertEqual(body["quarantined"], [])
        self.assertEqual([c["reason"] for c in body["conflicts"]], ["discount_over_limit"])
        stored = Sale.objects.get(pk=sale["aggregate_id"])
        self.assertEqual((stored.discount_cents, stored.total_cents, stored.status), (300, 1700, "completed"))
        detail = SyncConflict.objects.get(event_id=sale["event_id"]).detail
        self.assertEqual((detail["discount_cents"], detail["base_cents"], detail["limit_bps"], detail["discount_bps"]),
                         (300, 2000, 1000, 1500))
        self.assertEqual(detail["sale_id"], sale["aggregate_id"])
        self.assertTrue(Sale.objects.filter(pk=later["aggregate_id"]).exists())

    def test_unit_changed_after_the_sale_keeps_the_unit_sold(self):
        sale = self.sale()  # sold as 1 UN
        Product.objects.filter(pk=self.product.pk).update(unit="KG")
        weighed = Product.objects.create(tenant=self.tenant, sku="QUEIJO", name="Queijo", price_cents=4000, unit="UN")
        fraction = self.sale(product=weighed, qty=350, unit="KG")  # sold by weight, registered as UN meanwhile
        body = self.sync(sale, fraction)
        self.assertEqual(body["quarantined"], [])
        self.assertEqual(SaleItem.objects.get(sale_id=sale["aggregate_id"]).unit, "UN")
        self.assertEqual(SaleItem.objects.get(sale_id=fraction["aggregate_id"]).quantity_milli, 350)
        self.assertIn("unit_changed", self.reasons(sale))
        self.assertIn("unit_changed", self.reasons(fraction))
        item = SyncConflict.objects.get(event_id=sale["event_id"], reason="unit_changed").detail["items"][0]
        self.assertEqual((item["sold_unit"], item["current_unit"], item["product_name"]), ("UN", "KG", "Arroz"))
        nonsense = self.sale(unit="CX")  # a unit that never existed is still an integrity error
        self.assertEqual([row["event_id"] for row in self.sync(nonsense)["quarantined"]], [nonsense["event_id"]])

    def test_product_without_stock_control_or_deactivated_is_accepted_with_conflict(self):
        Product.objects.filter(pk=self.product.pk).update(stock_controlled=False, active=False)
        sale = self.sale()
        body = self.sync(sale)
        self.assertEqual(body["quarantined"], [])
        self.assertEqual(self.reasons(sale), ["product_inactive", "stock_control_changed"])
        self.assertEqual(Sale.objects.get(pk=sale["aggregate_id"]).status, "completed")
        self.assertFalse(StockMovement.objects.filter(product=self.product).exists())

    def test_deactivated_payment_method_is_accepted_with_conflict(self):
        ensure_payment_methods(self.tenant)
        PaymentMethod.objects.filter(tenant=self.tenant, kind="pix").update(active=False)
        voucher = PaymentMethod.objects.create(tenant=self.tenant, kind="other", name="Vale", active=False)
        pix = self.sale(payments=[{"method": "pix", "amount_cents": 1000}])
        other = self.sale(payments=[{"method": "other", "payment_method_id": str(voucher.id), "amount_cents": 1000}])
        cash = self.sale()
        body = self.sync(pix, other, cash)
        self.assertEqual(body["quarantined"], [])
        self.assertIn("payment_method_inactive", self.reasons(pix))
        self.assertIn("payment_method_inactive", self.reasons(other))
        self.assertNotIn("payment_method_inactive", self.reasons(cash))
        payment = SyncConflict.objects.get(event_id=other["event_id"], reason="payment_method_inactive").detail["payments"][0]
        self.assertEqual((payment["name"], payment["amount_cents"]), ("Vale", 1000))
        self.assertEqual(Sale.objects.count(), 3)

    def test_promotion_edited_after_the_sale_is_accepted_with_conflict(self):
        promo = Promotion.objects.create(tenant=self.tenant, name="Arroz em oferta", kind="price", product=self.product,
                                         price_cents=800)
        item = {"promo_discount_cents": 200, "promotion_id": str(promo.id), "promotion_name": "Arroz em oferta",
                "line_total_cents": 800}
        fresh = self.sale(item=item, promo_discount_cents=200, total_cents=800,
                          payments=[{"method": "cash", "amount_cents": 800}])
        # Sold one hour before the rule was last edited in the panel.
        earlier = self.sale(item=item, promo_discount_cents=200, total_cents=800, created_at=self.now - 3600000,
                            payments=[{"method": "cash", "amount_cents": 800}])
        body = self.sync(fresh, earlier)
        self.assertEqual(body["quarantined"], [])
        self.assertNotIn("promotion_changed", self.reasons(fresh))
        self.assertIn("promotion_changed", self.reasons(earlier))
        self.assertEqual(SaleItem.objects.get(sale_id=earlier["aggregate_id"]).promo_discount_cents, 200)
        Promotion.objects.filter(pk=promo.pk).update(active=False)
        ended = self.sale(item=item, promo_discount_cents=200, total_cents=800,
                          payments=[{"method": "cash", "amount_cents": 800}])
        self.sync(ended)
        self.assertIn("promotion_changed", self.reasons(ended))

    def test_one_event_keeps_one_conflict_per_reason(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(max_discount_bps=0)
        Product.objects.filter(pk=self.product.pk).update(unit="L")
        sale = self.sale(discount=100)
        body = self.sync(sale)
        self.assertEqual(sorted(c["reason"] for c in body["conflicts"]),
                         ["discount_over_limit", "negative_stock", "unit_changed"])
        self.assertTrue(all(c["detail"]["message"] for c in body["conflicts"]))
        listing = self.panel("GET", "/api/v1/sync/conflicts?status=open").json()["results"]
        self.assertEqual(sorted(r["reason"] for r in listing), ["discount_over_limit", "negative_stock", "unit_changed"])
        self.assertEqual({r["aggregate_id"] for r in listing}, {sale["aggregate_id"]})

    # 3. Shift closed with a negative expected amount --------------------------

    def test_cash_closed_with_negative_expected_amount_is_accepted(self):
        closed = self.event("cash.closed", self.session_id, {
            "counted_cents": 0, "expected_cents": -10000, "difference_cents": 10000, "created_at": self.now + 20,
            "operator_id": str(self.member.id)})
        later = self.sale()
        body = self.sync(self.opened(), closed, later)
        self.assertEqual(body["quarantined"], [])
        session = CashSession.objects.get(pk=uuid.UUID(self.session_id))
        self.assertEqual((session.expected_cents, session.counted_cents, session.difference_cents), (-10000, 0, 10000))
        self.assertEqual(self.reasons(closed), ["cash_expected_negative"])
        row = self.panel("GET", f"/api/v1/cash-sessions/{session.id}").json()
        self.assertEqual((row["expected_cents"], row["status"]), (-10000, "closed"))
        wrong = self.event("cash.closed", uuid.uuid4().hex, {
            "counted_cents": 0, "expected_cents": -10000, "difference_cents": 1, "created_at": self.now})
        self.assertEqual([r["event_id"] for r in self.sync(wrong)["quarantined"]], [wrong["event_id"]])

    # 4. sale.voided -----------------------------------------------------------

    def test_void_of_a_sale_already_reversed_in_the_panel_is_a_noop(self):
        cancelled, refunded, same = self.sale(), self.sale(), self.sale()
        self.sync(cancelled, refunded, same)
        self.assertEqual(self.panel("POST", f"/api/v1/sales/{cancelled['aggregate_id']}/cancel",
                                    {"reason": "Erro de digitação"}).status_code, 200)
        self.assertEqual(self.panel("POST", f"/api/v1/sales/{refunded['aggregate_id']}/refund",
                                    {"reason": "Pix devolvido"}).status_code, 200)
        self.assertEqual(self.panel("POST", f"/api/v1/sales/{same['aggregate_id']}/cancel",
                                    {"reason": "Cliente desistiu"}).status_code, 200)
        voids = [self.voided(cancelled), self.voided(refunded), self.voided(same)]
        body = self.sync(*voids, self.sale())
        self.assertEqual(body["quarantined"], [])
        self.assertEqual(self.reasons(voids[0]), ["void_already_reversed"])
        self.assertEqual(self.reasons(voids[1]), ["void_already_reversed"])
        self.assertEqual(self.reasons(voids[2]), [])  # same operation, same reason: nothing diverges
        stored = Sale.objects.get(pk=cancelled["aggregate_id"])
        self.assertEqual((stored.status, stored.cancellation_reason), ("cancelled", "Erro de digitação"))
        self.assertEqual(Sale.objects.get(pk=refunded["aggregate_id"]).status, "refunded")
        self.assertEqual(StockMovement.objects.filter(source="void").count(), 3)  # reversed once each
        detail = SyncConflict.objects.get(event_id=voids[0]["event_id"]).detail
        self.assertEqual((detail["panel_reason"], detail["terminal_reason"], detail["sale_status"]),
                         ("Erro de digitação", "Cliente desistiu", "cancelled"))

    def test_void_after_a_return_registered_in_the_panel_reverses_what_is_left(self):
        seller = Membership.objects.create(tenant=self.tenant, name="Vera", role="cashier", commission_bps=1000,
                                           user=get_user_model().objects.create_user(username="vera@example.test"))
        sale = self.sale(qty=3000, seller_id=str(seller.id))
        self.sync(sale)
        done = self.panel("POST", f"/api/v1/sales/{sale['aggregate_id']}/return", {
            "items": [{"product_id": str(self.product.id), "quantity_milli": 1000}],
            "refund_method": "external", "reason": "Pix devolvido"})
        self.assertEqual(done.status_code, 201, done.content)
        self.assertEqual(self.panel("POST", f"/api/v1/sales/{sale['aggregate_id']}/cancel",
                                    {"reason": "Painel"}).status_code, 409)  # the panel still refuses
        void = self.voided(sale)
        body = self.sync(void, self.sale())
        self.assertEqual(body["quarantined"], [])
        self.assertEqual(self.reasons(void), ["void_after_return"])
        self.assertEqual(Sale.objects.get(pk=sale["aggregate_id"]).status, "cancelled")
        # -3 sold, +1 returned in the panel, +2 by the void, -1 of the sale that followed in the batch.
        self.assertEqual(StockMovement.objects.get(source="void").delta_milli, 2000)
        self.assertEqual(balance(self.tenant, self.shop, self.product), -1000)
        detail = SyncConflict.objects.get(event_id=void["event_id"]).detail
        self.assertEqual((detail["returned_cents"], detail["total_cents"]), (1000, 3000))
        self.assertFalse(Commission.objects.filter(sale_id=sale["aggregate_id"]).exclude(status="cancelled").exists())

    def test_void_after_return_reverses_only_the_rest_of_a_paid_commission(self):
        seller = Membership.objects.create(tenant=self.tenant, name="Vera", role="cashier", commission_bps=1000,
                                           user=get_user_model().objects.create_user(username="vera@example.test"))
        sale = self.sale(qty=3000, seller_id=str(seller.id))
        self.sync(sale)
        commission_logic.pay(self.tenant, seller, self.owner, timezone.localdate())  # 10% of 30.00
        self.assertEqual(self.panel("POST", f"/api/v1/sales/{sale['aggregate_id']}/return", {
            "items": [{"product_id": str(self.product.id), "quantity_milli": 1000}],
            "refund_method": "external", "reason": "Pix devolvido"}).status_code, 201)
        self.assertEqual(self.sync(self.voided(sale))["quarantined"], [])
        rows = sorted(Commission.objects.filter(sale_id=sale["aggregate_id"]).values_list("amount_cents", flat=True))
        self.assertEqual(rows, [-200, -100, 300])  # the return took 1.00, the void takes the other 2.00

    def test_void_with_authorized_nfce_is_accepted_and_asks_for_fiscal_cancel(self):
        sale, panel_sale = self.sale(), self.sale()
        self.sync(sale, panel_sale)
        document = FiscalDocument.objects.create(tenant=self.tenant, shop=self.shop, sale_id=sale["aggregate_id"],
                                                 status="authorized", authorization="135260000000001")
        FiscalDocument.objects.create(tenant=self.tenant, shop=self.shop, sale_id=panel_sale["aggregate_id"],
                                      status="authorized")
        self.assertEqual(self.panel("POST", f"/api/v1/sales/{panel_sale['aggregate_id']}/cancel",
                                    {"reason": "Painel"}).status_code, 409)  # the panel still refuses
        void = self.voided(sale)
        body = self.sync(void, self.sale())
        self.assertEqual(body["quarantined"], [])
        self.assertEqual([c["reason"] for c in body["conflicts"] if c["event_id"] == void["event_id"]],
                         ["fiscal_cancel_required"])
        self.assertEqual(Sale.objects.get(pk=sale["aggregate_id"]).status, "cancelled")
        self.assertEqual(StockMovement.objects.filter(source="void", sale_id=sale["aggregate_id"]).count(), 1)
        detail = SyncConflict.objects.get(event_id=void["event_id"]).detail
        self.assertEqual((detail["fiscal_document_id"], detail["fiscal_status"], detail["sale_id"]),
                         (str(document.id), "authorized", sale["aggregate_id"]))

    # 5. sale.returned on a sale reversed in the panel -------------------------

    def test_return_of_a_sale_reversed_in_the_panel_is_accepted_with_conflict(self):
        sale = self.sale(qty=2000)
        self.sync(self.opened(), sale)
        self.assertEqual(self.panel("POST", f"/api/v1/sales/{sale['aggregate_id']}/refund",
                                    {"reason": "Estornado pelo financeiro"}).status_code, 200)
        self.assertEqual(self.panel("POST", f"/api/v1/sales/{sale['aggregate_id']}/return", {
            "items": [{"product_id": str(self.product.id), "quantity_milli": 1000}],
            "refund_method": "external", "reason": "Painel"}).status_code, 409)  # the panel still refuses
        ret = self.returned(sale)
        later = self.sale()
        body = self.sync(ret, later)
        self.assertEqual(body["quarantined"], [])
        self.assertEqual(self.reasons(ret), ["return_on_reversed_sale"])
        stored = SaleReturn.objects.get(pk=ret["aggregate_id"])
        self.assertEqual((stored.total_cents, stored.refund_method), (1000, "cash"))
        # Stock already came back with the panel reversal: -2 sold, +2 refunded, -1 of the later sale.
        self.assertFalse(StockMovement.objects.filter(source="return").exists())
        self.assertEqual(balance(self.tenant, self.shop, self.product), -1000)
        detail = SyncConflict.objects.get(event_id=ret["event_id"]).detail
        self.assertEqual((detail["sale_status"], detail["refund_method"], detail["total_cents"]), ("refunded", "cash", 1000))
        # The money did leave the drawer: the shift shows the cash refund.
        row = self.panel("GET", f"/api/v1/cash-sessions/{uuid.UUID(self.session_id)}").json()
        self.assertEqual(row["cash_refunds_cents"], 1000)

    def test_return_on_account_of_a_cancelled_fiado_sale_does_not_create_credit(self):
        customer = Customer.objects.create(tenant=self.tenant, name="Dona Rosa", credit_limit_cents=10000)
        sale = self.sale(qty=2000, customer_id=str(customer.id),
                         payments=[{"method": "credit_account", "amount_cents": 2000}])
        self.sync(sale)
        self.assertEqual(self.panel("POST", f"/api/v1/sales/{sale['aggregate_id']}/cancel",
                                    {"reason": "Desistiu"}).status_code, 200)
        ret = self.returned(sale, refund="account")
        self.assertEqual(self.sync(ret)["quarantined"], [])
        self.assertEqual(self.reasons(ret), ["return_on_reversed_sale"])
        self.assertFalse(CustomerCredit.objects.filter(customer=customer).exists())
        self.assertEqual(FinanceEntry.objects.get(sale_id=sale["aggregate_id"]).status, "cancelled")

    # 6. credit.received and stock.adjusted ------------------------------------

    def test_credit_received_from_missing_customer_is_quarantined_and_inactive_is_kept(self):
        customer = Customer.objects.create(tenant=self.tenant, name="Seu Zé", credit_limit_cents=10000)
        fiado = self.sale(customer_id=str(customer.id), payments=[{"method": "credit_account", "amount_cents": 1000}])
        self.sync(fiado)
        Customer.objects.filter(pk=customer.pk).update(active=False)
        ghost, paid, later = self.receipt(uuid.uuid4()), self.receipt(customer.id, 1000), self.sale()
        body = self.sync(ghost, paid, later)
        self.assertEqual([row["event_id"] for row in body["quarantined"]], [ghost["event_id"]])
        self.assertFalse(CreditReceipt.objects.filter(pk=ghost["aggregate_id"]).exists())
        # The money is in the drawer: the deactivated customer's debt is settled and the admin is told.
        self.assertEqual(CreditReceipt.objects.get(pk=paid["aggregate_id"]).amount_cents, 1000)
        self.assertEqual(FinanceEntry.objects.get(sale_id=fiado["aggregate_id"]).status, "settled")
        self.assertEqual(self.reasons(paid), ["customer_inactive"])
        self.assertTrue(Sale.objects.filter(pk=later["aggregate_id"]).exists())

    def test_stock_adjusted_for_missing_or_changed_product_never_blocks(self):
        inactive = Product.objects.create(tenant=self.tenant, sku="CAFE", name="Café", price_cents=900, active=False)
        homemade = Product.objects.create(tenant=self.tenant, sku="PAO", name="Pão", price_cents=100, unit="KG",
                                          stock_controlled=False)
        counted = Product.objects.create(tenant=self.tenant, sku="OVO", name="Ovo", price_cents=50, unit="UN")
        ghost, foreign = self.adjustment(uuid.uuid4()), self.adjustment(self.foreign_product.id)
        kept, skipped, fraction = self.adjustment(inactive.id), self.adjustment(homemade.id), self.adjustment(counted.id, 500)
        good = self.adjustment(self.product.id, 3000)
        body = self.sync(ghost, foreign, kept, skipped, fraction, good)
        self.assertEqual([row["event_id"] for row in body["quarantined"]], [ghost["event_id"], foreign["event_id"]])
        self.assertEqual(body["quarantined"][0]["message"], "Produto do ajuste de estoque não encontrado")
        self.assertEqual(balance(self.tenant, self.shop, self.product), 3000)
        self.assertEqual(balance(self.tenant, self.shop, inactive), 2000)
        self.assertEqual(self.reasons(kept), ["product_inactive"])
        self.assertFalse(StockMovement.objects.filter(product=homemade).exists())
        self.assertEqual(self.reasons(skipped), ["stock_control_changed"])
        self.assertEqual(balance(self.tenant, self.shop, counted), 500)
        self.assertEqual(self.reasons(fraction), ["unit_changed"])
        # A resend after the product stopped controlling stock is acknowledged, not judged again.
        Product.objects.filter(pk=self.product.pk).update(stock_controlled=False)
        self.assertEqual(self.sync(good)["quarantined"], [])
        self.assertEqual(StockMovement.objects.filter(product=self.product).count(), 1)
