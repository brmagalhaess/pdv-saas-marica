"""Pacote do contador (parte pura): leitura de XMLs, resumo mensal e SPED Fiscal EFD ICMS/IPI."""
import unittest
from datetime import date

from shared.fiscal import efd, month_report, nfce_xml
from shared.tests import nfce_samples as S

COMPANY = {"cnpj": S.CNPJ, "name": "Mercado Fiscal Ltda", "uf": "SP", "ie": S.IE, "city_ibge": "3550308",
           "cep": "01310100", "address": "Av. Paulista", "number": "1000", "neighborhood": "Bela Vista"}
ACCOUNTANT = {"name": "Ana Contadora", "cpf": "52998224725", "crc": "SP123456O8", "email": "ana@contab.test",
              "city_ibge": "3550308"}
E116 = {"e116": {"cod_rec": "046-2", "due_day": 20}}


def doc(xml, status="authorized"):
    nfe = nfce_xml.parse_nfe(xml)
    return {"status": status, "nfe": nfe, "series": nfe["series"], "number": nfe["number"], "key": nfe["key"],
            "local_date": nfe["issued_at"].date(), "warnings": []}


def inut(start, end, when="2026-10-21T09:00:00-03:00", year="26"):
    row = nfce_xml.parse_inutilization(S.inutilization(start, end, when=when, year=year))
    row["local_date"] = row["received_at"].date()
    return row


def regs(lines):
    return [line.split("|")[1] for line in lines]


class XMLReadingTests(unittest.TestCase):
    def test_authorized_nfce(self):
        nfe = nfce_xml.parse_nfe(S.nfce(42, issued="2026-10-31T23:30:00-03:00", change="1.10"))
        self.assertEqual((nfe["model"], nfe["series"], nfe["number"]), ("65", 1, 42))
        self.assertTrue(efd.valid_key(nfe["key"]))
        self.assertEqual(nfe["protocol"]["status"], "100")
        self.assertEqual(nfe["issued_at"].date(), date(2026, 10, 31))  # data do texto, não a de UTC
        self.assertEqual(nfe["totals"]["vNF"], 1798 + 2790 + 802)
        self.assertEqual(nfe["totals"]["vICMS"], 502)
        self.assertEqual([i["cfop"] for i in nfe["items"]], ["5405", "5102", "5102"])
        self.assertEqual(nfe["items"][0]["gtin"], "7891000315507")
        self.assertEqual(nfe["items"][1]["gtin"], "")
        self.assertEqual(nfe["payments"], [{"type": "01", "amount_cents": 5500, "description": ""}])
        self.assertEqual(nfe["change_cents"], 110)
        self.assertEqual(month_report.check_totals(nfe), [])

    def test_contingency_without_protocol(self):
        nfe = nfce_xml.parse_nfe(S.nfce(7, emission_type=9, protocol=False))
        self.assertEqual(nfe["kind"], "NFe")
        self.assertIsNone(nfe["protocol"])
        self.assertEqual(nfe["emission_type"], "9")
        self.assertEqual(nfe["key"][34], "9")

    def test_cancel_event_and_inutilization(self):
        key = S.key_of(S.nfce(3))
        event = nfce_xml.parse_event(S.cancel_event(key))
        self.assertTrue(nfce_xml.is_cancellation(event))
        self.assertEqual(event["key"], key)
        self.assertFalse(nfce_xml.is_cancellation(nfce_xml.parse_event(S.cancel_event(key, status="573"))))
        row = nfce_xml.parse_inutilization(S.inutilization(5, 8))
        self.assertEqual((row["series"], row["start"], row["end"], row["status"]), (1, 5, 8, "102"))

    def test_refuses_entities_and_wrong_documents(self):
        with self.assertRaises(nfce_xml.XMLError):
            nfce_xml.parse_nfe(b'<!DOCTYPE x [<!ENTITY a "b">]><nfeProc/>')
        with self.assertRaises(nfce_xml.XMLError):
            nfce_xml.parse_nfe(S.inutilization(1, 1))
        with self.assertRaises(nfce_xml.XMLError):
            nfce_xml.parse_event(S.nfce(1))

    def test_total_mismatch_is_reported(self):
        nfe = nfce_xml.parse_nfe(S.nfce(1).replace(b"<vNF>53.90</vNF>", b"<vNF>53.99</vNF>"))
        problems = month_report.check_totals(nfe)
        self.assertTrue(any(p.startswith("vNF") for p in problems))


class SummaryTests(unittest.TestCase):
    def test_gaps_respect_inutilization_and_previous_month(self):
        docs = [{"status": "authorized", "series": 1, "number": n} for n in (10, 11, 15)]
        docs.append({"status": "rejected", "series": 1, "number": 12})
        self.assertEqual(month_report.find_gaps(docs, []), [(1, 12, 14)])
        self.assertEqual(month_report.find_gaps(docs, [{"series": 1, "start": 13, "end": 14}]), [(1, 12, 12)])
        self.assertEqual(month_report.find_gaps(docs, [{"series": 1, "start": 12, "end": 14}], {1: 7}), [(1, 8, 9)])

    def test_reduction_and_analytic_sum(self):
        items = [{"code": "A", "description": "Carne", "ncm": "02013000", "cfop": "5102", "qty": "2",
                  "price": "50", "cst": "20", "rate": "18", "reduction": "61.11"},
                 {"code": "B", "description": "Leite", "ncm": "04012010", "cfop": "5102", "qty": "1",
                  "price": "5", "cst": "00", "rate": "7", "discount": "0.50"}]
        nfe = nfce_xml.parse_nfe(S.nfce(1, items=items))
        rows = month_report.analytic_rows(nfe)
        self.assertEqual(sum(r["operation_cents"] for r in rows), nfe["totals"]["vNF"])
        carne = [r for r in rows if r["cst"] == "020"][0]
        self.assertEqual(carne["bc_cents"], 3889)
        self.assertEqual(carne["reduction_cents"], 10000 - 3889)

    def test_summary_csv_and_html(self):
        docs = [doc(S.nfce(1, issued="2026-10-01T09:00:00-03:00")), doc(S.nfce(2, issued="2026-10-01T18:00:00-03:00"))]
        summary = month_report.summarize(docs, [inut(4, 4)], 2026, 10, {"name": "Mercado <Teste>"})
        self.assertEqual(summary["by_day"], [{"date": "2026-10-01", "count": 2, "total_cents": 10780,
                                              "icms_cents": 1004}])
        self.assertEqual(summary["warnings"]["gaps"], ["Série 1: nº 3 sem nota nem inutilização"])
        csv_text = month_report.render_csv(summary)
        self.assertTrue(csv_text.startswith("﻿"))
        self.assertIn("Autorizadas;2;107,80", csv_text)
        html = month_report.render_html(summary)
        self.assertIn("Mercado &lt;Teste&gt;", html)
        self.assertEqual(month_report.money(123456789), "1.234.567,89")

    def test_homologation_documents_and_inutilizations_are_ignored(self):
        production = doc(S.nfce(1, issued="2026-10-01T09:00:00-03:00"))
        homologation = doc(S.nfce(2, issued="2026-10-01T18:00:00-03:00", tp_amb=2))
        test_inut = nfce_xml.parse_inutilization(S.inutilization(3, 3, tp_amb=2))
        test_inut["local_date"] = test_inut["received_at"].date()
        summary = month_report.summarize([production, homologation], [test_inut], 2026, 10)
        self.assertEqual(summary["by_day"][0]["count"], 1)
        result = efd.build(COMPANY, ACCOUNTANT, 2026, 10, [production, homologation], [test_inut], options=E116)
        self.assertEqual(result["documents"], {"authorized": 1, "cancelled": 0, "inutilized": 0})
        self.assertNotIn(homologation["key"], result["text"])


class EFDTests(unittest.TestCase):
    def build(self, docs, inutilizations=(), year=2026, month=10, **options):
        return efd.build(COMPANY, ACCOUNTANT, year, month, docs, inutilizations, options={**E116, **options})

    def month(self):
        docs = [doc(S.nfce(n, issued=f"2026-10-{n + 1:02d}T10:00:00-03:00")) for n in (1, 2, 4)]
        docs.append(doc(S.nfce(3, issued="2026-10-04T10:00:00-03:00"), status="cancelled"))
        return docs

    def test_structure_counts_and_totals(self):
        result = self.build(self.month(), [inut(5, 6)])
        lines = result["lines"]
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["version"], "020")
        names = regs(lines)
        self.assertEqual(names[0], "0000")
        self.assertEqual(names[-1], "9999")
        self.assertEqual([n[0] for n in names if n.endswith("001")], ["0", "B", "C", "D", "E", "G", "H", "K", "1", "9"])
        self.assertEqual(names.count("C100"), 4)
        self.assertNotIn("C170", names)
        self.assertNotIn("0150", names)
        # 9900: um por tipo de registro, contagem igual às linhas; 9999 = total de linhas.
        informed = {line.split("|")[2]: int(line.split("|")[3]) for line in lines if line.startswith("|9900|")}
        self.assertEqual(informed, {n: names.count(n) for n in set(names)})
        self.assertEqual(lines[-1], f"|9999|{len(lines)}|")
        self.assertEqual(int(lines[names.index("C990")].split("|")[2]), sum(1 for n in names if n[0] == "C"))
        # C190 soma o C100; E110 = soma do ICMS dos C190 de saída.
        c100 = [line.split("|") for line in lines if line.startswith("|C100|") and "|00|" in line]
        c190_icms = sum(int(line.split("|")[7].replace(",", "")) for line in lines if line.startswith("|C190|"))
        self.assertEqual(sum(int(f[22].replace(",", "")) for f in c100), c190_icms)
        self.assertEqual(result["e110"]["VL_TOT_DEBITOS"], c190_icms)
        self.assertEqual(result["e110"]["VL_ICMS_RECOLHER"], 3 * 502)
        e116 = [line for line in lines if line.startswith("|E116|")][0]
        self.assertEqual(e116, "|E116|000|15,06|20112026|046-2|||||102026|")
        # NFC-e no C100: sem COD_PART e sem ST/IPI/PIS/COFINS; cancelada só com chave.
        first = c100[0]
        self.assertEqual(first[4], "")
        self.assertEqual(first[23:30], ["", "", "", "", "", "", ""])
        cancelled = [line.split("|") for line in lines if line.startswith("|C100|1|0||65|02|")][0]
        self.assertEqual(len(cancelled), 31)
        self.assertTrue(efd.valid_key(cancelled[9]))
        self.assertTrue(all(field == "" for field in cancelled[10:30]))
        # Inutilização não é escriturada a partir de 2023 (COD_SIT 05 saiu da tabela).
        self.assertNotIn("|05|", "".join(line for line in lines if line.startswith("|C100|")))
        self.assertTrue(result["data"].endswith(b"|\r\n"))

    def test_c100_c190_values_for_one_note(self):
        lines = self.build([doc(S.nfce(1, issued="2026-10-02T10:00:00-03:00"))])["lines"]
        c100 = [line for line in lines if line.startswith("|C100|")][0].split("|")
        self.assertEqual(c100[10:17], ["02102026", "02102026", "53,90", "0", "0,00", "0,00", "53,90"])
        c190 = [line for line in lines if line.startswith("|C190|")]
        self.assertEqual(c190, ["|C190|060|5405|0,00|17,98|0,00|0,00|0,00|0,00|0,00|0,00||",
                                "|C190|000|5102|18,00|27,90|27,90|5,02|0,00|0,00|0,00|0,00||",
                                "|C190|040|5102|0,00|8,02|0,00|0,00|0,00|0,00|0,00|0,00||"])
        self.assertIn("|0190|KG|Quilograma|", lines)
        self.assertIn("|0200|REFRI|Refrigerante 2L|7891000315507||UN|00|22021000||22|||0300700|", lines)

    def test_previous_credit_carries_over_without_e116(self):
        result = self.build(self.month(), previous_credit_cents=10000)
        self.assertEqual(result["e110"]["VL_SLD_CREDOR_TRANSPORTAR"], 10000 - 1506)
        self.assertEqual(result["e110"]["VL_ICMS_RECOLHER"], 0)
        self.assertNotIn("E116", regs(result["lines"]))
        self.assertEqual(result["problems"], [])

    def test_missing_e116_config_and_accountant_warn(self):
        result = efd.build(COMPANY, {}, 2026, 10, self.month())
        joined = " ".join(result["warnings"])
        self.assertIn("COD_REC", joined)
        self.assertIn("vencimento", joined)
        self.assertIn("contador incompletos", joined)

    def test_old_period_lists_inutilized_numbers(self):
        docs = [doc(S.nfce(1, issued="2022-05-10T10:00:00-03:00"))]
        result = self.build(docs, [inut(2, 4, when="2022-05-11T10:00:00-03:00", year="22")], year=2022, month=5)
        self.assertEqual(result["version"], "016")
        inutilized = [line for line in result["lines"] if "|65|05|" in line]
        self.assertEqual(len(inutilized), 3)
        self.assertTrue(all(line.split("|")[9] == "" for line in inutilized))
        self.assertEqual(result["problems"], [])

    def test_unknown_future_layout_warns(self):
        docs = [doc(S.nfce(1, issued="2027-03-10T10:00:00-03:00"))]
        result = self.build(docs, year=2027, month=3)
        self.assertTrue(any("não confirmado" in w for w in result["warnings"]))

    def test_february_inventory_warning_and_text_escaping(self):
        company = {**COMPANY, "name": "Padaria São João | Filial"}
        docs = [doc(S.nfce(1, issued="2026-02-10T10:00:00-03:00"))]
        result = efd.build(company, ACCOUNTANT, 2026, 2, docs, options=E116)
        self.assertTrue(any("inventário" in w for w in result["warnings"]))
        self.assertIn("|Padaria São João Filial|", result["lines"][0])
        self.assertIn("São".encode("latin-1"), result["data"])

    def test_validator_catches_tampering(self):
        lines = list(self.build(self.month())["lines"])
        broken = [line.replace("|C190|000|5102|18,00|27,90|", "|C190|000|5102|18,00|27,99|", 1) for line in lines]
        self.assertTrue(any("VL_DOC" in p for p in efd.validate(broken, cnpj=S.CNPJ)))
        missing = [line for line in lines if not line.startswith("|0190|KG")]
        problems = efd.validate(missing, cnpj=S.CNPJ)
        self.assertTrue(any("bloco 0" in p for p in problems))
        self.assertTrue(any("9999" in p for p in problems))
        bad_date = [line.replace("|02102026|", "|2026-10-02|", 1) for line in lines]
        self.assertTrue(any("data inválida" in p for p in efd.validate(bad_date)))
        other_cnpj = efd.validate(lines, cnpj="45723174000110")
        self.assertTrue(any("chave não corresponde" in p for p in other_cnpj))

    def test_document_outside_period_is_refused(self):
        with self.assertRaises(efd.EFDError):
            self.build([doc(S.nfce(1, issued="2026-11-01T00:10:00-03:00"))])


if __name__ == "__main__":
    unittest.main()
