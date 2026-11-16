from django.utils import timezone

from .api_common import iso
from .models import Subscription
from .station_logic import config_stations, is_hub


def plan(p):
    return {"id": str(p.id), "slug": p.slug, "name": p.name,
            "price_month_cents": p.price_month_cents,
            "price_year_cents": p.price_year_cents,
            "terminal_limit": p.terminal_limit, "shop_limit": p.shop_limit,
            "max_stations": p.max_stations,
            "features": p.features, "active": p.active}


def shop(row):
    return {"id": str(row.id), "name": row.name, "address": row.address, "active": row.active}


def category(row):
    return {"id": str(row.id), "name": row.name, "active": row.active,
            "commission_bps": row.commission_bps}


def product(row):
    return {"id": str(row.id), "sku": row.sku, "barcode": row.barcode, "ncm": row.ncm, "scale_code": row.scale_code,
            "name": row.name, "category_id": str(row.category_id) if row.category_id else None,
            "variant": row.variant, "unit": row.unit,
            "group_id": str(row.group_id) if row.group_id else None, "variant_values": row.variant_values,
            "price_cents": row.price_cents, "cost_cents": row.cost_cents,
            "stock_minimum_milli": row.stock_minimum_milli,
            "stock_controlled": row.stock_controlled,
            "image_url": row.image_url,
            "tax_fields": row.tax_fields, "active": row.active}


def contact(row):
    return {"id": str(row.id), "name": row.name, "document": row.document,
            "email": row.email, "phone": row.phone, "active": row.active,
            **({"credit_limit_cents": row.credit_limit_cents} if hasattr(row, "credit_limit_cents") else {})}


def member(row):
    return {"id": str(row.id), "user_id": row.user_id, "name": row.name,
            "email": row.user.email, "role": row.role, "active": row.active,
            "has_pin": bool(row.pin_hash), "commission_bps": row.commission_bps}


def terminal(row):
    # Mirror of the newest replaced computer (the list lives in TerminalFormerToken).
    send_only = (row.previous_token_hash and row.previous_token_expires_at
                 and row.previous_token_expires_at > timezone.now())
    mirror = row.config.get("mirror") if isinstance(row.config, dict) else None
    # Additional registers ("caixas adicionais") linked to this terminal, as it last reported.
    hub = is_hub(row)
    stations = config_stations(row)
    allowed = (Subscription.objects.filter(tenant_id=row.tenant_id)
               .values_list("plan__max_stations", flat=True).first() or 0) if hub else 0
    return {"id": str(row.id), "shop_id": str(row.shop_id), "name": row.name,
            "device_id": row.device_id, "version": row.version,
            "revoked": row.revoked, "paired": bool(row.token_hash),
            "last_seen_at": iso(row.last_seen_at),
            "heartbeat_pending_events": row.heartbeat_pending_events,
            "heartbeat_error": row.heartbeat_error,
            "transferred_at": iso(row.transferred_at),
            # While set, the replaced computer may still send its sales queue.
            "send_only_until": iso(row.previous_token_expires_at) if send_only else None,
            "previous_device_id": row.previous_device_id,
            "mirror_enabled": mirror.get("enabled") if isinstance(mirror, dict) else None,
            "hub": hub, "max_stations": allowed, "stations": stations,
            "stations_used": sum(not item["revoked"] for item in stations)}


def sync_conflict(row):
    return {"id": str(row.id), "event_id": row.event_id, "event_type": row.event_type,
            "aggregate_id": row.aggregate_id, "reason": row.reason, "status": row.status,
            "detail": row.detail, "resolution": row.resolution,
            "terminal_id": str(row.terminal_id), "terminal_name": row.terminal.name,
            "shop_id": str(row.shop_id), "shop_name": row.shop.name,
            "created_at": iso(row.created_at), "resolved_at": iso(row.resolved_at),
            "resolved_by": row.resolved_by_id}


def movement(row):
    return {"id": str(row.id), "shop_id": str(row.shop_id),
            "product_id": str(row.product_id), "product_name": row.product.name, "sku": row.product.sku,
            "unit": row.product.unit, "delta_milli": row.delta_milli,
            "source": row.source, "reason": row.reason, "reference": row.reference,
            "created_at": iso(row.created_at),
            "terminal_id": str(row.terminal_id) if row.terminal_id else None,
            "sale_id": str(row.sale_id) if row.sale_id else None}


def receipt(row):
    return {"id": str(row.id), "shop_id": str(row.shop_id),
            "supplier_id": str(row.supplier_id) if row.supplier_id else None,
            "supplier_name": row.supplier.name if row.supplier_id else None,
            "reference": row.reference, "idempotency_key": row.idempotency_key,
            "source_file": {"name": row.source_file_name, "mime": row.source_file_mime,
                            "size": row.source_file_size, "sha256": row.source_file_sha256}
            if row.source_file_name else None,
            "created_at": iso(row.created_at),
            "items": [{"product_id": str(item.product_id), "quantity_milli": item.quantity_milli,
                       "unit_cost_cents": item.unit_cost_cents,
                       "average_cost_cents": item.average_cost_cents,
                       "product_name": item.product.name, "sku": item.product.sku, "unit": item.product.unit}
                      for item in row.items.all()]}


def finance(row):
    return {"id": str(row.id), "shop_id": str(row.shop_id) if row.shop_id else None,
            "sale_id": str(row.sale_id) if row.sale_id else None,
            "customer_id": str(row.customer_id) if row.customer_id else None,
            "supplier_id": str(row.supplier_id) if row.supplier_id else None,
            "kind": row.kind, "description": row.description, "amount_cents": row.amount_cents,
            "due_date": row.due_date.isoformat(), "status": row.status,
            "settled_at": iso(row.settled_at), "created_at": iso(row.created_at)}


def sale(row, detail=False):
    result = {"id": str(row.id), "shop_id": str(row.shop_id),
              "terminal_id": str(row.terminal_id) if row.terminal_id else None,
              "customer_id": str(row.customer_id) if row.customer_id else None,
              "customer_name": row.customer_name_snapshot,
              "customer_document": row.customer_document_snapshot,
              "operator_id": str(row.operator_id) if row.operator_id else None,
              "operator_name": row.operator_name_snapshot,
              "seller_id": str(row.seller_id) if row.seller_id else None,
              "seller_name": row.seller_name_snapshot,
              "station_id": row.station_id, "station_name": row.station_name,
              "request_id": row.request_id, "gross_cents": row.gross_cents,
              "discount_cents": row.discount_cents, "total_cents": row.total_cents,
              "change_cents": row.change_cents, "status": row.status,
              "stock_status": row.stock_status, "fiscal_status": row.fiscal_status,
              "created_at": iso(row.created_at), "received_at": iso(row.received_at),
              "cancelled_at": iso(row.cancelled_at), "cancellation_reason": row.cancellation_reason}
    if detail:
        result["items"] = [{"product_id": str(x.product_id), "name": x.name, "ncm": x.product.ncm,
                            "unit": x.unit,
                            "quantity_milli": x.quantity_milli,
                            "unit_price_cents": x.unit_price_cents,
                            "gross_cents": x.gross_cents, "promo_discount_cents": x.promo_discount_cents,
                            "promotion_name": x.promotion_name, "discount_cents": x.discount_cents,
                            "line_total_cents": x.line_total_cents}
                           for x in row.items.all().order_by("line_number")]
        result["payments"] = [{"method": x.method, "amount_cents": x.amount_cents,
                               "name": x.payment_method.name if x.payment_method_id else None}
                              for x in row.payments.select_related("payment_method").order_by("line_number")]
        # A return line has no unit of its own: it is the unit the product was sold in.
        sold_units = {x["product_id"]: x["unit"] for x in result["items"]}
        result["returns"] = [{"id": str(r.id), "total_cents": r.total_cents, "refund_method": r.refund_method,
                              "reason": r.reason, "created_at": iso(r.created_at),
                              "items": [{"product_id": str(i.product_id), "name": i.product.name,
                                         "unit": sold_units.get(str(i.product_id), i.product.unit),
                                         "quantity_milli": i.quantity_milli, "amount_cents": i.amount_cents}
                                        for i in r.items.select_related("product")]}
                             for r in row.returns.prefetch_related("items").order_by("created_at")]
    return result
