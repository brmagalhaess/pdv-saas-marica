"""Geração do pacote mensal do contador (ZIP) e do SPED Fiscal, envio por e-mail e links seguros."""
import hashlib
import io
import logging
import secrets
import zipfile
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone

from shared.fiscal import efd, month_report
from shared.fiscal.data import CRT_REGIME

from . import sources
from .models import AccountantDelivery, AccountantPackage, AccountantSettings

logger = logging.getLogger(__name__)

LINK_DAYS = 30          # validade do link enviado ao contador
RETENTION_DAYS = 35     # o ZIP fica guardado; depois disso é apagado (pode ser gerado de novo)
REGIME_TEXT = {1: "Simples Nacional", 2: "Simples Nacional (excesso de sublimite)", 3: "Regime Normal",
               4: "MEI"}
PVA_NOTE = ("O arquivo do SPED Fiscal (EFD ICMS/IPI) é gerado só com as saídas por NFC-e. Antes da entrega, "
            "o contador precisa completá-lo (entradas, inventário, ajustes) e validá-lo no PVA da Receita Federal.")


def settings_for(tenant):
    row = AccountantSettings.objects.filter(tenant=tenant).first()
    return row or AccountantSettings(tenant=tenant)


def sped_policy(tenant, forced=False):
    """(gerar?, aviso) conforme o regime. CRT 3 gera; Simples/MEI só quando pedido, com aviso."""
    config = settings_for(tenant)
    if tenant.crt == 3:
        return True, ""
    notice = ("Empresa do Simples Nacional/MEI: em regra dispensada da EFD ICMS/IPI, salvo exigência da UF "
              "(confirme com o contador).")
    return bool(forced or config.sped_for_simples), notice


def company_data(tenant):
    return {"name": tenant.name, "trade_name": tenant.trade_name, "cnpj": tenant.document, "document": tenant.document,
            "uf": tenant.address_state, "ie": tenant.state_registration, "city_ibge": tenant.city_ibge_code,
            "im": tenant.municipal_registration, "cep": tenant.address_zip, "address": tenant.address_line,
            "number": tenant.address_number, "complement": tenant.address_complement,
            "neighborhood": tenant.address_neighborhood, "state_registration": tenant.state_registration,
            "regime": REGIME_TEXT.get(tenant.crt, CRT_REGIME.get(tenant.crt, "")), "crt": tenant.crt}


def accountant_data(tenant):
    config = settings_for(tenant)
    return {"name": config.name, "cpf": config.cpf, "crc": config.crc, "cnpj": config.cnpj, "cep": config.cep,
            "address": config.address, "number": config.number, "complement": config.complement,
            "neighborhood": config.neighborhood, "phone": config.phone, "email": tenant.accountant_email,
            "city_ibge": config.city_ibge}


def build_sped(tenant, year, month, docs, inutilizations):
    config = settings_for(tenant)
    options = {"e116": {"cod_or": config.e116_cod_or or "000", "cod_rec": config.e116_cod_rec,
                        "due_day": config.e116_due_day}}
    return efd.build(company_data(tenant), accountant_data(tenant), year, month, docs, inutilizations,
                     profile=config.sped_profile or "A", options=options)


def readme(tenant, year, month, summary, sped, sped_notice, base):
    period = f"{month:02d}/{year}"
    counts = {row["status"]: row["count"] for row in summary["by_status"]}
    lines = [
        f"PACOTE DO CONTADOR - NFC-e {period}",
        f"Empresa: {tenant.name} - CNPJ {tenant.document} - IE {tenant.state_registration}",
        f"Regime: {REGIME_TEXT.get(tenant.crt, '')} (CRT {tenant.crt})",
        f"Gerado em: {timezone.localtime():%d/%m/%Y %H:%M} pelo PDV2",
        "",
        "CONTEUDO",
        f"  {base}autorizadas/   XML de cada NFC-e autorizada no mes (nfeProc = NFe + protocolo). "
        f"{counts.get('authorized', 0)} arquivo(s).",
        f"  {base}canceladas/    NFC-e canceladas: XML da nota e XML do evento de cancelamento (procEventoNFe). "
        f"{counts.get('cancelled', 0)} nota(s).",
        f"  {base}inutilizadas/  XML das inutilizacoes de numeracao homologadas no mes (procInutNFe).",
        "  resumo.csv      Totais por situacao, dia, forma de pagamento (tPag), CFOP x CST/CSOSN, bases e valores",
        "                  de ICMS/ST/PIS/COFINS, inutilizacoes, conferencia e pendencias (separador ';').",
        "  resumo.html     O mesmo resumo para abrir no navegador e imprimir.",
    ]
    if sped:
        lines.append(f"  sped/           EFD ICMS/IPI (SPED Fiscal), leiaute {sped['version']}, Guia Pratico {sped['guide']}.")
    lines += [
        "",
        "COMO FOI CALCULADO",
        "  Os totais vem dos proprios XMLs (grupo ICMSTot e itens), na data de emissao (dhEmi) do fuso do emitente.",
        "  A conferencia confirma que a soma dos itens bate com o total de cada nota e que a soma analitica",
        "  (CFOP x CST x aliquota) bate com o total do mes.",
        "",
        "PENDENCIAS",
    ]
    if summary["warning_count"]:
        for key, label in month_report.WARNING_LABELS:
            for item in summary["warnings"].get(key, ()):
                lines.append(f"  - {label}: {item}")
    else:
        lines.append("  Nenhuma pendencia encontrada.")
    lines += ["", "SPED FISCAL (EFD ICMS/IPI)"]
    if sped:
        lines += [
            "  Contem somente as saidas por NFC-e (C100 + C190 por nota; canceladas com COD_SIT 02 e chave) e a",
            "  apuracao do ICMS (E100/E110/E116) calculada a partir dos C190. NAO contem entradas, CT-e, inventario",
            "  (bloco H), producao (bloco K) nem ajustes. O contador deve completar e VALIDAR NO PVA da Receita",
            "  Federal antes de transmitir.",
        ]
        for warning in sped["warnings"]:
            lines.append(f"  Aviso: {warning}")
        for problem in sped["problems"]:
            lines.append(f"  Conferencia interna: {problem}")
    else:
        lines.append("  Nao gerado. " + (sped_notice or ""))
    if sped_notice and sped:
        lines.append("  " + sped_notice)
    lines += ["", "Duvidas sobre o pacote: suporte do PDV2 (" + settings.PDV_SUPPORT_EMAIL + ")."]
    return "\r\n".join(lines) + "\r\n"


def _safe(text):
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in str(text or ""))[:60] or "sem-numero"


def build_zip(tenant, year, month, docs, inutilizations, summary, sped, sped_notice):
    cnpj = "".join(c for c in tenant.document if c.isalnum()) or "sem-cnpj"
    base = f"pacote-contador-{cnpj}-{year}-{month:02d}/"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        def put(name, data):
            info = zipfile.ZipInfo(base + name, date_time=timezone.localtime().timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
        archive.writestr(zipfile.ZipInfo(base + "autorizadas/"), b"")
        archive.writestr(zipfile.ZipInfo(base + "canceladas/"), b"")
        archive.writestr(zipfile.ZipInfo(base + "inutilizadas/"), b"")
        for doc in docs:
            name = _safe(doc["key"] or f"serie{doc['series']}-n{doc['number']}")
            if doc["status"] == "authorized" and doc.get("xml"):
                put(f"autorizadas/{name}-nfce.xml", doc["xml"])
            elif doc["status"] == "cancelled":
                if doc.get("xml"):
                    put(f"canceladas/{name}-nfce.xml", doc["xml"])
                if doc.get("cancel_xml"):
                    put(f"canceladas/{name}-cancelamento.xml", doc["cancel_xml"])
        for inut in inutilizations:
            put(f"inutilizadas/{cnpj}-serie{inut['series']}-{inut['start']}-{inut['end']}-inutilizacao.xml", inut["xml"])
        put("resumo.csv", month_report.render_csv(summary).encode("utf-8"))
        generated = timezone.localtime().strftime("%d/%m/%Y %H:%M")
        put("resumo.html", month_report.render_html(summary, generated, PVA_NOTE if sped else sped_notice).encode("utf-8"))
        if sped:
            put(f"sped/SPED-EFD-ICMS-IPI-{cnpj}-{year}{month:02d}.txt", sped["data"])
        put("LEIA-ME.txt", readme(tenant, year, month, summary, sped, sped_notice, "").encode("utf-8"))
    return buffer.getvalue(), base.rstrip("/") + ".zip"


def generate(package):
    """Monta o ZIP do pacote e grava. Devolve o pacote atualizado."""
    tenant = package.tenant
    AccountantPackage.objects.filter(pk=package.pk).update(status="running", error="")
    try:
        docs = sources.iter_month(tenant, package.year, package.month)
        inutilizations = sources.inutilizations_month(tenant, package.year, package.month)
        previous = sources.previous_numbers(tenant, package.year, package.month)
        summary = month_report.summarize(docs, inutilizations, package.year, package.month,
                                         company_data(tenant), previous)
        wanted, notice = sped_policy(tenant, package.sped_forced)
        sped = build_sped(tenant, package.year, package.month, docs, inutilizations) if (
            package.include_sped and wanted) else None
        content, filename = build_zip(tenant, package.year, package.month, docs, inutilizations, summary, sped, notice)
    except Exception as exc:
        logger.exception("accountant package %s failed", package.pk)
        message = str(exc) if isinstance(exc, ValueError) else "Falha interna ao gerar o pacote; consulte o suporte"
        AccountantPackage.objects.filter(pk=package.pk).update(status="failed", error=message[:500])
        raise ValueError(message)
    now = timezone.now()
    brief = {key: summary[key] for key in ("by_status", "taxes", "checks", "gaps", "warning_count", "warnings")}
    brief["sped"] = ({"generated": True, "version": sped["version"], "guide": sped["guide"],
                      "warnings": sped["warnings"], "problems": sped["problems"], "e110": sped["e110"],
                      "lines": len(sped["lines"])} if sped else {"generated": False, "notice": notice})
    package.content = content
    package.content_sha256 = hashlib.sha256(content).hexdigest()
    package.size_bytes = len(content)
    package.filename = filename
    package.sped_content = sped["data"] if sped else None
    package.summary = brief
    package.status = "ready"
    package.error = ""
    package.ready_at = now
    package.expires_at = now + timedelta(days=RETENTION_DAYS)
    package.save()
    return package


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def download_url(token):
    return f"{settings.PDV_PUBLIC_URL}/api/v1/accountant/download/{token}"


def send(delivery):
    """Envia o e-mail com link seguro (sem anexo). Grava sucesso ou falha no próprio envio."""
    package = delivery.package
    tenant = package.tenant
    if package.status != "ready" or not package.content:
        raise ValueError("O pacote não está disponível; gere novamente antes de enviar")
    token = secrets.token_urlsafe(32)
    now = timezone.now()
    expires = now + timedelta(days=LINK_DAYS)
    period = f"{package.month:02d}/{package.year}"
    summary = package.summary or {}
    counts = {row["status"]: row for row in summary.get("by_status", [])}
    authorized = counts.get("authorized", {"count": 0, "total_cents": 0})
    body = [
        "Olá,", "",
        f"O pacote mensal das NFC-e de {period} da empresa {tenant.name} (CNPJ {tenant.document}) está pronto.", "",
        f"Baixar o pacote (ZIP, {max(1, package.size_bytes // 1024)} KB):", download_url(token), "",
        f"O link é pessoal e vale até {timezone.localtime(expires):%d/%m/%Y}. Não o encaminhe.", "",
        f"Notas autorizadas: {authorized['count']} — R$ {month_report.money(authorized['total_cents'])}",
        f"Canceladas: {counts.get('cancelled', {}).get('count', 0)} · "
        f"Inutilizadas: {counts.get('inutilized', {}).get('count', 0)}",
        f"Pendências apontadas no resumo: {summary.get('warning_count', 0)}",
    ]
    if (summary.get("sped") or {}).get("generated"):
        body += ["", "O ZIP traz o SPED Fiscal (EFD ICMS/IPI) só com as saídas por NFC-e. "
                 "Complete e valide o arquivo no PVA da Receita Federal antes da entrega."]
    body += ["", "Conteúdo: XMLs autorizados, cancelados (com o evento) e inutilizados, resumo.csv, resumo.html e "
             "LEIA-ME.txt.", "", "PDV2 · CrownTech"]
    message = EmailMessage(f"PDV2: pacote do contador {period} — {tenant.name}", "\n".join(body),
                           None, [delivery.to_email])
    try:
        message.send(fail_silently=False)
    except Exception as exc:  # noqa: BLE001 - the failure is recorded and shown in the panel
        logger.warning("accountant e-mail %s failed: %s", delivery.pk, type(exc).__name__)
        reason = f"Falha ao enviar e-mail ({type(exc).__name__}). Confira o servidor de e-mail e reenvie."
        AccountantDelivery.objects.filter(pk=delivery.pk).update(status="failed", error=reason[:500])
        raise ValueError(reason)
    with transaction.atomic():
        AccountantDelivery.objects.filter(pk=delivery.pk).update(
            status="sent", error="", token_hash=_hash(token), link_expires_at=expires, sent_at=now)
        if not package.expires_at or package.expires_at < expires:
            AccountantPackage.objects.filter(pk=package.pk).update(expires_at=expires)
    return token


def find_by_token(token):
    """Envio válido para o token (link não expirado e ZIP ainda guardado) ou None."""
    if not token or len(token) > 100:
        return None
    delivery = (AccountantDelivery.objects.select_related("package", "tenant")
                .filter(token_hash=_hash(token), status="sent").first())
    if not delivery or not delivery.link_expires_at or delivery.link_expires_at <= timezone.now():
        return None
    if delivery.package.status != "ready" or not delivery.package.content:
        return None
    return delivery


def purge_expired(now=None):
    """Apaga o ZIP dos pacotes vencidos (o banco continua com os XMLs; basta gerar de novo)."""
    now = now or timezone.now()
    return AccountantPackage.objects.filter(status="ready", expires_at__lt=now).update(
        status="expired", content=None, sped_content=None)
