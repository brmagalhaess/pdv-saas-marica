"""Closing reports of the cashier (operator shift, register and whole day) for the thermal printer.

Pure functions: they receive the data already read by the store and return the same layout
the receipt uses (printers._Layout), so the browser preview and the ESC/POS spool show exactly
the same text. 80 mm paper has 48 columns and 58 mm has 32; every value is right aligned and a
line that does not fit is wrapped, never cut.
"""

from datetime import datetime

from .printers import _Layout, _amount, _receipt_zone

METHOD_LABELS = {"cash": "Dinheiro", "pix": "Pix", "card_debit": "Débito", "card_credit": "Crédito",
                 "card": "Cartão", "credit_account": "A prazo (fiado)", "store_credit": "Crédito na loja"}
METHOD_ORDER = ("cash", "pix", "card_debit", "card_credit", "card", "credit_account", "store_credit")
REFUND_LABELS = {"cash": "Dinheiro", "store_credit": "Crédito na loja", "account": "Abatido do fiado"}
UNIT_LABELS = {"UN": "UN", "KG": "KG", "L": "L"}


def _local(ms):
    return datetime.fromtimestamp(int(ms) / 1000, _receipt_zone())


def when(ms):
    """18/11/2026 08:05"""
    return _local(ms).strftime("%d/%m/%Y %H:%M") if ms else "-"


def clock(ms):
    """08:05"""
    return _local(ms).strftime("%H:%M") if ms else "--:--"


def duration(ms):
    """8h05min / 15min / 0min"""
    minutes = max(0, int(ms or 0)) // 60000
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}min" if hours else f"{minutes}min"


def quantity(milli, unit):
    unit = UNIT_LABELS.get(unit, unit or "UN")
    if unit == "UN" and milli % 1000 == 0:
        return f"{milli // 1000} UN"
    text = f"{milli / 1000:.3f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{text} {unit}"


def method_key_order(key):
    return (METHOD_ORDER.index(key) if key in METHOD_ORDER else len(METHOD_ORDER), key)


def merge(shifts):
    """Sum of several shifts (one register, or the whole day). Open shifts count what they have."""
    total = {"count": len(shifts), "opening_cents": 0, "sales_count": 0, "gross_cents": 0, "discount_cents": 0,
             "returns_count": 0, "returns_cents": 0, "voided_count": 0, "voided_cents": 0, "net_cents": 0,
             "supplied_cents": 0, "withdrawn_cents": 0, "expected_cents": 0, "counted_cents": 0,
             "difference_cents": 0, "paused_ms": 0, "effective_ms": 0, "open": 0, "methods": {}, "credit": {}}
    labels = {}
    for shift in shifts:
        sales = shift["sales"]
        total["opening_cents"] += shift["opening_cents"]
        for key in ("count", "gross_cents", "discount_cents", "returns_count", "returns_cents", "voided_count",
                    "voided_cents", "net_cents"):
            total["sales_count" if key == "count" else key] += sales[key]
        for key in ("supplied_cents", "withdrawn_cents", "expected_cents", "paused_ms", "effective_ms"):
            total[key] += shift[key]
        if shift["counted_cents"] is None:
            total["open"] += 1
        else:
            total["counted_cents"] += shift["counted_cents"]
            total["difference_cents"] += shift["difference_cents"]
        for row in shift["methods"]:
            total["methods"][row["key"]] = total["methods"].get(row["key"], 0) + row["amount_cents"]
            labels[row["key"]] = row["label"]
        for row in shift["credit_received"]:
            total["credit"][row["key"]] = total["credit"].get(row["key"], 0) + row["amount_cents"]
            labels["credit:" + row["key"]] = row["label"]
    total["methods"] = [{"key": key, "label": labels[key], "amount_cents": value}
                        for key, value in sorted(total["methods"].items(), key=lambda item: method_key_order(item[0]))]
    total["credit"] = [{"key": key, "label": labels["credit:" + key], "amount_cents": value}
                       for key, value in sorted(total["credit"].items(), key=lambda item: method_key_order(item[0]))]
    return total


def _minus(cents):
    """Amount taken out: "-12,00", but a plain "0,00" when nothing was."""
    return ("-" if cents else "") + _amount(cents)


def _signed(cents):
    return ("+" if cents > 0 else "") + _amount(cents)


def _difference_label(cents):
    return "Diferença (sobra)" if cents > 0 else "Diferença (falta)" if cents < 0 else "Diferença"


def _title(out, value):
    out.text(value, bold=True)


def _head(out, data, title, subtitle=""):
    out.header(data.get("issuer") or {}, address=False)
    out.text(title, "center", bold=True, tall=True)
    if subtitle:
        out.text(subtitle, "center")
    out.text("NÃO É DOCUMENTO FISCAL", "center")
    out.rule()


def _sales_block(out, sales, net_label="Líquido"):
    _title(out, "VENDAS")
    out.pair("Vendas concluídas", str(sales["count"]))
    out.pair("Bruto", _amount(sales["gross_cents"]))
    out.pair("Descontos", _minus(sales["discount_cents"]))
    out.pair(f"Devoluções ({sales['returns_count']})", _minus(sales["returns_cents"]))
    out.pair(net_label, _amount(sales["net_cents"]), bold=True)
    out.pair(f"Cancelamentos ({sales['voided_count']})", _amount(sales["voided_cents"]))
    out.text("Vendas canceladas não entram no bruto.", small=True)
    out.rule()


def _methods_block(out, methods, title="FORMAS DE PAGAMENTO"):
    _title(out, title)
    for row in methods:
        label = row["label"] + (" (líq. de troco)" if row["key"] == "cash" else "")
        out.pair(label, _amount(row["amount_cents"]))
    out.pair("Total", _amount(sum(row["amount_cents"] for row in methods)), bold=True)
    out.rule()


def _credit_block(out, credit):
    _title(out, "FIADO RECEBIDO")
    if not credit:
        out.text("Nenhum recebimento.")
    for row in credit:
        out.pair(row["label"], _amount(row["amount_cents"]))
    if credit:
        out.pair("Total", _amount(sum(row["amount_cents"] for row in credit)), bold=True)
    out.rule()


def _cash_check(out, opening, supplied, withdrawn, refunds, expected, counted, difference, credit_cash=None):
    _title(out, "CONFERÊNCIA DO DINHEIRO")
    out.pair("Troco inicial", _amount(opening))
    out.pair("Suprimentos", _amount(supplied))
    out.pair("Sangrias", _minus(withdrawn))
    if refunds:
        out.pair("Devoluções em dinheiro", _minus(refunds))
    if credit_cash:
        out.pair("Fiado recebido em dinheiro", _amount(credit_cash))
    out.pair("Esperado", _amount(expected), bold=True)
    if counted is None:
        out.pair("Contado", "turno aberto")
    else:
        out.pair("Contado", _amount(counted), bold=True)
        out.pair(_difference_label(difference), _signed(difference), bold=True)
    out.rule()


def _signatures(out):
    line = "_" * min(out.columns, 36)
    for who in ("Assinatura do operador", "Assinatura do gerente"):
        out.text("")
        out.text("")
        out.text(line, "center")
        out.text(who, "center")


def operator_report(shift, width=80, reprint=False):
    """RELATÓRIO DO OPERADOR: one shift, printed when it is closed (and again on request)."""
    out = _Layout(width)
    _head(out, shift, "RELATÓRIO DO OPERADOR", "Reimpressão" if reprint else "")
    out.pair("Loja", shift.get("shop_name") or "-")
    out.pair("Caixa", shift.get("station_name") or "-")
    out.pair("Operador", shift.get("operator_name") or "-")
    out.pair("Início", when(shift["opened_at"]))
    out.pair("Fim", when(shift["closed_at"]) if shift.get("closed_at") else "turno aberto")
    out.pair("Tempo em pausa", duration(shift["paused_ms"]))
    out.pair("Tempo efetivo", duration(shift["effective_ms"]), bold=True)
    if shift.get("closed_by_name"):
        out.pair("Fechado por", shift["closed_by_name"])
    if shift.get("supervisor_name"):
        out.pair("Autorizado por", shift["supervisor_name"])
    out.rule()
    out.pair("TROCO INICIAL", _amount(shift["opening_cents"]), bold=True)
    out.rule()
    _sales_block(out, shift["sales"])
    _methods_block(out, shift["methods"])
    _title(out, "SUPRIMENTOS E SANGRIAS")
    if not shift["movements"]:
        out.text("Nenhum movimento.")
    for row in shift["movements"]:
        supplied = row["kind"] == "supplied"
        out.pair(f"{clock(row['created_at'])} {'Suprimento' if supplied else 'Sangria'}",
                 ("" if supplied else "-") + _amount(row["amount_cents"]))
        if row.get("reason"):
            out.text(row["reason"])
    out.pair("Total suprimentos", _amount(shift["supplied_cents"]))
    out.pair("Total sangrias", _minus(shift["withdrawn_cents"]))
    out.rule()
    _credit_block(out, shift["credit_received"])
    credit_cash = sum(row["amount_cents"] for row in shift["credit_received"] if row["key"] == "cash")
    _cash_check(out, shift["opening_cents"], shift["supplied_cents"], shift["withdrawn_cents"],
                shift["cash_refunds_cents"], shift["expected_cents"], shift["counted_cents"],
                shift["difference_cents"], credit_cash)
    _title(out, "PAUSAS")
    if not shift["pauses"]:
        out.text("Nenhuma pausa.")
    for row in shift["pauses"]:
        end = clock(row["ended_at"]) if row.get("ended_at") else "agora"
        out.pair(f"{clock(row['started_at'])} às {end}", duration(row["duration_ms"]))
        if row.get("reason"):
            out.text(row["reason"])
    out.pair("Total em pausa", duration(shift["paused_ms"]), bold=True)
    out.rule()
    _signatures(out)
    out.text("")
    out.brand()
    return out.result(report="operator", shift_id=shift["id"])


def station_report(station_name, shifts, width=80, day="", issuer=None, shop_name=""):
    """RELATÓRIO DO CAIXA: every shift of one register in the day, summed."""
    out = _Layout(width)
    _head(out, {"issuer": issuer or {}}, "RELATÓRIO DO CAIXA", day)
    out.pair("Loja", shop_name or "-")
    out.pair("Caixa", station_name or "-")
    total = merge(shifts)
    out.pair("Turnos", str(total["count"]))
    if total["open"]:
        out.text(f"Atenção: {total['open']} turno(s) ainda aberto(s).", bold=True)
    out.rule()
    _title(out, "TURNOS")
    for shift in shifts:
        end = clock(shift["closed_at"]) if shift.get("closed_at") else "aberto"
        out.pair(shift.get("operator_name") or "-", f"{clock(shift['opened_at'])} às {end}")
        if shift["counted_cents"] is not None:
            out.pair(_difference_label(shift["difference_cents"]), _signed(shift["difference_cents"]), indent=2)
    out.rule()
    out.pair("TROCO INICIAL (soma)", _amount(total["opening_cents"]), bold=True)
    out.rule()
    _sales_block(out, {"count": total["sales_count"], "gross_cents": total["gross_cents"],
                       "discount_cents": total["discount_cents"], "returns_count": total["returns_count"],
                       "returns_cents": total["returns_cents"], "net_cents": total["net_cents"],
                       "voided_count": total["voided_count"], "voided_cents": total["voided_cents"]})
    _methods_block(out, total["methods"])
    _credit_block(out, total["credit"])
    credit_cash = sum(row["amount_cents"] for row in total["credit"] if row["key"] == "cash")
    _cash_check(out, total["opening_cents"], total["supplied_cents"], total["withdrawn_cents"],
                sum(shift["cash_refunds_cents"] for shift in shifts), total["expected_cents"],
                None if total["open"] == total["count"] and total["count"] else total["counted_cents"],
                total["difference_cents"], credit_cash)
    out.pair("Tempo em pausa", duration(total["paused_ms"]))
    out.pair("Tempo efetivo", duration(total["effective_ms"]))
    out.rule()
    _signatures(out)
    out.text("")
    out.brand()
    return out.result(report="station")


def day_report(day, shifts, products, cancellations, returns, width=80, issuer=None, shop_name=""):
    """CONSOLIDADO DO DIA: balance, products sold, cancellations and returns of every register."""
    out = _Layout(width)
    _head(out, {"issuer": issuer or {}}, "CONSOLIDADO DO DIA", day)
    out.pair("Loja", shop_name or "-")
    total = merge(shifts)
    registers = sorted({shift.get("station_name") or "-" for shift in shifts})
    out.pair("Caixas", str(len(registers)))
    out.pair("Turnos", str(total["count"]))
    if total["open"]:
        out.text(f"Atenção: {total['open']} turno(s) ainda aberto(s); os valores deles são parciais.", bold=True)
    out.rule()
    _title(out, "BALANÇO")
    out.rule()
    _sales_block(out, {"count": total["sales_count"], "gross_cents": total["gross_cents"],
                       "discount_cents": total["discount_cents"], "returns_count": total["returns_count"],
                       "returns_cents": total["returns_cents"], "net_cents": total["net_cents"],
                       "voided_count": total["voided_count"], "voided_cents": total["voided_cents"]},
                 net_label="Líquido do dia")
    _methods_block(out, total["methods"], "VENDAS POR FORMA DE PAGAMENTO")
    _title(out, "SANGRIAS E SUPRIMENTOS")
    out.pair("Suprimentos", _amount(total["supplied_cents"]))
    out.pair("Sangrias", _minus(total["withdrawn_cents"]))
    out.rule()
    _credit_block(out, total["credit"])
    _title(out, "DINHEIRO")
    out.pair("Esperado (todos os caixas)", _amount(total["expected_cents"]))
    out.pair("Contado (turnos fechados)", _amount(total["counted_cents"]))
    out.pair(_difference_label(total["difference_cents"]), _signed(total["difference_cents"]), bold=True)
    out.rule()
    _title(out, "DIFERENÇAS POR OPERADOR")
    by_operator = {}
    for shift in shifts:
        name = shift.get("operator_name") or "-"
        if shift["counted_cents"] is None:
            by_operator.setdefault(name, None)
        else:
            by_operator[name] = (by_operator.get(name) or 0) + shift["difference_cents"]
    for name in sorted(by_operator):
        value = by_operator[name]
        out.pair(name, "turno aberto" if value is None else _signed(value))
    if not by_operator:
        out.text("Nenhum turno no dia.")
    out.rule()
    _title(out, "PRODUTOS VENDIDOS")
    if not products:
        out.text("Nenhum produto vendido.")
    categories = {}
    for row in products:
        categories.setdefault(row.get("category") or "Sem categoria", []).append(row)
    ranked = sorted(categories.items(), key=lambda item: (-sum(row["amount_cents"] for row in item[1]), item[0]))
    for category, rows in ranked:
        if len(categories) > 1 or category != "Sem categoria":
            out.pair(category.upper(), _amount(sum(row["amount_cents"] for row in rows)), bold=True)
        for row in sorted(rows, key=lambda row: (-row["amount_cents"], row["name"])):
            out.text(row["name"])
            out.pair(quantity(row["quantity_milli"], row["unit"]), _amount(row["amount_cents"]), indent=2)
    if products:
        out.pair("Total dos produtos", _amount(sum(row["amount_cents"] for row in products)), bold=True)
    out.rule()
    _title(out, "CANCELAMENTOS")
    if not cancellations:
        out.text("Nenhuma venda cancelada.")
    for row in cancellations:
        out.pair(f"{clock(row['voided_at'] or row['created_at'])} Venda {str(row['id'])[:8]}",
                 _amount(row["total_cents"]))
        out.text(" · ".join(part for part in (row.get("operator_name"), row.get("reason")) if part))
    out.rule()
    _title(out, "DEVOLUÇÕES")
    if not returns:
        out.text("Nenhuma devolução.")
    for row in returns:
        out.pair(f"{clock(row['created_at'])} {REFUND_LABELS.get(row['refund_method'], row['refund_method'])}",
                 _amount(row["total_cents"]))
        out.text(" · ".join(part for part in (row.get("operator_name"), row.get("reason")) if part))
    out.rule()
    _signatures(out)
    out.text("")
    out.brand()
    return out.result(report="day")


def combine(layouts):
    """Several reports on one paper strip for the browser fallback (the printer gets one job each)."""
    if not layouts:
        return None
    blocks = []
    for index, layout in enumerate(layouts):
        if index:
            blocks += [{"kind": "text", "align": "center", "bold": False, "tall": False, "wide": False,
                        "lines": ["", "- - - corte aqui - - -", ""]}]
        blocks += layout["blocks"]
    return {**layouts[0], "blocks": blocks, "report": "combined"}
