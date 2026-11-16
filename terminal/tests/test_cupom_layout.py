import copy
import re
import unittest
from unittest import mock
from zoneinfo import ZoneInfoNotFoundError

from terminal import printers
from terminal.printers import (BRAND_LINE, fiscal_message, fiscal_state, layout_bytes, receipt_bytes,
                               receipt_layout, voucher_layout)

WIDTHS = ((80, 48), (58, 32))
KEY = "33261112345678000190650010000012341000123456"
DANFE_TITLE = "DOCUMENTO AUXILIAR DA NOTA FISCAL DE CONSUMIDOR ELETRÔNICA"
HOMOLOGATION = "EMITIDA EM AMBIENTE DE HOMOLOGAÇÃO - SEM VALOR FISCAL"
CUT = b"\x1d!\x00\x1bE\x00\x1ba\x00\x1dVB\xa0"


def sale_fixture():
    return {
        "id": "3f9a1c2e-0000-4000-8000-000000000000", "created_at": 1791048803219,
        "terminal_name": "02", "operator_name": "Ana", "seller_name": "Bruno",
        "issuer": {"trade_name": "Mercado Boa Vista", "legal_name": "Boa Vista Comércio de Alimentos Ltda",
                   "document": "12345678000190", "state_registration": "123456789",
                   "address": {"line": "Rua das Flores", "number": "100", "neighborhood": "Centro",
                               "city": "Maricá", "state": "RJ", "zip": "24900000"}},
        "customer_document": "12345678909", "customer_name": "João Silva",
        "items": [
            {"name": "Arroz tipo 1 5kg", "sku": "7891234567890", "ncm": "10063021", "unit": "UN",
             "quantity_milli": 3000, "unit_price_cents": 2490, "gross_cents": 7470, "line_total_cents": 4731,
             "promo_discount_cents": 2490, "promotion_name": "Leve 3 pague 2", "discount_cents": 249},
            {"name": "Queijo minas frescal artesanal da fazenda - embalagem sustentável", "sku": "123",
             "ncm": "04061010", "unit": "KG", "quantity_milli": 350, "unit_price_cents": 4000,
             "gross_cents": 1400, "line_total_cents": 1400},
        ],
        "gross_cents": 8870, "promo_discount_cents": 2490, "discount_cents": 249, "total_cents": 6131,
        "payments": [{"method": "cash", "amount_cents": 5000}, {"method": "credit_account", "amount_cents": 1000},
                     {"method": "store_credit", "amount_cents": 500},
                     {"method": "other", "payment_method_id": "vale", "amount_cents": 100}],
        "payment_names": {"vale": "Vale-alimentação"}, "change_cents": 469,
    }


def fiscal_fixture(**changes):
    fiscal = {"access_key": KEY, "protocol": "333260000123456", "number": 1234, "series": 1,
              "issued_at": 1791048803219, "authorized_at": 1791048805219,
              "url_consulta": "www.fazenda.rj.gov.br/nfce/consulta",
              "qr_url": "https://consultadfe.fazenda.rj.gov.br/consultaNFCe/QRCode?p=" + KEY + "|2|1|1|" + "A1" * 20}
    fiscal.update(changes)
    return fiscal


def fiscal_sale(state, **changes):
    if state == "contingency":
        changes.setdefault("protocol", None)
        changes.setdefault("authorized_at", None)
    return dict(sale_fixture(), fiscal=fiscal_fixture(state=state, **changes))


def lines_of(layout):
    return [line for block in layout["blocks"] for line in block.get("lines", [])]


def joined(layout):
    return " ".join(" ".join(line.split()) for line in lines_of(layout))


def parse_escpos(payload):
    """Walk an ESC/POS stream: return (printed text, commands) or fail on anything unknown."""
    fixed = {b"\x1b@": 0, b"\x1bM": 1, b"\x1b ": 1, b"\x1bt": 1, b"\x1b3": 1, b"\x1ba": 1, b"\x1bE": 1,
             b"\x1d!": 1, b"\x1dP": 2, b"\x1dL": 2, b"\x1dW": 2}
    allowed = {b"\x1bM": {0, 1}, b"\x1ba": {0, 1, 2}, b"\x1bE": {0, 1}, b"\x1d!": {0, 1, 16, 17}, b"\x1bt": {2}}
    index, text, commands = 0, bytearray(), []
    while index < len(payload):
        byte = payload[index]
        if byte not in (0x1b, 0x1d):
            if byte != 0x0a and byte < 0x20:
                raise AssertionError(f"control byte {byte:#x} at {index}")
            text.append(byte)
            index += 1
            continue
        head = payload[index:index + 2]
        if head == b"\x1d(":
            if payload[index + 2:index + 3] != b"k":
                raise AssertionError(f"unknown GS ( command at {index}")
            size = 5 + int.from_bytes(payload[index + 3:index + 5], "little")
        elif head == b"\x1dV":
            if payload[index + 2:index + 3] != b"B":
                raise AssertionError(f"unknown cut at {index}")
            size = 4
        elif head in fixed:
            size = 2 + fixed[head]
            if head in allowed and payload[index + 2] not in allowed[head]:
                raise AssertionError(f"invalid argument for {head!r} at {index}")
        else:
            raise AssertionError(f"unknown command {head!r} at {index}")
        if index + size > len(payload):
            raise AssertionError(f"truncated command {head!r} at {index}")
        commands.append(payload[index:index + size])
        index += size
    return bytes(text), commands


class TimezoneFallbackTests(unittest.TestCase):
    """Windows without tzdata raises ZoneInfoNotFoundError (a KeyError) inside the sale transaction."""

    def setUp(self):
        patcher = mock.patch.object(printers, "ZoneInfo", side_effect=ZoneInfoNotFoundError(
            "No time zone found with key America/Sao_Paulo"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_zone_database_falls_back_to_fixed_brasilia_offset(self):
        self.assertEqual(printers._receipt_date(1791048803219), "03/10/2026 14:33:23")
        self.assertEqual(printers._receipt_date("2026-10-03T17:33:23Z"), "03/10/2026 14:33:23")

    def test_receipt_and_voucher_render_without_zone_database(self):
        for width, _columns in WIDTHS:
            self.assertIn("03/10/2026 14:33:23", joined(receipt_layout(sale_fixture(), width)))
            self.assertIn(b"03/10/2026 14:33:23", receipt_bytes(fiscal_sale("authorized"), width))
            voucher = voucher_layout({"title": "RECEBIMENTO DE FIADO", "created_at": 1791048803219, "rows": []}, width)
            self.assertIn("03/10/2026 14:33:23", joined(voucher))

    def test_date_formatting_never_raises(self):
        for value in (None, "", "ontem", object(), float("nan"), float("inf"), 10 ** 30, -10 ** 30, [], {}, b"x"):
            with self.subTest(value=value):
                self.assertIsInstance(printers._receipt_date(value), str)


class DateFormattingTests(unittest.TestCase):
    def test_any_failure_while_formatting_is_contained(self):
        with mock.patch.object(printers, "_receipt_zone", side_effect=RuntimeError("boom")):
            self.assertEqual(printers._receipt_date(1791048803219), "1791048803219")
            self.assertTrue(receipt_bytes(sale_fixture()).endswith(CUT))


class FiscalStateTests(unittest.TestCase):
    def test_explicit_state_and_previous_shape(self):
        cases = [
            ({}, "none"),
            ({"fiscal": None}, "none"),
            ({"fiscal": {"authorized": True, "qr_url": "https://example.org"}}, "none"),  # no key: never fiscal
            ({"fiscal": {"authorized": True, "access_key": KEY}}, "none"),  # no protocol
            ({"fiscal": {"authorized": True, "access_key": KEY, "protocol": "1"}}, "authorized"),
            ({"fiscal": {"state": "authorized", "access_key": KEY, "protocol": "1"}}, "authorized"),
            ({"fiscal": {"state": "authorized", "access_key": KEY}}, "none"),
            ({"fiscal": {"state": "authorized", "protocol": "1"}}, "none"),
            ({"fiscal": {"state": "contingency", "access_key": KEY}}, "contingency"),
            ({"fiscal": {"contingency": True, "access_key": KEY}}, "contingency"),
            ({"fiscal": {"state": "homologation", "access_key": KEY, "protocol": "1"}}, "homologation"),
            ({"fiscal": {"state": "homologation", "access_key": KEY}}, "none"),
            ({"fiscal": {"state": "none", "access_key": KEY, "protocol": "1", "authorized": True}}, "none"),
            ({"fiscal": {"state": "autorizada", "access_key": KEY}}, "none"),  # unknown value is not trusted
            ({"fiscal": {"authorized": True, "access_key": KEY, "protocol": "1"},
              "issuer": {"fiscal_environment": "homologation"}}, "homologation"),
            ({"fiscal": {"state": "authorized", "access_key": KEY, "protocol": "1", "environment": "homologacao"}},
             "homologation"),
            ({"fiscal": {"authorized": True, "access_key": KEY, "protocol": "1"},
              "issuer": {"fiscal_environment": "production"}}, "authorized"),
            ({"fiscal": {"authorized": True, "access_key": KEY, "protocol": "1"},
              "issuer": {"fiscal_environment": "non_fiscal"}}, "authorized"),
        ]
        for sale, expected in cases:
            with self.subTest(sale=sale):
                self.assertEqual(fiscal_state(sale), expected)

    def test_fiscal_message_area(self):
        self.assertEqual(fiscal_message("none"), [])
        self.assertEqual(fiscal_message("authorized"), [])
        self.assertEqual(fiscal_message("none", homologation=True), [])
        self.assertEqual(fiscal_message("contingency"),
                         [("EMITIDA EM CONTINGÊNCIA", True), ("Pendente de autorização", False)])
        self.assertEqual(fiscal_message("homologation"), [(HOMOLOGATION, True)])
        self.assertEqual([text for text, _bold in fiscal_message("contingency", homologation=True)],
                         ["EMITIDA EM CONTINGÊNCIA", "Pendente de autorização", HOMOLOGATION])


class ReceiptWidthTests(unittest.TestCase):
    def sales(self):
        long_sale = fiscal_sale("authorized", url_consulta="https://www.sefaz.exemplo.gov.br/portal/nfce/consulta-publica",
                                infcpl="Procon RJ: Av. Rio Branco 25, 5º andar. Troca em até 7 dias com este cupom.",
                                taxes_cents=123456789, taxes_federal_cents=1, taxes_state_cents=2,
                                taxes_municipal_cents=3, taxes_source="IBPT")
        long_sale["issuer"]["trade_name"] = "Supermercado e Distribuidora de Alimentos Boa Vista de Maricá"
        long_sale["customer_name"] = "Maria Aparecida dos Santos Oliveira de Albuquerque Cavalcanti"
        long_sale["terminal_name"] = "Caixa rápido da entrada principal"
        long_sale["items"][0].update(name="Produto" + "x" * 90, quantity_milli=1234567000,
                                     unit_price_cents=123456789, gross_cents=152415765279,
                                     promotion_name="Promoção de aniversário da loja com nome comprido")
        long_sale["gross_cents"] = long_sale["total_cents"] = 152415765279
        long_sale["payment_names"] = {"vale": "Vale-alimentação corporativo da empresa parceira"}
        yield "long", long_sale
        yield "none", sale_fixture()
        yield "voided", dict(fiscal_sale("authorized"), status="voided")
        yield "bare", {"items": [], "gross_cents": 0, "total_cents": 0}
        for state in ("authorized", "contingency", "homologation"):
            yield state, fiscal_sale(state)

    def test_no_line_exceeds_the_paper_columns(self):
        for name, sale in self.sales():
            for width, columns in WIDTHS:
                with self.subTest(sale=name, width=width):
                    layout = receipt_layout(sale, width)
                    self.assertEqual((layout["paper_width_mm"], layout["columns"]), (width, columns))
                    for block in layout["blocks"]:
                        limit = columns // 2 if block.get("wide") else columns
                        for line in block.get("lines", []):
                            self.assertLessEqual(len(line), limit, line)
                            line.encode("cp850")  # strict: every glyph exists in the selected code page
                            self.assertFalse(any(ord(ch) < 32 for ch in line))

    def test_fixed_texts_keep_their_accents_in_cp850(self):
        for width, _columns in WIDTHS:
            none, danfe = receipt_layout(sale_fixture(), width), receipt_layout(fiscal_sale("contingency"), width)
            for layout in (none, danfe):
                for line in lines_of(layout):
                    self.assertNotIn("?", line)  # nothing was replaced while encoding
            text = joined(none) + " " + joined(danfe)
            for expected in ("NÃO É DOCUMENTO FISCAL", "CÓDIGO DESCRIÇÃO", "Promoção:", "Promoções", "Crédito na loja",
                             "ELETRÔNICA", "CONTINGÊNCIA", "Pendente de autorização", "NFC-e nº", "Série",
                             "Obrigado pela preferência!"):
                self.assertIn(expected, text)
            payload = receipt_bytes(sale_fixture(), width)
            self.assertIn("NÃO É DOCUMENTO FISCAL".encode("cp850"), payload)
            self.assertIn("Obrigado pela preferência!".encode("cp850"), payload)
            for stale in (b"NAO", b"Promocao", b"Promocoes", b"Cartao", b"PREFERENCIA", b"Credito"):
                self.assertNotIn(stale, payload)


class ReceiptContentTests(unittest.TestCase):
    def test_non_fiscal_receipt(self):
        for width, columns in WIDTHS:
            with self.subTest(width=width):
                layout = receipt_layout(sale_fixture(), width)
                text, lines = joined(layout), lines_of(layout)
                self.assertEqual(layout["fiscal_state"], "none")
                self.assertIn("COMPROVANTE DE VENDA NÃO É DOCUMENTO FISCAL 03/10/2026 14:33:23", text)
                for absent in ("DOCUMENTO AUXILIAR", "Chave de Acesso", "Protocolo", "NFC-e", "EMITIDA", "Tributos"):
                    self.assertNotIn(absent, text)
                self.assertFalse(any(block["kind"] == "qr" for block in layout["blocks"]))
                self.assertIn("CNPJ 12.345.678/0001-90", text)
                self.assertIn("IE 123456789", text)
                self.assertIn("Centro - Maricá/RJ", text)
                self.assertIn("CEP 24900-000", text)
                self.assertIn("CONSUMIDOR CPF 123.456.789-09 - João Silva", text)
                self.assertIn("Venda: 3f9a1c2e · Caixa: 02", text)
                self.assertIn("Operador: Ana", text)
                self.assertIn("Vendedor: Bruno", text)
                # The separator never dangles at a line break.
                self.assertFalse(any(line.startswith("·") or line.endswith("·") for line in lines))
                title = layout["blocks"][0]
                self.assertEqual((title["bold"], title["tall"], title["wide"]), (True, True, width == 80))

    def test_items_take_two_lines_without_ncm_or_verbose_labels(self):
        for width, columns in WIDTHS:
            with self.subTest(width=width):
                lines = lines_of(receipt_layout(sale_fixture(), width))
                text = " ".join(lines)
                for absent in ("NCM", "10063021", "Unit:", "Total item", "ITENS DA COMPRA"):
                    self.assertNotIn(absent, text)
                self.assertIn("# CÓDIGO DESCRIÇÃO", lines)
                header = lines[lines.index("# CÓDIGO DESCRIÇÃO") + 1]
                self.assertEqual((header.strip().split("  ")[0], header[-8:]), ("QTDE UN x VL UNIT", "VL TOTAL"))
                first = next(i for i, line in enumerate(lines) if line.startswith("001 7891234567890 Arroz"))
                detail = next(line for line in lines[first + 1:] if " x " in line)
                self.assertEqual(detail, "    3 UN x 24,90".ljust(columns - 5) + "74,70")
                weighed = next(line for line in lines if "0,350 KG x 40,00" in line)
                self.assertTrue(weighed.endswith(" 14,00"))
                promo = [line for line in lines if "Promoção: Leve 3 pague 2" in line]
                self.assertTrue(promo and promo[0].endswith(" -24,90"))
                self.assertTrue(any(line.strip().startswith("Desconto no item") and line.endswith(" -2,49")
                                    for line in lines))

    def test_line_value_is_gross_even_for_sales_stored_without_it(self):
        sale = sale_fixture()
        sale["items"][0].pop("gross_cents")
        lines = lines_of(receipt_layout(sale, 80))
        self.assertIn("    3 UN x 24,90".ljust(43) + "74,70", lines)

    def test_totals_payments_and_change(self):
        for width, columns in WIDTHS:
            with self.subTest(width=width):
                layout = receipt_layout(sale_fixture(), width)
                lines = lines_of(layout)

                def value(label):
                    matches = [line for line in lines if line.startswith(label + " ")]
                    self.assertEqual(len(matches), 1, label)
                    self.assertEqual(len(matches[0]), columns)
                    return matches[0][len(label):].strip()

                self.assertEqual(value("Qtde. total de itens"), "2")
                self.assertEqual(value("Valor total R$"), "88,70")
                self.assertEqual(value("Promoções"), "-24,90")
                self.assertEqual(value("Desconto R$"), "-2,49")
                self.assertEqual(value("VALOR A PAGAR R$"), "61,31")
                self.assertEqual(value("FORMA DE PAGAMENTO"), "VALOR PAGO")
                self.assertEqual(value("Dinheiro"), "50,00")
                self.assertEqual(value("A prazo"), "10,00")
                self.assertEqual(value("Crédito na loja"), "5,00")
                self.assertEqual(value("Vale-alimentação"), "1,00")
                self.assertEqual(value("Troco R$"), "4,69")
                total = next(b for b in layout["blocks"] if b.get("lines", [""])[0].startswith("VALOR A PAGAR R$"))
                self.assertTrue(total["bold"] and total["tall"])
                order = [next(i for i, line in enumerate(lines) if line.startswith(label)) for label in (
                    "Qtde. total de itens", "Valor total R$", "Promoções", "Desconto R$", "VALOR A PAGAR R$",
                    "FORMA DE PAGAMENTO", "Dinheiro", "Troco R$")]
                self.assertEqual(order, sorted(order))

    def test_zero_discounts_and_change_without_cash_are_not_printed(self):
        sale = sale_fixture()
        sale.update(promo_discount_cents=0, discount_cents=0, change_cents=0,
                    payments=[{"method": "pix", "amount_cents": 100}, {"method": "card_credit", "amount_cents": 100},
                              {"method": "card_debit", "amount_cents": 100}, {"method": "card", "amount_cents": 100}])
        text = joined(receipt_layout(sale, 58))
        for absent in ("Promoções", "Desconto R$", "Troco"):
            self.assertNotIn(absent, text)
        for label in ("Pix 1,00", "Cartão de crédito 1,00", "Cartão de débito 1,00", "Cartão 1,00"):
            self.assertIn(label, text)

    def test_unidentified_consumer_and_name_only(self):
        sale = sale_fixture()
        sale.update(customer_document="", customer_name="")
        self.assertIn("CONSUMIDOR NÃO IDENTIFICADO", joined(receipt_layout(sale, 58)))
        sale["customer_name"] = "Maria"
        self.assertIn("CONSUMIDOR: Maria", joined(receipt_layout(sale, 58)))
        sale["customer_document"] = "12.345.678/0001-90"
        self.assertIn("CONSUMIDOR CNPJ 12.345.678/0001-90 - Maria", joined(receipt_layout(sale, 80)))

    def test_authorized_danfe(self):
        for width, columns in WIDTHS:
            with self.subTest(width=width):
                layout = receipt_layout(fiscal_sale("authorized"), width)
                text, lines = joined(layout), lines_of(layout)
                self.assertEqual(layout["fiscal_state"], "authorized")
                self.assertIn(DANFE_TITLE, text)
                title = next(b for b in layout["blocks"] if b.get("lines", [""])[0].startswith("DOCUMENTO AUXILIAR"))
                self.assertEqual((title["bold"], title["align"], len(title["lines"])), (True, "center", 2))
                for absent in ("NÃO É DOCUMENTO FISCAL", "COMPROVANTE DE VENDA", "EMITIDA", "Via consumidor",
                               "Via estabelecimento"):
                    self.assertNotIn(absent, text)
                self.assertIn("Consulte pela Chave de Acesso em www.fazenda.rj.gov.br/nfce/", text)
                self.assertIn("NFC-e nº 000001234 Série 001 03/10/2026 14:33:23", text)
                self.assertIn("Protocolo de autorização: 333260000123456", text)
                self.assertIn("Data de autorização: 03/10/2026 14:33:25", text)
                self.assertIn("CONSUMIDOR CPF 123.456.789-09 - João Silva", text)
                key_lines = [line for line in lines if re.fullmatch(r"\d{4}( \d{4})*", line)]
                self.assertEqual("".join(key_lines).replace(" ", ""), KEY)
                self.assertEqual([len(line.split()) for line in key_lines], [6, 5] if width == 80 else [4, 4, 3])
                qr = [block for block in layout["blocks"] if block["kind"] == "qr"]
                self.assertEqual(len(qr), 1)
                self.assertEqual(qr[0]["label"], "Consulta via leitor de QR Code")
                self.assertEqual(qr[0]["value"], fiscal_fixture()["qr_url"])
                self.assertTrue(qr[0]["data_uri"].startswith("data:image/svg+xml;base64,"))
                self.assertLessEqual(qr[0]["width_mm"], layout["printable_width_mm"])
                self.assertGreaterEqual(qr[0]["width_mm"], 25)
                order = [text.index(part) for part in ("VALOR A PAGAR R$", "Consulte pela Chave", "CONSUMIDOR CPF",
                                                       "NFC-e nº", "Protocolo de autorização", "Venda: 3f9a1c2e",
                                                       "Obrigado pela preferência!", BRAND_LINE)]
                self.assertEqual(order, sorted(order))

    def test_previous_fiscal_shape_still_prints_the_danfe(self):
        sale = dict(sale_fixture(), fiscal={"authorized": True, "access_key": KEY, "protocol": "123456789012345",
                                           "number": 7, "series": 2, "qr_url": "https://example.org/consultar",
                                           "taxes_cents": 3120})
        layout = receipt_layout(sale, 80)
        text = joined(layout)
        self.assertEqual(layout["fiscal_state"], "authorized")
        self.assertIn(DANFE_TITLE, text)
        self.assertIn("Chave de Acesso", text)
        self.assertNotIn("Consulte pela Chave de Acesso em", text)  # no consult address was given
        self.assertIn("NFC-e nº 000000007 Série 002 03/10/2026 14:33:23", text)  # falls back to the sale time
        self.assertIn("Protocolo de autorização: 123456789012345", text)
        self.assertNotIn("Data de autorização", text)
        self.assertIn("Tributos Totais Incidentes (Lei Federal 12.741/2012): R$ 31,20", text)
        self.assertIn(b"\x1d(k", receipt_bytes(sale, 80))

    def test_contingency_danfe_prints_two_copies(self):
        for width, columns in WIDTHS:
            with self.subTest(width=width):
                sale = fiscal_sale("contingency")
                layout = receipt_layout(sale, width)
                text = joined(layout)
                self.assertEqual(layout["fiscal_state"], "contingency")
                self.assertIn(DANFE_TITLE, text)
                self.assertIn("EMITIDA EM CONTINGÊNCIA Pendente de autorização", text)
                message = next(b for b in layout["blocks"] if b.get("lines") == ["EMITIDA EM CONTINGÊNCIA"])
                self.assertEqual((message["bold"], message["align"]), (True, "center"))
                self.assertIn("Via consumidor", text)
                self.assertIn("NFC-e nº 000001234 Série 001", text)
                for absent in ("Protocolo", "Data de autorização", "NÃO É DOCUMENTO FISCAL", "HOMOLOGAÇÃO"):
                    self.assertNotIn(absent, text)
                self.assertEqual(sum(block["kind"] == "qr" for block in layout["blocks"]), 1)
                self.assertLess(text.index("VALOR A PAGAR R$"), text.index("EMITIDA EM CONTINGÊNCIA"))
                self.assertLess(text.index("EMITIDA EM CONTINGÊNCIA"), text.index("Consulte pela Chave"))
                payload = receipt_bytes(sale, width)
                self.assertEqual(payload.count(b"\x1dVB\xa0"), 2)
                self.assertEqual(payload.count(b"\x1b@"), 2)
                self.assertEqual((payload.count(b"Via consumidor"), payload.count(b"Via estabelecimento")), (1, 1))
                self.assertLess(payload.index(b"Via consumidor"), payload.index(b"Via estabelecimento"))
                self.assertEqual(receipt_bytes(fiscal_sale("authorized"), width).count(b"\x1dVB\xa0"), 1)

    def test_homologation_danfe(self):
        by_environment = dict(sale_fixture(), fiscal=fiscal_fixture(authorized=True))
        by_environment["issuer"]["fiscal_environment"] = "homologation"
        for name, sale in (("explicit", fiscal_sale("homologation")), ("environment", by_environment)):
            for width, columns in WIDTHS:
                with self.subTest(sale=name, width=width):
                    layout = receipt_layout(sale, width)
                    text = joined(layout)
                    self.assertEqual(layout["fiscal_state"], "homologation")
                    self.assertIn(DANFE_TITLE, text)
                    self.assertIn(HOMOLOGATION, text)
                    message = next(b for b in layout["blocks"] if b.get("lines", [""])[0].startswith("EMITIDA EM"))
                    self.assertEqual((message["bold"], message["align"]), (True, "center"))
                    self.assertIn("Protocolo de autorização: 333260000123456", text)
                    self.assertNotIn("CONTINGÊNCIA", text)
                    self.assertEqual(receipt_bytes(sale, width).count(b"\x1dVB\xa0"), 1)

    def test_contingency_in_homologation_shows_both_messages(self):
        text = joined(receipt_layout(fiscal_sale("contingency", environment="homologation"), 58))
        self.assertIn("EMITIDA EM CONTINGÊNCIA", text)
        self.assertIn(HOMOLOGATION, text)

    def test_homologation_environment_alone_never_makes_a_sale_fiscal(self):
        sale = sale_fixture()
        sale["issuer"]["fiscal_environment"] = "homologation"
        layout = receipt_layout(sale, 80)
        self.assertEqual(layout["fiscal_state"], "none")
        self.assertNotIn("HOMOLOGAÇÃO", joined(layout))
        self.assertIn("NÃO É DOCUMENTO FISCAL", joined(layout))

    def test_cancelled_sale_is_not_reprinted_as_a_valid_danfe(self):
        sale = dict(fiscal_sale("authorized", cancel_protocol="333260000999999"), status="voided")
        for width, _columns in WIDTHS:
            layout = receipt_layout(sale, width)
            text = joined(layout)
            self.assertEqual(layout["fiscal_state"], "none")
            self.assertIn("*** VENDA CANCELADA ***", text)
            self.assertIn("NÃO É DOCUMENTO FISCAL", text)
            self.assertIn("NFC-e CANCELADA nº 000001234 Série 001", text)
            self.assertIn("Protocolo de cancelamento: 333260000999999", text)
            self.assertNotIn("DOCUMENTO AUXILIAR", text)
            self.assertNotIn("Protocolo de autorização", text)
            self.assertFalse(any(block["kind"] == "qr" for block in layout["blocks"]))
            self.assertNotIn(b"\x1d(k", receipt_bytes(sale, width))

    def test_missing_or_oversized_qr_never_fails_and_is_announced(self):
        for qr_url in (None, "", "https://example.org/?p=" + "a" * 700):
            for state in ("authorized", "contingency"):
                with self.subTest(qr_url=qr_url, state=state):
                    sale = fiscal_sale(state, qr_url=qr_url)
                    layout = receipt_layout(sale, 58)
                    self.assertFalse(any(block["kind"] == "qr" for block in layout["blocks"]))
                    self.assertIn("QR Code indisponível. Consulte pela chave de acesso.", joined(layout))
                    self.assertNotIn(b"\x1d(k", receipt_bytes(sale, 58))

    def test_approximate_taxes_only_when_informed(self):
        danfe = fiscal_sale("authorized", taxes_cents=3120, taxes_federal_cents=1200, taxes_state_cents=1800,
                            taxes_municipal_cents=120, taxes_source="IBPT")
        for width, _columns in WIDTHS:
            text = joined(receipt_layout(danfe, width))
            self.assertIn("Tributos Totais Incidentes (Lei Federal 12.741/2012): R$ 31,20", text)
            for part in ("Federal R$ 12,00", "Estadual R$ 18,00", "Municipal R$ 1,20", "Fonte: IBPT"):
                self.assertIn(part, text)
            plain = joined(receipt_layout(dict(sale_fixture(), taxes_cents=3120), width))
            self.assertIn("Tributos aprox. (Lei 12.741/2012): R$ 31,20", plain)
            self.assertNotIn("Fonte:", plain)
            self.assertNotIn("Tributos", joined(receipt_layout(fiscal_sale("authorized"), width)))
            self.assertNotIn("12.741", joined(receipt_layout(sale_fixture(), width)))

    def test_taxpayer_message_and_terminal_name(self):
        sale = fiscal_sale("authorized", infcpl="Troca em até 7 dias com este cupom.")
        sale["terminal_name"] = "Caixa 02"
        text = joined(receipt_layout(sale, 80))
        self.assertIn("Troca em até 7 dias com este cupom.", text)
        self.assertIn("Venda: 3f9a1c2e · Caixa 02 · Operador: Ana", text)
        self.assertNotIn("Caixa: Caixa", text)

    def test_rendering_does_not_mutate_the_sale(self):
        for state in ("authorized", "contingency", "homologation"):
            sale = fiscal_sale(state)
            original = copy.deepcopy(sale)
            receipt_bytes(sale, 58)
            self.assertEqual(sale, original)


class BrandLineTests(unittest.TestCase):
    def layouts(self):
        for width, _columns in WIDTHS:
            yield receipt_layout(sale_fixture(), width)
            yield receipt_layout({"items": [], "gross_cents": 0, "total_cents": 0}, width)
            for state in ("authorized", "contingency", "homologation"):
                yield receipt_layout(fiscal_sale(state), width)
            yield voucher_layout({"title": "DEVOLUCAO", "rows": [], "signature": True}, width)
            yield voucher_layout({"title": "RECEBIMENTO DE FIADO", "rows": [["pair", "VALOR RECEBIDO", "R$ 1,00", True]],
                                  "issuer": sale_fixture()["issuer"], "customer_name": "Seu Zé"}, width)

    def test_brand_is_the_last_line_in_small_type(self):
        self.assertEqual(BRAND_LINE, "PDV2 - crowntech.com.br")
        self.assertTrue(BRAND_LINE.isascii())
        for layout in self.layouts():
            last = layout["blocks"][-1]
            self.assertEqual((last["kind"], last["lines"], last["align"], last.get("small")),
                             ("text", [BRAND_LINE], "center", True))
            self.assertEqual(sum(bool(block.get("small")) for block in layout["blocks"]), 1)
            self.assertEqual(sum(BRAND_LINE in line for line in lines_of(layout)), 1)
            payload = layout_bytes(layout)
            # ESC M 1 (font B) only around the brand line, then back to font A before the cut.
            self.assertTrue(payload.endswith(
                b"\x1bM\x01\x1ba\x01\x1bE\x00\x1d!\x00" + BRAND_LINE.encode("ascii") + b"\n\x1bM\x00" + CUT))
            self.assertEqual(payload.count(b"\x1bM\x01"), 1)


class EscPosTests(unittest.TestCase):
    def test_stream_is_well_formed_and_prints_exactly_the_layout(self):
        sales = [sale_fixture(), fiscal_sale("authorized"), fiscal_sale("homologation"),
                 dict(fiscal_sale("authorized"), status="voided")]
        for sale in sales:
            for width, columns in WIDTHS:
                with self.subTest(state=(sale.get("fiscal") or {}).get("state"), width=width):
                    layout = receipt_layout(sale, width)
                    payload = receipt_bytes(sale, width)
                    text, commands = parse_escpos(payload)
                    self.assertEqual(commands[0], b"\x1b@")
                    self.assertEqual(commands[-1], b"\x1dVB\xa0")
                    self.assertIn(b"\x1dW" + (columns * 12).to_bytes(2, "little"), commands)
                    self.assertIn(b"\x1bt\x02", commands)  # CP850
                    expected = []
                    for block in layout["blocks"]:
                        if block["kind"] == "rule":
                            expected.append("-" * columns)
                        elif block["kind"] == "qr":
                            expected += ["", block["label"]]
                        else:
                            expected += block["lines"]
                    self.assertEqual(text.decode("cp850").split("\n"), expected + [""])
                    qr_commands = [command for command in commands if command.startswith(b"\x1d(k")]
                    self.assertEqual(len(qr_commands), 5 * sum(block["kind"] == "qr" for block in layout["blocks"]))
                    for block in layout["blocks"]:
                        if block["kind"] == "qr":
                            self.assertIn(b"\x1d(k\x03\x001C" + bytes([block["module_size"]]), commands)
                            self.assertTrue(any(command.endswith(block["value"].encode()) for command in qr_commands))

    def test_contingency_copies_and_vouchers_are_well_formed(self):
        for width, _columns in WIDTHS:
            _text, commands = parse_escpos(receipt_bytes(fiscal_sale("contingency"), width))
            self.assertEqual((commands.count(b"\x1b@"), commands.count(b"\x1dVB\xa0")), (2, 2))
            voucher = voucher_layout({"title": "DEVOLUCAO", "rows": [["rule"], ["text", "Motivo: Avaria"]],
                                      "signature": True}, width)
            text, commands = parse_escpos(layout_bytes(voucher))
            self.assertEqual(commands[-1], b"\x1dVB\xa0")
            self.assertIn("DEVOLUÇÃO", text.decode("cp850"))

    def test_customer_data_cannot_inject_printer_commands(self):
        sale = fiscal_sale("authorized", infcpl="Obs\x1b@\x1dV\x00 fim", url_consulta="sefaz\x1bp\x00.gov.br")
        sale["customer_name"] = "Maria\x1dVB\x00"
        sale["items"][0]["name"] = "Arroz\x1b@\n\x1dV\x00"
        sale["payment_names"] = {"vale": "Vale\x1b!\x30"}
        _text, commands = parse_escpos(receipt_bytes(sale, 58))
        self.assertEqual((commands.count(b"\x1b@"), commands.count(b"\x1dVB\xa0")), (1, 1))
        self.assertFalse(any(command.startswith(b"\x1bp") for command in commands))


class VoucherLayoutTests(unittest.TestCase):
    def doc(self):
        return {"title": "DEVOLUCAO", "issuer": sale_fixture()["issuer"], "created_at": 1791048803219,
                "reference": "Devolucao: 0123456789abcdef", "customer_name": "João Silva", "signature": True,
                "rows": [["pair", "Venda original", "3f9a1c2e"], ["rule"], ["text", "Arroz tipo 1 5kg"],
                         ["pair", "  1 x devolvido", "R$ 24,90"], ["rule"], ["pair", "TOTAL DEVOLVIDO", "R$ 24,90", True],
                         ["pair", "Devolvido em", "Credito na loja"], ["text", "Motivo: Credito indevido"],
                         ["pair", "Credito na loja disponivel", "R$ 24,90"]]}

    def test_voucher_shares_the_receipt_skeleton(self):
        for width, columns in WIDTHS:
            with self.subTest(width=width):
                layout = voucher_layout(self.doc(), width)
                lines, text = lines_of(layout), joined(layout)
                self.assertEqual((layout["paper_width_mm"], layout["columns"]), (width, columns))
                for block in layout["blocks"]:
                    limit = columns // 2 if block.get("wide") else columns
                    for line in block.get("lines", []):
                        self.assertLessEqual(len(line), limit)
                        line.encode("cp850")
                self.assertIn("Mercado Boa Vista", text)
                self.assertIn("CNPJ 12.345.678/0001-90", text)
                self.assertNotIn("Rua das Flores", text)  # compact header
                title = next(b for b in layout["blocks"] if b.get("lines") == ["DEVOLUÇÃO"])
                self.assertEqual((title["bold"], title["tall"], title["align"]), (True, True, "center"))
                self.assertIn("NÃO É DOCUMENTO FISCAL", lines)
                self.assertIn("03/10/2026 14:33:23", lines)
                self.assertIn("Devolução: 0123456789abcdef", lines)
                self.assertIn("Devolvido em".ljust(columns - 15) + "Crédito na loja", lines)
                self.assertIn("Crédito na loja disponível", text)
                self.assertIn("Motivo: Credito indevido", lines)  # typed text is left as typed
                self.assertIn("Cliente: João Silva", lines)
                self.assertIn("Assinatura do cliente", lines)
                self.assertEqual(lines[-1], BRAND_LINE)
                self.assertNotIn("DEVOLUCAO", text)

    def test_accented_titles_and_fiado_voucher_pass_through(self):
        layout = voucher_layout({"title": "DEVOLUÇÃO", "reference": "Devolução: 1", "rows": []}, 58)
        self.assertIn("DEVOLUÇÃO", lines_of(layout))
        layout = voucher_layout({"title": "RECEBIMENTO DE FIADO", "reference": "Recibo: 1", "customer_name": "Seu Zé",
                                 "rows": [["pair", "Fiado anterior", "R$ 15,00"],
                                          ["pair", "VALOR RECEBIDO", "R$ 10,00", True]]}, 58)
        lines = lines_of(layout)
        self.assertEqual(lines[0], "RECEBIMENTO DE FIADO")  # no issuer: no empty header
        self.assertNotIn("Assinatura do cliente", lines)
        bold = next(b for b in layout["blocks"] if b.get("lines", [""])[0].startswith("VALOR RECEBIDO"))
        self.assertTrue(bold["bold"])
        self.assertEqual(lines[-1], BRAND_LINE)


if __name__ == "__main__":
    unittest.main()
