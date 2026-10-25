"""Promotion engine shared by the terminal (pricing, offline) and the cloud (simulation).

Pure functions over plain dicts so the terminal can run them on its SQLite
snapshot without the cloud. Money in cents, quantities in thousandths.

Promotion dict: {"id", "name", "kind": "price"|"wholesale"|"buy_x_pay_y",
"product_id", "category", "price_cents", "min_quantity_milli", "buy_quantity",
"pay_quantity", "starts_at_ms", "ends_at_ms", "active"}
Product dict: {"id", "category", "unit", "price_cents"}
"""


def _half_up(numerator, denominator):
    return (2 * numerator + denominator) // (2 * denominator)


def in_effect(promotion, now_ms):
    if not promotion.get("active", True):
        return False
    starts, ends = promotion.get("starts_at_ms"), promotion.get("ends_at_ms")
    return (starts is None or now_ms >= starts) and (ends is None or now_ms < ends)


def applies_to(promotion, product):
    if promotion.get("product_id"):
        return promotion["product_id"] == product["id"]
    return bool(promotion.get("category")) and promotion["category"] == product.get("category")


def discount_for(promotion, product, quantity_milli):
    """Discount in cents this promotion gives on one cart line (0 when it does not apply)."""
    list_price = int(product["price_cents"])
    gross = _half_up(list_price * quantity_milli, 1000)
    kind = promotion.get("kind")
    if kind in ("price", "wholesale"):
        price = promotion.get("price_cents")
        if price is None or price >= list_price:
            return 0
        if kind == "wholesale" and quantity_milli < int(promotion.get("min_quantity_milli") or 0):
            return 0
        discount = gross - _half_up(int(price) * quantity_milli, 1000)
    elif kind == "buy_x_pay_y":
        buy, pay = int(promotion.get("buy_quantity") or 0), int(promotion.get("pay_quantity") or 0)
        if product.get("unit") != "UN" or buy < 2 or not 1 <= pay < buy:
            return 0
        free_units = (quantity_milli // 1000 // buy) * (buy - pay)
        discount = free_units * list_price
    else:
        return 0
    return max(0, min(discount, gross))


def best_promotion(product, quantity_milli, promotions, now_ms):
    """(discount_cents, promotion) with the best price for the customer, or (0, None)."""
    best = (0, None)
    for promotion in promotions:
        if not in_effect(promotion, now_ms) or not applies_to(promotion, product):
            continue
        discount = discount_for(promotion, product, quantity_milli)
        if discount > best[0]:
            best = (discount, promotion)
    return best


def describe(promotion):
    """Short customer-facing label, e.g. "Leve 3 pague 2"."""
    kind = promotion.get("kind")
    if kind == "buy_x_pay_y":
        return f"Leve {promotion['buy_quantity']} pague {promotion['pay_quantity']}"
    if kind == "wholesale":
        quantity = int(promotion.get("min_quantity_milli") or 0) / 1000
        return f"Atacado a partir de {quantity:g}"
    return "Preço promocional"
