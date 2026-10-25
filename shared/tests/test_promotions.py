import unittest

from shared.promotions import best_promotion, describe, discount_for

RICE = {"id": "rice", "category": "Mercearia", "unit": "UN", "price_cents": 1000}
CHEESE = {"id": "cheese", "category": "Frios", "unit": "KG", "price_cents": 4990}


class PromotionEngineTests(unittest.TestCase):
    def test_price_wholesale_and_buy_x_pay_y(self):
        price = {"id": "p", "name": "Oferta", "kind": "price", "product_id": "rice", "price_cents": 850}
        self.assertEqual(discount_for(price, RICE, 3000), 450)
        wholesale = {"id": "w", "name": "Atacado", "kind": "wholesale", "category": "Mercearia",
                     "price_cents": 900, "min_quantity_milli": 6000}
        self.assertEqual(discount_for(wholesale, RICE, 5000), 0)
        self.assertEqual(discount_for(wholesale, RICE, 6000), 600)
        three_for_two = {"id": "b", "name": "Leve 3", "kind": "buy_x_pay_y", "product_id": "rice", "buy_quantity": 3, "pay_quantity": 2}
        self.assertEqual(discount_for(three_for_two, RICE, 7000), 2000)  # two complete sets, one unit free each
        self.assertEqual(discount_for({**three_for_two, "product_id": "cheese"}, CHEESE, 3000), 0)  # weighed: not allowed
        self.assertEqual(describe(three_for_two), "Leve 3 pague 2")

    def test_fractional_price_and_best_rule_without_stacking(self):
        cheese = {"id": "c", "name": "Queijo", "kind": "price", "product_id": "cheese", "price_cents": 3990}
        self.assertEqual(discount_for(cheese, CHEESE, 350), 350)  # 17.47 − 13.97
        rules = [{"id": "p", "name": "Oferta", "kind": "price", "product_id": "rice", "price_cents": 950},
                 {"id": "b", "name": "Leve 3", "kind": "buy_x_pay_y", "product_id": "rice", "buy_quantity": 3, "pay_quantity": 2}]
        self.assertEqual(best_promotion(RICE, 2000, rules, 0)[1]["id"], "p")  # 1.00 off beats nothing
        self.assertEqual(best_promotion(RICE, 3000, rules, 0)[1]["id"], "b")  # 10.00 off beats 1.50

    def test_period_and_active_flag(self):
        rule = {"id": "p", "name": "Fim de semana", "kind": "price", "product_id": "rice", "price_cents": 800,
                "starts_at_ms": 1000, "ends_at_ms": 2000}
        self.assertEqual(best_promotion(RICE, 1000, [rule], 999), (0, None))
        self.assertEqual(best_promotion(RICE, 1000, [rule], 1500)[0], 200)
        self.assertEqual(best_promotion(RICE, 1000, [rule], 2000), (0, None))
        self.assertEqual(best_promotion(RICE, 1000, [{**rule, "active": False}], 1500), (0, None))
