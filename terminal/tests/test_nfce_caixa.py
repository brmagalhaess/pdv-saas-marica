"""NFC-e no caixa: migração do SQLite, regra de impressão, pedido à nuvem, contingência off-line,
envio posterior, cancelamento e o diálogo "Imprimir a nota fiscal?" (regras puras via node)."""
import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import requests

from shared.licensing import generate_keypair, payload_digest, sign_lease
from shared.nfce import keys
from shared.nfce.xml import from_etree, parse
from shared.security import hash_pin
from terminal import nfce, sync
from terminal.app import create_app
from terminal.printers import receipt_bytes, receipt_layout
from terminal.store import Store, TerminalError

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "terminal" / "static"
CSC = "CSC0TESTE0123456789ABCDEF"
ISSUER = {"document": "11222333000181", "name": "Mercado Fiscal Ltda", "trade_name": "Mercado Fiscal",
          "state_registration": "110042490114", "address_line": "Av. Paulista", "address_number": "1000",
          "address_complement": "", "address_neighborhood": "Bela Vista", "address_city": "Sao Paulo",
          "address_state": "SP", "address_zip": "01310100", "city_ibge_code": "3550308", "crt": 1}
FISCAL = {"v": 1, "profile_id": None, "ready": True, "missing": [],
          "fields": {"origin": "0", "ncm": "19053100", "cfop": "5102", "icms_csosn": "102", "pis_cst": "49",
                     "cofins_cst": "49"}}
NFCE_ON = {"enabled": True, "environment": "homologation", "mode": "sefaz", "contingency": True, "wait_ms": 8000,
           "cancel_minutes": 30, "print_rules": {"cash": "ask", "pix": "never", "card_debit": "always"}}


class Answer:
    def __init__(self, status, body):
        self.status_code, self._body, self.ok = status, body, 200 <= status < 300

    def json(self):
        return self._body


class FakeCloud:
    """requests.request double for the terminal's cloud calls."""

    def __init__(self, online=True):
        self.online = online
        self.calls = []
        self.authorize = True
        self.disabled = False
        self.contingency_error = None
        self.expires_in_days = 20

    def __call__(self, method, url, timeout=None, **kwargs):
        path = url.split("/api/v1/terminal/", 1)[1]
        self.calls.append((method, path, kwargs.get("json")))
        if not self.online:
            raise requests.ConnectionError("sem rede")
        if path == "sync":
            return Answer(200, {"accepted_event_ids": [e["event_id"] for e in kwargs["json"]["events"]],
                                "conflicts": [], "quarantined": []})
        if path == "nfce" and self.disabled:
            return Answer(200, {"status": "disabled", "fiscal": None})
        if path == "nfce":
            key = keys.access_key("SP", "2611", "11222333000181", 65, 1, 7, 1, "12345670")
            return Answer(200, {"status": "authorized", "fiscal": {
                "status": "authorized", "state": "authorized", "access_key": key, "protocol": "135260000000001",
                "number": 7, "series": 1, "issued_at": int(time.time() * 1000), "authorized_at": int(time.time() * 1000),
                "url_consulta": "https://www.homologacao.nfce.fazenda.sp.gov.br/NFCeConsultaPublica",
                "qr_url": f"https://www.homologacao.nfce.fazenda.sp.gov.br/qrcode?p={key}|3|2",
                "environment": "homologation", "taxes_cents": 120}})
        if path == "nfce/reserve":
            return Answer(200, {"id": "r1", "environment": "homologation", "series": 1, "numbers": list(range(11, 21)),
                                "csc_id": "000001", "csc": CSC, "qr_version": 2, "issuer": ISSUER, "tech": {},
                                "approx_fallback": "31.45", "ranges": [[11, 20]],
                                "expires_at": datetime.fromtimestamp(time.time() + self.expires_in_days * 86400,
                                                                     timezone.utc).isoformat()})
        if path == "nfce/contingency" and self.contingency_error:
            return Answer(409, {"error": "Faixa liberada", "code": self.contingency_error})
        if path == "nfce/contingency":
            return Answer(200, {"status": "contingency", "fiscal": {"status": "contingency", "state": "contingency"}})
        if path.startswith("nfce/"):
            return Answer(200, {"status": "authorized", "fiscal": {"status": "authorized", "state": "authorized",
                                                                   "protocol": "135260000000002"}})
        return Answer(404, {"error": "?"})


class NfceTerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name,
                               "DB_PATH": Path(self.temp.name) / "terminal.sqlite3", "START_WORKER": False})
        self.store = self.app.extensions["terminal_store"]
        self.client = self.app.test_client()
        tenant_id, shop_id, terminal_id = (str(uuid.uuid4()) for _ in range(3))
        self.cashier_id, self.manager_id = str(uuid.uuid4()), str(uuid.uuid4())
        private, public = generate_keypair()
        self.operators = [{"id": self.cashier_id, "name": "Ana", "role": "cashier", "pin_hash": hash_pin("123987")},
                          {"id": self.manager_id, "name": "Bruno", "role": "manager", "pin_hash": hash_pin("654321")}]
        self.product = {"id": str(uuid.uuid4()), "sku": "BISC", "barcode": "7891000100103", "name": "Biscoito recheado",
                        "unit": "UN", "category": "Mercearia", "price_cents": 899, "quantity_milli": 100000,
                        "active": True, "fiscal": FISCAL}
        now = int(time.time())
        claims = {"schema": 1, "tenant_id": tenant_id, "shop_id": shop_id, "terminal_id": terminal_id,
                  "device_id": self.store.identity()["device_id"], "issued_at": now - 5, "expires_at": now + 3600,
                  "features": ["pdv"], "status": "active", "operators_hash": payload_digest(self.operators)}
        self.lease = sign_lease(claims, private)
        self.public = public
        self.store.set_pairing({"token": "t" * 48, "tenant_id": tenant_id, "shop_id": shop_id,
                                "terminal_id": terminal_id, "lease": self.lease, "public_key": public},
                               "http://127.0.0.1:8000")
        self.bootstrap(NFCE_ON)
        self.cashier = self.store.operator(self.cashier_id)
        self.store.open_cash(self.cashier_id, 0)

    def bootstrap(self, nfce_settings):
        methods = [{"id": "cash", "kind": "cash", "name": "Dinheiro", "active": True, "print_rule": "ask"},
                   {"id": "pix", "kind": "pix", "name": "Pix", "active": True, "print_rule": "never"},
                   {"id": "card_debit", "kind": "card_debit", "name": "Débito", "active": True, "print_rule": "always"},
                   {"id": "card_credit", "kind": "card_credit", "name": "Crédito", "active": True, "print_rule": "bogus"}]
        self.store.apply_bootstrap({"products": [self.product], "operators": self.operators, "lease": self.lease,
                                    "public_key": self.public, "payment_methods": methods, "nfce": nfce_settings,
                                    "issuer": {"legal_name": "Mercado Fiscal Ltda", "document": "11222333000181"}})

    def sell(self, payments=None, request_id=None, document=""):
        payments = payments or [{"method": "cash", "amount_cents": 2000}]
        return self.store.sale(self.cashier, {"request_id": request_id or uuid.uuid4().hex,
                                              "items": [{"product_id": self.product["id"], "quantity_milli": 2000}],
                                              "discount_cents": 100, "customer_document": document, "payments": payments})

    def print_jobs(self):
        return [job for job in self.store.print_jobs() if job["kind"] == "receipt"]

    def set_receipt_printer(self):
        self.store.save_printer("receipt", "file", str(Path(self.temp.name) / "spool"), "escpos80")

    # ------------------------------------------------------------------ storage and rules

    def test_migration_adds_tables_to_an_old_database(self):
        path = Path(self.temp.name) / "old.sqlite3"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        store = Store(path)
        with store.connection() as conn:
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"nfce_docs", "nfce_reservation"} <= tables)
        self.assertEqual(store.sync_status()["nfce"], {"waiting": 0, "overdue": 0, "rejected": 0, "contingency": 0})
        Store(path)  # idempotent

    def test_bootstrap_keeps_settings_and_print_rules(self):
        config = self.store.nfce_config()
        self.assertEqual((config["enabled"], config["environment"], config["print_rules"]["pix"]),
                         (True, "homologation", "never"))
        methods = {row["kind"]: row for row in self.store.payment_methods()}
        self.assertEqual(methods["card_debit"]["print_rule"], "always")
        self.assertNotIn("print_rule", methods["card_credit"])  # unknown value dropped

    def test_sale_waits_for_the_note_and_decides_printing(self):
        self.set_receipt_printer()
        cash = self.sell()
        self.assertEqual(cash["nfce"]["print"], "ask")
        self.assertEqual(self.print_jobs(), [])  # printed only after the NFC-e answer
        self.assertEqual(self.sell([{"method": "pix", "amount_cents": 1698}])["nfce"]["print"], "never")
        self.assertEqual(self.sell([{"method": "card_debit", "amount_cents": 1698}])["nfce"]["print"], "always")
        split = self.sell([{"method": "pix", "amount_cents": 698}, {"method": "card_debit", "amount_cents": 1000}])
        self.assertEqual(split["nfce"]["print"], "always")
        split = self.sell([{"method": "pix", "amount_cents": 698}, {"method": "cash", "amount_cents": 1000}])
        self.assertEqual(split["nfce"]["print"], "ask")
        # NFC-e off: the receipt prints right away, as before.
        self.bootstrap({"enabled": False, "environment": "non_fiscal"})
        self.sell()
        self.assertEqual(len(self.print_jobs()), 1)

    # ------------------------------------------------------------------ cloud and contingency

    def test_authorized_note_prints_the_danfe(self):
        sale = self.sell()
        cloud = FakeCloud()
        with patch.object(sync.requests, "request", cloud):
            detail = sync.request_nfce(self.store, sale["id"])
        self.assertEqual([call[1] for call in cloud.calls], ["sync", "nfce"])
        self.assertEqual(detail["fiscal"]["status"], "authorized")
        self.assertEqual(detail["receipt"]["fiscal_state"], "homologation")
        text = " ".join(line for block in detail["receipt"]["blocks"] for line in block.get("lines", []))
        self.assertIn("DOCUMENTO AUXILIAR DA NOTA FISCAL DE CONSUMIDOR ELETRÔNICA", text)
        self.assertIn("SEM VALOR FISCAL", text)
        self.assertIn("135260000000001", text)
        self.assertTrue(any(block["kind"] == "qr" for block in detail["receipt"]["blocks"]))
        self.set_receipt_printer()
        self.assertEqual(self.store.print_sale(sale["id"], self.cashier_id)["status"], "queued")  # F9 / "Sim"

    def test_offline_contingency_is_built_locally_and_uploaded_later(self):
        cloud = FakeCloud()
        with patch.object(sync.requests, "request", cloud):
            sync.nfce_step(self.store)  # reserves numbers while online
        with self.store.connection() as conn:
            self.assertEqual(nfce.reservation(conn)["numbers"][0], 11)
        sale = self.sell(document="52998224725")
        cloud.online = False
        with patch.object(sync.requests, "request", cloud):
            detail = sync.request_nfce(self.store, sale["id"], wait_s=2)
        fiscal = detail["fiscal"]
        self.assertEqual((fiscal["status"], fiscal["number"], fiscal["emission_type"]), ("contingency", 11, 9))
        self.assertTrue(keys.valid_key(fiscal["access_key"]))
        self.assertEqual(fiscal["access_key"][34], "9")
        # QR Code v2 offline: hash with the CSC over the parameters, DigestValue in hex.
        params = fiscal["qr_url"].split("?p=")[1].split("|")
        self.assertEqual(params[1:3], ["2", "2"])
        self.assertEqual(params[7], hashlib.sha1(("|".join(params[:7]) + CSC).encode()).hexdigest().upper())
        layout = receipt_layout(detail, 80)
        text = " ".join(line for block in layout["blocks"] for line in block.get("lines", []))
        self.assertIn("EMITIDA EM CONTINGÊNCIA", text)
        self.assertIn("Pendente de autorização", text)
        self.assertEqual(receipt_bytes(detail, 80).count(b"\x1dV"), 2)  # two copies, each with its cut
        with self.store.connection() as conn:
            row = nfce.row(conn, sale["id"])
            self.assertEqual(nfce.reservation(conn)["numbers"][0], 12)
        xml = from_etree(parse(row["xml"]))
        self.assertEqual((xml.findtext("infNFe/ide/tpEmis"), xml.findtext("infNFe/ide/nNF")), ("9", "11"))
        self.assertEqual(xml.findtext("infNFe/dest/CPF"), "52998224725")
        self.assertIsNone(xml.find("Signature"))  # the certificate never leaves the cloud
        self.assertEqual(self.store.sync_status()["nfce"]["contingency"], 1)
        # Back online: the worker uploads it.
        cloud.online = True
        with patch.object(sync.requests, "request", cloud):
            done = sync.nfce_step(self.store)
        self.assertEqual(done["uploaded"], 1)
        upload = next(call for call in cloud.calls if call[1] == "nfce/contingency")
        self.assertEqual(upload[2]["sale_id"], sale["id"])
        with patch.object(sync.requests, "request", cloud):
            self.assertEqual(sync.nfce_step(self.store)["refreshed"], 1)
        with self.store.connection() as conn:
            self.assertEqual(nfce.row(conn, sale["id"])["status"], "authorized")

    def reserve(self, cloud=None):
        cloud = cloud or FakeCloud()
        with patch.object(sync.requests, "request", cloud):
            sync.nfce_step(self.store)
        return cloud

    def test_reservation_asks_are_spaced_and_send_the_numbers_used_here(self):
        cloud = self.reserve()
        self.assertEqual(sum(1 for call in cloud.calls if call[1] == "nfce/reserve"), 1)
        with self.store.transaction() as conn:
            current = nfce.reservation(conn)
            current["numbers"], current["used"] = [19, 20], list(range(11, 19))
            conn.execute("UPDATE nfce_reservation SET data=? WHERE id=1", (json.dumps(current),))
        # Low on numbers, but the last ask was a moment ago: no new request yet.
        self.assertFalse(self.store.nfce_needs_reservation())
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "nfce_reserve_attempt_at", str(int(time.time() * 1000) - 10 * 60 * 1000))
        self.assertTrue(self.store.nfce_needs_reservation())
        self.reserve(cloud)
        asks = [call for call in cloud.calls if call[1] == "nfce/reserve"]
        self.assertEqual(asks[-1][2], {"used": list(range(11, 19))})

    def test_expired_or_expiring_block_is_not_used(self):
        cloud = FakeCloud()
        cloud.expires_in_days = 2  # inside the renewal window: the worker asks again
        self.reserve(cloud)
        with self.store.connection() as conn:
            self.assertTrue(nfce.needs_reservation(conn, nfce.config(conn, self.store._meta)))
        cloud.expires_in_days = 0.5  # less than the safety margin: no contingency note with it
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "nfce_reserve_attempt_at", "0")
        self.reserve(cloud)
        sale = self.sell()
        cloud.online = False
        with patch.object(sync.requests, "request", cloud):
            detail = sync.request_nfce(self.store, sale["id"], wait_s=2)
        self.assertEqual(detail["nfce"]["status"], "pending")
        self.assertIn("vencida", detail["nfce"]["error"])

    def test_block_released_in_the_cloud_turns_the_note_into_a_normal_request(self):
        cloud = self.reserve()
        sale = self.sell()
        cloud.online = False
        with patch.object(sync.requests, "request", cloud):
            sync.request_nfce(self.store, sale["id"], wait_s=2)
        cloud.online, cloud.contingency_error = True, "reservation_released"
        with patch.object(sync.requests, "request", cloud):
            sync.nfce_step(self.store)
        with self.store.connection() as conn:
            self.assertEqual(nfce.row(conn, sale["id"])["status"], "pending")
            self.assertEqual(nfce.pending_uploads(conn), [])
        cloud.contingency_error = None
        with patch.object(sync.requests, "request", cloud):
            self.assertEqual(sync.nfce_step(self.store)["retried"], 1)
        self.assertEqual(self.store.sale_detail(sale["id"])["fiscal"]["status"], "authorized")

    def test_sale_while_nfce_is_off_in_the_cloud_never_gets_a_late_note(self):
        sale = self.sell()
        cloud = FakeCloud()
        cloud.disabled = True
        with patch.object(sync.requests, "request", cloud):
            sync.request_nfce(self.store, sale["id"])
        cloud.disabled = False
        with self.store.connection() as conn:
            self.assertEqual(nfce.row(conn, sale["id"])["status"], "disabled")
            self.assertEqual(nfce.to_retry(conn), [])
        with patch.object(sync.requests, "request", cloud):
            self.assertEqual(sync.nfce_step(self.store)["retried"], 0)
        self.assertEqual(sum(1 for call in cloud.calls if call[:2] == ("POST", "nfce")), 1)

    def test_csc_is_not_in_local_backups(self):
        self.reserve()
        backup = self.store.backup_now()
        with sqlite3.connect(Path(backup["directory"]) / backup["name"]) as conn:
            data = json.loads(conn.execute("SELECT data FROM nfce_reservation").fetchone()[0])
        self.assertNotIn("csc", data)
        self.assertEqual(data["numbers"][0], 11)
        from terminal import mirror
        staging, _seq = mirror._local_copy(self.store)
        try:
            with sqlite3.connect(staging) as conn:
                self.assertNotIn("csc", json.loads(conn.execute("SELECT data FROM nfce_reservation").fetchone()[0]))
        finally:
            mirror._remove_local_copy(staging)
        with self.store.connection() as conn:
            self.assertEqual(nfce.reservation(conn)["csc"], CSC)  # the live database keeps it

    def test_offline_without_reservation_stays_pending_and_is_retried(self):
        sale = self.sell()
        cloud = FakeCloud(online=False)
        with patch.object(sync.requests, "request", cloud):
            detail = sync.request_nfce(self.store, sale["id"], wait_s=2)
        self.assertEqual(detail["nfce"]["status"], "pending")
        self.assertEqual(detail["receipt"]["fiscal_state"], "none")
        self.assertEqual(self.store.sync_status()["nfce"]["waiting"], 1)
        cloud.online = True
        with patch.object(sync.requests, "request", cloud):
            self.assertEqual(sync.nfce_step(self.store)["retried"], 1)
        self.assertEqual(self.store.sale_detail(sale["id"])["fiscal"]["status"], "authorized")

    def test_overdue_contingency_is_flagged(self):
        sale = self.sell()
        self.store.nfce_record(sale["id"], "contingency", {"access_key": "1" * 44})
        with self.store.transaction() as conn:
            conn.execute("UPDATE nfce_docs SET created_at=?", (int(time.time() * 1000) - 21 * 3600 * 1000,))
        self.assertEqual(self.store.sync_status()["nfce"]["overdue"], 1)

    def test_void_of_a_sale_with_nfce_needs_reason_and_deadline(self):
        sale = self.sell()
        with patch.object(sync.requests, "request", FakeCloud()):
            sync.request_nfce(self.store, sale["id"])
        manager = self.store.operator(self.manager_id)
        with self.assertRaises(TerminalError) as short:
            self.store.void_sale(manager, sale["id"], "Desistiu")
        self.assertIn("15 caracteres", str(short.exception))
        with self.store.transaction() as conn:
            row = nfce.row(conn, sale["id"])
            data = dict(row["data"], authorized_at=int(time.time() * 1000) - 31 * 60 * 1000)
            conn.execute("UPDATE nfce_docs SET data=? WHERE sale_id=?", (json.dumps(data), sale["id"]))
        with self.assertRaises(TerminalError) as late:
            self.store.void_sale(manager, sale["id"], "Cliente desistiu da compra no caixa")
        self.assertEqual(late.exception.code, "nfce_cancel_expired")
        with self.store.transaction() as conn:
            conn.execute("UPDATE nfce_docs SET data=? WHERE sale_id=?",
                         (json.dumps(dict(data, authorized_at=int(time.time() * 1000))), sale["id"]))
        with self.assertRaises(TerminalError):  # a cashier still needs the manager PIN
            self.store.void_sale(self.cashier, sale["id"], "Cliente desistiu da compra no caixa", "000000")
        voided = self.store.void_sale(self.cashier, sale["id"], "Cliente desistiu da compra no caixa", "654321")
        self.assertEqual(voided["status"], "voided")
        event = [e for e in self.store.pending_events() if e["type"] == "sale.voided"][0]
        self.assertEqual(event["data"]["reason"], "Cliente desistiu da compra no caixa")

    # ------------------------------------------------------------------ routes

    def test_routes(self):
        csrf = self.client.get("/api/session").json["csrf"]
        login = self.client.post("/api/login", json={"operator_id": self.cashier_id, "pin": "123987"},
                                 headers={"X-CSRF-Token": csrf})
        csrf = login.json["csrf"]
        sale = self.sell()
        with patch.object(sync.requests, "request", FakeCloud()):
            answer = self.client.post(f"/api/sales/{sale['id']}/nfce", json={}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(answer.status_code, 200, answer.json)
        self.assertEqual(answer.json["sale"]["fiscal"]["status"], "authorized")
        no_printer = self.client.post(f"/api/sales/{sale['id']}/print", json={}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(no_printer.status_code, 409)
        self.assertEqual(self.client.post("/api/sales/nao-existe/nfce", json={}, headers={"X-CSRF-Token": csrf}).status_code, 404)


@unittest.skipUnless(shutil.which("node"), "node não está disponível")
class NfceDialogTests(unittest.TestCase):
    """Pure rules of the "Imprimir a nota fiscal?" dialog in terminal-core.js."""

    @classmethod
    def setUpClass(cls):
        script = """
const C = require(process.argv[1]);
const authorized = {nfce: {enabled: true, print: 'ask'}, fiscal: {status: 'authorized', number: 7, environment: 'homologation',
  qr_url: 'https://x.test/qrcode?p=1|3|2', access_key: '3'.repeat(44)}};
console.log(JSON.stringify({
  decision: [C.nfcePrintDecision([{method: 'cash'}], {}), C.nfcePrintDecision([{method: 'pix'}], {}),
    C.nfcePrintDecision([{method: 'card_credit'}], {}), C.nfcePrintDecision([{method: 'cash'}, {method: 'card_debit'}], {}),
    C.nfcePrintDecision([{method: 'cash'}], {cash: 'never'}), C.nfcePrintDecision([{method: 'cash'}, {method: 'pix'}], {cash: 'never'}),
    C.nfcePrintDecision([{method: 'other', payment_method_id: 'v1'}], {v1: 'always', other: 'never'})],
  keys: ['Enter', 's', 'S', 'Escape', 'n', 'N', 'x'].map(C.printPromptKey),
  plan: [C.nfcePlan(authorized), C.nfcePlan({}), C.nfcePlan({nfce: {enabled: true, print: 'never', status: 'pending'}})],
  text: [C.nfceStatusText(authorized), C.nfceStatusText({fiscal: {status: 'contingency', number: 11}}),
    C.nfceStatusText({fiscal: {status: 'rejected', error: '778 - NCM inexistente'}}), C.nfceStatusText({nfce: {status: 'pending'}})],
  share: [C.nfceShareLinks(authorized.fiscal, '(21) 99999-0000'), C.nfceShareLinks(authorized.fiscal, 'cliente@example.test'),
    C.nfceShareLinks(authorized.fiscal, '123'), C.nfceShareLinks({}, 'cliente@example.test')],
  chip: [C.nfceChip({waiting: 0, rejected: 0}), C.nfceChip({waiting: 2, overdue: 0}), C.nfceChip({waiting: 1, overdue: 1}),
    C.nfceChip({rejected: 1})],
}));
"""
        result = subprocess.run(["node", "-e", script, str(STATIC / "terminal-core.js")],
                                capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.out = json.loads(result.stdout)

    def test_print_decision_matches_the_python_rule(self):
        from shared.nfce.payments import print_decision
        self.assertEqual(self.out["decision"], ["ask", "ask", "always", "always", "never", "ask", "always"])
        self.assertEqual(print_decision([{"method": "cash"}, {"method": "pix"}], {"cash": "never"}), self.out["decision"][5])

    def test_keys_enter_s_yes_and_esc_n_no(self):
        self.assertEqual(self.out["keys"], ["yes", "yes", "yes", "no", "no", "no", None])

    def test_plan_and_status_texts(self):
        self.assertEqual(self.out["plan"][0], {"issue": True, "status": "authorized", "print": "ask", "fiscal": True})
        self.assertEqual(self.out["plan"][1], {"issue": False, "print": "always"})
        self.assertEqual(self.out["plan"][2]["print"], "never")
        self.assertIn("NFC-e nº 7 autorizada (homologação", self.out["text"][0])
        self.assertIn("contingência", self.out["text"][1])
        self.assertIn("24 h", self.out["text"][1])
        self.assertIn("778 - NCM inexistente", self.out["text"][2])
        self.assertIn("pendente", self.out["text"][3])

    def test_share_only_builds_public_links(self):
        whatsapp, email, invalid, empty = self.out["share"]
        self.assertTrue(whatsapp["whatsapp"].startswith("https://wa.me/5521999990000?text="))
        self.assertIn("https%3A%2F%2Fx.test%2Fqrcode", whatsapp["whatsapp"])
        self.assertTrue(email["email"].startswith("mailto:cliente%40example.test?subject="))
        self.assertIsNone(invalid)
        self.assertIsNone(empty)

    def test_chip_warns_and_flags_overdue(self):
        hidden, waiting, overdue, rejected = self.out["chip"]
        self.assertTrue(hidden["hidden"])
        self.assertEqual((waiting["state"], waiting["text"]), ("warn", "NFC-e: 2 aguardando envio"))
        self.assertEqual(overdue["state"], "danger")
        self.assertIn("24 horas", overdue["title"])
        self.assertIn("rejeitada", rejected["text"])


if __name__ == "__main__":
    unittest.main()
