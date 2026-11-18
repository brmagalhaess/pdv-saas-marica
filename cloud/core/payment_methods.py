"""Company payment configuration with built-in defaults created on demand."""
from .models import PaymentMethod

BUILTIN = (  # kind, default name, default settlement days
    ("cash", "Dinheiro", 0),
    ("pix", "Pix", 0),
    ("card_credit", "Crédito", 30),
    ("card_debit", "Débito", 1),
    ("credit_account", "A prazo (fiado)", 30),
    ("store_credit", "Crédito na loja", 0),
)
BUILTIN_KINDS = tuple(kind for kind, _, _ in BUILTIN)


def ensure(tenant):
    """All methods of a company, creating missing built-ins with defaults."""
    existing = set(PaymentMethod.objects.filter(tenant=tenant).exclude(kind="other").values_list("kind", flat=True))
    for position, (kind, name, days) in enumerate(BUILTIN):
        if kind not in existing:
            PaymentMethod.objects.get_or_create(tenant=tenant, kind=kind,
                                                defaults={"name": name, "settlement_days": days, "position": position})
    return list(PaymentMethod.objects.filter(tenant=tenant).order_by("position", "created_at", "id"))


def method_data(row):
    from shared.nfce.payments import default_print_rule
    return {"id": str(row.id), "kind": row.kind, "name": row.name, "fee_bps": row.fee_bps,
            "settlement_days": row.settlement_days, "active": row.active, "builtin": row.kind != "other",
            # NFC-e printing after the sale: always | ask | never (empty in the database = default of the kind).
            "print_rule": row.print_rule or default_print_rule(row.kind),
            "print_rule_default": not row.print_rule}


def fee_lookup(tenant):
    """(kind_or_id) -> (name, fee_bps) for reports."""
    result = {}
    for row in ensure(tenant):
        result[row.kind if row.kind != "other" else row.id] = (row.name, row.fee_bps)
    return result
