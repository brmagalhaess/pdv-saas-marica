import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from local_pdv import LocalPDV, PDVError


class LocalPDVTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "terminal.sqlite3"
        self.pdv = LocalPDV(self.path, "empresa-a", "loja-1", "caixa-1")
        self.pdv.upsert_product("arroz", "Arroz 1 kg", 699, 10, "7890000000001")

    def test_sale_is_atomic_idempotent_and_survives_restart(self):
        self.pdv.open_cash(10000)
        items = [{"product_id": "arroz", "quantity": 2}]
        payments = [{"method": "cash", "amount_cents": 600},
                    {"method": "pix", "amount_cents": 898}]
        sale = self.pdv.complete_sale("pedido-1", items, payments)
        self.assertEqual(sale["total_cents"], 1398)
        self.assertEqual(sale["change_cents"], 100)
        self.assertEqual(self.pdv.products()[0]["stock_qty"], 8)
        self.assertEqual(self.pdv.complete_sale("pedido-1", items, payments)["id"], sale["id"])
        self.assertEqual(self.pdv.status()["sales_count"], 1)
        with self.assertRaisesRegex(PDVError, "outra venda"):
            self.pdv.complete_sale("pedido-1", items, [{"method": "cash", "amount_cents": 1500}])

        before = self.pdv.pending_events()
        restarted = LocalPDV(self.path)
        self.assertEqual(restarted.status()["database_integrity"], "ok")
        self.assertEqual(restarted.status()["sales_count"], 1)
        self.assertEqual(restarted.pending_events(), before)
        self.assertEqual(restarted.products()[0]["stock_qty"], 8)
        sale_event = next(e for e in before if e["type"] == "sale.completed")
        self.assertEqual(sale_event["data"]["items"][0]["unit_price_cents"], 699)
        restarted.acknowledge_event(sale_event["event_id"])
        restarted.acknowledge_event(sale_event["event_id"])
        self.assertEqual(restarted.status()["pending_events"], 1)
        self.assertEqual(restarted.close_cash(10500)["difference_cents"], 0)
        with self.assertRaisesRegex(PDVError, "Abra o caixa"):
            restarted.complete_sale("pedido-2", items, [{"method": "cash", "amount_cents": 1398}])

    def test_failed_sale_never_changes_stock_or_events(self):
        self.pdv.open_cash(0)
        before = self.pdv.status()["pending_events"]
        with self.assertRaisesRegex(PDVError, "Estoque insuficiente"):
            self.pdv.complete_sale("excesso", [{"product_id": "arroz", "quantity": 11}],
                                   [{"method": "cash", "amount_cents": 10000}])
        with self.assertRaisesRegex(PDVError, "Pagamentos"):
            self.pdv.complete_sale("incompleto", [{"product_id": "arroz", "quantity": 1}],
                                   [{"method": "cash", "amount_cents": 100}])
        with self.assertRaisesRegex(PDVError, "troco"):
            self.pdv.complete_sale("troco", [{"product_id": "arroz", "quantity": 1}],
                                   [{"method": "pix", "amount_cents": 700}])
        self.assertEqual(self.pdv.products()[0]["stock_qty"], 10)
        self.assertEqual(self.pdv.status()["sales_count"], 0)
        self.assertEqual(self.pdv.status()["pending_events"], before)

    def test_exception_between_stock_and_sale_rolls_back(self):
        self.pdv.open_cash(0)
        def fail(_stage):
            raise RuntimeError("queda simulada")
        pdv = LocalPDV(self.path, fault_hook=fail)
        with self.assertRaisesRegex(RuntimeError, "queda simulada"):
            pdv.complete_sale("queda-1", [{"product_id": "arroz", "quantity": 1}],
                              [{"method": "cash", "amount_cents": 699}])
        restarted = LocalPDV(self.path)
        self.assertEqual(restarted.products()[0]["stock_qty"], 10)
        self.assertEqual(restarted.status()["sales_count"], 0)
        self.assertEqual(restarted.status()["pending_events"], 1)

    def test_process_termination_before_commit_rolls_back(self):
        self.pdv.open_cash(0)
        code = """import os
from local_pdv import LocalPDV
p = LocalPDV(%r, fault_hook=lambda stage: os._exit(17))
p.complete_sale('process-killed', [{'product_id':'arroz','quantity':1}], [{'method':'cash','amount_cents':699}])
""" % str(self.path)
        project = Path(__file__).resolve().parents[2]
        result = subprocess.run([sys.executable, "-c", code], cwd=project, check=False)
        self.assertEqual(result.returncode, 17)
        restarted = LocalPDV(self.path)
        self.assertEqual(restarted.products()[0]["stock_qty"], 10)
        self.assertEqual(restarted.status()["sales_count"], 0)
        self.assertEqual(restarted.status()["database_integrity"], "ok")

    def test_two_simultaneous_sales_cannot_oversell(self):
        self.pdv.upsert_product("ultimo", "Último item", 200, 1)
        self.pdv.open_cash(0)
        def attempt(request_id):
            try:
                return self.pdv.complete_sale(request_id, [{"product_id": "ultimo", "quantity": 1}],
                                              [{"method": "cash", "amount_cents": 200}])
            except PDVError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ["a", "b"]))
        self.assertEqual(sum(isinstance(x, dict) for x in results), 1)
        self.assertEqual(self.pdv.status()["sales_count"], 1)
        self.assertEqual(next(x for x in self.pdv.products() if x["id"] == "ultimo")["stock_qty"], 0)

    def test_stock_adjustment_is_idempotent_and_terminal_identity_is_fixed(self):
        self.assertEqual(self.pdv.lookup_product("7890000000001")["id"], "arroz")
        first = self.pdv.adjust_stock("arroz", 5, "recebimento", "entrada-1")
        second = self.pdv.adjust_stock("arroz", 5, "recebimento", "entrada-1")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.pdv.products()[0]["stock_qty"], 15)
        self.assertEqual(self.pdv.status()["pending_events"], 1)
        with self.assertRaisesRegex(PDVError, "outro ajuste"):
            self.pdv.adjust_stock("arroz", 6, "recebimento", "entrada-1")
        with self.assertRaisesRegex(PDVError, "Identidade"):
            LocalPDV(self.path, "empresa-b", "loja-1", "caixa-1")


if __name__ == "__main__":
    unittest.main()
