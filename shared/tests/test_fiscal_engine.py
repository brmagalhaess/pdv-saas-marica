import random
import unittest
from datetime import date
from decimal import Decimal

from shared.fiscal import compute_document, product_issues, resolve, summary
from shared.fiscal.engine import SCHEMA, allocate_discount, commercial_gtin, line_gross, pct, reduce_base
from shared.fiscal.ibpt import lookup_from, parse
from shared.fiscal.presets import PRESETS, preset_fields, preset_for, public
from shared.fiscal.readiness import company_issues

SIMPLES = {"crt": 1, "uf": "SP"}
NORMAL = {"crt": 3, "uf": "SP"}


def item(fiscal, price=1000, quantity=1000, **extra):
    return {"product_id": extra.pop("product_id", "p1"), "code": "P1", "description": "Produto", "unit": "UN",
            "barcode": extra.pop("barcode", ""), "quantity_milli": quantity, "unit_price_cents": price,
            "fiscal": fiscal, **extra}


def fields(slug, crt, ncm="22021000", **overrides):
    return resolve(preset_fields(slug, crt), ncm, overrides)


class RoundingTests(unittest.TestCase):
    def test_half_up_to_the_cent(self):
        self.assertEqual(pct(250, "1"), 3)             # 2.5 -> 3 (HALF_EVEN would give 2)
        self.assertEqual(pct(350, "1"), 4)
        self.assertEqual(pct(1, "0.65"), 0)
        self.assertEqual(line_gross(333, 1500), 500)   # 4.995 -> 5.00, same as the terminal
        self.assertEqual(reduce_base(10000, "33.33"), 6667)

    def test_discount_allocation_cents_sum_exactly(self):
        self.assertEqual(allocate_discount(100, [333, 333, 334]), [33, 33, 34])
        self.assertEqual(allocate_discount(101, [999, 6158]), [14, 87])
        self.assertEqual(allocate_discount(1, [1, 1, 1]), [1, 0, 0])
        self.assertEqual(allocate_discount(0, [5, 5]), [0, 0])
        with self.assertRaises(ValueError):
            allocate_discount(11, [5, 5])
        rng = random.Random(7)
        for _ in range(300):
            values = [rng.randint(1, 50000) for _ in range(rng.randint(1, 12))]
            total = rng.randint(0, sum(values))
            shares = allocate_discount(total, values)
            self.assertEqual(sum(shares), total)
            self.assertTrue(all(0 <= s <= v for s, v in zip(shares, values)))


class SimplesTests(unittest.TestCase):
    def test_csosn_102(self):
        doc = compute_document(SIMPLES, [item(fields("simples_tributado", 1), price=1000, quantity=2000)])
        line = doc["items"][0]
        self.assertEqual(doc["schema"], SCHEMA)
        self.assertEqual(line["icms"]["group"], "ICMSSN102")
        self.assertEqual((line["icms"]["csosn"], line["icms"]["cst"]), ("102", None))
        self.assertEqual(line["icms"]["value_cents"], 0)
        self.assertEqual(line["cfop"], "5102")
        self.assertEqual(line["pis"], {"cst": "49", "group": "Outr", "bc_cents": 0, "rate": "0.00", "value_cents": 0})
        self.assertEqual(line["quantity"], "2.0000")
        self.assertEqual(doc["totals"]["total_cents"], 2000)
        self.assertTrue(doc["ready"])

    def test_csosn_500_with_st_retained(self):
        fiscal = fields("simples_st", 1, cest="0300700", st_retained_bc_unit="12.5", st_retained_rate="18",
                        st_retained_substitute_unit="1.2")
        doc = compute_document(SIMPLES, [item(fiscal, price=1599, quantity=3000)])
        line = doc["items"][0]
        self.assertEqual(line["icms"]["group"], "ICMSSN500")
        self.assertEqual(line["cfop"], "5405")
        self.assertEqual(line["icms"]["st_retained"], {"bc_cents": 3750, "rate": "18.00", "substitute_cents": 360,
                                                       "value_cents": 315, "fcp_bc_cents": 0, "fcp_rate": None,
                                                       "fcp_cents": 0})
        self.assertEqual(doc["totals"]["st_retained_cents"], 315)
        self.assertEqual(doc["totals"]["st_cents"], 0)
        self.assertEqual(doc["totals"]["total_cents"], 4797)  # ST already paid does not add to vNF
        self.assertTrue(line["ready"])

    def test_csosn_500_without_cest_is_not_ready(self):
        doc = compute_document(SIMPLES, [item(fields("simples_st", 1))])
        messages = [issue["message"] for issue in doc["items"][0]["issues"]]
        self.assertFalse(doc["ready"])
        self.assertTrue(any(message.startswith("Falta CEST") for message in messages))

    def test_csosn_101_credit_and_201_st(self):
        credit = compute_document({"crt": 1, "uf": "SP", "simples_credit_rate": "2.56"},
                                  [item(resolve({"icms_csosn": "101", "cfop": "5102", "pis_cst": "49", "cofins_cst": "49"}, "22021000"), price=10000)])
        self.assertEqual(credit["items"][0]["icms"]["sn_credit_cents"], 256)
        self.assertEqual(credit["totals"]["sn_credit_cents"], 256)
        st = compute_document(SIMPLES, [item(resolve({"icms_csosn": "202", "cfop": "5405", "cest": "0300700",
                                                      "icms_rate": "18", "st_mva": "40", "st_rate": "18",
                                                      "pis_cst": "49", "cofins_cst": "49"}, "22021000"), price=10000)])
        self.assertEqual(st["items"][0]["icms"]["st"]["bc_cents"], 14000)
        self.assertEqual(st["items"][0]["icms"]["st"]["value_cents"], 720)
        self.assertEqual(st["totals"]["total_cents"], 10720)


class NormalTests(unittest.TestCase):
    def test_cst_00_with_fcp(self):
        fiscal = fields("normal_tributado", 3, icms_rate="18", fcp_rate="2")
        doc = compute_document(NORMAL, [item(fiscal, price=10000)])
        icms = doc["items"][0]["icms"]
        self.assertEqual((icms["group"], icms["bc_cents"], icms["value_cents"], icms["fcp_cents"]), ("ICMS00", 10000, 1800, 200))
        self.assertEqual(doc["items"][0]["pis"]["value_cents"], 65)
        self.assertEqual(doc["items"][0]["cofins"]["value_cents"], 300)
        self.assertEqual(doc["totals"]["total_cents"], 10000)

    def test_cst_20_reduced_base(self):
        fiscal = fields("normal_reducao", 3, icms_rate="18", icms_bc_reduction="33.33", fcp_rate="2")
        doc = compute_document(NORMAL, [item(fiscal, price=10000)])
        icms = doc["items"][0]["icms"]
        self.assertEqual(icms["group"], "ICMS20")
        self.assertEqual(icms["bc_reduction"], "33.33")
        self.assertEqual(icms["bc_cents"], 6667)
        self.assertEqual(icms["value_cents"], 1200)      # 6667 x 18% = 1200.06
        self.assertEqual(icms["fcp_cents"], 133)         # 6667 x 2% = 133.34
        self.assertEqual(doc["totals"]["icms_bc_cents"], 6667)

    def test_cst_20_without_reduction_is_not_ready(self):
        doc = compute_document(NORMAL, [item(fields("normal_reducao", 3, icms_rate="18"))])
        self.assertIn("icms_reduction_missing", [i["code"] for i in doc["items"][0]["issues"]])

    def test_cst_60_st_retained(self):
        fiscal = fields("normal_st", 3, cest="0300700", st_retained_bc_unit="5", st_retained_rate="20",
                        st_retained_substitute_unit="0.6", st_retained_fcp_rate="2")
        doc = compute_document(NORMAL, [item(fiscal, price=799, quantity=6000)])
        retained = doc["items"][0]["icms"]["st_retained"]
        self.assertEqual(doc["items"][0]["icms"]["group"], "ICMS60")
        self.assertEqual((retained["bc_cents"], retained["substitute_cents"], retained["value_cents"]), (3000, 360, 240))
        self.assertEqual(retained["fcp_cents"], 60)
        self.assertEqual(doc["totals"]["fcp_st_retained_cents"], 60)
        self.assertEqual(doc["totals"]["icms_cents"], 0)
        self.assertEqual(doc["totals"]["total_cents"], 4794)

    def test_cst_10_st_and_51_deferral_and_40(self):
        st = compute_document(NORMAL, [item(resolve({"icms_cst": "10", "cfop": "5405", "cest": "0300700",
                                                     "icms_rate": "18", "st_mva": "40", "st_rate": "18",
                                                     "st_fcp_rate": "2", "pis_cst": "07", "cofins_cst": "07"}, "22021000"),
                                            price=10000)])
        self.assertEqual(st["items"][0]["icms"]["st"]["value_cents"], 720)   # 14000 x 18% - 1800
        self.assertEqual(st["items"][0]["icms"]["st"]["fcp_cents"], 280)
        self.assertEqual(st["totals"]["total_cents"], 11000)
        self.assertEqual(st["items"][0]["pis"]["group"], "NT")
        deferred = compute_document(NORMAL, [item(resolve({"icms_cst": "51", "cfop": "5102", "icms_rate": "18",
                                                           "icms_deferral_rate": "33.3333", "pis_cst": "07",
                                                           "cofins_cst": "07"}, "22021000"), price=10000)])
        icms = deferred["items"][0]["icms"]
        self.assertEqual((icms["operation_cents"], icms["deferred_cents"], icms["value_cents"]), (1800, 600, 1200))
        exempt = compute_document({"crt": 3, "uf": "RJ"}, [item(fields("normal_isento", 3))])
        self.assertEqual(exempt["items"][0]["icms"]["group"], "ICMS40")
        self.assertIn("cbenef_missing", [i["code"] for i in exempt["items"][0]["issues"]])
        self.assertTrue(exempt["ready"])  # cBenef is advice: the accountant confirms

    def test_pis_cofins_without_icms_and_ibs_cbs(self):
        fiscal = fields("normal_tributado", 3, icms_rate="18", pis_cofins_exclude_icms=True,
                        ibs_cbs_cst="000", ibs_cbs_class="000001", cbs_rate="0.9", ibs_state_rate="0.1")
        doc = compute_document(NORMAL, [item(fiscal, price=10000)])
        line = doc["items"][0]
        self.assertEqual(line["pis"]["bc_cents"], 8200)
        self.assertEqual(line["pis"]["value_cents"], 53)       # 8200 x 0.65% = 53.3
        self.assertEqual(line["cofins"]["value_cents"], 246)
        self.assertEqual(line["ibs_cbs"]["bc_cents"], 10000 - 1800 - 53 - 246)
        self.assertEqual(line["ibs_cbs"]["cbs_cents"], 71)     # 7901 x 0.9% = 71.109
        self.assertEqual(line["ibs_cbs"]["ibs_cents"], 8)
        self.assertEqual(doc["totals"]["cbs_cents"], 71)
        self.assertEqual(doc["totals"]["total_cents"], 10000)  # IBS/CBS of 2026 do not add to vNF

    def test_ipi_only_in_nfe(self):
        fiscal = fields("normal_tributado", 3, icms_rate="18", ipi_cst="50", ipi_rate="10")
        nfe = compute_document(NORMAL, [item(fiscal, price=10000)], model=55)
        self.assertEqual(nfe["items"][0]["ipi"]["value_cents"], 1000)
        self.assertEqual(nfe["items"][0]["icms"]["bc_cents"], 11000)  # IPI enters the ICMS base for final consumer
        self.assertEqual(nfe["totals"]["total_cents"], 11000)
        nfce = compute_document(NORMAL, [item(fiscal, price=10000)], model=65)
        self.assertIsNone(nfce["items"][0]["ipi"])
        self.assertIn("ipi_nfce", [i["code"] for i in nfce["items"][0]["issues"]])


class DocumentTests(unittest.TestCase):
    def test_document_discount_is_apportioned_and_totals_are_sums(self):
        fiscal = fields("normal_tributado", 3, icms_rate="18")
        doc = compute_document(NORMAL, [item(fiscal, price=333, quantity=3000, product_id="a"),
                                        item(fiscal, price=4990, quantity=1234, product_id="b")], discount_cents=101)
        self.assertEqual([line["discount_cents"] for line in doc["items"]], [14, 87])
        self.assertEqual([line["icms"]["value_cents"] for line in doc["items"]], [177, 1093])
        totals = doc["totals"]
        self.assertEqual((totals["products_cents"], totals["discount_cents"], totals["icms_cents"]), (7157, 101, 1270))
        self.assertEqual(totals["total_cents"], 7056)
        with self.assertRaises(ValueError):
            compute_document(NORMAL, [item(fiscal, discount_cents=1)], discount_cents=1)

    def test_random_documents_keep_sum_invariants(self):
        rng = random.Random(42)
        profiles = [fields("normal_tributado", 3, icms_rate="18", fcp_rate="2"),
                    fields("normal_reducao", 3, icms_rate="12", icms_bc_reduction="41.67"),
                    resolve({"icms_cst": "10", "cfop": "5405", "icms_rate": "18", "st_mva": "35.5", "st_rate": "18",
                             "pis_cst": "01", "pis_rate": "1.65", "cofins_cst": "01", "cofins_rate": "7.6"}, "22021000")]
        for _ in range(200):
            items = [item(rng.choice(profiles), price=rng.randint(1, 99999), quantity=rng.randint(1, 50000),
                          product_id=str(index)) for index in range(rng.randint(1, 8))]
            gross = sum(line_gross(i["unit_price_cents"], i["quantity_milli"]) for i in items)
            doc = compute_document(NORMAL, items, discount_cents=rng.randint(0, gross))
            t = doc["totals"]
            for key, path in (("icms_cents", ("icms", "value_cents")), ("pis_cents", ("pis", "value_cents"))):
                self.assertEqual(t[key], sum(line[path[0]][path[1]] for line in doc["items"]))
            self.assertEqual(t["total_cents"], t["products_cents"] - t["discount_cents"] + t["st_cents"]
                             + t["fcp_st_cents"] + t["ipi_cents"] + t["freight_cents"] + t["other_cents"])
            self.assertEqual(t["total_cents"], sum(line["total_cents"] for line in doc["items"]))

    def test_gtin_and_tributable_unit(self):
        self.assertEqual(commercial_gtin("7891000315507"), "7891000315507")
        self.assertEqual(commercial_gtin("2000000000015"), "SEM GTIN")   # internal code
        self.assertEqual(commercial_gtin("123"), "SEM GTIN")
        fiscal = fields("simples_tributado", 1, tax_unit="CX", tax_unit_factor="0.0833")
        line = compute_document(SIMPLES, [item(fiscal, price=250, quantity=12000, barcode="7891000315507")])["items"][0]
        self.assertEqual((line["gtin"], line["tax_gtin"], line["tax_unit"], line["tax_quantity"]),
                         ("7891000315507", "7891000315507", "CX", "0.9996"))

    def test_invalid_input_raises(self):
        with self.assertRaises(ValueError):
            compute_document(SIMPLES, [])
        with self.assertRaises(ValueError):
            compute_document(SIMPLES, [item({}, quantity=0)])
        with self.assertRaises(ValueError):
            compute_document(SIMPLES, [item({}, discount_cents=2000)])
        with self.assertRaises(ValueError):
            compute_document(SIMPLES, [item({})], model=59)


IBPT_CSV = """codigo;ex;tipo;descricao;nacionalfederal;importadosfederal;estadual;municipal;vigenciainicio;vigenciafim;chave;versao;fonte
22021000;;0;Água com açúcar;13.45;15.45;18.00;0.00;01/10/2026;31/12/2026;ABC;26.2.A;IBPT/empresometro.com.br
22021000;01;0;Exceção;10.00;12.00;18.00;0.00;01/10/2026;31/12/2026;ABC;26.2.A;IBPT
01012100;;0;Cavalos;4.20;6.20;12.00;0.00;01/10/2025;31/12/2025;ABC;25.1.A;IBPT
0101;;1;Serviço;1;1;1;1;01/10/2026;31/12/2026;ABC;26.2.A;IBPT
abc;;0;quebrada;x;1;1;1;;;;;
"""


class IbptTests(unittest.TestCase):
    def test_parse_and_lookup(self):
        rows, errors = parse(IBPT_CSV.encode("utf-8"))
        self.assertEqual([row["ncm"] for row in rows], ["22021000", "22021000", "01012100"])
        self.assertEqual(errors, 1)
        self.assertEqual(rows[0]["federal_national"], Decimal("13.45"))
        self.assertEqual(rows[0]["valid_until"], date(2026, 12, 31))
        lookup = lookup_from(rows, today=date(2026, 11, 18))
        self.assertEqual(lookup("22021000", "01")["federal_national"], Decimal("10.00"))
        self.assertEqual(lookup("22021000", "02")["federal_national"], Decimal("13.45"))
        self.assertIsNone(lookup("01012100"))  # expired
        with self.assertRaises(ValueError):
            parse("a;b\n1;2\n")

    def test_lei_12741_approximate_tax(self):
        lookup = lookup_from(parse(IBPT_CSV)[0], today=date(2026, 11, 18))
        national = fields("simples_tributado", 1)
        imported = fields("simples_tributado", 1, origin=2)
        doc = compute_document(SIMPLES, [item(national, price=1000, product_id="a"),
                                         item(imported, price=1000, product_id="b")], approx=lookup)
        a, b = doc["items"][0]["approx_tax"], doc["items"][1]["approx_tax"]
        self.assertEqual((a["source"], a["federal_cents"], a["state_cents"], a["total_cents"]), ("ibpt", 135, 180, 315))
        self.assertEqual(b["federal_cents"], 155)  # imported origin uses importadosfederal
        self.assertEqual(doc["totals"]["approx_tax_cents"], 315 + 335)
        fallback = compute_document(SIMPLES, [item(fields("simples_tributado", 1, ncm="01012100"), price=1000)],
                                    approx=lookup, approx_fallback="31.45")
        self.assertEqual(fallback["items"][0]["approx_tax"]["source"], "fallback")
        self.assertEqual(fallback["totals"]["approx_tax_cents"], 315)  # 314.5 -> 315
        none = compute_document(SIMPLES, [item(national, price=1000)])
        self.assertEqual(none["items"][0]["approx_tax"]["source"], "none")


class PresetAndReadinessTests(unittest.TestCase):
    def test_presets_are_clean_and_match_their_regime(self):
        for preset in PRESETS:
            for crt in preset["crts"]:
                values = resolve(preset_fields(preset["slug"], crt), "22021000")
                codes = [issue["code"] for issue in product_issues(values, crt, "SP")]
                self.assertNotIn("csosn_regime", codes)
                self.assertNotIn("cst_regime", codes)
                self.assertNotIn("cfop_nfce", codes)
        self.assertEqual(preset_for("isento", 1)["slug"], "simples_isento")
        self.assertEqual(preset_for("isento", 3)["slug"], "normal_isento")
        self.assertEqual({p["slug"] for p in public(1) if p["applies"]},
                         {"simples_tributado", "simples_st", "simples_isento"})
        self.assertTrue(all(p["when"] for p in public()))

    def test_messages_for_the_shopkeeper(self):
        self.assertIn("Falta NCM", summary(product_issues(resolve(preset_fields("simples_tributado", 1)), 1))["missing"][0])
        normal_on_simples = product_issues(fields("normal_tributado", 3, icms_rate="18"), 1, "SP")
        self.assertIn("csosn_regime", [i["code"] for i in normal_on_simples])
        missing_rate = summary(product_issues(fields("normal_tributado", 3), 3, "SP"))
        self.assertIn("Falta a alíquota de ICMS para o CST 00", missing_rate["missing"])
        hint = product_issues(fields("simples_tributado", 1, ncm="22030000"), 1, "SP")
        self.assertIn("cest_suggested", [i["code"] for i in hint])
        wrong_cfop = product_issues(fields("simples_tributado", 1, cfop="5405"), 1, "SP")
        self.assertIn("cfop_st_mismatch", [i["code"] for i in wrong_cfop])
        nfe_cfop = product_issues(fields("simples_tributado", 1, cfop="6102"), 1, "SP")
        self.assertIn("cfop_nfce", [i["code"] for i in nfe_cfop])
        self.assertNotIn("cfop_nfce", [i["code"] for i in product_issues(fields("simples_tributado", 1, cfop="6102"), 1, "SP", model=55)])

    def test_company_issues(self):
        good = {"document": "11.222.333/0001-81", "state_registration": "110.042.490.114", "address_state": "SP",
                "address_zip": "01310100", "address_line": "Av. Paulista", "address_number": "1000",
                "address_neighborhood": "Bela Vista", "address_city": "São Paulo", "city_ibge_code": "3550308",
                "crt": 1, "cnae": "4711302"}
        self.assertEqual(company_issues(good), [])
        bad = dict(good, document="11222333000182", state_registration="110042490115", city_ibge_code="3304557")
        codes = {issue["code"] for issue in company_issues(bad)}
        self.assertEqual(codes, {"cnpj_invalid", "ie_invalid", "ibge_invalid"})


if __name__ == "__main__":
    unittest.main()
