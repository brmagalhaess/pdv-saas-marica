"""Formas de pagamento da venda -> grupo pag/detPag da NFC-e, e regra de impressão do DANFE."""

# tPag (MOC 7.0 / NT 2020.006): 01 dinheiro, 03 crédito, 04 débito, 05 crédito loja,
# 17 Pix (dinâmico/estático), 99 outros. "A prazo" (fiado) é crédito concedido pela loja: 05.
TPAG = {"cash": "01", "card_credit": "03", "card_debit": "04", "card": "03",
        "credit_account": "05", "store_credit": "05", "pix": "17", "other": "99"}
CARD_TPAG = {"03", "04"}

PRINT_RULES = ("always", "ask", "never")
DEFAULT_PRINT_RULES = {"cash": "ask", "pix": "ask", "card_credit": "always", "card_debit": "always",
                       "card": "always", "credit_account": "always", "store_credit": "ask", "other": "ask"}


def tpag(method):
    return TPAG.get(method, "99")


def default_print_rule(kind):
    return DEFAULT_PRINT_RULES.get(kind, "ask")


def print_decision(payments, rules):
    """always | ask | never for the DANFE of a sale.

    payments: [{"method", "payment_method_id"?}]; rules: {kind or payment_method_id: rule}.
    Split payment: prints if any method says "always", asks if any says "ask", else never.
    """
    found = []
    for payment in payments or []:
        method = payment.get("method")
        rule = None
        if method == "other" and payment.get("payment_method_id"):
            rule = rules.get(payment["payment_method_id"])
        rule = rule or rules.get(method) or default_print_rule(method)
        found.append(rule if rule in PRINT_RULES else "ask")
    if "always" in found:
        return "always"
    if "ask" in found or not found:
        return "ask"
    return "never"
