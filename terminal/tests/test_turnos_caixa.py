"""Caixa por turno de operador: abrir, pausar, retomar, fechar, relatórios (operador, caixa e dia) e migração.

Regra: um turno ativo por estação; o turno é do operador que o abriu. Outro operador não vende
nem mexe na gaveta desse turno: aguarda, ou um gerente encerra o turno (contagem + PIN).
"""
import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from shared.licensing import generate_keypair, payload_digest, sign_lease
from shared.security import hash_pin
from terminal import cash_reports
from terminal.app import create_app
from terminal.printers import layout_bytes
from terminal.store import Store, TerminalError

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "terminal" / "static" / "terminal-core.js"
PINS = {"ana": "123987", "caio": "246813", "bruno": "654321"}


class ShiftFixture(unittest.TestCase):
    capabilities = ["restore", "send-only", "terminal-config", "mirror-key", "stations", "cash_shifts"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name,
                               "DB_PATH": Path(self.temp.name) / "terminal.sqlite3", "START_WORKER": False})
        self.store = self.app.extensions["terminal_store"]
        self.client = self.app.test_client()
        tenant_id, shop_id, terminal_id = (str(uuid.uuid4()) for _ in range(3))
        self.ids = {name: str(uuid.uuid4()) for name in PINS}
        operators = [
            {"id": self.ids["ana"], "name": "Ana", "role": "cashier", "pin_hash": hash_pin(PINS["ana"])},
            {"id": self.ids["caio"], "name": "Caio", "role": "cashier", "pin_hash": hash_pin(PINS["caio"])},
            {"id": self.ids["bruno"], "name": "Bruno", "role": "manager", "pin_hash": hash_pin(PINS["bruno"])},
        ]
        self.ana = {"id": self.ids["ana"], "name": "Ana", "role": "cashier"}
        self.caio = {"id": self.ids["caio"], "name": "Caio", "role": "cashier"}
        self.bruno = {"id": self.ids["bruno"], "name": "Bruno", "role": "manager"}
        self.arroz = {"id": str(uuid.uuid4()), "sku": "ARROZ", "barcode": "7890000000001", "name": "Arroz Tipo 1 5kg",
                      "unit": "UN", "category": "Mercearia", "price_cents": 2500, "quantity_milli": 100000, "active": True}
        self.banana = {"id": str(uuid.uuid4()), "sku": "BANANA", "barcode": None, "name": "Banana prata",
                       "unit": "KG", "category": "Hortifrúti", "price_cents": 699, "quantity_milli": 50000, "active": True}
        self.leite = {"id": str(uuid.uuid4()), "sku": "LEITE", "barcode": "7890000000002", "name": "Leite integral",
                      "unit": "L", "category": "", "price_cents": 550, "quantity_milli": 50000, "active": True}
        self.vale = str(uuid.uuid4())
        self.customer = str(uuid.uuid4())
        now = int(time.time())
        private, public = generate_keypair()
        claims = {"schema": 1, "tenant_id": tenant_id, "shop_id": shop_id, "terminal_id": terminal_id,
                  "device_id": self.store.identity()["device_id"], "issued_at": now - 5, "expires_at": now + 3600,
                  "features": ["pdv"], "status": "active", "operators_hash": payload_digest(operators)}
        lease = sign_lease(claims, private)
        self.store.set_pairing({"token": "t" * 48, "tenant_id": tenant_id, "shop_id": shop_id,
                                "terminal_id": terminal_id, "lease": lease, "public_key": public,
                                "capabilities": self.capabilities}, "http://127.0.0.1:8000")
        methods = [{"id": kind, "kind": kind, "name": kind, "active": True}
                   for kind in ("cash", "pix", "card_debit", "card_credit", "credit_account", "store_credit")]
        methods.append({"id": self.vale, "kind": "other", "name": "Vale-refeição", "active": True})
        self.store.apply_bootstrap({"products": [self.arroz, self.banana, self.leite], "operators": operators,
                                    "lease": lease, "public_key": public, "payment_methods": methods,
                                    "customers": [{"id": self.customer, "name": "Dona Maria", "credit_limit_cents": 100000,
                                                   "open_credit_cents": 0, "store_credit_cents": 5000}]})

    def sell(self, operator, payments, items=None, customer=None):
        items = items or [{"product_id": self.arroz["id"], "quantity_milli": 1000}]
        return self.store.sale(operator, {"request_id": str(uuid.uuid4()), "items": items, "payments": payments,
                                          "customer_id": customer})

    def events(self, kind):
        with self.store.connection() as conn:
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT payload FROM outbox WHERE event_type=? ORDER BY seq", (kind,))]

    # Flask helpers
    def csrf(self):
        return self.client.get("/api/session").json["csrf"]

    def login(self, name):
        response = self.client.post("/api/login", json={"operator_id": self.ids[name], "pin": PINS[name]},
                                    headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(response.status_code, 200, response.json)

    def post(self, path, payload=None):
        return self.client.post(path, json=payload or {}, headers={"X-CSRF-Token": self.csrf()})


class ShiftRulesTests(ShiftFixture):
    def test_shift_belongs_to_the_operator_that_opened_it(self):
        self.store.open_cash(self.ids["ana"], 15000)
        state = self.store.cash_state()
        self.assertEqual((state["operator_id"], state["operator_name"], state["paused"]), (self.ids["ana"], "Ana", None))
        with self.assertRaisesRegex(TerminalError, "Já existe caixa aberto"):
            self.store.open_cash(self.ids["caio"], 0)

    def test_another_operator_cannot_sell_or_touch_the_drawer(self):
        self.store.open_cash(self.ids["ana"], 15000)
        for attempt in (lambda: self.sell(self.caio, [{"method": "cash", "amount_cents": 2500}]),
                        lambda: self.store.cash_movement(self.ids["caio"], "supplied", 100, "troco"),
                        lambda: self.sell(self.bruno, [{"method": "cash", "amount_cents": 2500}])):
            with self.assertRaises(TerminalError) as caught:
                attempt()
            self.assertEqual(caught.exception.code, "cash_other_operator")
            self.assertIn("Ana", str(caught.exception))
        self.sell(self.ana, [{"method": "cash", "amount_cents": 2500}])

    def test_manager_closes_the_shift_of_another_and_the_next_operator_starts_their_own(self):
        self.store.open_cash(self.ids["ana"], 1000)
        self.sell(self.ana, [{"method": "cash", "amount_cents": 2500}])
        with self.assertRaisesRegex(TerminalError, "PIN de gerente"):
            self.store.close_cash(self.ids["caio"], 3500, PINS["caio"])
        closed = self.store.close_cash(self.ids["caio"], 3500, PINS["bruno"])
        self.assertEqual(closed["difference_cents"], 0)
        self.assertEqual(closed["report"]["layout"]["report"], "combined")
        self.assertIsNone(self.store.cash_state())
        self.store.open_cash(self.ids["caio"], 2000)
        self.assertEqual(self.store.cash_state()["operator_name"], "Caio")
        self.sell(self.caio, [{"method": "pix", "amount_cents": 2500}])

    def test_pause_blocks_sales_until_owner_or_manager_pin(self):
        self.store.open_cash(self.ids["ana"], 0)
        state = self.store.pause_cash(self.ana, "Almoço")
        self.assertEqual((state["paused"]["reason"], state["paused"]["operator_name"]), ("Almoço", "Ana"))
        with self.assertRaisesRegex(TerminalError, "já está pausado"):
            self.store.pause_cash(self.ana)
        for attempt in (lambda: self.sell(self.ana, [{"method": "cash", "amount_cents": 2500}]),
                        lambda: self.store.cash_movement(self.ids["ana"], "supplied", 100, "troco")):
            with self.assertRaises(TerminalError) as caught:
                attempt()
            self.assertEqual(caught.exception.code, "cash_paused")
        with self.assertRaisesRegex(TerminalError, "PIN inválido"):
            self.store.resume_cash(self.ana, PINS["caio"])  # another cashier's PIN does not resume
        self.assertIsNone(self.store.resume_cash(self.ana, PINS["ana"])["paused"])
        self.sell(self.ana, [{"method": "cash", "amount_cents": 2500}])
        self.store.pause_cash(self.ana, "")
        self.assertIsNone(self.store.resume_cash(self.caio, PINS["bruno"])["paused"])  # manager PIN, typed by anyone
        pauses = self.events("cash.paused")
        resumed = self.events("cash.resumed")
        self.assertEqual([event["data"]["reason"] for event in pauses], ["Almoço", ""])
        self.assertEqual([event["data"]["operator_id"] for event in resumed], [self.ids["ana"], self.ids["bruno"]])
        self.assertEqual({event["data"]["cash_session_id"] for event in pauses + resumed},
                         {self.store.cash_state()["id"]})

    def test_wrong_pins_lock_the_resume_screen(self):
        self.store.open_cash(self.ids["ana"], 0)
        self.store.pause_cash(self.ana)
        for _ in range(5):
            with self.assertRaises(TerminalError):
                self.store.resume_cash(self.ana, "000000")
        with self.assertRaises(TerminalError) as caught:
            self.store.resume_cash(self.ana, PINS["ana"])
        self.assertEqual(caught.exception.status, 429)

    def test_only_owner_or_manager_pauses(self):
        self.store.open_cash(self.ids["ana"], 0)
        with self.assertRaises(TerminalError) as caught:
            self.store.pause_cash(self.caio)
        self.assertEqual(caught.exception.status, 403)
        self.store.pause_cash(self.bruno, "Conferência")
        self.assertEqual(self.store.cash_state()["paused"]["operator_name"], "Bruno")

    def test_closing_a_paused_shift_ends_the_pause_and_reports_effective_time(self):
        self.store.open_cash(self.ids["ana"], 0)
        with self.store.transaction() as conn:  # an hour shift, 15 minutes of it paused
            start = int(time.time() * 1000) - 3_600_000
            conn.execute("UPDATE cash_sessions SET opened_at=?", (start,))
            conn.execute("INSERT INTO cash_pauses(id,session_id,operator_id,reason,started_at,ended_at) "
                         "SELECT 'p1',id,?,?,?,? FROM cash_sessions", (self.ids["ana"], "Café", start + 600_000, start + 1_500_000))
        self.store.pause_cash(self.ana, "Fim do expediente")
        closed = self.store.close_cash(self.ids["ana"], 0, PINS["bruno"])
        with self.store.connection() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM cash_pauses WHERE ended_at IS NULL").fetchone())
            data = self.store._shift_data(conn, conn.execute("SELECT * FROM cash_sessions").fetchone())
        self.assertEqual(len(data["pauses"]), 2)
        self.assertGreaterEqual(data["paused_ms"], 900_000)
        self.assertLessEqual(data["effective_ms"] + data["paused_ms"], 3_600_000 + 5_000)
        self.assertEqual(data["closed_by_name"], "Ana")
        self.assertEqual(data["supervisor_name"], "Bruno")
        text = "\n".join(line for block in closed["report"]["layout"]["blocks"] if block["kind"] == "text"
                         for line in block["lines"])
        self.assertIn("Café", text)
        self.assertIn("Fim do expediente", text)
        event = self.events("cash.closed")[-1]["data"]
        self.assertEqual(event["paused_ms"], data["paused_ms"])
        self.assertEqual(event["shift_operator_id"], self.ids["ana"])

    def test_older_cloud_gets_the_events_it_knows(self):
        with self.store.transaction() as conn:
            conn.execute("UPDATE meta SET value=? WHERE key='cloud_capabilities'", (json.dumps(["stations"]),))
        self.store.open_cash(self.ids["ana"], 0)
        self.store.pause_cash(self.ana)
        self.store.resume_cash(self.ana, PINS["ana"])
        self.store.close_cash(self.ids["ana"], 0, PINS["bruno"])
        self.assertEqual(self.events("cash.paused") + self.events("cash.resumed"), [])
        self.assertEqual(set(self.events("cash.closed")[0]["data"]),
                         {"counted_cents", "expected_cents", "difference_cents", "created_at", "operator_id",
                          "supervisor_id"})
        with self.store.connection() as conn:  # the pause is still kept here, for the report
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cash_pauses").fetchone()[0], 1)

    def test_manager_refund_on_the_shift_of_a_cashier_still_works(self):
        self.store.open_cash(self.ids["ana"], 10000)
        sale = self.sell(self.ana, [{"method": "cash", "amount_cents": 2500}])
        self.store.return_sale(self.bruno, sale["id"], {"return_id": str(uuid.uuid4()), "refund_method": "cash",
                                                         "reason": "Avaria", "items": [
                                                             {"product_id": self.arroz["id"], "quantity_milli": 1000}]})
        self.store.pause_cash(self.ana)
        with self.assertRaises(TerminalError) as caught:
            self.store.return_sale(self.bruno, sale["id"], {"return_id": str(uuid.uuid4()), "refund_method": "cash",
                                                             "reason": "x", "items": []})
        self.assertIn(caught.exception.code, ("cash_paused", None))

    def test_summary_covers_every_form_of_payment(self):
        self.store.open_cash(self.ids["ana"], 0)
        self.sell(self.ana, [{"method": "cash", "amount_cents": 3000}])  # 500 of change
        self.sell(self.ana, [{"method": "credit_account", "amount_cents": 2500}], customer=self.customer)
        self.sell(self.ana, [{"method": "store_credit", "amount_cents": 2500}], customer=self.customer)
        self.sell(self.ana, [{"method": "other", "payment_method_id": self.vale, "amount_cents": 2500}])
        self.sell(self.ana, [{"method": "pix", "amount_cents": 1000}, {"method": "card_debit", "amount_cents": 1500}])
        summary = self.store.cash_summary()
        methods = {row["label"]: row["amount_cents"] for row in summary["methods"]}
        self.assertEqual(methods, {"Dinheiro": 2500, "Pix": 1000, "Débito": 1500, "Crédito": 0,
                                   "A prazo (fiado)": 2500, "Crédito na loja": 2500, "Vale-refeição": 2500})
        self.assertEqual(summary["categories"]["credit_account_cents"], 2500)
        self.assertEqual(summary["categories"]["store_credit_cents"], 2500)
        self.assertEqual(summary["categories"]["other_cents"], 2500)
        self.assertEqual(summary["expected_cents"], 2500)
        closed = self.store.close_cash(self.ids["ana"], 2500, PINS["bruno"])
        lines = [line for block in closed["report"]["layout"]["blocks"] if block["kind"] == "text" for line in block["lines"]]
        for label in ("Dinheiro (líq. de troco)", "A prazo (fiado)", "Crédito na loja", "Vale-refeição", "Pix", "Débito"):
            self.assertTrue(any(line.startswith(label) for line in lines), label)
        totals = self.events("cash.closed")[0]["data"]["totals"]
        self.assertEqual(totals["by_method"], {"cash": 2500, "pix": 1000, "card_debit": 1500, "credit_account": 2500,
                                               "store_credit": 2500, "other:" + self.vale: 2500})
        self.assertEqual(totals["sales_count"], 5)


class ReportLayoutTests(ShiftFixture):
    def closed_shift(self, printer_format=None):
        if printer_format:
            self.store.save_printer("receipt", "file", str(Path(self.temp.name) / "bobina.bin"), printer_format)
        self.store.open_cash(self.ids["ana"], 15000)
        self.sell(self.ana, [{"method": "cash", "amount_cents": 2500}])
        self.sell(self.ana, [{"method": "other", "payment_method_id": self.vale, "amount_cents": 3495}],
                  items=[{"product_id": self.banana["id"], "quantity_milli": 5000}])
        self.store.cash_movement(self.ids["ana"], "withdrawn", 1000, "Recolhimento para o cofre da gerência com observação longa")
        self.store.pause_cash(self.ana, "Almoço")
        self.store.resume_cash(self.ana, PINS["ana"])
        return self.store.close_cash(self.ids["ana"], 17000, PINS["bruno"])

    def assert_fits(self, layout, columns):
        self.assertEqual(layout["columns"], columns)
        for block in layout["blocks"]:
            if block["kind"] != "text":
                continue
            limit = columns // 2 if block.get("wide") else columns
            for line in block["lines"]:
                self.assertLessEqual(len(line), limit, line)
        raw = layout_bytes(layout)
        text = raw.decode("cp850")
        for word in ("RELATÓRIO", "CONFERÊNCIA DO DINHEIRO", "Devoluções", "NÃO É DOCUMENTO FISCAL"):
            self.assertIn(word, text)
        self.assertNotIn("?", "".join(line for block in layout["blocks"] if block["kind"] == "text"
                                      for line in block["lines"]).encode("cp850", "replace").decode("cp850"))

    def test_operator_report_on_80_mm_has_48_columns_and_prints(self):
        closed = self.closed_shift("escpos80")
        layout = closed["report"]["layout"]
        self.assert_fits(layout, 48)
        self.assertEqual(len(closed["report"]["jobs"]), 1)
        text = [line for block in layout["blocks"] if block["kind"] == "text" for line in block["lines"]]
        difference = next(line for line in text if line.startswith("Diferença"))
        self.assertTrue(difference.endswith("+5,00"), difference)
        self.assertEqual(len(difference), 48)
        self.assertTrue(any("Assinatura do operador" in line for line in text))
        self.assertTrue(any("Assinatura do gerente" in line for line in text))
        bold_titles = {line for block in layout["blocks"] if block.get("bold") for line in block["lines"]}
        self.assertTrue({"VENDAS", "FORMAS DE PAGAMENTO", "PAUSAS"} <= bold_titles)
        with self.store.connection() as conn:
            job = conn.execute("SELECT payload,status FROM print_jobs ORDER BY created_at DESC,rowid DESC").fetchone()
        self.assertEqual(job["status"], "queued")
        self.assertIn("RELATÓRIO DO OPERADOR".encode("cp850"), job["payload"])

    def test_58_mm_keeps_32_columns_and_wraps_instead_of_cutting(self):
        layout = self.closed_shift("escpos58")["report"]["layout"]
        self.assert_fits(layout, 32)
        text = [line for block in layout["blocks"] if block["kind"] == "text" for line in block["lines"]]
        joined = " ".join(text)
        self.assertIn("Recolhimento para o cofre da gerência com observação longa", " ".join(joined.split()))

    def test_without_a_printer_the_layout_comes_back_for_the_browser(self):
        closed = self.closed_shift()
        self.assertEqual((closed["report"]["jobs"], closed["report"]["printer"]), ([], False))
        self.assert_fits(closed["report"]["layout"], 48)

    def test_pure_helpers(self):
        self.assertEqual(cash_reports.duration(3_900_000), "1h05min")
        self.assertEqual(cash_reports.duration(59_000), "0min")
        self.assertEqual(cash_reports.quantity(1500, "KG"), "1,5 KG")
        self.assertEqual(cash_reports.quantity(3000, "UN"), "3 UN")
        self.assertEqual(cash_reports.quantity(250, "L"), "0,25 L")


class DayCloseTests(ShiftFixture):
    def day(self):
        self.store.save_printer("receipt", "file", str(Path(self.temp.name) / "bobina.bin"), "escpos80")
        self.store.open_cash(self.ids["ana"], 10000)
        self.sell(self.ana, [{"method": "cash", "amount_cents": 5000}],
                  items=[{"product_id": self.arroz["id"], "quantity_milli": 2000}])
        voided = self.sell(self.ana, [{"method": "pix", "amount_cents": 550}],
                           items=[{"product_id": self.leite["id"], "quantity_milli": 1000}])
        self.store.void_sale(self.bruno, voided["id"], "Cliente desistiu")
        self.store.close_cash(self.ids["ana"], 14900, PINS["bruno"])  # 100 short
        self.store.open_cash(self.ids["caio"], 5000)
        self.sell(self.caio, [{"method": "card_credit", "amount_cents": 1748}],
                  items=[{"product_id": self.banana["id"], "quantity_milli": 2500}])
        self.sell(self.caio, [{"method": "credit_account", "amount_cents": 1100}], customer=self.customer,
                  items=[{"product_id": self.leite["id"], "quantity_milli": 2000}])
        self.store.close_cash(self.ids["caio"], 5000, PINS["bruno"])

    def test_close_day_needs_a_manager_pin_and_prints_every_report_in_order(self):
        self.day()
        with self.assertRaisesRegex(TerminalError, "PIN de gerente"):
            self.store.close_day(self.ids["ana"], PINS["ana"])
        with self.store.connection() as conn:
            before = conn.execute("SELECT COUNT(*) FROM print_jobs").fetchone()[0]
        result = self.store.close_day(self.ids["ana"], PINS["bruno"])
        self.assertEqual([layout["report"] for layout in result["layouts"]], ["operator", "operator", "station", "day"])
        self.assertEqual((result["shifts"], result["open_shifts"], len(result["jobs"])), (2, 0, 4))
        with self.store.connection() as conn:
            payloads = [row[0] for row in conn.execute("SELECT payload FROM print_jobs ORDER BY created_at,rowid")][before:]
        self.assertEqual(len(payloads), 4)
        self.assertIn("CONSOLIDADO DO DIA".encode("cp850"), payloads[-1])
        day = result["layouts"][-1]
        lines = [line for block in day["blocks"] if block["kind"] == "text" for line in block["lines"]]
        for line in lines:
            self.assertLessEqual(len(line), 48, line)
        products = lines[lines.index("PRODUTOS VENDIDOS") + 1:lines.index("CANCELAMENTOS")]
        # Grouped by category, the category with more money first; units as sold.
        self.assertEqual(products[0].split()[0], "MERCEARIA")
        self.assertIn("Arroz Tipo 1 5kg", products)
        self.assertTrue(any(line.strip().startswith("2 UN") and line.endswith("50,00") for line in products))
        self.assertTrue(any(line.strip().startswith("2,5 KG") and line.endswith("17,48") for line in products))
        self.assertTrue(any(line.strip().startswith("2 L") and line.endswith("11,00") for line in products))
        self.assertTrue(any(line.startswith("GERAL") for line in products))  # bootstrap names an empty category
        self.assertTrue(any(line.startswith("Ana") and line.endswith("-1,00") for line in lines))
        self.assertTrue(any(line.startswith("Caio") and line.endswith("0,00") for line in lines))
        cancelled = lines[lines.index("CANCELAMENTOS") + 1:lines.index("DEVOLUÇÕES")]
        self.assertTrue(any(line.endswith("5,50") for line in cancelled))
        self.assertTrue(any("Cliente desistiu" in line for line in cancelled))
        methods = lines[lines.index("VENDAS POR FORMA DE PAGAMENTO") + 1:]
        for label in ("A prazo (fiado)", "Crédito"):
            self.assertTrue(any(line.startswith(label) for line in methods), label)
        station = result["layouts"][2]
        station_lines = [line for block in station["blocks"] if block["kind"] == "text" for line in block["lines"]]
        self.assertTrue(any(line.startswith("Turnos") and line.endswith("2") for line in station_lines))

    def test_open_shift_is_listed_and_marked_partial(self):
        self.store.open_cash(self.ids["ana"], 0)
        self.sell(self.ana, [{"method": "cash", "amount_cents": 2500}])
        listing = self.store.shifts_of_day()
        self.assertEqual([(row["operator_name"], row["status"]) for row in listing["shifts"]], [("Ana", "open")])
        result = self.store.close_day(self.ids["bruno"], PINS["bruno"])
        self.assertEqual(result["open_shifts"], 1)
        self.assertEqual(result["jobs"], [])  # no printer: everything comes back for the browser
        day_lines = [line for block in result["layouts"][-1]["blocks"] if block["kind"] == "text" for line in block["lines"]]
        self.assertTrue(any("ainda aberto" in line for line in day_lines))

    def test_additional_cashier_sees_only_its_own_shifts(self):
        self.store.open_cash(self.ids["ana"], 0)
        with self.store.station("st-2"):
            self.store.open_cash(self.ids["caio"], 0)
            own = self.store.shifts_of_day()
            self.assertEqual([row["operator_name"] for row in own["shifts"]], ["Caio"])
            self.assertFalse(own["all_stations"])
        everything = self.store.shifts_of_day()
        self.assertTrue(everything["all_stations"])
        self.assertEqual(sorted(row["operator_name"] for row in everything["shifts"]), ["Ana", "Caio"])
        ana_id = next(row["id"] for row in everything["shifts"] if row["operator_name"] == "Ana")
        with self.store.station("st-2"):
            with self.assertRaises(TerminalError) as caught:
                self.store.print_shift_report(ana_id, self.ids["caio"])
            self.assertEqual(caught.exception.status, 404)

    def test_day_must_be_a_date(self):
        with self.assertRaisesRegex(TerminalError, "Data inválida"):
            self.store.shifts_of_day("ontem")


class ShiftRoutesTests(ShiftFixture):
    def test_routes_pause_resume_list_print_and_close_day(self):
        self.login("ana")
        self.assertEqual(self.post("/api/cash/open", {"opening_cents": 5000}).status_code, 200)
        session = self.client.get("/api/session").json
        self.assertEqual(session["cash"]["operator_name"], "Ana")
        paused = self.post("/api/cash/pause", {"reason": "Banheiro"})
        self.assertEqual(paused.status_code, 200, paused.json)
        self.assertEqual(paused.json["cash"]["paused"]["reason"], "Banheiro")
        sale = self.post("/api/sales", {"request_id": str(uuid.uuid4()), "payments": [{"method": "cash", "amount_cents": 2500}],
                                        "items": [{"product_id": self.arroz["id"], "quantity_milli": 1000}]})
        self.assertEqual((sale.status_code, sale.json["code"]), (409, "cash_paused"))
        self.assertEqual(self.post("/api/cash/resume", {"pin": "999999"}).status_code, 403)
        resumed = self.post("/api/cash/resume", {"pin": PINS["ana"]})
        self.assertIsNone(resumed.json["cash"]["paused"])
        closed = self.post("/api/cash/close", {"counted_cents": 5000, "manager_pin": PINS["bruno"]})
        self.assertEqual(closed.status_code, 200, closed.json)
        shift_id = closed.json["id"]
        listing = self.client.get("/api/cash/shifts").json
        self.assertEqual([row["id"] for row in listing["shifts"]], [shift_id])
        reprint = self.post(f"/api/cash/shifts/{shift_id}/print")
        self.assertEqual(reprint.status_code, 200, reprint.json)
        self.assertIn("Reimpressão", json.dumps(reprint.json["layout"], ensure_ascii=False))
        self.assertEqual(self.post("/api/cash/day/close", {"manager_pin": PINS["ana"]}).status_code, 403)
        day = self.post("/api/cash/day/close", {"manager_pin": PINS["bruno"]})
        self.assertEqual(day.status_code, 200, day.json)
        self.assertEqual(len(day.json["layouts"]), 3)

    def test_cashier_lists_and_reprints_only_own_shifts(self):
        self.store.open_cash(self.ids["ana"], 0)
        self.store.close_cash(self.ids["ana"], 0, PINS["bruno"])
        self.login("caio")
        self.assertEqual(self.client.get("/api/cash/shifts").json["shifts"], [])
        ana_shift = self.store.shifts_of_day()["shifts"][0]["id"]
        self.assertEqual(self.post(f"/api/cash/shifts/{ana_shift}/print").status_code, 404)
        self.login("bruno")
        self.assertEqual(len(self.client.get("/api/cash/shifts").json["shifts"]), 1)
        self.assertEqual(self.post(f"/api/cash/shifts/{ana_shift}/print").status_code, 200)

    def test_operator_switch_sees_whose_shift_is_open(self):
        self.login("ana")
        self.post("/api/cash/open", {"opening_cents": 0})
        self.post("/api/cash/pause", {"reason": "Troca de operador"})
        self.post("/api/logout")
        self.login("caio")
        cash = self.client.get("/api/session").json["cash"]
        self.assertEqual((cash["operator_name"], cash["paused"]["reason"]), ("Ana", "Troca de operador"))
        refused = self.post("/api/cash/movement", {"kind": "supplied", "amount_cents": 100, "reason": "troco"})
        self.assertEqual(refused.json["code"], "cash_other_operator")
        # A manager PIN typed by Caio closes Ana's shift; then Caio starts his own.
        self.assertEqual(self.post("/api/cash/close", {"counted_cents": 0, "manager_pin": PINS["bruno"]}).status_code, 200)
        self.assertEqual(self.post("/api/cash/open", {"opening_cents": 1000}).status_code, 200)
        self.assertEqual(self.client.get("/api/session").json["cash"]["operator_name"], "Caio")


class ShiftMigrationTests(unittest.TestCase):
    def test_database_from_before_shifts_keeps_its_open_cashier_as_the_operator_shift(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "terminal.sqlite3"
            Store(path)
            conn = sqlite3.connect(path)
            conn.execute("DROP TABLE cash_pauses")
            conn.execute("DROP TABLE cash_closings")
            conn.execute("INSERT INTO operators(id,name,role,pin_hash,active) VALUES ('op1','Ana','cashier','x',1)")
            conn.execute("INSERT INTO cash_sessions(id,operator_id,opening_cents,opened_at,station_id) "
                         "VALUES ('c1','op1',1500,1000,'server')")
            conn.commit()
            conn.close()
            store = Store(path)
            state = store.cash_state()
            self.assertEqual((state["id"], state["operator_id"], state["operator_name"], state["paused"]),
                             ("c1", "op1", "Ana", None))
            with store.connection() as check:
                tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                columns = [row[1] for row in check.execute("PRAGMA table_info(cash_sessions)")]
            self.assertTrue({"cash_pauses", "cash_closings"} <= tables)
            self.assertEqual(columns[-1], "station_id")  # older executables keep their INSERTs
            before = store.cash_state()
            Store(path)  # running the migration again changes nothing
            self.assertEqual(Store(path).cash_state(), before)

    def test_replayed_journal_rebuilds_pauses_and_closing(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "terminal.sqlite3")
            identity = {"tenant_id": "t", "shop_id": "s", "terminal_id": "x"}
            with store.transaction() as conn:
                for key, value in identity.items():
                    store._set_meta(conn, key, value)

            def item(seq, kind, aggregate, data):
                event_id = uuid.uuid4().hex
                return {"seq": seq, "event_id": event_id, "created_at": 1,
                        "event": {"event_id": event_id, "type": kind, "aggregate_id": aggregate, "identity": identity,
                                  "data": data}}
            result = store.apply_journal_events([
                item(1, "cash.opened", "c1", {"opening_cents": 0, "operator_id": "op1", "created_at": 1000}),
                item(2, "cash.paused", "p1", {"cash_session_id": "c1", "operator_id": "op1", "reason": "Almoço",
                                              "created_at": 2000}),
                item(3, "cash.resumed", "p1", {"cash_session_id": "c1", "operator_id": "op1", "created_at": 5000}),
                item(4, "cash.closed", "c1", {"counted_cents": 0, "expected_cents": 0, "difference_cents": 0,
                                              "created_at": 9000, "operator_id": "op1", "supervisor_id": "mg1"})])
            self.assertEqual((result["applied"], result["warnings"]), (4, 0))
            with store.connection() as conn:
                pause = conn.execute("SELECT started_at,ended_at,resumed_by,reason FROM cash_pauses").fetchone()
                closing = conn.execute("SELECT closed_by,supervisor_id FROM cash_closings").fetchone()
            self.assertEqual(tuple(pause), (2000, 5000, "op1", "Almoço"))
            self.assertEqual(tuple(closing), ("op1", "mg1"))


@unittest.skipUnless(shutil.which("node"), "node não está disponível")
class PaymentKeysCoreTests(unittest.TestCase):
    def run_core(self, script):
        result = subprocess.run(["node", "-e", "const C = require(process.argv[1]);" + script, str(CORE)],
                                capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def test_every_form_has_icon_digit_and_alt_letter(self):
        out = self.run_core("""
const rows = ['cash','pix','card_debit','card_credit','credit_account','store_credit'].map(kind => ({id: kind, kind, name: kind, active: true}))
  .concat(Array.from({length: 10}, (_, i) => ({id: 'o' + i, kind: 'other', name: 'Própria ' + i, active: true})));
const options = C.paymentOptions(rows);
console.log(JSON.stringify({
  options: options.map(o => [o.key, o.alt, o.icon, o.method]),
  alt: ['KeyD', 'KeyP', 'KeyB', 'KeyC', 'KeyF', 'KeyL', 'Digit1', 'Numpad9', 'Digit0', 'KeyZ', 'Enter'].map(code => {
    const option = C.payAltOption(code, options); return option === 'split' ? 'split' : option ? (option.id || option.method) : null; }),
  text: [C.payShortcutText(options[0]), C.payShortcutText(options[14]), C.payShortcutText(C.SPLIT_OPTION)],
}));""")
        self.assertEqual(out["options"][:6], [["1", "D", "cash", "cash"], ["2", "P", "pix", "pix"],
                                              ["3", "B", "card", "card_debit"], ["4", "C", "card", "card_credit"],
                                              ["5", "F", "book", "credit_account"], ["6", "L", "gift", "store_credit"]])
        own = out["options"][6:]
        self.assertEqual([row[0] for row in own], ["7", "8", "9"] + [""] * 7)
        self.assertEqual([row[1] for row in own], [str(n) for n in range(1, 10)] + [""])
        self.assertEqual({row[2] for row in own}, {"wallet"})
        self.assertEqual(out["alt"], ["cash", "pix", "card_debit", "card_credit", "credit_account", "store_credit",
                                      "o0", "o8", "split", None, None])
        self.assertEqual(out["text"], ["tecla 1 ou Alt+D", "Alt+9", "tecla 0 ou Alt+0"])

    def test_shift_view_tells_whose_cashier_is_open(self):
        out = self.run_core("""
const me = {id: 'ana', name: 'Ana'};
console.log(JSON.stringify([
  C.shiftView({operator: me, cash: null}).state,
  C.shiftView({operator: me, cash: {operator_id: 'ana', operator_name: 'Ana', opened_at: 1, paused: null}}).state,
  C.shiftView({operator: me, cash: {operator_id: 'ana', operator_name: 'Ana', opened_at: 1, paused: {since: 5, reason: 'Almoço'}}}),
  C.shiftView({operator: me, cash: {operator_id: 'caio', operator_name: 'Caio', opened_at: 1, paused: {since: 7}}}),
  C.shiftView({operator: me, cash: {operator_id: '', opened_at: 1, paused: null}}).state,
]));""")
        self.assertEqual(out[0], "closed")
        self.assertEqual(out[1], "mine")
        self.assertEqual((out[2]["state"], out[2]["since"], out[2]["reason"]), ("paused", 5, "Almoço"))
        self.assertEqual((out[3]["state"], out[3]["owner"], out[3]["paused"], out[3]["pausedSince"]), ("other", "Caio", True, 7))
        self.assertEqual(out[4], "mine")  # a session restored without operator is nobody else's


class ScreenWiringTests(unittest.TestCase):
    """The page wires what the backend and the core expose (no browser here)."""

    def test_buttons_icons_and_pause_screen(self):
        page = (ROOT / "terminal" / "templates" / "index.html").read_text()
        script = (ROOT / "terminal" / "static" / "terminal.js").read_text()
        for symbol in ("i-cash", "i-pix", "i-card", "i-book", "i-gift", "i-wallet", "i-split"):
            self.assertIn(f'id="{symbol}"', page)
        for node in ("pauseView", "pauseForm", "pausePin", "paySplitAccount", "paySplitStore"):
            self.assertIn(f'id="{node}"', page)
        for route in ("/api/cash/pause", "/api/cash/resume", "/api/cash/shifts", "/api/cash/day/close"):
            self.assertIn(route, script)
        self.assertIn("aria-keyshortcuts", script)
        self.assertIn("C.payAltOption(event.code", script)


if __name__ == "__main__":
    unittest.main()
