import unittest

from shared.fiscal.schema import FiscalFieldError, clean_fields, legacy_fields, resolve
from shared.fiscal.validators import (IE_RULES, cnpj_value, valid_cbenef, valid_cep, valid_cest, valid_cfop,
                                      valid_cnpj, valid_cpf, valid_ibge_city, valid_ie, valid_ncm)

# Examples published in the SINTEGRA "conferência de inscrições estaduais" pages (one or more per UF).
VALID_IE = {
    "AC": ["01.004.823/001-12"], "AL": ["240000048"], "AP": ["030123459"], "AM": ["99.999.999-0"],
    "BA": ["123456-63", "1000003-06", "612345-57"], "CE": ["06000001-5"], "DF": ["07300001001-09"],
    "ES": ["999999990"], "GO": ["10.987.654-7"], "MA": ["120000385"], "MT": ["0013000001-9"],
    "MG": ["062.307.904/0081"], "PA": ["15-999999-5"], "PB": ["06000001-5"], "PR": ["123.45678-50"],
    "PE": ["0321418-40"], "PI": ["012345679"], "RJ": ["99.999.99-3"], "RN": ["20.040.040-1", "20.0.040.040-0"],
    "RS": ["224/3658792"], "RO": ["0000000062521-3"], "RR": ["24006628-1"], "SC": ["251.040.852"],
    "SP": ["110.042.490.114", "P-01100424.3/002"], "SE": ["27123456-3"], "TO": ["29.01.022783-6", "29.022783-6"],
}


def _flip_last_digit(value):
    digits = [c for c in value]
    for index in range(len(digits) - 1, -1, -1):
        if digits[index].isdigit():
            digits[index] = str((int(digits[index]) + 1) % 10)
            break
    return "".join(digits)


class DocumentTests(unittest.TestCase):
    def test_cnpj_numeric_and_alphanumeric(self):
        self.assertTrue(valid_cnpj("11.222.333/0001-81"))
        self.assertFalse(valid_cnpj("11.222.333/0001-82"))
        self.assertFalse(valid_cnpj("00.000.000/0000-00"))
        self.assertFalse(valid_cnpj("1122233300018"))
        # Example of the Receita Federal for the alphanumeric CNPJ (from July 2026).
        self.assertTrue(valid_cnpj("12.ABC.345/01DE-35"))
        self.assertFalse(valid_cnpj("12.ABC.345/01DE-36"))
        self.assertEqual(cnpj_value("12.abc.345/01de-35"), "12ABC34501DE35")

    def test_cpf(self):
        self.assertTrue(valid_cpf("529.982.247-25"))
        self.assertFalse(valid_cpf("529.982.247-24"))
        self.assertFalse(valid_cpf("111.111.111-11"))

    def test_ie_all_ufs_have_a_rule_and_known_examples_pass(self):
        self.assertEqual(len(IE_RULES), 27)
        for uf, examples in VALID_IE.items():
            for value in examples:
                with self.subTest(uf=uf, ie=value):
                    self.assertTrue(valid_ie(value, uf))
                    if not value.startswith("P"):  # rural SP: only the 9th digit is checked
                        self.assertFalse(valid_ie(_flip_last_digit(value), uf))

    def test_ie_wrong_uf_or_shape(self):
        self.assertFalse(valid_ie("110042490114", "RJ"))
        self.assertFalse(valid_ie("", "SP"))
        self.assertFalse(valid_ie("ISENTO", "SP"))
        self.assertFalse(valid_ie("110042490114", "XX"))
        self.assertFalse(valid_ie("P011004243002", "MG"))
        self.assertFalse(valid_ie("111111111", "SC"))

    def test_cep_ibge_and_codes(self):
        self.assertTrue(valid_cep("01310-100"))
        self.assertFalse(valid_cep("0131010"))
        self.assertTrue(valid_ibge_city("3550308"))          # São Paulo
        self.assertTrue(valid_ibge_city("3304557", "RJ"))    # Rio de Janeiro
        self.assertFalse(valid_ibge_city("3304557", "SP"))   # wrong UF
        self.assertFalse(valid_ibge_city("3550309"))          # wrong check digit
        self.assertTrue(valid_ibge_city("2201919"))           # official exception to the check digit
        self.assertFalse(valid_ibge_city("9950308"))          # no such UF code
        self.assertTrue(valid_ncm("22021000"))
        self.assertTrue(valid_ncm("2202.10.00"))
        self.assertFalse(valid_ncm("2202100"))
        self.assertFalse(valid_ncm("00000000"))
        self.assertTrue(valid_cest("0300700"))
        self.assertFalse(valid_cest("2900100"))
        self.assertTrue(valid_cfop("5102"))
        self.assertFalse(valid_cfop("4102"))
        self.assertFalse(valid_cfop("5000"))
        self.assertTrue(valid_cbenef("RJ850001"))
        self.assertTrue(valid_cbenef("PR830001AB"))
        self.assertFalse(valid_cbenef("850001"))


class SchemaTests(unittest.TestCase):
    def test_clean_normalizes_codes_and_rates(self):
        clean = clean_fields({"cfop": "5.102", "icms_cst": "00", "icms_rate": "18,00", "pis_rate": 0.65,
                              "origin": "0", "cest": "03.007.00", "ncm": "", "tax_unit": "cx"})
        self.assertEqual(clean, {"cfop": "5102", "icms_cst": "00", "icms_rate": "18", "pis_rate": "0.65",
                                 "origin": 0, "cest": "0300700", "tax_unit": "CX"})

    def test_clean_rejects_with_field_and_portuguese_message(self):
        cases = [({"icms_cst": "61"}, "icms_cst"), ({"icms_rate": "101"}, "icms_rate"),
                 ({"icms_rate": "1.23456"}, "icms_rate"), ({"cest": "123"}, "cest"),
                 ({"cfop": "1102x"}, "cfop"), ({"origin": 9}, "origin"), ({"made_up": 1}, "made_up"),
                 ({"icms_cst": "00", "icms_csosn": "102"}, "icms_csosn"), ({"tax_gtin": "7891000315508"}, "tax_gtin")]
        for raw, field in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(FiscalFieldError) as ctx:
                    clean_fields(raw)
                self.assertEqual(ctx.exception.field, field)
        with self.assertRaises(FiscalFieldError):
            clean_fields({"ncm": "22021000"}, allow={"cfop"})

    def test_legacy_tax_fields_and_layering(self):
        legacy = {"CST": "60", "cfop": "5405", "origem": "0", "aliquota_icms": "abc", "nada": 1, "CEST": "0300700"}
        self.assertEqual(legacy_fields(legacy), {"icms_cst": "60", "cfop": "5405", "origin": 0, "cest": "0300700"})
        profile = {"cfop": "5102", "icms_csosn": "102", "pis_cst": "49"}
        resolved = resolve(profile, "22021000", {"icms_csosn": "500", "cfop": "5405"}, {"cest": "0300700"})
        self.assertEqual(resolved["icms_csosn"], "500")
        self.assertEqual(resolved["cfop"], "5405")
        self.assertEqual(resolved["cest"], "0300700")
        self.assertEqual(resolved["ncm"], "22021000")
        self.assertEqual(resolved["pis_cst"], "49")
        # A CST chosen in a higher layer replaces the CSOSN of the profile.
        self.assertNotIn("icms_csosn", resolve(profile, "", {"icms_cst": "00"}))


if __name__ == "__main__":
    unittest.main()
