import copy
import unittest

from terminal.printers import receipt_bytes, receipt_layout


class ReceiptLayoutTests(unittest.TestCase):
    def setUp(self):
        self.sale = {
            "id": "venda-teste", "created_at": 1791048803219,
            "issuer": {"trade_name": "Mercado Maricá", "document": "12345678000199",
                       "address": {"line": "Avenida dos Comerciantes da Região Metropolitana",
                                   "number": "12345", "city": "Maricá", "state": "RJ"}},
            "customer_document": "12345678909", "customer_name": "Maria",
            "items": [{"name": "Queijo minas frescal artesanal da fazenda - embalagem sustentável",
                       "sku": "123", "unit": "KG", "quantity_milli": 350,
                       "unit_price_cents": 4000, "line_total_cents": 1260, "discount_cents": 140}],
            "gross_cents": 1400, "discount_cents": 140, "total_cents": 1260,
            "payments": [{"method": "cash", "amount_cents": 2000}], "change_cents": 740,
        }

    def test_complete_data_fits_both_paper_widths(self):
        for width, columns in ((80, 48), (58, 32)):
            with self.subTest(width=width):
                layout = receipt_layout(self.sale, width)
                lines = [line for b in layout["blocks"] for line in b.get("lines", [])]
                text = " ".join(lines)
                self.assertTrue(all(len(line) <= columns for line in lines))
                self.assertIn("sustentável", text)  # neither product nor address truncated
                self.assertIn("Metropolitana", text)
                self.assertIn("12.345.678/0001-99", text)
                self.assertIn("123.456.789-09", text)
                self.assertIn("0,350 KG", text)
                self.assertIn("03/10/2026 14:33:23", text)  # Sao Paulo; independent of host TZ
                self.assertTrue(any(line.startswith("Troco R$") and line.endswith(" 7,40") for line in lines))

    def test_thermal_output_uses_the_same_lines_and_full_print_area(self):
        for width, dots in ((80, 576), (58, 384)):
            payload = receipt_bytes(self.sale, width)
            self.assertIn(b"\x1dW" + dots.to_bytes(2, "little"), payload)
            self.assertIn(b"\x1bM\x00", payload)  # font A
            self.assertIn(b"\x1bt\x02", payload)  # CP850, including Portuguese accents
            # Double width only on 80 mm; 58 mm would be left with 16 columns.
            self.assertIn(b"\x1ba\x01\x1bE\x01\x1d!" + (b"\x11" if width == 80 else b"\x01") + b"Mercado", payload)
            self.assertTrue(payload.endswith(b"\x1dVB\xa0"))  # 20 mm before cut
            for block in receipt_layout(self.sale, width)["blocks"]:
                for line in block.get("lines", []):
                    self.assertIn((line + "\n").encode("cp850"), payload)

    def test_missing_identity_never_prints_none_or_invents_fiscal_qr(self):
        self.sale.pop("issuer")
        self.sale["fiscal"] = {"qr_url": "https://example.org/consultar", "authorized": True}
        payload = receipt_bytes(self.sale)
        self.assertNotIn(b"None", payload)
        self.assertNotIn(b"CNPJ/CPF:", payload)
        self.assertNotIn(b"\x1d(k", payload)
        self.assertIn("NÃO É DOCUMENTO FISCAL".encode("cp850"), payload)

    def test_authorized_key_is_complete_and_qr_prints(self):
        self.sale["fiscal"] = {"authorized": True, "access_key": "12345678901" * 4,
                              "protocol": "123456789012345", "number": 1, "series": 1,
                              "qr_url": "https://example.org/consultar"}
        layout = receipt_layout(self.sale, 58)
        self.assertEqual(sum(b["kind"] == "qr" for b in layout["blocks"]), 1)
        text = "".join(line for b in layout["blocks"] for line in b.get("lines", [])).replace(" ", "")
        self.assertIn(self.sale["fiscal"]["access_key"], text)
        self.assertIn(b"\x1d(k", receipt_bytes(self.sale, 58))

    def test_long_qr_fits_and_big_title_wraps_within_printer_width(self):
        self.sale["issuer"]["trade_name"] = "Mercado de Maricá com um nome muito comprido"
        self.sale["fiscal"] = {"authorized": True, "access_key": "1" * 44,
                              "protocol": "123456789012345", "qr_url": "https://example.org/?p=" + "abcdef" * 100}
        for width, columns in ((58, 32), (80, 48)):
            layout = receipt_layout(self.sale, width)
            title = layout["blocks"][0]
            scale = 2 if title["wide"] else 1
            self.assertTrue(all(len(line) * scale <= columns for line in title["lines"]))
            qr = next(b for b in layout["blocks"] if b["kind"] == "qr")
            self.assertLessEqual(qr["width_mm"], layout["printable_width_mm"] + 0.1)
            self.assertGreaterEqual(qr["module_size"], 1)

    def test_rendering_does_not_mutate_sale_and_neutralizes_printer_commands(self):
        self.sale["issuer"]["trade_name"] = "Nome\x1b@\x1dV\x00"
        original = copy.deepcopy(self.sale)
        layout = receipt_layout(self.sale)
        for block in layout["blocks"]:
            for line in block.get("lines", []):
                self.assertFalse(any(ord(ch) < 32 for ch in line))
        self.assertEqual(self.sale, original)


if __name__ == "__main__":
    unittest.main()
