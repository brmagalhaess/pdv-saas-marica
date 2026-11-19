"""Produto com "Controlar estoque" desligado (pão, salgados por KG) vende com saldo zero no caixa.

O SQLite guarda stock_controlled como 0/1; a tela testa true/false. As leituras de produto
para o navegador precisam devolver booleano, senão o caixa recusa "sem estoque".
"""
import time
import tempfile
import unittest
import uuid
from pathlib import Path

from shared.licensing import generate_keypair, payload_digest, sign_lease
from shared.security import hash_pin
from terminal.store import Store


class FreeStockTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "terminal.sqlite3")
        private, public = generate_keypair()
        operators = [{"id": "op-1", "name": "Ana", "role": "cashier", "pin_hash": hash_pin("123987")}]
        now = int(time.time())
        claims = {"schema": 1, "tenant_id": str(uuid.uuid4()), "shop_id": str(uuid.uuid4()),
                  "terminal_id": str(uuid.uuid4()), "device_id": self.store.identity()["device_id"],
                  "issued_at": now - 5, "expires_at": now + 3600, "features": ["pdv"], "status": "active",
                  "operators_hash": payload_digest(operators)}
        lease = sign_lease(claims, private)
        self.store.set_pairing({"token": "t" * 48, "tenant_id": claims["tenant_id"], "shop_id": claims["shop_id"],
                                "terminal_id": claims["terminal_id"], "lease": lease, "public_key": public},
                               "http://127.0.0.1:8000")
        products = [
            {"id": "pao", "sku": "PAO", "name": "Pão francês", "unit": "KG", "category": "Padaria",
             "price_cents": 1690, "quantity_milli": 0, "active": True, "stock_controlled": False, "scale_code": "1"},
            {"id": "arroz", "sku": "ARROZ", "name": "Arroz", "unit": "UN", "category": "Mercearia",
             "price_cents": 2890, "quantity_milli": 0, "active": True},
        ]
        self.store.apply_bootstrap({"products": products, "operators": operators, "lease": lease, "public_key": public})

    def test_product_reads_return_booleans(self):
        found = {row["id"]: row for row in self.store.product_search("")["products"]}
        self.assertIs(found["pao"]["stock_controlled"], False)
        self.assertIs(found["arroz"]["stock_controlled"], True)
        self.assertIs(self.store.product_search("PAO")["products"][0]["stock_controlled"], False)
        self.assertIs(self.store.product_by_scale_code("00001")["stock_controlled"], False)
        self.assertIs(self.store.products_by_ids(["pao"])[0]["stock_controlled"], False)

    def test_screen_check_treats_zero_as_uncontrolled(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "terminal.js").read_text(encoding="utf-8")
        self.assertIn("const isOut = product => !freeStock(product)", source)
        self.assertIn("product.stock_controlled === 0", source)


if __name__ == "__main__":
    unittest.main()
