"""Balança do balcão: EAN-13 de pesados, etiquetas ZPL/EPL/PPLA/TSPL, protocolos Toledo/Filizola,
ida e volta com o leitor do caixa, rotas e permissões."""
import json
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from datetime import datetime
from pathlib import Path

from shared.licensing import generate_keypair, payload_digest, sign_lease
from shared.security import hash_pin
from terminal.app import create_app
from terminal.labels import (LABEL_LANGUAGES, LabelError, decode_weighed, ean13_check_digit, ean13_modules,
                             ean13_valid, label_fields, line_total, weighed_barcode, weighed_label_bytes)
from terminal.printers import label_bytes

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "terminal" / "static"
NOW = datetime(2026, 11, 18, 14, 3)
CHEESE = {"name": "Queijo muçarela", "scale_code": "42", "price_cents": 4990}


class WeighedBarcodeTests(unittest.TestCase):
    def test_check_digit_matches_real_ean13(self):
        self.assertTrue(ean13_valid("7894900011517"))
        self.assertEqual(ean13_check_digit("789490001151"), "7")
        self.assertFalse(ean13_valid("7894900011518"))
        self.assertFalse(ean13_valid("789490001151"))

    def test_same_format_the_cashier_reads(self):
        # 2 + PLU(5) + gramas(5) + 0 + DV: o exemplo do teste do caixa (terminal-core scaleLabel).
        self.assertEqual(weighed_barcode("54321", 899, 1250)["barcode"], "2543210125005")
        self.assertEqual(decode_weighed("2543210125005"), {"scale_code": "54321", "payload": 1250})
        by_price = weighed_barcode("42", 1499, 1234, "price")
        self.assertEqual(by_price, {"barcode": "2000420185002", "payload": 1850, "total_cents": 1850})
        self.assertEqual(weighed_barcode("00042", 1499, 1234)["barcode"][:6], "200042")

    def test_rounding_is_the_sale_rounding(self):
        self.assertEqual(line_total(899, 1234), 1109)   # 1109,366
        self.assertEqual(line_total(1000, 1255), 1255)
        self.assertEqual(line_total(999, 1500), 1499)   # 1498,5 sobe

    def test_impossible_labels_are_refused_with_a_clear_reason(self):
        cases = [(("", 1000, 500), "não tem código de balança"), (("ABC", 1000, 500), "1 a 99999"),
                 (("123456", 1000, 500), "1 a 99999"), (("0", 1000, 500), "1 a 99999"),
                 (("42", 1000, 0), "Peso inválido"), (("42", 1000, 100000), "99,999 kg"),
                 (("42", 0, 500), "sem preço")]
        for args, message in cases:
            with self.subTest(args=args), self.assertRaisesRegex(LabelError, message):
                weighed_barcode(*args)
        with self.assertRaisesRegex(LabelError, "R\\$ 999,99"):
            weighed_barcode("42", 9990, 10500, "price")
        self.assertEqual(weighed_barcode("42", 9990, 10500)["payload"], 10500)  # no modo peso cabe

    def test_modules_have_guards_and_95_bars(self):
        bits = ean13_modules("2000420045603")
        self.assertEqual(len(bits), 95)
        self.assertTrue(bits.startswith("101") and bits.endswith("101") and bits[45:50] == "01010")


class LabelBytesSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fields = label_fields(CHEESE, 456, "weight", 3, "Padaria Sol", now=NOW)

    def test_fields(self):
        self.assertEqual(self.fields["barcode"], "2000420045603")
        self.assertEqual(self.fields["total_text"], "R$ 22,75")
        self.assertEqual(self.fields["weight_text"], "0,456 kg")
        self.assertEqual(self.fields["unit_price_text"], "R$ 49,90/kg")
        self.assertEqual(self.fields["packed_at"], "18/11/2026 14:03")
        self.assertEqual(self.fields["valid_until"], "21/11/2026")
        self.assertEqual(label_fields(CHEESE, 456, "weight", 0, "", now=NOW)["valid_until"], "")

    def test_zpl(self):
        self.assertEqual(weighed_label_bytes(self.fields, "zpl", "50x30", 2), (
            b"^XA^CI0^PW400^LL240^LH0,0^FO12,8^A0N,18,18^FDPadaria Sol^FS^FO12,30^A0N,24,24^FDQueijo mucarela^FS"
            b"^FO12,60^A0N,18,18^FD0,456 kg x R$ 49,90/kg^FS^FO12,84^A0N,36,36^FDTotal R$ 22,75^FS"
            b"^FO12,126^A0N,18,18^FDEmb 18/11/26 14:03 Val 21/11/26^FS^FO105,150^BY2^BEN,60,Y,N^FD200042004560^FS"
            b"^PQ2,0,1,Y^XZ"))

    def test_zpl_other_sizes(self):
        small = weighed_label_bytes(self.fields, "zpl", "40x30", 1)
        self.assertIn(b"^PW320^LL240", small)
        self.assertIn(b"^A0N,16,16^FDEmb 18/11/26 14:03 Val 21/11/26", small)  # letra menor no 40 mm
        self.assertIn(b"^FO65,148^BY2^BEN,62", small)
        large = weighed_label_bytes(self.fields, "zpl", "60x40", 1)
        self.assertIn(b"^PW480^LL320", large)
        self.assertIn(b"^A0N,44,44^FDTotal R$ 22,75", large)

    def test_epl(self):
        self.assertEqual(weighed_label_bytes(self.fields, "epl", "50x30", 2), (
            b'\nN\nq400\nQ240,24\nA12,8,0,2,1,1,N,"Padaria Sol"\nA12,30,0,3,1,1,N,"Queijo mucarela"\n'
            b'A12,60,0,2,1,1,N,"0,456 kg x R$ 49,90/kg"\nA12,84,0,4,1,1,N,"Total R$ 22,75"\n'
            b'A12,126,0,2,1,1,N,"Emb 18/11/26 14:03 Val 21/11/26"\nB105,150,0,E30,2,2,60,B,"200042004560"\nP2\n'))

    def test_ppla_uses_native_ean13(self):
        self.assertEqual(weighed_label_bytes(self.fields, "ppla", "50x30", 2), (
            b"\x02L\rm\rD11\rH14\rPC\r121100002700015Padaria Sol\r131100002320015Queijo mucarela\r"
            b"1211000020500150,456 kg x R$ 49,90/kg\r141100001580015Total R$ 22,75\r"
            b"121100001220015Emb 18/11/26 14:03 Val 21/11/26\r1F2207500080131200042004560\rQ0002\rE\r"))

    def test_tspl(self):
        self.assertEqual(weighed_label_bytes(self.fields, "tspl", "50x30", 2), (
            b'SIZE 50 mm,30 mm\r\nGAP 2 mm,0 mm\r\nDIRECTION 1\r\nREFERENCE 0,0\r\nCLS\r\n'
            b'TEXT 12,8,"2",0,1,1,"Padaria Sol"\r\nTEXT 12,30,"3",0,1,1,"Queijo mucarela"\r\n'
            b'TEXT 12,60,"2",0,1,1,"0,456 kg x R$ 49,90/kg"\r\nTEXT 12,84,"4",0,1,1,"Total R$ 22,75"\r\n'
            b'TEXT 12,126,"2",0,1,1,"Emb 18/11/26 14:03 Val 21/11/26"\r\n'
            b'BARCODE 105,150,"EAN13",60,1,0,2,2,"200042004560"\r\nPRINT 1,2\r\n'))

    def test_hostile_names_cannot_inject_commands(self):
        fields = label_fields({"name": 'Lingui^XZ"ça\r\nP9', "scale_code": "7", "price_cents": 2990}, 1000,
                              "weight", 0, "", now=NOW)
        zpl = weighed_label_bytes(fields, "zpl", "50x30", 1)
        self.assertEqual(zpl.count(b"^XZ"), 1)
        epl = weighed_label_bytes(fields, "epl", "50x30", 1)
        self.assertEqual(epl.count(b"\nP"), 1)
        self.assertIn(b"\"Lingui^XZ'ca P9\"", epl)  # aspas e quebra de linha neutralizadas
        for language in LABEL_LANGUAGES:
            with self.assertRaises(LabelError):
                weighed_label_bytes(fields, language, "50x30", 0)
        with self.assertRaises(LabelError):
            weighed_label_bytes(fields, "zpl", "70x50", 1)

    def test_shelf_labels_in_ppla_and_tspl(self):
        product = {"id": "x", "name": "Arroz 5kg", "barcode": "7894900011517", "price_cents": 2490, "unit": "UN"}
        self.assertEqual(label_bytes(product, "ppla"),
                         b"\x02L\rm\rD11\rH14\rPC\r131100002400020Arroz 5kg\r141100001650020R$ 24,90\r"
                         b"1F2208000250020789490001151\rQ0001\rE\r")
        self.assertIn(b'BARCODE 16,100,"EAN13",80,1,0,2,2,"789490001151"', label_bytes(product, "tspl"))
        self.assertIn(b'"128"', label_bytes({**product, "barcode": "SKU-1"}, "tspl"))


@unittest.skipUnless(shutil.which("node"), "node não está disponível")
class RoundTripWithCashierTests(unittest.TestCase):
    """O código impresso aqui é lido pelo caixa (terminal-core.js) com o mesmo peso e valor."""

    CASES = [("54321", 899, 1250), ("42", 1499, 1234), ("1", 4990, 456), ("99999", 999, 1500),
             ("00007", 12990, 75), ("101", 5990, 99999), ("8", 350, 2003), ("12", 799, 1)]

    def run_node(self, script, payload):
        result = subprocess.run(["node", "-e", script, str(STATIC / "terminal-core.js"), str(STATIC / "scale-core.js"),
                                 json.dumps(payload)], capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def test_python_label_is_read_back_by_the_cashier(self):
        labels = []
        for plu, price, milli in self.CASES:
            for mode in ("weight", "price"):
                try:
                    code = weighed_barcode(plu, price, milli, mode)
                except LabelError:
                    continue
                labels.append({"plu": plu, "price": price, "milli": milli, "mode": mode, **code})
        out = self.run_node("""
const C = require(process.argv[1]), S = require(process.argv[2]);
const labels = JSON.parse(process.argv[3]);
console.log(JSON.stringify(labels.map(l => {
  const read = C.scaleLabel(l.barcode);
  return {read, qty: C.scaleLabelQuantity(read, {unit: 'KG', price_cents: l.price}, l.mode),
          js: S.weighedBarcode(l.plu, l.price, l.milli, l.mode).barcode, modules: S.ean13Modules(l.barcode)};
})));""", labels)
        self.assertGreaterEqual(len(labels), 14)
        for label, back in zip(labels, out):
            with self.subTest(label=label):
                self.assertEqual(back["js"], label["barcode"])  # o JS da pré-visualização gera o mesmo código
                self.assertEqual(back["modules"], ean13_modules(label["barcode"]))
                self.assertEqual(back["read"]["scaleCode"], f"{int(label['plu']):05d}")
                if label["mode"] == "weight":
                    self.assertEqual(back["qty"], label["milli"])
                else:
                    # Pelo preço, o caixa recalcula o peso; o valor cobrado é o da etiqueta.
                    self.assertEqual(line_total(label["price"], back["qty"]), label["total_cents"])

    def test_js_label_fields_match_python(self):
        out = self.run_node("""
const S = require(process.argv[2]);
const p = JSON.parse(process.argv[3]);
console.log(JSON.stringify(S.labelFields(p, 456, {barcodeMode: 'weight', validityDays: 3, storeName: 'Padaria Sol',
  now: new Date(2026, 10, 18, 14, 3)})));""", CHEESE)
        self.assertEqual(out, label_fields(CHEESE, 456, "weight", 3, "Padaria Sol", now=NOW))


@unittest.skipUnless(shutil.which("node"), "node não está disponível")
class ScaleProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = r"""
const S = require(process.argv[1]);
const feed = (protocol, chunks) => { const p = S.createScaleParser(protocol); return chunks.flatMap(c => p.push(c)); };
const st = S.createStability(2);
const out = {
  toledo: feed('toledo', ['\x0201234\x03', '\x02II', 'III\x03', '\x02NNNNN\x03\x02SSSSS\x03', '\x0200000\x03', 'lixo\x02012', '345\x03']),
  toledoSix: feed('toledo', ['\x02015250\x03']),
  filizola: feed('filizola', ['\x0201.234\x03', '\x02 2,500\x03']),
  urano: feed('filizola', ['PESO L:   0,750 kg\r', 'PESO L:   0,7']),
  generic: feed('generic', ['ST,GS,  1.250kg\r\n', '500 g\n', '-0.020kg\r\n', '0.000kg\r\n']),
  request: [S.requestBytes('toledo', false) && [...S.requestBytes('toledo', false)], S.requestBytes('toledo', true),
            S.requestBytes('generic', false), [...S.requestBytes('filizola', false)]],
  stability: [st.push({milli: 500, stable: null, status: 'ok'}), st.push({milli: 500, stable: null, status: 'ok'}),
              st.push({milli: 0, stable: false, status: 'unstable'}), st.push({milli: 700, stable: true, status: 'ok'})],
  protocols: Object.keys(S.PROTOCOLS),
};
console.log(JSON.stringify(out));
"""
        result = subprocess.run(["node", "-e", script, str(STATIC / "scale-core.js")],
                                capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.out = json.loads(result.stdout)

    def test_toledo_frames_status_and_split_chunks(self):
        self.assertEqual(self.out["toledo"], [
            {"milli": 1234, "stable": True, "status": "ok"}, {"milli": 0, "stable": False, "status": "unstable"},
            {"milli": 0, "stable": False, "status": "negative"}, {"milli": 0, "stable": False, "status": "overload"},
            {"milli": 0, "stable": True, "status": "zero"}, {"milli": 12345, "stable": True, "status": "ok"}])
        self.assertEqual(self.out["toledoSix"], [{"milli": 15250, "stable": True, "status": "ok"}])

    def test_filizola_and_urano(self):
        self.assertEqual(self.out["filizola"], [{"milli": 1234, "stable": True, "status": "ok"},
                                                {"milli": 2500, "stable": True, "status": "ok"}])
        self.assertEqual(self.out["urano"], [{"milli": 750, "stable": True, "status": "ok"}])

    def test_generic_lines(self):
        self.assertEqual(self.out["generic"], [
            {"milli": 1250, "stable": None, "status": "ok"}, {"milli": 500, "stable": None, "status": "ok"},
            {"milli": 0, "stable": None, "status": "negative"}, {"milli": 0, "stable": None, "status": "zero"}])

    def test_enq_is_sent_only_on_request_mode(self):
        self.assertEqual(self.out["request"], [[5], None, None, [5]])
        self.assertEqual(self.out["protocols"], ["generic", "toledo", "filizola"])

    def test_stability_trusts_protocol_or_repetition(self):
        self.assertEqual(self.out["stability"], [False, True, False, True])


class ScaleRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name,
                               "DB_PATH": Path(self.temp.name) / "terminal.sqlite3", "START_WORKER": False})
        self.store = self.app.extensions["terminal_store"]
        self.client = self.app.test_client()
        ids = {role: str(uuid.uuid4()) for role in ("cashier", "manager", "inventory", "viewer")}
        self.ids = ids
        operators = [{"id": ids["cashier"], "name": "Ana", "role": "cashier", "pin_hash": hash_pin("123987")},
                     {"id": ids["manager"], "name": "Bruno", "role": "manager", "pin_hash": hash_pin("654321")},
                     {"id": ids["inventory"], "name": "Carla", "role": "inventory", "pin_hash": hash_pin("111222")},
                     {"id": ids["viewer"], "name": "Davi", "role": "viewer", "pin_hash": hash_pin("333444")}]
        self.pins = {"cashier": "123987", "manager": "654321", "inventory": "111222", "viewer": "333444"}

        def product(name, code, price, unit="KG"):
            return {"id": str(uuid.uuid4()), "sku": name[:6].upper(), "name": name, "unit": unit, "scale_code": code,
                    "category": "Balcão", "price_cents": price, "quantity_milli": 50000, "active": True}
        self.cheese = product("Queijo muçarela", "42", 4990)
        self.bread = product("Pão francês", "1", 1499)
        self.banana = product("Banana prata", "", 699)
        self.soda = product("Refrigerante", "77", 999, unit="UN")
        self.tomato = product("Tomate", "00009", 899)
        self.tomato_twin = product("Abobrinha", "9", 599)  # mesmo PLU lido pelo caixa: 00009
        self.products = [self.cheese, self.bread, self.banana, self.soda, self.tomato, self.tomato_twin]
        tenant_id, shop_id, terminal_id = (str(uuid.uuid4()) for _ in range(3))
        private, public = generate_keypair()
        now = int(time.time())
        lease = sign_lease({"schema": 1, "tenant_id": tenant_id, "shop_id": shop_id, "terminal_id": terminal_id,
                            "device_id": self.store.identity()["device_id"], "issued_at": now - 5,
                            "expires_at": now + 3600, "features": ["pdv"], "status": "active",
                            "operators_hash": payload_digest(operators)}, private)
        self.store.set_pairing({"token": "t" * 48, "tenant_id": tenant_id, "shop_id": shop_id,
                                "terminal_id": terminal_id, "lease": lease, "public_key": public},
                               "http://127.0.0.1:8000")
        self.store.apply_bootstrap({"products": self.products, "operators": operators, "lease": lease,
                                    "public_key": public, "shop": {"name": "Padaria Sol"}})

    def csrf(self):
        return self.client.get("/api/session").json["csrf"]

    def login(self, role):
        response = self.client.post("/api/login", json={"operator_id": self.ids[role], "pin": self.pins[role]},
                                    headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(response.status_code, 200, response.json)

    def post(self, path, payload):
        return self.client.post(path, json=payload, headers={"X-CSRF-Token": self.csrf()})

    def stock(self, product):
        return self.store.products_by_ids([product["id"]])[0]["stock_milli"]

    def test_counter_attendant_prints_without_cash_and_stock_stays(self):
        self.login("inventory")
        self.assertIsNone(self.store.cash_state())
        found = self.client.get("/api/scale/products?q=mucarela").json["products"]
        self.assertEqual([row["id"] for row in found], [self.cheese["id"]])
        before = self.stock(self.cheese)
        response = self.post("/api/scale/labels", {"product_id": self.cheese["id"], "weight_milli": 456, "copies": 2})
        self.assertEqual(response.status_code, 200, response.json)
        label = response.json["label"]
        self.assertEqual(label["barcode"], "2000420045603")
        self.assertEqual(label["total_cents"], 2275)
        self.assertEqual(label["store_name"], "Padaria Sol")
        self.assertEqual(response.json["output"], "browser")  # sem etiquetadora: navegador
        self.assertIsNone(response.json["job"])
        self.assertEqual(self.stock(self.cheese), before)  # o estoque baixa na venda, não na etiqueta
        with self.store.connection() as conn:
            row = dict(conn.execute("SELECT * FROM scale_labels").fetchone())
            audit = conn.execute("SELECT * FROM audit WHERE action='scale.label_printed'").fetchone()
        self.assertEqual((row["weight_milli"], row["total_cents"], row["copies"], row["operator_id"], row["manual"]),
                         (456, 2275, 2, self.ids["inventory"], 0))
        self.assertEqual(audit["subject_id"], self.cheese["id"])
        self.assertIn("peso=456g; valor=2275; codigo=2000420045603; copias=2", audit["detail"])
        self.assertEqual(audit["operator_id"], self.ids["inventory"])

    def test_cashier_reads_the_printed_label_back(self):
        self.login("cashier")
        label = self.post("/api/scale/labels", {"product_id": self.cheese["id"], "weight_milli": 1234}).json["label"]
        decoded = decode_weighed(label["barcode"])
        found = self.client.get("/api/products/scale/" + decoded["scale_code"]).json["product"]
        self.assertEqual(found["id"], self.cheese["id"])
        self.assertEqual(decoded["payload"], 1234)

    def test_price_mode_is_a_shop_setting_for_managers(self):
        self.login("cashier")
        self.assertEqual(self.post("/api/scale/settings", {"barcode_mode": "price"}).status_code, 403)
        self.login("manager")
        saved = self.post("/api/scale/settings", {"barcode_mode": "price", "validity_days": 0, "size": "60x40"})
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertEqual((saved.json["settings"]["barcode_mode"], saved.json["size"]), ("price", "60x40"))
        label = self.post("/api/scale/labels", {"product_id": self.bread["id"], "weight_milli": 1234}).json["label"]
        self.assertEqual(label["barcode"], "2000010185009")
        self.assertEqual(label["valid_until"], "")
        self.assertEqual(self.post("/api/scale/settings", {"validity_days": 400}).status_code, 400)
        self.assertEqual(self.post("/api/scale/settings", {"barcode_mode": "x"}).status_code, 400)
        too_much = self.post("/api/scale/labels", {"product_id": self.cheese["id"], "weight_milli": 25000})
        self.assertEqual(too_much.status_code, 409)
        self.assertIn("R$ 999,99", too_much.json["error"])

    def test_manual_weight_needs_manager_pin_unless_allowed(self):
        self.login("cashier")
        request = {"product_id": self.cheese["id"], "weight_milli": 500, "manual": True}
        self.assertEqual(self.post("/api/scale/labels", request).status_code, 403)
        self.assertEqual(self.post("/api/scale/labels", {**request, "manager_pin": "000000"}).status_code, 403)
        authorized = self.post("/api/scale/labels", {**request, "manager_pin": "654321"})
        self.assertEqual(authorized.status_code, 200, authorized.json)
        with self.store.connection() as conn:
            row = conn.execute("SELECT manual,authorizer_id FROM scale_labels").fetchone()
        self.assertEqual((row["manual"], row["authorizer_id"]), (1, self.ids["manager"]))
        self.assertFalse(self.client.get("/api/scale/settings").json["permissions"]["manual_weight"])
        self.login("manager")
        self.assertEqual(self.post("/api/scale/labels", request).status_code, 200)
        self.post("/api/scale/settings", {"manual_weight": "everyone"})
        self.login("cashier")
        self.assertTrue(self.client.get("/api/scale/settings").json["permissions"]["manual_weight"])
        self.assertEqual(self.post("/api/scale/labels", request).status_code, 200)

    def test_products_without_usable_plu_are_refused(self):
        self.login("manager")
        missing = self.post("/api/scale/labels", {"product_id": self.banana["id"], "weight_milli": 900})
        self.assertEqual((missing.status_code, missing.json["code"]), (409, "label_invalid"))
        self.assertIn("painel", missing.json["error"])
        duplicated = self.post("/api/scale/labels", {"product_id": self.tomato["id"], "weight_milli": 900})
        self.assertEqual(duplicated.status_code, 409)
        self.assertIn("repetido", duplicated.json["error"])
        unit = self.post("/api/scale/labels", {"product_id": self.soda["id"], "weight_milli": 900})
        self.assertEqual(unit.status_code, 400)
        for bad in ({"weight_milli": 0}, {"weight_milli": 1.5}, {"weight_milli": 500, "copies": 51},
                    {"weight_milli": 500, "manual": "sim"}, {"weight_milli": 500, "output": "fax"}):
            with self.subTest(bad=bad):
                self.assertEqual(self.post("/api/scale/labels", {"product_id": self.cheese["id"], **bad}).status_code, 400)
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scale_labels").fetchone()[0], 0)

    def test_next_free_plu_only_for_managers(self):
        self.login("cashier")
        self.assertEqual(self.client.get("/api/scale/next-plu").status_code, 403)
        self.login("manager")
        self.assertEqual(self.client.get("/api/scale/next-plu").json, {"plu": "00002"})

    def test_roles_without_scale_are_blocked(self):
        self.assertEqual(self.client.get("/api/scale/products").status_code, 401)
        self.login("viewer")
        self.assertEqual(self.client.get("/api/scale/products").status_code, 403)
        self.assertEqual(self.post("/api/scale/labels", {"product_id": self.cheese["id"], "weight_milli": 500}).status_code, 403)
        self.assertEqual(self.client.get("/api/scale/settings").status_code, 403)
        self.login("inventory")
        self.assertEqual(self.client.get("/api/scale/labels").status_code, 403)  # conferência é do gerente
        self.assertEqual(self.post("/api/scale/products/" + self.cheese["id"] + "/validity",
                                   {"validity_days": 5}).status_code, 403)

    def test_label_printer_queue_uses_station_language_and_size(self):
        self.login("manager")
        out = str(Path(self.temp.name) / "labels")
        self.assertEqual(self.post("/api/print/settings", {"kind": "label", "driver": "file", "target": out,
                                                           "format": "ppla"}).status_code, 200)
        self.post("/api/scale/settings", {"size": "40x30"})
        response = self.post("/api/scale/labels", {"product_id": self.cheese["id"], "weight_milli": 456, "copies": 3})
        self.assertEqual((response.json["output"], response.json["printer"], response.json["size"]),
                         ("printer", "ppla", "40x30"))
        job = self.store.begin_print()
        self.assertEqual(job["id"], response.json["job"]["id"])
        self.assertTrue(job["payload"].startswith(b"\x02L\r"))
        self.assertIn(b"1F22", job["payload"])
        self.assertIn(b"Q0003\rE\r", job["payload"])
        self.store.finish_print(job["id"])
        # Gôndola pela rota antiga, agora ligada à etiquetadora.
        shelf = self.post("/api/print/labels", {"product_id": self.soda["id"], "quantity": 2})
        self.assertEqual(shelf.status_code, 200, shelf.json)
        self.assertEqual(shelf.json["quantity"], 2)
        # PPLA/TSPL ficam fora do terminal.config: a nuvem ainda não conhece esses formatos.
        with self.store.connection() as conn:
            self.store._set_meta(conn, "cloud_capabilities", json.dumps(["terminal-config"]))
            conn.commit()
            candidate = self.store._config_candidate(conn, None)
        self.assertTrue(candidate is None or all(row["format"] in ("zpl", "epl", "escpos58", "escpos80")
                                                 for row in candidate[1]["print_settings"]))

    def test_validity_per_product_and_favorites(self):
        self.login("manager")
        self.assertEqual(self.post("/api/scale/products/" + self.bread["id"] + "/validity",
                                   {"validity_days": 1}).status_code, 200)
        label = self.post("/api/scale/labels", {"product_id": self.bread["id"], "weight_milli": 300}).json["label"]
        self.assertEqual(label["validity_days"], 1)
        self.post("/api/scale/labels", {"product_id": self.bread["id"], "weight_milli": 310})
        favorites = self.client.get("/api/scale/products").json
        self.assertTrue(favorites["favorites"])
        self.assertEqual(favorites["products"][0]["id"], self.bread["id"])
        self.assertNotIn(self.soda["id"], [row["id"] for row in favorites["products"]])
        by_plu = self.client.get("/api/scale/products?q=00001").json["products"]
        self.assertEqual([row["id"] for row in by_plu], [self.bread["id"]])
        by_label = self.client.get("/api/scale/products?q=" + label["barcode"]).json["products"]
        self.assertEqual([row["id"] for row in by_label], [self.bread["id"]])
        history = self.client.get("/api/scale/labels").json["labels"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["operator_name"], "Bruno")
        self.post("/api/scale/products/" + self.bread["id"] + "/validity", {"validity_days": None})
        self.assertEqual(self.post("/api/scale/labels", {"product_id": self.bread["id"],
                                                         "weight_milli": 300}).json["label"]["validity_days"], 3)


class ScalePageTests(unittest.TestCase):
    def test_page_loads_scale_scripts_before_the_cashier(self):
        html = (ROOT / "terminal" / "templates" / "index.html").read_text(encoding="utf-8")
        order = [html.index(name) for name in ("terminal-core.js", "scale-core.js", "scale.js", "terminal.js")]
        self.assertEqual(order, sorted(order))
        self.assertIn('data-tab="scale"', html)
        self.assertIn('id="labelPrinterForm"', html)
        js = (STATIC / "terminal.js").read_text(encoding="utf-8")
        self.assertIn("key.toLowerCase() === 'b'", js)  # Ctrl+B, fora de F1–F12/Esc do caixa


if __name__ == "__main__":
    unittest.main()
