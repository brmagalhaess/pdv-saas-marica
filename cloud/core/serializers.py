from .api_common import iso


def plan(p):
    return {"id": str(p.id), "slug": p.slug, "name": p.name,
            "price_month_cents": p.price_month_cents,
            "price_year_cents": p.price_year_cents,
            "terminal_limit": p.terminal_limit, "shop_limit": p.shop_limit,
            "features": p.features, "active": p.active}


def shop(row):
    return {"id": str(row.id), "name": row.name, "address": row.address, "active": row.active}


def category(row):
    return {"id": str(row.id), "name": row.name, "active": row.active}


def product(row):
    return {"id": str(row.id), "sku": row.sku, "barcode": row.barcode,
            "name": row.name, "category_id": str(row.category_id) if row.category_id else None,
            "variant": row.variant, "unit": row.unit,
            "price_cents": row.price_cents, "cost_cents": row.cost_cents,
            "stock_minimum_milli": row.stock_minimum_milli,
            "image_url": row.image_url,
            "tax_fields": row.tax_fields, "active": row.active}


def contact(row):
    return {"id": str(row.id), "name": row.name, "document": row.document,
            "email": row.email, "phone": row.phone, "active": row.active}


def member(row):
    return {"id": str(row.id), "user_id": row.user_id, "name": row.name,
            "email": row.user.email, "role": row.role, "active": row.active,
            "has_pin": bool(row.pin_hash)}


def terminal(row):
    return {"id": str(row.id), "shop_id": str(row.shop_id), "name": row.name,
            "device_id": row.device_id, "version": row.version,
            "revoked": row.revoked, "paired": bool(row.token_hash),
            "last_seen_at": iso(row.last_seen_at)}


def movement(row):
    return {"id": str(row.id), "shop_id": str(row.shop_id),
            "product_id": str(row.product_id), "delta_milli": row.delta_milli,
            "source": row.source, "reason": row.reason, "reference": row.reference,
            "created_at": iso(row.created_at),
            "terminal_id": str(row.terminal_id) if row.terminal_id else None,
            "sale_id": str(row.sale_id) if row.sale_id else None}


def receipt(row):
    return {"id": str(row.id), "shop_id": str(row.shop_id),
            "supplier_id": str(row.supplier_id) if row.supplier_id else None,
            "reference": row.reference, "idempotency_key": row.idempotency_key,
            "source_file": {"name": row.source_file_name, "mime": row.source_file_mime,
                            "size": row.source_file_size, "sha256": row.source_file_sha256}
            if row.source_file_name else None,
            "created_at": iso(row.created_at),
            "items": [{"product_id": str(item.product_id), "quantity_milli": item.quantity_milli,
                       "unit_cost_cents": item.unit_cost_cents,
                       "average_cost_cents": item.average_cost_cents,
                       "product_name": item.product.name, "unit": item.product.unit}
                      for item in row.items.all()]}


def finance(row):
    return {"id": str(row.id), "shop_id": str(row.shop_id) if row.shop_id else None,
            "customer_id": str(row.customer_id) if row.customer_id else None,
            "supplier_id": str(row.supplier_id) if row.supplier_id else None,
            "kind": row.kind, "description": row.description, "amount_cents": row.amount_cents,
            "due_date": row.due_date.isoformat(), "status": row.status,
            "settled_at": iso(row.settled_at), "created_at": iso(row.created_at)}


def sale(row, detail=False):
    result = {"id": str(row.id), "shop_id": str(row.shop_id),
              "terminal_id": str(row.terminal_id) if row.terminal_id else None,
              "customer_id": str(row.customer_id) if row.customer_id else None,
              "request_id": row.request_id, "gross_cents": row.gross_cents,
              "discount_cents": row.discount_cents, "total_cents": row.total_cents,
              "change_cents": row.change_cents, "status": row.status,
              "stock_status": row.stock_status, "fiscal_status": row.fiscal_status,
              "created_at": iso(row.created_at), "received_at": iso(row.received_at),
              "cancelled_at": iso(row.cancelled_at), "cancellation_reason": row.cancellation_reason}
    if detail:
        result["items"] = [{"product_id": str(x.product_id), "name": x.name, "unit": x.unit,
                            "quantity_milli": x.quantity_milli,
                            "unit_price_cents": x.unit_price_cents,
                            "gross_cents": x.gross_cents, "discount_cents": x.discount_cents,
                            "line_total_cents": x.line_total_cents}
                           for x in row.items.all().order_by("line_number")]
        result["payments"] = [{"method": x.method, "amount_cents": x.amount_cents}
                              for x in row.payments.all().order_by("line_number")]
    return result
