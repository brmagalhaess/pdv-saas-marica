"""Resumo mensal das NFC-e para o contador (Python puro).

Entrada: documentos normalizados (ver cloud/core/accountant/sources.py) e inutilizações.
Saída: dict com totais por situação, por dia, por forma de pagamento (tPag), por CFOP × CST/CSOSN ×
alíquota, bases/valores de ICMS, ST, PIS e COFINS, conferência "totais da nota = soma dos itens" e
pendências (contingência não transmitida, rejeitadas sem correção, lacunas de numeração).
Também gera resumo.csv (`;`, vírgula decimal) e resumo.html imprimível.
"""
import csv
import io
from collections import defaultdict
from html import escape

STATUS_LABELS = {
    "authorized": "Autorizadas", "cancelled": "Canceladas", "inutilized": "Inutilizadas",
    "contingency": "Contingência não transmitida", "pending": "Pendentes de retorno",
    "rejected": "Rejeitadas", "denied": "Denegadas",
}
STATUS_ORDER = ("authorized", "cancelled", "inutilized", "contingency", "pending", "rejected", "denied")

# Meios de pagamento (tPag) — MOC NFC-e e NT 2020.006 / 2023.004.
TPAG = {
    "01": "Dinheiro", "02": "Cheque", "03": "Cartão de crédito", "04": "Cartão de débito",
    "05": "Crédito loja", "10": "Vale alimentação", "11": "Vale refeição", "12": "Vale presente",
    "13": "Vale combustível", "15": "Boleto bancário", "16": "Depósito bancário",
    "17": "PIX dinâmico", "18": "Transferência / carteira digital", "19": "Fidelidade / cashback",
    "20": "PIX estático", "21": "Crédito em loja", "22": "Pagamento eletrônico não informado",
    "90": "Sem pagamento", "99": "Outros",
}


def money(cents):
    """Centavos -> '1.234,56' (padrão brasileiro, para leitura)."""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    whole = f"{cents // 100:,}".replace(",", ".")
    return f"{sign}{whole},{cents % 100:02d}"


def plain(cents):
    """Centavos -> '1234,56' (CSV/SPED: sem separador de milhar)."""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100},{cents % 100:02d}"


def rate_text(value):
    return f"{value:.2f}".replace(".", ",")


def item_cst(item):
    """CST do ICMS como no SPED: origem + CST (3 dígitos) ou o CSOSN (Simples)."""
    icms = item["icms"]
    if icms.get("csosn"):
        return icms["csosn"]
    return (icms.get("origin") or "0")[:1] + (icms.get("cst") or "")


def item_operation_cents(item):
    """VL_OPR do C190: mercadoria − desconto + frete + seguro + outras + ST + FCP-ST + IPI."""
    icms = item["icms"]
    return (item["gross_cents"] - item["discount_cents"] + item["freight_cents"] + item["insurance_cents"]
            + item["other_cents"] + icms.get("st_cents", 0) + icms.get("fcp_st_cents", 0) + item.get("ipi_cents", 0))


def item_reduction_cents(item):
    """VL_RED_BC: parte da operação não tributada pela redução de base (CST 20/70)."""
    icms = item["icms"]
    if icms.get("cst") not in ("20", "70") or not icms.get("bc_cents"):
        return 0
    base = item["gross_cents"] - item["discount_cents"] + item["freight_cents"] + item["insurance_cents"] + item["other_cents"]
    return max(0, base - icms["bc_cents"])


def analytic_rows(nfe):
    """Linhas analíticas (C190) de uma nota: (CST, CFOP, alíquota) -> valores, na ordem de aparição."""
    rows = {}
    for item in nfe["items"]:
        icms = item["icms"]
        rate = icms.get("rate") if icms.get("value_cents") or icms.get("bc_cents") else 0
        key = (item_cst(item), item["cfop"], rate_text(rate or 0))
        row = rows.setdefault(key, {"cst": key[0], "cfop": key[1], "rate": key[2], "operation_cents": 0,
                                    "bc_cents": 0, "icms_cents": 0, "st_bc_cents": 0, "st_cents": 0,
                                    "reduction_cents": 0, "ipi_cents": 0, "items": 0})
        row["operation_cents"] += item_operation_cents(item)
        row["bc_cents"] += icms.get("bc_cents", 0)
        row["icms_cents"] += icms.get("value_cents", 0)
        row["st_bc_cents"] += icms.get("st_bc_cents", 0)
        row["st_cents"] += icms.get("st_cents", 0)
        row["reduction_cents"] += item_reduction_cents(item)
        row["ipi_cents"] += item.get("ipi_cents", 0)
        row["items"] += 1
    return list(rows.values())


def check_totals(nfe):
    """Diferenças entre ICMSTot e a soma dos itens (lista vazia = confere)."""
    items = nfe["items"]
    tot = nfe["totals"]
    sums = {
        "vProd": sum(i["gross_cents"] for i in items),
        "vDesc": sum(i["discount_cents"] for i in items),
        "vFrete": sum(i["freight_cents"] for i in items),
        "vSeg": sum(i["insurance_cents"] for i in items),
        "vOutro": sum(i["other_cents"] for i in items),
        "vBC": sum(i["icms"].get("bc_cents", 0) for i in items),
        "vICMS": sum(i["icms"].get("value_cents", 0) for i in items),
        "vBCST": sum(i["icms"].get("st_bc_cents", 0) for i in items),
        "vST": sum(i["icms"].get("st_cents", 0) for i in items),
        "vPIS": sum(i["pis"]["value_cents"] for i in items),
        "vCOFINS": sum(i["cofins"]["value_cents"] for i in items),
    }
    problems = [f"{name}: nota {money(tot[name])} × itens {money(value)}"
                for name, value in sums.items() if tot.get(name, 0) != value]
    expected_nf = (tot["vProd"] - tot["vDesc"] - tot.get("vICMSDeson", 0) + tot["vST"] + tot.get("vFCPST", 0)
                   + tot["vFrete"] + tot["vSeg"] + tot["vOutro"] + tot.get("vII", 0) + tot.get("vIPI", 0)
                   + tot.get("vIPIDevol", 0))
    if expected_nf != tot["vNF"]:
        problems.append(f"vNF: nota {money(tot['vNF'])} × cálculo {money(expected_nf)}")
    paid = sum(p["amount_cents"] for p in nfe["payments"]) - nfe.get("change_cents", 0)
    if nfe["payments"] and paid != tot["vNF"] and not any(p["type"] == "90" for p in nfe["payments"]):
        problems.append(f"Pagamentos − troco {money(paid)} × vNF {money(tot['vNF'])}")
    return problems


def find_gaps(docs, inutilizations, previous_numbers=None):
    """Faixas de numeração sem nota nem inutilização, por série: [(série, início, fim)].

    previous_numbers: {série: maior número usado antes do mês} para conferir a virada do mês.
    Números usados = autorizadas, canceladas, denegadas, contingência e pendentes (rejeitada não usa
    o número: se ninguém reaproveitou, precisa ser inutilizada).
    """
    used = defaultdict(set)
    for doc in docs:
        if doc["status"] in ("authorized", "cancelled", "denied", "contingency", "pending") and doc.get("number"):
            used[doc.get("series") or 0].add(doc["number"])
    covered = defaultdict(set)
    for inut in inutilizations:
        covered[inut["series"]].update(range(inut["start"], inut["end"] + 1))
    gaps = []
    previous_numbers = previous_numbers or {}
    for series in sorted(set(used) | set(covered)):
        numbers = used.get(series, set())
        known = numbers | covered[series]
        if not known:
            continue
        start = min(known)
        if previous_numbers.get(series):
            start = min(start, previous_numbers[series] + 1)
        missing = [n for n in range(start, max(known) + 1)
                   if n not in numbers and n not in covered[series]]
        run_start = prev = None
        for number in missing:
            if run_start is None:
                run_start = prev = number
            elif number == prev + 1:
                prev = number
            else:
                gaps.append((series, run_start, prev))
                run_start = prev = number
        if run_start is not None:
            gaps.append((series, run_start, prev))
    return gaps


def _bucket():
    return {"count": 0, "total_cents": 0}


def summarize(docs, inutilizations, year, month, company=None, previous_numbers=None):
    by_status = {status: _bucket() for status in STATUS_ORDER}
    by_day = defaultdict(lambda: {"count": 0, "total_cents": 0, "icms_cents": 0})
    by_payment = defaultdict(lambda: {"count": 0, "amount_cents": 0})
    analytic = {}
    taxes = {name: 0 for name in ("vProd", "vDesc", "vFrete", "vSeg", "vOutro", "vBC", "vICMS", "vBCST", "vST",
                                  "vFCP", "vFCPST", "vIPI", "vPIS", "vCOFINS", "vNF", "vTotTrib")}
    items_check = {"operation_cents": 0}
    change_cents = 0
    mismatches, missing_xml, contingency, rejected, pending, others = [], [], [], [], [], []
    for doc in docs:
        nfe = doc.get("nfe")
        status = doc["status"]
        bucket = by_status.setdefault(status, _bucket())
        bucket["count"] += 1
        if nfe:
            bucket["total_cents"] += nfe["totals"]["vNF"]
        label = (f"Série {doc.get('series') or 0} nº {doc['number']}" if doc.get("number")
                 else f"Nota sem número (venda {str(doc.get('sale_id') or '')[:8] or '?'})")
        if status == "contingency":
            contingency.append(f"{label} emitida em contingência em {doc['local_date']:%d/%m/%Y} e ainda não transmitida")
        elif status == "pending":
            pending.append(f"{label} aguardando retorno da SEFAZ")
        elif status == "rejected" and not doc.get("corrected"):
            reason = doc.get("error_message") or "motivo não informado"
            rejected.append(f"{label} rejeitada sem correção ({reason[:160]})")
        for warning in doc.get("warnings", ()):
            others.append(f"{label}: {warning}")
        if status in ("authorized", "cancelled") and not nfe:
            missing_xml.append(f"{label} sem XML arquivado")
        if status != "authorized" or not nfe:
            continue
        problems = check_totals(nfe)
        if problems:
            mismatches.append(f"{label} ({doc.get('key') or ''}): " + "; ".join(problems))
        tot = nfe["totals"]
        for name in taxes:
            taxes[name] += tot.get(name, 0)
        day = by_day[doc["local_date"].isoformat()]
        day["count"] += 1
        day["total_cents"] += tot["vNF"]
        day["icms_cents"] += tot["vICMS"]
        for payment in nfe["payments"]:
            row = by_payment[payment["type"]]
            row["count"] += 1
            row["amount_cents"] += payment["amount_cents"]
        change_cents += nfe.get("change_cents", 0)
        for row in analytic_rows(nfe):
            key = (row["cfop"], row["cst"], row["rate"])
            target = analytic.setdefault(key, {k: (0 if isinstance(v, int) else v) for k, v in row.items()})
            for name, value in row.items():
                if isinstance(value, int):
                    target[name] += value
            items_check["operation_cents"] += row["operation_cents"]
    inut_list = []
    for inut in inutilizations:
        count = inut["end"] - inut["start"] + 1
        by_status["inutilized"]["count"] += count
        inut_list.append({"series": inut["series"], "start": inut["start"], "end": inut["end"],
                          "protocol": inut.get("protocol", ""), "date": inut["local_date"].isoformat()})
    gaps = find_gaps(docs, inutilizations, previous_numbers)
    gap_text = [f"Série {s}: nº {a}" + (f" a {b}" if b != a else "") + " sem nota nem inutilização" for s, a, b in gaps]
    payments = [{"type": code, "label": TPAG.get(code, f"Código {code}"), **values}
                for code, values in sorted(by_payment.items())]
    paid_total = sum(p["amount_cents"] for p in payments) - change_cents
    warnings = {"contingency": contingency, "rejected": rejected, "pending": pending, "gaps": gap_text,
                "missing_xml": missing_xml, "mismatches": mismatches, "other": others}
    return {
        "year": year, "month": month, "company": company or {},
        "by_status": [{"status": s, "label": STATUS_LABELS.get(s, s), **by_status[s]} for s in by_status],
        "by_day": [{"date": d, **v} for d, v in sorted(by_day.items())],
        "by_payment": payments, "change_cents": change_cents, "payments_net_cents": paid_total,
        "by_cfop_cst": [analytic[k] for k in sorted(analytic)],
        "taxes": taxes, "inutilizations": inut_list, "gaps": [list(g) for g in gaps],
        "checks": {
            "items_operation_cents": items_check["operation_cents"],
            "documents_total_cents": taxes["vNF"],
            "analytic_matches_documents": items_check["operation_cents"] == taxes["vNF"],
            "payments_match_documents": paid_total == taxes["vNF"],
            "documents_with_mismatch": len(mismatches),
        },
        "warnings": warnings,
        "warning_count": sum(len(v) for v in warnings.values()),
    }


TAX_LABELS = (("vProd", "Valor dos produtos"), ("vDesc", "Descontos"), ("vFrete", "Frete"), ("vSeg", "Seguro"),
              ("vOutro", "Outras despesas"), ("vNF", "Valor total das notas"), ("vBC", "Base de cálculo do ICMS"),
              ("vICMS", "ICMS"), ("vFCP", "FCP"), ("vBCST", "Base de cálculo do ICMS ST"), ("vST", "ICMS ST"),
              ("vFCPST", "FCP ST"), ("vIPI", "IPI"), ("vPIS", "PIS"), ("vCOFINS", "COFINS"),
              ("vTotTrib", "Tributos aproximados (Lei 12.741)"))
WARNING_LABELS = (("contingency", "Notas em contingência não transmitidas"),
                  ("pending", "Notas aguardando retorno da SEFAZ"),
                  ("rejected", "Rejeitadas sem correção"),
                  ("gaps", "Lacunas de numeração não inutilizadas"),
                  ("missing_xml", "Notas sem XML arquivado"),
                  ("mismatches", "Totais da nota diferentes da soma dos itens"),
                  ("other", "Outros avisos"))


def render_csv(summary):
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    company = summary.get("company", {})
    writer.writerow(["Resumo das NFC-e", f"{summary['month']:02d}/{summary['year']}", company.get("name", ""),
                     company.get("document", "")])
    writer.writerow([])
    writer.writerow(["Situação", "Quantidade", "Valor total (R$)"])
    for row in summary["by_status"]:
        writer.writerow([row["label"], row["count"], plain(row["total_cents"])])
    writer.writerow([])
    writer.writerow(["Dia", "Notas autorizadas", "Valor (R$)", "ICMS (R$)"])
    for row in summary["by_day"]:
        y, m, d = row["date"].split("-")
        writer.writerow([f"{d}/{m}/{y}", row["count"], plain(row["total_cents"]), plain(row["icms_cents"])])
    writer.writerow([])
    writer.writerow(["Forma de pagamento (tPag)", "Descrição", "Pagamentos", "Valor (R$)"])
    for row in summary["by_payment"]:
        writer.writerow([row["type"], row["label"], row["count"], plain(row["amount_cents"])])
    writer.writerow(["", "Troco", "", plain(-summary["change_cents"])])
    writer.writerow(["", "Líquido", "", plain(summary["payments_net_cents"])])
    writer.writerow([])
    writer.writerow(["CFOP", "CST/CSOSN", "Alíquota ICMS (%)", "Valor da operação (R$)", "Base ICMS (R$)",
                     "ICMS (R$)", "Base ICMS ST (R$)", "ICMS ST (R$)", "Redução de base (R$)", "Itens"])
    for row in summary["by_cfop_cst"]:
        writer.writerow([row["cfop"], row["cst"], row["rate"], plain(row["operation_cents"]), plain(row["bc_cents"]),
                         plain(row["icms_cents"]), plain(row["st_bc_cents"]), plain(row["st_cents"]),
                         plain(row["reduction_cents"]), row["items"]])
    writer.writerow([])
    writer.writerow(["Totais das notas autorizadas", "Valor (R$)"])
    for key, label in TAX_LABELS:
        writer.writerow([label, plain(summary["taxes"][key])])
    writer.writerow([])
    writer.writerow(["Inutilizações", "Série", "Início", "Fim", "Data", "Protocolo"])
    for row in summary["inutilizations"]:
        writer.writerow(["", row["series"], row["start"], row["end"], "/".join(reversed(row["date"].split("-"))),
                         row["protocol"]])
    writer.writerow([])
    writer.writerow(["Conferência", "Resultado"])
    checks = summary["checks"]
    writer.writerow(["Soma analítica (CFOP/CST) = total das notas",
                     "OK" if checks["analytic_matches_documents"] else "DIVERGENTE"])
    writer.writerow(["Pagamentos − troco = total das notas", "OK" if checks["payments_match_documents"] else "DIVERGENTE"])
    writer.writerow(["Notas com total diferente da soma dos itens", checks["documents_with_mismatch"]])
    writer.writerow([])
    writer.writerow(["Pendências", "Detalhe"])
    for key, label in WARNING_LABELS:
        for line in summary["warnings"].get(key, ()):
            writer.writerow([label, line])
    if not summary["warning_count"]:
        writer.writerow(["Nenhuma pendência encontrada", ""])
    return "﻿" + out.getvalue()


def _table(headers, rows, numeric_from=1):
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(
        f"<td{' class=n' if index >= numeric_from else ''}>{escape(str(cell))}</td>" for index, cell in enumerate(row))
        + "</tr>" for row in rows)
    if not rows:
        body = f"<tr><td colspan={len(headers)}>Nada no período.</td></tr>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(summary, generated_at="", sped_note=""):
    company = summary.get("company", {})
    period = f"{summary['month']:02d}/{summary['year']}"
    warnings = ""
    for key, label in WARNING_LABELS:
        lines = summary["warnings"].get(key, ())
        if lines:
            warnings += f"<h3>{escape(label)} ({len(lines)})</h3><ul>" + "".join(
                f"<li>{escape(line)}</li>" for line in lines) + "</ul>"
    if not warnings:
        warnings = "<p class=ok>Nenhuma pendência encontrada.</p>"
    checks = summary["checks"]
    parts = [
        "<!doctype html><html lang=pt-BR><head><meta charset=utf-8>",
        f"<title>Resumo NFC-e {period}</title><style>",
        "body{font:13px/1.45 system-ui,Arial,sans-serif;color:#111;margin:24px;max-width:1000px}",
        "h1{font-size:20px;margin:0}h2{font-size:15px;margin:22px 0 6px;border-bottom:1px solid #999}",
        "h3{font-size:13px;margin:10px 0 4px}table{border-collapse:collapse;width:100%;margin:4px 0}",
        "th,td{border:1px solid #bbb;padding:3px 6px;text-align:left}td.n{text-align:right;white-space:nowrap}",
        "th{background:#eee}.ok{color:#075e26}.bad{color:#9b1c1c;font-weight:600}.muted{color:#555}",
        "@media print{body{margin:0}h2{break-after:avoid}table{page-break-inside:auto}}</style></head><body>",
        f"<h1>Resumo das NFC-e — {period}</h1>",
        f"<p>{escape(company.get('name', ''))} · CNPJ {escape(company.get('document', ''))} · "
        f"IE {escape(company.get('state_registration', ''))} · {escape(company.get('regime', ''))}</p>",
        f"<p class=muted>Gerado em {escape(generated_at)} a partir dos XMLs autorizados (a soma confere com os arquivos "
        "das pastas do pacote).</p>",
        "<h2>Pendências</h2>", warnings,
        "<h2>Por situação</h2>",
        _table(["Situação", "Quantidade", "Valor (R$)"],
               [[r["label"], r["count"], money(r["total_cents"])] for r in summary["by_status"]]),
        "<h2>Totais das notas autorizadas</h2>",
        _table(["Campo", "Valor (R$)"], [[label, money(summary["taxes"][key])] for key, label in TAX_LABELS]),
        "<h2>Por CFOP e CST/CSOSN</h2>",
        _table(["CFOP", "CST/CSOSN", "Alíq. %", "Operação", "Base ICMS", "ICMS", "Base ST", "ICMS ST", "Red. base"],
               [[r["cfop"], r["cst"], r["rate"], money(r["operation_cents"]), money(r["bc_cents"]),
                 money(r["icms_cents"]), money(r["st_bc_cents"]), money(r["st_cents"]),
                 money(r["reduction_cents"])] for r in summary["by_cfop_cst"]], numeric_from=2),
        "<h2>Por forma de pagamento</h2>",
        _table(["tPag", "Descrição", "Pagamentos", "Valor (R$)"],
               [[r["type"], r["label"], r["count"], money(r["amount_cents"])] for r in summary["by_payment"]]
               + [["", "Troco", "", money(-summary["change_cents"])],
                  ["", "Líquido", "", money(summary["payments_net_cents"])]], numeric_from=2),
        "<h2>Por dia</h2>",
        _table(["Dia", "Notas", "Valor (R$)", "ICMS (R$)"],
               [["/".join(reversed(r["date"].split("-"))), r["count"], money(r["total_cents"]), money(r["icms_cents"])]
                for r in summary["by_day"]]),
        "<h2>Inutilizações</h2>",
        _table(["Série", "Início", "Fim", "Data", "Protocolo"],
               [[r["series"], r["start"], r["end"], "/".join(reversed(r["date"].split("-"))), r["protocol"]]
                for r in summary["inutilizations"]], numeric_from=5),
        "<h2>Conferência</h2><ul>",
        f"<li>Soma analítica (CFOP/CST) = total das notas: "
        f"{'<b class=ok>OK</b>' if checks['analytic_matches_documents'] else '<b class=bad>DIVERGENTE</b>'}</li>",
        f"<li>Pagamentos − troco = total das notas: "
        f"{'<b class=ok>OK</b>' if checks['payments_match_documents'] else '<b class=bad>DIVERGENTE</b>'}</li>",
        f"<li>Notas com total diferente da soma dos itens: {checks['documents_with_mismatch']}</li></ul>",
    ]
    if sped_note:
        parts.append(f"<h2>SPED Fiscal</h2><p>{escape(sped_note)}</p>")
    parts.append("</body></html>")
    return "".join(parts)
