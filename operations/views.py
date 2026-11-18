import os
from datetime import date
from decimal import Decimal,InvalidOperation
from pathlib import Path
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse,HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from core.api_common import endpoint,APIError,method,payload,fields,text,uuid_value,membership_context,platform_user,audit
from core.models import Plan,Sale,BillingEvent,FiscalDocument,Tenant,Terminal,Subscription,BillingInvoice
from shared.secrets import encrypt_secret
from shared.licensing import payload_digest
from integrations.billing import verify_webhook_token
from .models import IntegrationAccount,ProviderJob,FiscalPayload,BillingBinding
from .services import enqueue,JobConflict,capacity_fits
from core.fiscal import company_data,company_status,tech_responsible
from shared.fiscal.data import CRT_REGIME,REGIME_CRT,SPECIAL_TAX_REGIMES,UF_CODES
from shared.fiscal.validators import (cnpj_value,digits as digits_only,ie_value,valid_cnpj,valid_cpf,
                                      valid_ibge_city,valid_ie)

OWNER=('owner','admin')

@endpoint
def company(request):
    method(request,'GET','PATCH');member=membership_context(request,OWNER,write=request.method=='PATCH')
    tenant=member.tenant
    if request.method=='PATCH':
        data=payload(request);fields(data,('name','trade_name','document','state_registration',
            'municipal_registration','address_line','address_number','address_complement',
            'address_neighborhood','address_city','address_state','address_zip','tax_regime',
            'fiscal_environment','nfce_series','nfce_next_number','max_discount_bps','business_hours',
            'crt','cnae','special_tax_regime','city_ibge_code','simples_credit_rate','approx_tax_fallback_rate'))
        if not data:raise APIError('Informe os dados a alterar')
        if 'name' in data:tenant.name=text(data['name'],'Razão social',160)
        if 'trade_name' in data:tenant.trade_name=text(data['trade_name'],'Nome fantasia',160,optional=True)
        if 'document' in data:
            value=text(data['document'],'Documento',30,optional=True)
            code=cnpj_value(value)
            if value and (any(c not in '0123456789./- ' for c in value.upper() if not c.isalpha())
                          or not (valid_cnpj(code) or (len(code)==11 and valid_cpf(code)))):
                raise APIError('CNPJ ou CPF inválido: confira os dígitos verificadores')
            tenant.document=code
        for field,label,limit in (('municipal_registration','Inscrição municipal',30),
                                   ('address_line','Logradouro',180),('address_number','Número',20),
                                   ('address_complement','Complemento',80),('address_neighborhood','Bairro',100),
                                   ('address_city','Cidade',100)):
            if field in data:setattr(tenant,field,text(data[field],label,limit,optional=True))
        if 'address_state' in data:
            value=text(data['address_state'],'UF',2,optional=True).upper()
            if value and value not in UF_CODES:raise APIError('Informe uma UF válida com duas letras')
            tenant.address_state=value
        if 'state_registration' in data:
            value=text(data['state_registration'],'Inscrição estadual',30,optional=True)
            code=ie_value(value)
            uf=tenant.address_state
            # An IE saved before this check is only validated when it changes (readiness still reports it).
            unchanged=code==ie_value(tenant.state_registration)
            if not unchanged and code and code!='ISENTO' and uf in UF_CODES and not valid_ie(code,uf):
                raise APIError(f'Inscrição estadual inválida para {uf}: confira os dígitos')
            if code and code!='ISENTO' and not uf and not code.lstrip('P').isdigit():
                raise APIError('Inscrição estadual inválida')
            tenant.state_registration=code
        if 'city_ibge_code' in data:
            value=digits_only(text(data['city_ibge_code'],'Código IBGE do município',10,optional=True))
            if value and not valid_ibge_city(value,tenant.address_state or None):
                raise APIError('Código IBGE do município inválido (7 dígitos, da UF da empresa)')
            tenant.city_ibge_code=value
        if 'cnae' in data:
            value=digits_only(text(data['cnae'],'CNAE',12,optional=True))
            if value and len(value)!=7:raise APIError('CNAE deve ter 7 dígitos')
            tenant.cnae=value
        if 'special_tax_regime' in data:
            value=text(data['special_tax_regime'],'Regime especial de tributação',1,optional=True)
            if value and value not in SPECIAL_TAX_REGIMES:raise APIError('Regime especial de tributação inválido')
            tenant.special_tax_regime=value
        for field,label in (('simples_credit_rate','Alíquota de crédito do Simples (pCredSN)'),
                            ('approx_tax_fallback_rate','Percentual aproximado de tributos')):
            if field in data:
                value=data[field]
                if value is None or value=='':setattr(tenant,field,None);continue
                try:number=Decimal(str(value).replace(',','.'))
                except InvalidOperation:raise APIError(f'{label} inválida')
                if not number.is_finite() or not 0<=number<=100 or number!=number.quantize(Decimal('0.0001')):
                    raise APIError(f'{label} deve ficar entre 0 e 100, com até 4 casas')
                setattr(tenant,field,number)
        if 'address_zip' in data:
            value=text(data['address_zip'],'CEP',20,optional=True)
            digits=''.join(c for c in value if c.isascii() and c.isdigit())
            if value and len(digits)!=8:raise APIError('Informe o CEP com 8 dígitos')
            tenant.address_zip=digits
        if 'crt' in data:
            if type(data['crt']) is not int or data['crt'] not in CRT_REGIME:
                raise APIError('Regime tributário (CRT) inválido: use 1, 2, 3 ou 4')
            tenant.crt=data['crt'];tenant.tax_regime=CRT_REGIME[tenant.crt]
        elif 'tax_regime' in data:
            # Older clients still send the text regime; the CRT follows it.
            value=text(data['tax_regime'],'Regime tributário',30)
            if value not in REGIME_CRT:
                raise APIError('Regime tributário inválido')
            tenant.tax_regime=value;tenant.crt=REGIME_CRT[value]
        if 'fiscal_environment' in data:
            value=text(data['fiscal_environment'],'Ambiente fiscal',20)
            if value not in ('non_fiscal','homologation','production'):
                raise APIError('Ambiente fiscal inválido')
            tenant.fiscal_environment=value
        for field,label,minimum,maximum in (('nfce_series','Série NFC-e',1,999),('nfce_next_number','Próximo número NFC-e',1,10**12)):
            if field in data:
                value=data[field]
                if type(value) is not int or not minimum<=value<=maximum:raise APIError(f'{label} inválida')
                setattr(tenant,field,value)
        if 'max_discount_bps' in data:
            value = data['max_discount_bps']
            if type(value) is not int or not 0 <= value <= 10000:
                raise APIError('Desconto máximo deve ficar entre 0% e 100%')
            tenant.max_discount_bps = value
        if 'business_hours' in data:
            value = data['business_hours']
            if not isinstance(value, dict):
                raise APIError('Horário de funcionamento inválido')
            normalized = {}
            for day in ('mon','tue','wed','thu','fri','sat','sun'):
                row = value.get(day, {})
                if not isinstance(row, dict):
                    raise APIError('Horário de funcionamento inválido')
                enabled = row.get('enabled', False)
                if type(enabled) is not bool:
                    raise APIError('Horário de funcionamento inválido')
                opening = str(row.get('open', '')).strip()
                closing = str(row.get('close', '')).strip()
                if enabled:
                    import re
                    if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', opening) or not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', closing):
                        raise APIError(f'Informe horários válidos para {day}')
                normalized[day] = {'enabled': enabled, 'open': opening if enabled else '', 'close': closing if enabled else ''}
            tenant.business_hours = normalized
        changed=set(data)
        if changed&{'crt','tax_regime'}:changed|={'crt','tax_regime'}
        with transaction.atomic():
            tenant.save(update_fields=sorted(changed));audit(tenant,request.user,'company.updated',tenant,{'fields':sorted(changed)})
    return JsonResponse({'id':str(tenant.pk),'name':tenant.name,'trade_name':tenant.trade_name,
                         'document':tenant.document,'state_registration':tenant.state_registration,
                         'municipal_registration':tenant.municipal_registration,
                         'address_line':tenant.address_line,'address_number':tenant.address_number,
                         'address_complement':tenant.address_complement,
                         'address_neighborhood':tenant.address_neighborhood,'address_city':tenant.address_city,
                         'address_state':tenant.address_state,'address_zip':tenant.address_zip,
                         'tax_regime':tenant.tax_regime,'fiscal_environment':tenant.fiscal_environment,
                         'nfce_series':tenant.nfce_series,'nfce_next_number':tenant.nfce_next_number,
                         'max_discount_bps': tenant.max_discount_bps,
                         'business_hours': tenant.business_hours or {},
                         **company_data(tenant),'fiscal_status':company_status(tenant),
                         'tech_responsible':tech_responsible()})

@endpoint
def integration(request,provider):
    method(request,'GET','POST','PATCH')
    if provider=='asaas':
        user=platform_user(request,superuser=True);tenant=None
    elif provider=='focusnfe':
        member=membership_context(request,OWNER,write=request.method!='GET');user=request.user;tenant=member.tenant
    else:raise APIError('Provedor desconhecido',404)
    row=IntegrationAccount.objects.filter(provider=provider,tenant=tenant).first()
    if request.method!='GET':
        data=payload(request);fields(data,('token','environment','enabled'),('environment','enabled'))
        environments=('sandbox','production') if provider=='asaas' else ('homologation','production')
        if data['environment'] not in environments or type(data['enabled']) is not bool:raise APIError('Ambiente inválido')
        if not row and not data.get('token'):raise APIError('Informe a credencial')
        row=row or IntegrationAccount(provider=provider,tenant=tenant)
        if data.get('token'):
            token=text(data['token'],'Credencial',1000)
            try:row.token_encrypted=encrypt_secret(token)
            except ValueError:raise APIError('Chave de criptografia do servidor não configurada',503)
        row.environment=data['environment'];row.enabled=data['enabled'];row.save()
        audit(tenant,user,'integration.configured',row,{'provider':provider,'environment':row.environment,'enabled':row.enabled})
    return JsonResponse({'provider':provider,'configured':bool(row),'enabled':row.enabled if row else False,
                         'environment':row.environment if row else ('sandbox' if provider=='asaas' else 'homologation')})

@endpoint
def jobs(request):
    method(request,'GET')
    if request.headers.get('X-Tenant-ID'):
        member=membership_context(request,('owner','admin','manager','finance'))
        rows=ProviderJob.objects.filter(tenant=member.tenant)
    else:
        platform_user(request,roles=('platform_ops','platform_billing'));rows=ProviderJob.objects.all()
    return JsonResponse({'results':[{'id':str(x.pk),'kind':x.kind,'status':x.status,'attempts':x.attempts,
        'last_error':x.last_error,'created_at':x.created_at.isoformat(),'result':x.result} for x in rows.order_by('-created_at')[:100]]})

@endpoint
def subscribe(request):
    method(request,'POST');member=membership_context(request,OWNER)
    data=payload(request);fields(data,('plan_id','cycle','name','document','email','billing_type','idempotency_key'),
                                ('plan_id','cycle','name','document','email','idempotency_key'))
    plan=Plan.objects.filter(pk=uuid_value(data['plan_id']),active=True).first()
    if not plan:raise APIError('Plano não encontrado',404)
    cycle=data['cycle'];billing=data.get('billing_type','PIX')
    if cycle not in ('MONTHLY','YEARLY') or billing not in ('PIX','BOLETO','UNDEFINED'):raise APIError('Ciclo ou cobrança inválidos')
    if not IntegrationAccount.objects.filter(provider='asaas',tenant=None,enabled=True).exists():raise APIError('Cobrança ainda não configurada pela plataforma',503)
    key='billing.subscribe:'+str(member.tenant_id)+':'+text(data['idempotency_key'],'Identificador',100)
    body={'plan_id':str(plan.pk),'cycle':cycle,'amount_cents':plan.price_year_cents if cycle=='YEARLY' else plan.price_month_cents,
          'name':text(data['name'],'Nome',160),'document':text(data['document'],'CPF/CNPJ',30),
          'email':text(data['email'],'Email',254),'next_due_date':timezone.localdate().isoformat(),'billing_type':billing}
    with transaction.atomic():
        type(member.tenant).objects.select_for_update().get(pk=member.tenant_id)
        if not capacity_fits(member.tenant,plan):raise APIError('Plano abaixo do uso atual: ajuste lojas/terminais antes de contratar',409)
        pending=ProviderJob.objects.filter(tenant=member.tenant,kind='billing.subscribe',status__in=['queued','running','needs_review']).exclude(idempotency_key=key)
        if pending.exists():raise APIError('Já há solicitação de assinatura pendente de conciliação',409)
        if BillingBinding.objects.filter(tenant=member.tenant,active=True).exists() and not ProviderJob.objects.filter(idempotency_key=key).exists():
            raise APIError('Já há assinatura no provedor; solicite alteração à plataforma para evitar cobrança duplicada',409)
        try:job=enqueue('billing.subscribe',member.tenant,key,body)
        except JobConflict as exc:raise APIError(str(exc),409)
        audit(member.tenant,request.user,'billing.requested',job)
    return JsonResponse({'job_id':str(job.pk),'status':job.status},status=202)

@csrf_exempt
@endpoint
def asaas_webhook(request):
    method(request,'POST')
    if not verify_webhook_token(request.headers.get('asaas-access-token'),os.environ.get('PDV_ASAAS_WEBHOOK_TOKEN','')):
        raise APIError('Webhook não autenticado',401)
    data=payload(request)
    event_id=text(data.get('id'),'Evento',160);event_type=text(data.get('event'),'Tipo',80)
    payment=data.get('payment')
    if not isinstance(payment,dict) or not payment.get('id'):
        return JsonResponse({'accepted':False,'reason':'Evento sem cobrança ignorado'})
    payment_id=text(payment['id'],'Cobrança',160)
    minimal={'event':event_type,'payment_id':payment_id}
    with transaction.atomic():
        existing=BillingEvent.objects.filter(external_id=event_id).first()
        if existing and existing.payload!=minimal:raise APIError('Evento repetido com conteúdo divergente',409)
        if not existing:BillingEvent.objects.create(provider='asaas',external_id=event_id,event_type=event_type,payload=minimal)
        try:job=enqueue('billing.reconcile',None,'asaas:'+event_id,{'payment_id':payment_id})
        except JobConflict as exc:raise APIError(str(exc),409)
    return JsonResponse({'accepted':True,'job_id':str(job.pk)})

@endpoint
def fiscal_submit(request):
    method(request,'POST');member=membership_context(request,('owner','admin','manager'),write=True)
    data=payload(request);fields(data,('sale_id','fiscal_payload'),('sale_id','fiscal_payload'))
    body=data['fiscal_payload']
    if not isinstance(body,dict):raise APIError('Payload fiscal inválido')
    if body.get('tipo_emissao',1) not in (1,'1'):raise APIError('Contingência fiscal local ainda não habilitada')
    if not IntegrationAccount.objects.filter(provider='focusnfe',tenant=member.tenant,enabled=True).exists():raise APIError('Configure a integração fiscal antes de emitir',503)
    with transaction.atomic():
        sale=Sale.objects.select_for_update().filter(pk=uuid_value(data['sale_id']),tenant=member.tenant,status='completed').first()
        if not sale:raise APIError('Venda não encontrada',404)
        try:total=Decimal(str(body.get('valor_total')))*100
        except (InvalidOperation,TypeError):raise APIError('Total fiscal inválido')
        if not total.is_finite() or total!=sale.total_cents:raise APIError('Total fiscal difere da venda')
        issuer=''.join(x for x in str(body.get('cnpj_emitente',body.get('cpf_emitente',''))) if x.isdigit())
        expected=''.join(x for x in member.tenant.document if x.isdigit())
        if not expected or issuer!=expected:raise APIError('Emitente deve corresponder ao documento cadastrado na empresa')
        items=body.get('items',[]);sale_items=list(sale.items.select_related('product'))
        if not isinstance(items,list) or len(items)!=len(sale_items):raise APIError('Itens fiscais não correspondem à venda')
        for item,line in zip(items,sale_items):
            if not isinstance(item,dict) or str(item.get('codigo_produto')) not in (str(line.product_id),line.product.sku):raise APIError('Produto fiscal divergente')
            try:quantity=Decimal(str(item.get('quantidade_comercial')))*1000
            except InvalidOperation:raise APIError('Quantidade fiscal inválida')
            if not quantity.is_finite() or quantity!=line.quantity_milli:raise APIError('Quantidade fiscal divergente')
        record=FiscalDocument.objects.filter(sale=sale).first()
        if record:
            if FiscalPayload.objects.get(document=record).request_payload!=body:raise APIError('Venda já possui requisição fiscal com outros dados',409)
        else:
            record=FiscalDocument.objects.create(tenant=member.tenant,shop=sale.shop,sale=sale,external_id='pdv2-'+str(sale.pk),status='queued')
            FiscalPayload.objects.create(document=record,request_payload=body)
        job=enqueue('fiscal.submit',member.tenant,'fiscal.submit:'+str(record.pk),{'document_id':str(record.pk)})
        audit(member.tenant,request.user,'fiscal.requested',record)
    return JsonResponse({'document_id':str(record.pk),'job_id':str(job.pk),'status':record.status},status=202)

@endpoint
def fiscal_documents(request):
    method(request,'GET');member=membership_context(request,('owner','admin','manager','finance','viewer'))
    rows=FiscalDocument.objects.filter(tenant=member.tenant).order_by('-created_at')[:100]
    return JsonResponse({'results':[{'id':str(x.pk),'sale_id':str(x.sale_id),'status':x.status,'reference':x.external_id,
                                    'authorization':x.authorization,'error_message':x.error_message,'created_at':x.created_at.isoformat(),
                                    'xml_archived':FiscalPayload.objects.filter(document=x).exclude(xml_sha256='').exists()} for x in rows]})

@endpoint
def fiscal_xml(request,pk):
    method(request,'GET');member=membership_context(request,('owner','admin','manager','finance'))
    row=FiscalPayload.objects.filter(document_id=uuid_value(pk),document__tenant=member.tenant).first()
    if not row:raise APIError('Documento não encontrado',404)
    if not row.xml_content:raise APIError('XML ainda não arquivado; consulte a fila de integração',409)
    response=HttpResponse(bytes(row.xml_content),content_type='application/xml')
    response['Content-Disposition']='attachment; filename="nfce-'+str(row.document_id)+'.xml"'
    response['Cache-Control']='private, no-store'
    response['X-Content-SHA256']=row.xml_sha256
    audit(member.tenant,request.user,'fiscal.xml_downloaded',row.document)
    return response

@endpoint
def fiscal_cancel(request,pk):
    method(request,'POST');member=membership_context(request,('owner','admin','manager'),write=True)
    data=payload(request);fields(data,('reason',),('reason',))
    reason=text(data['reason'],'Justificativa',255)
    if len(reason)<15:raise APIError('Justificativa deve ter pelo menos 15 caracteres')
    with transaction.atomic():
        row=FiscalDocument.objects.select_for_update().filter(pk=uuid_value(pk),tenant=member.tenant).first()
        if not row:raise APIError('Documento não encontrado',404)
        if row.status!='authorized':raise APIError('Somente notas autorizadas podem solicitar cancelamento',409)
        try:job=enqueue('fiscal.cancel',member.tenant,'fiscal.cancel:'+str(row.pk),{'document_id':str(row.pk),'reason':reason})
        except JobConflict as exc:raise APIError(str(exc),409)
        audit(member.tenant,request.user,'fiscal.cancel_requested',row)
    return JsonResponse({'job_id':str(job.pk),'status':job.status},status=202)

@endpoint
def reconcile_job(request,pk):
    method(request,'POST')
    data=payload(request);fields(data,('subscription_id','recover_customer'))
    if 'recover_customer' in data and type(data['recover_customer']) is not bool:raise APIError('Opção de recuperação inválida')
    if request.headers.get('X-Tenant-ID'):
        member=membership_context(request,OWNER)
        queryset=ProviderJob.objects.filter(tenant=member.tenant)
    else:
        platform_user(request,roles=('platform_ops','platform_billing'));queryset=ProviderJob.objects.all()
    with transaction.atomic():
        row=queryset.select_for_update().filter(pk=uuid_value(pk)).first()
        if not row:raise APIError('Tarefa não encontrada',404)
        if row.status!='needs_review':raise APIError('Tarefa não está aguardando revisão',409)
        if row.kind in ('fiscal.submit','fiscal.cancel'):
            # Recovery is a provider GET, never an automatic replay of a fiscal mutation.
            recovered=enqueue('fiscal.poll',row.tenant,'reconcile:'+str(row.pk),{'document_id':row.payload['document_id']})
        elif row.kind in ('fiscal.poll','fiscal.archive','billing.reconcile'):
            row.status='queued';row.attempts=0;row.available_at=timezone.now();row.last_error='';row.save()
            recovered=row
        elif row.kind=='billing.subscribe' and data.get('subscription_id'):
            platform_user(request,roles=('platform_billing',))
            remote_id=text(data['subscription_id'],'Assinatura no provedor',160)
            try:recovered=enqueue('billing.recover',row.tenant,'reconcile:'+str(row.pk),{'source_job_id':str(row.pk),'subscription_id':remote_id})
            except JobConflict as exc:raise APIError(str(exc),409)
        elif row.kind=='billing.subscribe' and data.get('recover_customer') is True:
            platform_user(request,roles=('platform_billing',))
            if row.result.get('customer_id'):raise APIError('Cliente já confirmado; informe a assinatura do provedor',409)
            recovered=enqueue('billing.recover_customer',row.tenant,'recover_customer:'+str(row.pk),{'source_job_id':str(row.pk)})
        else:raise APIError('Confirme a assinatura no provedor antes de realizar nova cobrança; reenvio automático bloqueado',409)
        audit(row.tenant,request.user,'job.reconciliation_requested',row)
    return JsonResponse({'job_id':str(recovered.pk),'status':recovered.status},status=202)

@endpoint
def health(request):
    method(request,'GET');platform_user(request)
    marker=Path('/backup-status/last-success')
    try:backup_timestamp=int(marker.read_text().strip())
    except (OSError,ValueError):backup_timestamp=None
    return JsonResponse({'pending_jobs':ProviderJob.objects.filter(status='queued').count(),
                         'jobs_needing_review':ProviderJob.objects.filter(status='needs_review').count(),
                         'last_backup_at':backup_timestamp,'backup_offsite_configured':bool(os.environ.get('PDV_BACKUP_OFFSITE')=='1')})

@endpoint
def platform_metrics(request):
    method(request,'GET');user=platform_user(request)
    active=Subscription.objects.filter(status='active',current_period_end__gt=timezone.now(),tenant__active=True)
    financial=user.is_superuser or user.groups.filter(name='platform_billing').exists()
    mrr=None;paid=None
    if financial:
        contracted={x.external_id:((x.amount_cents+6)//12 if x.cycle=='YEARLY' else x.amount_cents) for x in BillingBinding.objects.filter(active=True)}
        mrr=sum(contracted.get(sub.external_id,sub.plan.price_month_cents)
                for sub in active.filter(license_source='subscription').select_related('plan'))
        paid=BillingInvoice.objects.filter(status='paid').aggregate(value=Sum('amount_cents'))['value'] or 0
    return JsonResponse({'tenants_total':Tenant.objects.count(),'tenants_active':Tenant.objects.filter(active=True).count(),
        'subscriptions_active':active.count(),'subscriptions_vip':active.filter(license_source='vip').count(),
        'subscriptions_trial':Subscription.objects.filter(status='trial',trial_ends_at__gt=timezone.now()).count(),
        'subscriptions_past_due':Subscription.objects.filter(status='past_due').count(),
        'terminals_total':Terminal.objects.count(),'terminals_revoked':Terminal.objects.filter(revoked=True).count(),
        'mrr_cents':mrr,'paid_invoices_cents':paid,'pending_jobs':ProviderJob.objects.filter(status='queued').count(),
        'jobs_needing_review':ProviderJob.objects.filter(status='needs_review').count()})
