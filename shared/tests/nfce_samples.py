"""Geradores de XMLs realistas de NFC-e (nfeProc, procEventoNFe, procInutNFe) para os testes.

Os valores são calculados como a SEFAZ confere: vProd = qCom × vUnCom, ICMSTot = soma dos itens,
vNF = vProd − vDesc + vST + vFCPST + vFrete + vSeg + vOutro + vIPI. Nada aqui é assinado.
"""
from decimal import ROUND_HALF_UP, Decimal

NSURI = "http://www.portalfiscal.inf.br/nfe"
CNPJ = "11222333000181"
IE = "110042490114"
UF_CODES = {"SP": "35", "RJ": "33", "MG": "31", "AM": "13", "PR": "41"}


def _money(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _fmt(value):
    return f"{_money(value):.2f}"


def check_digit(key43):
    total, weight = 0, 2
    for digit in reversed(key43):
        total += int(digit) * weight
        weight = 2 if weight == 9 else weight + 1
    rest = total % 11
    return "0" if rest < 2 else str(11 - rest)


def access_key(issued, number, series=1, cnpj=CNPJ, uf="SP", model=65, emission_type=1, code=None):
    yymm = issued[2:4] + issued[5:7]
    code = code if code is not None else f"{(number * 7919) % 10**8:08d}"
    key43 = f"{UF_CODES[uf]}{yymm}{cnpj}{model:02d}{series:03d}{number:09d}{emission_type}{code}"
    return key43 + check_digit(key43)


DEFAULT_ITEMS = (
    {"code": "REFRI", "gtin": "7891000315507", "description": "Refrigerante 2L", "ncm": "22021000",
     "cest": "0300700", "cfop": "5405", "unit": "UN", "qty": "2", "price": "8.99", "cst": "60"},
    {"code": "ARROZ", "gtin": "SEM GTIN", "description": "Arroz tipo 1 5kg", "ncm": "10063021",
     "cfop": "5102", "unit": "UN", "qty": "1", "price": "27.90", "cst": "00", "rate": "18"},
    {"code": "BANANA", "gtin": "SEM GTIN", "description": "Banana prata", "ncm": "08039000",
     "cfop": "5102", "unit": "KG", "qty": "1.235", "price": "6.49", "cst": "40"},
)


def _item_values(spec):
    qty = Decimal(spec.get("qty", "1"))
    price = Decimal(spec.get("price", "1"))
    gross = _money(qty * price)
    discount = _money(spec.get("discount", "0"))
    base = gross - discount
    cst = spec.get("cst", "")
    csosn = spec.get("csosn", "")
    rate = Decimal(spec.get("rate", "0"))
    reduction = Decimal(spec.get("reduction", "0"))
    icms_bc = icms = Decimal("0")
    if cst in ("00", "20") and rate:
        icms_bc = _money(base * (1 - reduction / 100)) if cst == "20" else base
        icms = _money(icms_bc * rate / 100)
    pis_cst = spec.get("pis_cst", "01" if cst else "49")
    cofins_cst = spec.get("cofins_cst", pis_cst)
    pis_rate = Decimal(spec.get("pis_rate", "0.65" if pis_cst == "01" else "0"))
    cofins_rate = Decimal(spec.get("cofins_rate", "3.00" if cofins_cst == "01" else "0"))
    pis_bc = base if pis_cst == "01" else Decimal("0")
    return {"qty": qty, "price": price, "gross": gross, "discount": discount, "base": base, "cst": cst,
            "csosn": csosn, "rate": rate, "reduction": reduction, "icms_bc": icms_bc, "icms": icms,
            "pis_cst": pis_cst, "cofins_cst": cofins_cst, "pis_rate": pis_rate, "cofins_rate": cofins_rate,
            "pis_bc": pis_bc, "pis": _money(pis_bc * pis_rate / 100), "cofins": _money(pis_bc * cofins_rate / 100)}


def _det(index, spec, values):
    origin = spec.get("origin", "0")
    if values["csosn"]:
        icms = (f"<ICMSSN102><orig>{origin}</orig><CSOSN>{values['csosn']}</CSOSN></ICMSSN102>"
                if values["csosn"] != "500" else
                f"<ICMSSN500><orig>{origin}</orig><CSOSN>500</CSOSN></ICMSSN500>")
    elif values["cst"] == "00":
        icms = (f"<ICMS00><orig>{origin}</orig><CST>00</CST><modBC>3</modBC><vBC>{_fmt(values['icms_bc'])}</vBC>"
                f"<pICMS>{values['rate']:.2f}</pICMS><vICMS>{_fmt(values['icms'])}</vICMS></ICMS00>")
    elif values["cst"] == "20":
        icms = (f"<ICMS20><orig>{origin}</orig><CST>20</CST><modBC>3</modBC><pRedBC>{values['reduction']:.2f}</pRedBC>"
                f"<vBC>{_fmt(values['icms_bc'])}</vBC><pICMS>{values['rate']:.2f}</pICMS>"
                f"<vICMS>{_fmt(values['icms'])}</vICMS></ICMS20>")
    elif values["cst"] == "60":
        icms = f"<ICMS60><orig>{origin}</orig><CST>60</CST></ICMS60>"
    else:
        icms = f"<ICMS40><orig>{origin}</orig><CST>{values['cst'] or '40'}</CST></ICMS40>"
    if values["pis_cst"] == "01":
        pis = (f"<PISAliq><CST>01</CST><vBC>{_fmt(values['pis_bc'])}</vBC><pPIS>{values['pis_rate']:.4f}</pPIS>"
               f"<vPIS>{_fmt(values['pis'])}</vPIS></PISAliq>")
        cofins = (f"<COFINSAliq><CST>01</CST><vBC>{_fmt(values['pis_bc'])}</vBC>"
                  f"<pCOFINS>{values['cofins_rate']:.4f}</pCOFINS><vCOFINS>{_fmt(values['cofins'])}</vCOFINS></COFINSAliq>")
    else:
        pis = (f"<PISOutr><CST>{values['pis_cst']}</CST><vBC>0.00</vBC><pPIS>0.0000</pPIS><vPIS>0.00</vPIS></PISOutr>")
        cofins = (f"<COFINSOutr><CST>{values['cofins_cst']}</CST><vBC>0.00</vBC><pCOFINS>0.0000</pCOFINS>"
                  f"<vCOFINS>0.00</vCOFINS></COFINSOutr>")
    cest = f"<CEST>{spec['cest']}</CEST>" if spec.get("cest") else ""
    discount = f"<vDesc>{_fmt(values['discount'])}</vDesc>" if values["discount"] else ""
    gtin = spec.get("gtin", "SEM GTIN")
    return (f'<det nItem="{index}"><prod><cProd>{spec["code"]}</cProd><cEAN>{gtin}</cEAN>'
            f'<xProd>{spec["description"]}</xProd><NCM>{spec["ncm"]}</NCM>{cest}<CFOP>{spec["cfop"]}</CFOP>'
            f'<uCom>{spec.get("unit", "UN")}</uCom><qCom>{values["qty"]:.4f}</qCom><vUnCom>{values["price"]:.10f}</vUnCom>'
            f'<vProd>{_fmt(values["gross"])}</vProd><cEANTrib>{gtin}</cEANTrib><uTrib>{spec.get("unit", "UN")}</uTrib>'
            f'<qTrib>{values["qty"]:.4f}</qTrib><vUnTrib>{values["price"]:.10f}</vUnTrib>{discount}<indTot>1</indTot></prod>'
            f'<imposto><vTotTrib>0.00</vTotTrib><ICMS>{icms}</ICMS><PIS>{pis}</PIS><COFINS>{cofins}</COFINS></imposto></det>')


def nfce(number, issued="2026-10-15T10:30:00-03:00", series=1, items=DEFAULT_ITEMS, payments=None,
         change="0", emission_type=1, protocol=True, status="100", cnpj=CNPJ, uf="SP", crt=3, tp_amb=1,
         customer_cpf=None):
    """nfeProc completo (NFe + protNFe). protocol=False devolve só a NFe (contingência não transmitida)."""
    key = access_key(issued, number, series, cnpj, uf, 65, emission_type)
    values = [_item_values(spec) for spec in items]
    dets = "".join(_det(index + 1, spec, value) for index, (spec, value) in enumerate(zip(items, values)))
    total = lambda name: sum((v[name] for v in values), Decimal("0"))  # noqa: E731
    v_prod, v_desc = total("gross"), total("discount")
    v_nf = v_prod - v_desc
    payments = payments or [("01", v_nf + Decimal(change))]
    pag = "".join(f"<detPag><tPag>{kind}</tPag><vPag>{_fmt(amount)}</vPag></detPag>" for kind, amount in payments)
    if Decimal(change):
        pag += f"<vTroco>{_fmt(change)}</vTroco>"
    dest = f"<dest><CPF>{customer_cpf}</CPF><indIEDest>9</indIEDest></dest>" if customer_cpf else ""
    contingency = ("<dhCont>" + issued + "</dhCont><xJust>Sem comunicacao com a SEFAZ no momento</xJust>"
                   if emission_type == 9 else "")
    nfe = (f'<NFe xmlns="{NSURI}"><infNFe Id="NFe{key}" versao="4.00"><ide><cUF>{UF_CODES[uf]}</cUF>'
           f'<cNF>{key[35:43]}</cNF><natOp>VENDA</natOp><mod>65</mod><serie>{series}</serie><nNF>{number}</nNF>'
           f'<dhEmi>{issued}</dhEmi><tpNF>1</tpNF><idDest>1</idDest><cMunFG>3550308</cMunFG><tpImp>4</tpImp>'
           f'<tpEmis>{emission_type}</tpEmis><cDV>{key[-1]}</cDV><tpAmb>{tp_amb}</tpAmb><finNFe>1</finNFe>'
           f'<indFinal>1</indFinal><indPres>1</indPres><procEmi>0</procEmi><verProc>PDV2</verProc>{contingency}</ide>'
           f'<emit><CNPJ>{cnpj}</CNPJ><xNome>Mercado Fiscal Ltda</xNome><enderEmit><xLgr>Av. Paulista</xLgr>'
           f'<nro>1000</nro><xBairro>Bela Vista</xBairro><cMun>3550308</cMun><xMun>Sao Paulo</xMun><UF>{uf}</UF>'
           f'<CEP>01310100</CEP></enderEmit><IE>{IE}</IE><CRT>{crt}</CRT></emit>{dest}{dets}'
           f'<total><ICMSTot><vBC>{_fmt(total("icms_bc"))}</vBC><vICMS>{_fmt(total("icms"))}</vICMS>'
           f'<vICMSDeson>0.00</vICMSDeson><vFCP>0.00</vFCP><vBCST>0.00</vBCST><vST>0.00</vST><vFCPST>0.00</vFCPST>'
           f'<vFCPSTRet>0.00</vFCPSTRet><vProd>{_fmt(v_prod)}</vProd><vFrete>0.00</vFrete><vSeg>0.00</vSeg>'
           f'<vDesc>{_fmt(v_desc)}</vDesc><vII>0.00</vII><vIPI>0.00</vIPI><vIPIDevol>0.00</vIPIDevol>'
           f'<vPIS>{_fmt(total("pis"))}</vPIS><vCOFINS>{_fmt(total("cofins"))}</vCOFINS><vOutro>0.00</vOutro>'
           f'<vNF>{_fmt(v_nf)}</vNF><vTotTrib>0.00</vTotTrib></ICMSTot></total>'
           f'<transp><modFrete>9</modFrete></transp><pag>{pag}</pag></infNFe></NFe>')
    if not protocol:
        return ('<?xml version="1.0" encoding="UTF-8"?>' + nfe).encode()
    prot = (f'<protNFe versao="4.00"><infProt><tpAmb>{tp_amb}</tpAmb><verAplic>SP_NFCE</verAplic><chNFe>{key}</chNFe>'
            f'<dhRecbto>{issued}</dhRecbto><nProt>135{number:012d}</nProt><digVal>x</digVal><cStat>{status}</cStat>'
            f'<xMotivo>Autorizado o uso da NF-e</xMotivo></infProt></protNFe>')
    return (f'<?xml version="1.0" encoding="UTF-8"?><nfeProc xmlns="{NSURI}" versao="4.00">{nfe}{prot}</nfeProc>').encode()


def key_of(xml):
    text = xml.decode()
    start = text.index('Id="NFe') + 7
    return text[start:start + 44]


def cancel_event(key, when="2026-10-15T10:45:00-03:00", status="135", cnpj=CNPJ, tp_amb=1):
    return (f'<?xml version="1.0" encoding="UTF-8"?><procEventoNFe xmlns="{NSURI}" versao="1.00">'
            f'<evento versao="1.00"><infEvento Id="ID110111{key}01"><cOrgao>35</cOrgao><tpAmb>{tp_amb}</tpAmb>'
            f'<CNPJ>{cnpj}</CNPJ><chNFe>{key}</chNFe><dhEvento>{when}</dhEvento><tpEvento>110111</tpEvento>'
            f'<nSeqEvento>1</nSeqEvento><verEvento>1.00</verEvento><detEvento versao="1.00">'
            f'<descEvento>Cancelamento</descEvento><nProt>135000000000001</nProt>'
            f'<xJust>Cliente desistiu da compra no caixa</xJust></detEvento></infEvento></evento>'
            f'<retEvento versao="1.00"><infEvento><tpAmb>{tp_amb}</tpAmb><cStat>{status}</cStat>'
            f'<xMotivo>Evento registrado e vinculado a NF-e</xMotivo><chNFe>{key}</chNFe><tpEvento>110111</tpEvento>'
            f'<nSeqEvento>1</nSeqEvento><dhRegEvento>{when}</dhRegEvento><nProt>135999000000001</nProt>'
            f'</infEvento></retEvento></procEventoNFe>').encode()


def inutilization(start, end, series=1, when="2026-10-20T09:00:00-03:00", cnpj=CNPJ, year="26", status="102", tp_amb=1):
    return (f'<?xml version="1.0" encoding="UTF-8"?><procInutNFe xmlns="{NSURI}" versao="4.00">'
            f'<inutNFe versao="4.00"><infInut Id="ID35{year}{cnpj}65{series:03d}{start:09d}{end:09d}">'
            f'<tpAmb>{tp_amb}</tpAmb><xServ>INUTILIZAR</xServ><cUF>35</cUF><ano>{year}</ano><CNPJ>{cnpj}</CNPJ>'
            f'<mod>65</mod><serie>{series}</serie><nNFIni>{start}</nNFIni><nNFFin>{end}</nNFFin>'
            f'<xJust>Falha no sistema pulou a numeracao</xJust></infInut></inutNFe>'
            f'<retInutNFe versao="4.00"><infInut><tpAmb>{tp_amb}</tpAmb><cStat>{status}</cStat>'
            f'<xMotivo>Inutilizacao de numero homologado</xMotivo><cUF>35</cUF><ano>{year}</ano><CNPJ>{cnpj}</CNPJ>'
            f'<mod>65</mod><serie>{series}</serie><nNFIni>{start}</nNFIni><nNFFin>{end}</nNFFin>'
            f'<dhRecbto>{when}</dhRecbto><nProt>135000000777</nProt></infInut></retInutNFe></procInutNFe>').encode()
