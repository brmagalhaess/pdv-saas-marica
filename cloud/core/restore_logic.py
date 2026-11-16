"""Paged, resumable download of one terminal's history ("Recuperar minha loja").

The cursor is opaque to the terminal: base64url of a small JSON that carries
the snapshot limits taken on the first request (`asof`, `cutoff`, the open
cash session), the keyset position and the running counters. The same cursor
returns the same page while the data does not change, and the counters add up
what was actually sent, so the final `totals` always match what a correct
terminal applied.

Dates in records are epoch milliseconds, as the terminal stores them.
"""
import base64
import binascii
import json
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone as utc_timezone

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .api_common import APIError, audit, digest
from .station_logic import MAIN_STATION_ID
from .models import (CashMovement, CashSession, CreditReceipt, FinanceEntry, Sale, SaleItem, SalePayment,
                     SaleReturn, SyncEvent, Terminal)

SECTIONS = ("config", "cash_session", "sale", "credit_receipt")
CURSOR_MAX = 4000
OPEN_MAX = 64  # one open cash session per register of the shop
# p: records sent · cf: config · cs/co/so: cash sessions sent/open/stale open omitted ·
# cm/cmc: cash movements · s/sc/sv: sales, cents, voided · r/rc: returns · cr/crc: credit receipts
COUNTERS = ("p", "cf", "cs", "co", "so", "cm", "cmc", "s", "sc", "sv", "r", "rc", "cr", "crc")


def ms(value):
    return round(value.timestamp() * 1000) if value is not None else None


def _invalid_cursor():
    return APIError("Cursor inválido", 400, code="invalid_cursor")


class _State:
    """Decoded cursor."""

    def __init__(self, terminal_id, cutoff, asof, open_ids, section, key, counters, total):
        self.terminal_id, self.cutoff, self.asof, self.open_ids = terminal_id, cutoff, asof, open_ids
        self.section, self.key, self.counters, self.total = section, key, counters, total
        self.cutoff_at = datetime.fromtimestamp(cutoff / 1000, tz=utc_timezone.utc)
        self.open_forms = [form for pk in open_ids for form in (str(pk), pk.hex)]

    def encode(self, section, key, counters):
        raw = json.dumps({"v": 1, "t": self.terminal_id, "cutoff": self.cutoff, "asof": self.asof.isoformat(),
                          "open": [pk.hex for pk in self.open_ids], "sec": section, "key": key,
                          "n": counters, "total": self.total}, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(text, terminal, now, history_days):
    if len(text) > CURSOR_MAX:
        raise _invalid_cursor()
    try:
        data = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        if not isinstance(data, dict) or data.get("v") != 1 or data.get("t") != str(terminal.id):
            raise ValueError
        cutoff, total, counters, key = data["cutoff"], data["total"], data["n"], data["key"]
        asof = datetime.fromisoformat(data["asof"])
        if asof.tzinfo is None or type(cutoff) is not int or type(total) is not int or total < 0:
            raise ValueError
        open_ids = [uuid.UUID(value) for value in data["open"]]
        if len(open_ids) > OPEN_MAX or data["sec"] not in SECTIONS:
            raise ValueError
        if (not isinstance(counters, dict) or set(counters) != set(COUNTERS)
                or any(type(value) is not int or value < 0 for value in counters.values())):
            raise ValueError
        if not isinstance(key, list) or len(key) not in (0, 2):
            raise ValueError
        if key and data["sec"] != "config":
            datetime.fromisoformat(key[0])
            uuid.UUID(key[1])
        state = _State(data["t"], cutoff, asof, open_ids, data["sec"], key, counters, total)
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, OSError, binascii.Error):
        raise _invalid_cursor()
    if cutoff < ms(now - timedelta(days=history_days + 1)):
        raise _invalid_cursor()
    return state


def _sessions(terminal, state):
    # The terminal admits one open cash session per register: only the newest open
    # one of each travels; older ones left open in the cloud are counted as omitted.
    return (CashSession.objects.filter(tenant=terminal.tenant, terminal=terminal)
            .filter(Q(opened_at__gte=state.cutoff_at, closed_at__isnull=False) | Q(id__in=state.open_ids))
            .order_by("opened_at", "id"))


def _sales(terminal, state):
    open_credit = FinanceEntry.objects.filter(sale=OuterRef("pk"), kind="receivable", status="open")
    wanted = Q(created_at__gte=state.cutoff_at) | Q(Exists(open_credit))
    if state.open_forms:
        returned_now = SaleReturn.objects.filter(sale=OuterRef("pk"), cash_session_id__in=state.open_forms)
        wanted |= Q(cash_session_id__in=state.open_forms) | Q(Exists(returned_now))
    return (Sale.objects.filter(tenant=terminal.tenant, terminal=terminal, received_at__lte=state.asof)
            .filter(wanted).order_by("received_at", "id"))


def _receipts(terminal, state):
    wanted = Q(created_at__gte=state.cutoff_at)
    if state.open_forms:
        wanted |= Q(cash_session_id__in=state.open_forms)
    return (CreditReceipt.objects.filter(tenant=terminal.tenant, terminal=terminal, received_at__lte=state.asof)
            .filter(wanted).order_by("received_at", "id"))


QUERIES = {"cash_session": (_sessions, "opened_at"), "sale": (_sales, "received_at"),
           "credit_receipt": (_receipts, "received_at")}


def _member_id(value):
    return str(value) if value else None


def _session_records(rows):
    movements = defaultdict(list)
    for row in CashMovement.objects.filter(session__in=rows).order_by("created_at", "id"):
        movements[row.session_id].append({"id": str(row.id), "kind": row.kind, "amount_cents": row.amount_cents,
                                          "reason": row.reason, "operator_id": _member_id(row.operator_id),
                                          "created_at": ms(row.created_at)})
    return [{"kind": "cash_session", "id": str(row.id), "operator_id": _member_id(row.operator_id),
             "station_id": row.station_id, "station_name": row.station_name,
             "opening_cents": row.opening_cents, "opened_at": ms(row.opened_at), "closed_at": ms(row.closed_at),
             "counted_cents": row.counted_cents, "expected_cents": row.expected_cents,
             "incomplete": row.incomplete, "movements": movements[row.id]} for row in rows]


def _rebuilt_sale_data(sale):
    """Fallback for a sale without its original event: same keys the terminal sends."""
    items = [{"product_id": str(item.product_id), "name": item.name, "sku": item.product.sku or "",
              "ncm": item.product.ncm or "", "unit": item.unit,
              "stock_controlled": item.product.stock_controlled, "quantity_milli": item.quantity_milli,
              "unit_price_cents": item.unit_price_cents, "gross_cents": item.gross_cents,
              "promo_discount_cents": item.promo_discount_cents,
              "promotion_id": str(item.promotion_id) if item.promotion_id else None,
              "promotion_name": item.promotion_name, "discount_cents": item.discount_cents,
              "line_total_cents": item.line_total_cents}
             for item in SaleItem.objects.filter(sale=sale).select_related("product").order_by("line_number")]
    payments = []
    for payment in SalePayment.objects.filter(sale=sale).order_by("line_number"):
        line = {"method": payment.method, "amount_cents": payment.amount_cents}
        if payment.payment_method_id:
            line["payment_method_id"] = str(payment.payment_method_id)
        payments.append(line)
    return {"request_id": sale.request_id, "cash_session_id": sale.cash_session_id, "created_at": ms(sale.created_at),
            "gross_cents": sale.gross_cents, "promo_discount_cents": sale.promo_discount_cents,
            "discount_cents": sale.discount_cents, "total_cents": sale.total_cents, "change_cents": sale.change_cents,
            "customer_id": _member_id(sale.customer_id), "customer_name": sale.customer_name_snapshot,
            "customer_document": sale.customer_document_snapshot, "operator_id": _member_id(sale.operator_id),
            "seller_id": _member_id(sale.seller_id), "items": items, "payments": payments}


def _sale_records(terminal, rows):
    by_id = {row.id: row for row in rows}
    originals = {}
    # One query per page. A quarantined sale.completed may share the aggregate,
    # so only the payload that produced the stored sale is trusted.
    for event in SyncEvent.objects.filter(terminal=terminal, event_type="sale.completed",
                                          aggregate_id__in=[form for pk in by_id for form in (str(pk), pk.hex)]):
        try:
            sale = by_id.get(uuid.UUID(event.aggregate_id))
            data = event.payload.get("data") if isinstance(event.payload, dict) else None
            if sale is not None and isinstance(data, dict) and digest(data) == sale.content_hash:
                originals[sale.id] = (event.event_id, data)
        except ValueError:
            continue
    returns = defaultdict(list)
    for row in (SaleReturn.objects.filter(tenant=terminal.tenant, sale__in=rows)
                .prefetch_related("items").order_by("created_at", "id")):
        returns[row.sale_id].append({
            "id": str(row.id), "refund_method": row.refund_method, "total_cents": row.total_cents,
            "reason": row.reason, "operator_id": _member_id(row.operator_id),
            "cash_session_id": row.cash_session_id or "", "created_at": ms(row.created_at),
            "origin": "terminal" if row.terminal_id else "panel",
            "items": [{"product_id": str(item.product_id), "quantity_milli": item.quantity_milli,
                       "amount_cents": item.amount_cents} for item in row.items.all()]})
    records = []
    for row in rows:
        event_id, data = originals.get(row.id) or (None, None)
        voided = row.status in ("cancelled", "refunded")
        # station_id stays outside "data": the content hash of the sale does not cover it.
        records.append({"kind": "sale", "id": str(row.id), "event_id": event_id, "station_id": row.station_id,
                        "status": "voided" if voided else "completed",
                        "voided_at": ms(row.cancelled_at) if voided else None,
                        "void_reason": row.cancellation_reason if voided else "",
                        "data": data if data is not None else _rebuilt_sale_data(row),
                        "returns": returns[row.id]})
    return records


def _receipt_records(rows):
    return [{"kind": "credit_receipt", "id": str(row.id), "customer_id": str(row.customer_id),
             "cash_session_id": row.cash_session_id or "", "method": row.method, "amount_cents": row.amount_cents,
             "operator_id": _member_id(row.operator_id), "created_at": ms(row.created_at)} for row in rows]


def _fetch(terminal, state, section, key, room):
    """Up to `room` records of one section after `key`, as (key, record) pairs,
    and whether the section still has more."""
    if section == "config":
        config = terminal.config if isinstance(terminal.config, dict) else {}
        if key or not config:
            return [], False
        return ([(["config", ""], {"kind": "config", "data": config})], False) if room else ([], True)
    build, field = QUERIES[section]
    rows = build(terminal, state)
    if key:
        when, pk = datetime.fromisoformat(key[0]), uuid.UUID(key[1])
        rows = rows.filter(Q(**{field + "__gt": when}) | Q(**{field: when, "id__gt": pk}))
    rows = list(rows[:room + 1])
    more, rows = len(rows) > room, rows[:room]
    if section == "cash_session":
        records = _session_records(rows)
    elif section == "sale":
        records = _sale_records(terminal, rows)
    else:
        records = _receipt_records(rows)
    return [([getattr(row, field).isoformat(), str(row.id)], record) for row, record in zip(rows, records)], more


def _number(value):
    return value if type(value) is int else 0


def _count(counters, record):
    counters["p"] += 1
    kind = record["kind"]
    if kind == "config":
        counters["cf"] += 1
    elif kind == "cash_session":
        counters["cs"] += 1
        counters["co"] += record["closed_at"] is None
        counters["cm"] += len(record["movements"])
        counters["cmc"] += sum(row["amount_cents"] for row in record["movements"])
    elif kind == "sale":
        counters["s"] += 1
        counters["sc"] += _number(record["data"].get("total_cents"))
        counters["sv"] += record["status"] == "voided"
        counters["r"] += len(record["returns"])
        counters["rc"] += sum(row["total_cents"] for row in record["returns"])
    else:
        counters["cr"] += 1
        counters["crc"] += record["amount_cents"]


def _totals(n):
    return {"sales": {"count": n["s"], "total_cents": n["sc"], "voided_count": n["sv"]},
            "returns": {"count": n["r"], "total_cents": n["rc"]},
            "credit_receipts": {"count": n["cr"], "total_cents": n["crc"]},
            "cash_sessions": {"count": n["cs"], "open_count": n["co"], "stale_open_omitted": n["so"]},
            "cash_movements": {"count": n["cm"], "total_cents": n["cmc"]},
            "config": {"count": n["cf"]}}


def current_open_session_ids(terminal):
    """Ids of the cash sessions that are really open on the terminal: zero or
    one per register of the shop, oldest first.

    Each register admits a single open cash session, so a session still open in
    the cloud counts only when it is the newest session of its register. An
    open one older than another session of the same register was closed at the
    counter and its closing never arrived (replaced computer, quarantined
    event): it is stale. Sessions without a label belong to the main register.
    """
    sessions = CashSession.objects.filter(tenant=terminal.tenant, terminal=terminal)
    found = []
    for station_id in (sessions.filter(closed_at__isnull=True).order_by()
                       .values_list("station_id", flat=True).distinct()):
        if station_id in ("", MAIN_STATION_ID):
            of_station = sessions.filter(station_id__in=("", MAIN_STATION_ID))
        else:
            of_station = sessions.filter(station_id=station_id)
        newest = of_station.order_by("-opened_at", "-id").values_list("opened_at", "id", "closed_at").first()
        if newest and newest[2] is None and newest[:2] not in found:
            found.append(newest[:2])
    # More open registers than a terminal can hold: the most recent ones travel.
    return [pk for _, pk in sorted(found)[-OPEN_MAX:]]


def _start(terminal, history_days):
    """Snapshot limits of a new restore, under the same lock the sync intake takes."""
    with transaction.atomic():
        Terminal.objects.select_for_update().get(pk=terminal.pk)
        asof = timezone.now()
        open_ids = current_open_session_ids(terminal)
        counters = dict.fromkeys(COUNTERS, 0)
        counters["so"] = (CashSession.objects.filter(tenant=terminal.tenant, terminal=terminal,
                                                     closed_at__isnull=True).count() - len(open_ids))
        state = _State(str(terminal.id), ms(asof - timedelta(days=history_days)), asof, open_ids,
                       SECTIONS[0], [], counters, 0)
        state.total = (bool(terminal.config) + _sessions(terminal, state).count()
                       + _sales(terminal, state).count() + _receipts(terminal, state).count())
        audit(terminal.tenant, None, "terminal.restore_started", terminal,
              {"cutoff": state.cutoff, "estimated_records": state.total})
    return state


def restore_page(terminal, cursor, limit, *, history_days, max_bytes):
    now = timezone.now()
    state = _decode(cursor, terminal, now, history_days) if cursor else _start(terminal, history_days)
    counters = dict(state.counters)
    records, size, next_cursor = [], 0, None
    index, key = SECTIONS.index(state.section), state.key
    while index < len(SECTIONS):
        section = SECTIONS[index]
        batch, more = _fetch(terminal, state, section, key, limit - len(records))
        full = False
        for record_key, record in batch:
            record_size = len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode())
            if records and size + record_size > max_bytes:
                full = True  # the page closes early; this record opens the next one
                break
            records.append(record)
            size += record_size
            _count(counters, record)
            key = record_key
        if full or more:
            next_cursor = state.encode(section, key, counters)
            break
        index, key = index + 1, []
    done = next_cursor is None
    totals = _totals(counters) if done else None
    if done:
        audit(terminal.tenant, None, "terminal.restore_finished", terminal, totals)
    return {"records": records, "next_cursor": next_cursor, "done": done,
            "progress": {"position": counters["p"], "total": max(state.total, counters["p"])},
            "cutoff": state.cutoff, "history_days": history_days, "server_time": int(now.timestamp()),
            "totals": totals}
