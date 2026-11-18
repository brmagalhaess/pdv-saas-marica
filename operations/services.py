import calendar
import hashlib
import logging
from datetime import date,datetime,timedelta
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from core.models import BillingInvoice,FiscalDocument,Subscription,Tenant,Terminal,Shop
from shared.licensing import payload_digest
from shared.secrets import decrypt_secret
from integrations.base import ProviderError
from integrations.billing import Asaas
from integrations.fiscal import FocusNFe
from .models import IntegrationAccount,ProviderJob,BillingBinding,FiscalPayload

logger=logging.getLogger(__name__)

class JobConflict(ValueError):pass
class StillPending(Exception):pass

def capacity_fits(tenant,plan):
    return (Terminal.objects.filter(tenant=tenant,revoked=False).count()<=plan.terminal_limit
            and Shop.objects.filter(tenant=tenant,active=True).count()<=plan.shop_limit)

def enqueue(kind,tenant,key,payload,delay=0):
    hashed=payload_digest(payload)
    job,created=ProviderJob.objects.get_or_create(idempotency_key=key,defaults={'kind':kind,'tenant':tenant,
        'request_hash':hashed,'payload':payload,'available_at':timezone.now()+timedelta(seconds=delay)})
    if not created and (job.request_hash!=hashed or job.tenant_id!=getattr(tenant,'pk',None) or job.kind!=kind):
        raise JobConflict('Identificador já utilizado com dados diferentes')
    return job

def account(provider,tenant=None):
    record=IntegrationAccount.objects.filter(provider=provider,tenant=tenant,enabled=True).first()
    if not record:raise ValueError('Integração não configurada ou desativada')
    token=decrypt_secret(record.token_encrypted)
    return Asaas(token,record.environment) if provider=='asaas' else FocusNFe(token,record.environment)

def _end_period(due,cycle):
    month=due.month+(12 if cycle=='YEARLY' else 1)
    year=due.year+(month-1)//12;month=(month-1)%12+1
    next_date=date(year,month,min(due.day,calendar.monthrange(year,month)[1]))
    return timezone.make_aware(datetime.combine(next_date,datetime.min.time()))

def subscribe(job):
    from core.models import Plan
    data=job.payload
    plan=Plan.objects.get(pk=data['plan_id'],active=True)
    expected=plan.price_year_cents if data['cycle']=='YEARLY' else plan.price_month_cents
    if expected!=data['amount_cents']:raise ValueError('Preço do plano mudou; gere outra solicitação')
    if not capacity_fits(job.tenant,plan):raise ValueError('Plano solicitado está abaixo do uso atual; ajuste lojas/terminais antes de contratar')
    provider=account('asaas')
    reference='pdv2:'+str(job.tenant_id)
    result=dict(job.result)
    if not result.get('customer_id'):
        customers=provider.find_customers(reference).get('data',[])
        exact=[x for x in customers if x.get('externalReference')==reference]
        if len(exact)>1:raise ValueError('Há pagadores duplicados no provedor; concilie antes de continuar')
        customer=exact[0] if exact else provider.create_customer(data['name'],data['document'],data['email'],reference)
        if exact:
            expected_document=''.join(c for c in data['document'] if c.isdigit())
            if ''.join(c for c in str(customer.get('cpfCnpj','')) if c.isdigit())!=expected_document:
                raise ValueError('Documento do pagador existente diverge; concilie o cadastro no provedor')
        result['customer_id']=customer['id']
        ProviderJob.objects.filter(pk=job.pk).update(result=result)
    remote=provider.create_subscription(result['customer_id'],expected,data['cycle'],data['next_due_date'],str(job.pk),data['billing_type'])
    with transaction.atomic():
        BillingBinding.objects.get_or_create(external_id=remote['id'],defaults={'tenant':job.tenant,'plan':plan,
            'customer_id':result['customer_id'],'cycle':data['cycle'],'amount_cents':expected})
    # Creating a subscription is deliberately not treated as a paid invoice.
    return {'customer_id':result['customer_id'],'subscription_id':remote['id'],'payment_confirmed':False}

def reconcile_payment(job):
    provider=account('asaas')
    payment=provider.get_payment(job.payload['payment_id'])
    if payment.get('id')!=job.payload['payment_id']:raise ValueError('Identificador da cobrança retornada diverge da consulta')
    binding=BillingBinding.objects.select_related('tenant','plan').filter(external_id=payment.get('subscription')).first()
    if not binding:raise ValueError('Cobrança não pertence a uma assinatura cadastrada no PDV2')
    if payment.get('customer')!=binding.customer_id:raise ValueError('Pagador divergente')
    amount=int((Decimal(str(payment['value']))*100).quantize(Decimal('1')))
    if amount!=binding.amount_cents:raise ValueError('Valor cobrado diverge da assinatura')
    due=date.fromisoformat(payment['dueDate'])
    state=provider.payment_state(payment)
    blocked_capacity=False
    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=binding.tenant_id)
        sub=Subscription.objects.select_for_update().get(tenant=binding.tenant)
        invoice=BillingInvoice.objects.filter(provider='asaas',external_id=payment['id']).first()
        if invoice and invoice.tenant_id!=binding.tenant_id:raise ValueError('Cobrança vinculada a outra empresa')
        if not invoice:invoice=BillingInvoice(tenant=binding.tenant,subscription=sub,provider='asaas',external_id=payment['id'],amount_cents=amount,due_date=due)
        invoice.status=state;invoice.checkout_url=payment.get('invoiceUrl','');invoice.save()
        period_end=_end_period(due,binding.cycle)
        # A verified first payment takes over a complimentary/manual grant even
        # when that grant lasts longer than the newly purchased billing cycle.
        # Once paid, older invoices must not shorten the paid period.
        paid_takeover = sub.license_source != 'subscription'
        if state=='paid' and binding.active and (paid_takeover or sub.current_period_end is None or period_end>=sub.current_period_end):
            if not capacity_fits(binding.tenant,binding.plan):blocked_capacity=True
            else:
                sub.plan=binding.plan;sub.status='active';sub.current_period_end=period_end;sub.external_id=binding.external_id
                # Only a payment verified against the provider changes a grant
                # from trial or VIP to a paid subscription.
                sub.license_source='subscription';sub.save()
        elif state in ('refunded','disputed') and binding.active and sub.external_id==binding.external_id and sub.current_period_end==period_end:
            sub.status='past_due';sub.save(update_fields=['status','updated_at'])
        elif state=='overdue' and binding.active and sub.external_id==binding.external_id and sub.current_period_end and sub.current_period_end<=timezone.now():
            sub.status='past_due';sub.save(update_fields=['status','updated_at'])
        ProviderJob.objects.filter(pk=job.pk).update(tenant=binding.tenant)
    if blocked_capacity:raise ValueError('Pagamento confirmado, mas plano abaixo do uso atual; ajuste lojas/terminais e concilie novamente')
    return {'invoice_id':str(invoice.pk),'status':state}

def recover_customer(job):
    source=ProviderJob.objects.get(pk=job.payload['source_job_id'],tenant=job.tenant,kind='billing.subscribe')
    if source.result.get('customer_id'):
        return {'recovered':True,'source_job_id':str(source.pk)}
    if source.status!='needs_review':raise ValueError('Solicitação original não está em revisão')
    reference='pdv2:'+str(job.tenant_id)
    matches=[x for x in account('asaas').find_customers(reference).get('data',[]) if x.get('externalReference')==reference]
    if len(matches)!=1:raise ValueError('É necessário exatamente um cliente com a referência original no provedor')
    customer=matches[0]
    document=''.join(c for c in str(customer.get('cpfCnpj','')) if c.isdigit())
    expected=''.join(c for c in source.payload['document'] if c.isdigit())
    if document!=expected:raise ValueError('Documento do cliente recuperado diverge da solicitação')
    with transaction.atomic():
        row=ProviderJob.objects.select_for_update().get(pk=source.pk)
        if row.status!='needs_review':raise ValueError('Solicitação mudou; atualize a fila')
        row.result={'customer_id':customer['id'],'customer_recovered':True};row.status='queued';row.last_error=''
        row.available_at=timezone.now();row.locked_at=None;row.save()
    return {'source_job_id':str(source.pk),'recovered':True}

def recover_subscription(job):
    """Bind a provider-confirmed ambiguous create without sending another charge."""
    source=ProviderJob.objects.get(pk=job.payload['source_job_id'],tenant=job.tenant,kind='billing.subscribe',status__in=['needs_review','done'])
    provider=account('asaas')
    remote=provider.get_subscription(job.payload['subscription_id'])
    data=source.payload
    if (remote.get('id')!=job.payload['subscription_id'] or remote.get('externalReference')!=str(source.pk) or remote.get('customer')!=source.result.get('customer_id')
        or remote.get('cycle')!=data['cycle'] or Decimal(str(remote.get('value',0)))*100!=data['amount_cents']):
        raise ValueError('A assinatura no provedor não corresponde à solicitação original')
    if remote.get('deleted') or remote.get('status')!='ACTIVE':raise ValueError('Assinatura não está ativa no provedor')
    with transaction.atomic():
        binding,created=BillingBinding.objects.get_or_create(external_id=remote['id'],defaults={'tenant':job.tenant,
            'plan_id':data['plan_id'],'customer_id':remote['customer'],'cycle':data['cycle'],'amount_cents':data['amount_cents']})
        if binding.tenant_id!=job.tenant_id:raise ValueError('Assinatura já vinculada a outra empresa')
        source.status='done';source.last_error='';source.result={'subscription_id':remote['id'],'customer_id':remote['customer'],'recovered':True};source.save()
    # This read does not grant access. Each payment must still be independently reconciled.
    for payment in provider.list_subscription_payments(remote['id']).get('data',[]):
        enqueue('billing.reconcile',job.tenant,'recovered:'+str(job.pk)+':'+payment['id'],{'payment_id':payment['id']})
    return {'subscription_id':remote['id'],'payment_confirmed':False,'recovered':True}

# NFC-e jobs: the logic lives in core.nfce.service (both modes, Focus NFe and direct SEFAZ).
def _fiscal_document(job):
    return FiscalDocument.objects.select_related('tenant','sale').get(pk=job.payload['document_id'],tenant=job.tenant)

def fiscal_submit(job):
    from core.nfce import service
    record=service.send(_fiscal_document(job))
    return {'document_id':str(record.pk),'status':record.status}

def fiscal_poll(job):
    from core.nfce import service
    record=service.poll(_fiscal_document(job))
    if record.status=='pending':raise StillPending('Aguardando resposta fiscal')
    return {'document_id':str(record.pk),'status':record.status}

def fiscal_transmit(job):
    from core.nfce import service
    record=_fiscal_document(job)
    if record.status!='contingency':return {'document_id':str(record.pk),'status':record.status}
    record=service.transmit(record)
    return {'document_id':str(record.pk),'status':record.status}

def fiscal_archive(job):
    record=FiscalDocument.objects.get(pk=job.payload['document_id'],tenant=job.tenant)
    payload=FiscalPayload.objects.get(document=record)
    if payload.xml_content:return {'document_id':str(record.pk),'sha256':payload.xml_sha256}
    if record.status!='authorized':raise ValueError('A nota precisa estar autorizada para arquivar o XML')
    from core.nfce.service import _focus_client
    xml=_focus_client(job.tenant).download_xml(record.xml_url,record.access_key,record.authorization)
    checksum=hashlib.sha256(xml).hexdigest()
    FiscalPayload.objects.filter(document=record).update(xml_content=xml,xml_sha256=checksum)
    return {'document_id':str(record.pk),'sha256':checksum}

def fiscal_cancel(job):
    from core.models import FiscalEvent
    from core.nfce import service
    record=_fiscal_document(job)
    event=FiscalEvent.objects.get(pk=job.payload['event_id'],document=record)
    if event.status=='registered':return {'document_id':str(record.pk),'status':'cancelled'}
    service.run_cancel(record,event)
    return {'document_id':str(record.pk),'status':'cancelled'}

def fiscal_inutilize(job):
    from core.models import FiscalInutilization
    from core.nfce import service
    row=FiscalInutilization.objects.get(pk=job.payload['inutilization_id'],tenant=job.tenant)
    if row.status=='done':return {'inutilization_id':str(row.pk),'status':row.status}
    row=service.run_inutilization(row)
    return {'inutilization_id':str(row.pk),'status':row.status,'protocol':row.protocol}

def fiscal_reconcile_replaced(job):
    from core.nfce import service
    service.reconcile_replaced(_fiscal_document(job))
    return {'document_id':job.payload['document_id']}

def _accountant(name):
    # Monthly accountant package (core/accountant); imported lazily to keep app loading order simple.
    def run(job):
        from core.accountant import jobs
        return getattr(jobs,name)(job)
    run.__name__=name
    return run

HANDLERS={'billing.subscribe':subscribe,'billing.reconcile':reconcile_payment,'billing.recover':recover_subscription,
          'billing.recover_customer':recover_customer,'fiscal.submit':fiscal_submit,
          'fiscal.poll':fiscal_poll,'fiscal.archive':fiscal_archive,'fiscal.cancel':fiscal_cancel,
          'accountant.package':_accountant('run_package'),'accountant.send':_accountant('run_send'),
          'accountant.monthly':_accountant('run_monthly'),
          'fiscal.transmit':fiscal_transmit,'fiscal.inutilize':fiscal_inutilize,
          'fiscal.reconcile_replaced':fiscal_reconcile_replaced}
# Safe to retry on a provider error: queries, and transmissions identified by the access key
# (a repeated NF-e is answered with 204 and then consulted).
RETRY_SAFE=('billing.reconcile','fiscal.poll','fiscal.archive','fiscal.transmit','fiscal.reconcile_replaced')

def run_one():
    now=timezone.now()
    # A process may die after the provider accepted a mutation. Never blindly replay that mutation.
    mutations=['billing.subscribe','fiscal.submit','fiscal.cancel','fiscal.inutilize']
    ProviderJob.objects.filter(status='running',locked_at__lt=now-timedelta(minutes=5),kind__in=mutations).update(status='needs_review',last_error='Processo interrompido; consultar provedor antes de repetir')
    ProviderJob.objects.filter(status='running',locked_at__lt=now-timedelta(minutes=5)).exclude(kind__in=mutations).update(status='queued',locked_at=None)
    with transaction.atomic():
        job=ProviderJob.objects.select_for_update(skip_locked=True).filter(status='queued',available_at__lte=now).order_by('available_at').first()
        if not job:return False
        job.status='running';job.locked_at=now;job.attempts+=1;job.save()
    try:
        result=HANDLERS[job.kind](job)
        ProviderJob.objects.filter(pk=job.pk).update(status='done',result=result,locked_at=None,last_error='')
    except (ProviderError,StillPending) as exc:
        safe=(isinstance(exc,StillPending) or (job.kind in RETRY_SAFE and getattr(exc,'status',None) not in (400,401,403,404)))
        if job.kind=='fiscal.cancel':safe=False
        retry=safe and job.attempts<30
        ProviderJob.objects.filter(pk=job.pk).update(status='queued' if retry else 'needs_review',
            available_at=timezone.now()+timedelta(seconds=min(900,15*2**min(job.attempts,6))),locked_at=None,last_error=str(exc)[:500])
    except Exception as exc:
        # The worker wraps handlers and already logs the traceback (logs_failures); never print it twice.
        logger.error('Job %s (%s) of tenant %s failed on attempt %s',job.pk,job.kind,job.tenant_id or '-',job.attempts,
                     exc_info=getattr(HANDLERS.get(job.kind),'logs_failures',None) is not True)
        # Avoid recording credentials or third-party response bodies in user-visible errors.
        # APIError carries a user-facing message too (missing certificate, fiscal setup...).
        message=(str(exc) if isinstance(exc,ValueError) else getattr(exc,'message',None) if isinstance(getattr(exc,'message',None),str)
                 else 'Falha interna ao executar tarefa; consulte o suporte')
        ProviderJob.objects.filter(pk=job.pk).update(status='needs_review',locked_at=None,last_error=message[:500])
    return True
