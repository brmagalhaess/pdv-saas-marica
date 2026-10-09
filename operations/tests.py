"""Integration boundaries: isolation, canonical payment state and safe recovery."""
import json
import os
from datetime import timedelta
from unittest.mock import Mock,patch
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase,Client
from django.utils import timezone
from core.models import Plan,Tenant,Subscription,Membership,BillingInvoice,BillingEvent,Shop
from .models import ProductRelease
from integrations.base import ProviderError
from integrations.billing import Asaas
from shared.secrets import encrypt_secret
from .models import IntegrationAccount,ProviderJob,BillingBinding
from .services import enqueue,JobConflict,reconcile_payment,recover_customer,run_one

class OperationsTests(TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'PDV_ENCRYPTION_KEY':Fernet.generate_key().decode(),
                                      'PDV_ASAAS_WEBHOOK_TOKEN':'test-webhook-token-32-characters-long'})
        self.env.start();self.addCleanup(self.env.stop)
        self.user=get_user_model().objects.create_user(username='owner@example.test',email='owner@example.test',password='Test-safe72!')
        self.plan=Plan.objects.create(slug='ops-test',name='Ops',price_month_cents=14900,price_year_cents=149000)
        self.tenant=Tenant.objects.create(name='Mercado A')
        self.other=Tenant.objects.create(name='Mercado B')
        self.sub=Subscription.objects.create(tenant=self.tenant,plan=self.plan,status='trial',trial_ends_at=timezone.now()+timedelta(days=7))
        Membership.objects.create(tenant=self.tenant,user=self.user,name='Dona',role='owner')
        self.client.force_login(self.user)
        self.headers={'HTTP_X_TENANT_ID':str(self.tenant.pk)}

    def post(self,path,data,**kwargs):
        return self.client.post('/api/v1/ops/'+path,json.dumps(data),content_type='application/json',**self.headers,**kwargs)

    def test_integration_secret_is_encrypted_and_tenant_scoped(self):
        response=self.post('integrations/focusnfe',{'environment':'homologation','enabled':True,'token':'not-a-real-provider-key'})
        self.assertEqual(response.status_code,200,response.content)
        row=IntegrationAccount.objects.get(tenant=self.tenant)
        self.assertNotIn('not-a-real-provider-key',row.token_encrypted)
        self.assertNotIn('token',response.json())
        response=self.client.get('/api/v1/ops/integrations/focusnfe',HTTP_X_TENANT_ID=str(self.other.pk))
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.post('integrations/asaas',{'environment':'sandbox','enabled':True,'token':'key'}).status_code,403)

    def test_webhook_authenticated_deduplicated_and_does_not_trust_payload_status(self):
        body={'id':'evt_1','event':'PAYMENT_CONFIRMED','payment':{'id':'pay_1','status':'CONFIRMED'}}
        path='/api/v1/ops/webhooks/asaas'
        self.assertEqual(self.client.post(path,json.dumps(body),content_type='application/json').status_code,401)
        auth={'HTTP_ASAAS_ACCESS_TOKEN':os.environ['PDV_ASAAS_WEBHOOK_TOKEN']}
        for _ in range(2):
            response=self.client.post(path,json.dumps(body),content_type='application/json',**auth)
            self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(BillingEvent.objects.count(),1)
        self.assertEqual(ProviderJob.objects.filter(kind='billing.reconcile').count(),1)
        self.sub.refresh_from_db();self.assertEqual(self.sub.status,'trial')
        body['payment']['id']='pay_other'
        self.assertEqual(self.client.post(path,json.dumps(body),content_type='application/json',**auth).status_code,409)

    def test_canonical_payment_updates_once_and_old_payment_does_not_shorten_period(self):
        BillingBinding.objects.create(tenant=self.tenant,plan=self.plan,external_id='sub_1',customer_id='cus_1',cycle='MONTHLY',amount_cents=14900)
        provider=Mock();provider.payment_state=Asaas.payment_state
        provider.get_payment.return_value={'id':'pay_1','subscription':'sub_1','customer':'cus_1','value':149,'dueDate':'2026-09-29','status':'PENDING'}
        job=enqueue('billing.reconcile',None,'payment-1',{'payment_id':'pay_1'})
        with patch('operations.services.account',return_value=provider):
            reconcile_payment(job)
            self.sub.refresh_from_db();self.assertEqual(self.sub.status,'trial')
            provider.get_payment.return_value['status']='RECEIVED'
            reconcile_payment(job);reconcile_payment(job)
            self.sub.refresh_from_db();self.assertEqual(self.sub.status,'active')
            self.assertEqual(self.sub.license_source,'subscription')
            end=self.sub.current_period_end
            self.assertEqual(BillingInvoice.objects.count(),1)
            provider.get_payment.return_value.update(id='pay_old',dueDate='2026-08-29',status='RECEIVED')
            old_job=enqueue('billing.reconcile',None,'payment-old',{'payment_id':'pay_old'})
            reconcile_payment(old_job)
            self.sub.refresh_from_db();self.assertEqual(self.sub.current_period_end,end)
            provider.get_payment.return_value.update(value=0)
            with self.assertRaises(ValueError):reconcile_payment(old_job)

    def test_idempotency_requires_same_content_and_tenant(self):
        job=enqueue('test',self.tenant,'idem',{'value':1})
        self.assertEqual(enqueue('test',self.tenant,'idem',{'value':1}).pk,job.pk)
        with self.assertRaises(JobConflict):enqueue('test',self.tenant,'idem',{'value':2})
        with self.assertRaises(JobConflict):enqueue('test',self.other,'idem',{'value':1})

    def test_paid_downgrade_cannot_exceed_capacity(self):
        Shop.objects.create(tenant=self.tenant,name='A');Shop.objects.create(tenant=self.tenant,name='B')
        BillingBinding.objects.create(tenant=self.tenant,plan=self.plan,external_id='sub_1',customer_id='cus_1',cycle='MONTHLY',amount_cents=14900)
        provider=Mock();provider.payment_state=Asaas.payment_state
        provider.get_payment.return_value={'id':'pay_1','subscription':'sub_1','customer':'cus_1','value':149,'dueDate':'2026-09-29','status':'RECEIVED'}
        job=enqueue('billing.reconcile',None,'capacity-payment',{'payment_id':'pay_1'})
        with patch('operations.services.account',return_value=provider):
            with self.assertRaisesRegex(ValueError,'abaixo do uso'):reconcile_payment(job)
        self.sub.refresh_from_db();self.assertEqual(self.sub.status,'trial')
        self.assertEqual(BillingInvoice.objects.get().status,'paid')

    def test_inactive_old_binding_cannot_change_vip_license(self):
        BillingBinding.objects.create(tenant=self.tenant,plan=self.plan,external_id='old_sub',
            customer_id='old_customer',cycle='MONTHLY',amount_cents=14900,active=False)
        self.sub.status='active';self.sub.license_source='vip'
        self.sub.current_period_end=timezone.now()+timedelta(days=180)
        self.sub.external_id='';self.sub.save()
        provider=Mock();provider.payment_state=Asaas.payment_state
        provider.get_payment.return_value={'id':'late_payment','subscription':'old_sub',
            'customer':'old_customer','value':149,'dueDate':'2026-08-29','status':'OVERDUE'}
        job=enqueue('billing.reconcile',None,'late-old-payment',{'payment_id':'late_payment'})
        with patch('operations.services.account',return_value=provider):
            reconcile_payment(job)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status,'active')
        self.assertEqual(self.sub.license_source,'vip')
        self.assertEqual(BillingInvoice.objects.get().status,'overdue')

    def test_verified_first_payment_replaces_longer_vip_grant(self):
        vip_plan=Plan.objects.create(slug='vip-long',name='VIP longo',terminal_limit=3,
                                     price_month_cents=29900,price_year_cents=299000)
        vip_end=timezone.now()+timedelta(days=365)
        self.sub.plan=vip_plan;self.sub.status='active';self.sub.license_source='vip'
        self.sub.current_period_end=vip_end;self.sub.external_id='';self.sub.save()
        BillingBinding.objects.create(tenant=self.tenant,plan=self.plan,external_id='new_paid_sub',
            customer_id='new_customer',cycle='MONTHLY',amount_cents=14900)
        provider=Mock();provider.payment_state=Asaas.payment_state
        provider.get_payment.return_value={'id':'first_paid_payment','subscription':'new_paid_sub',
            'customer':'new_customer','value':149,'dueDate':timezone.localdate().isoformat(),
            'status':'RECEIVED'}
        job=enqueue('billing.reconcile',None,'vip-conversion-payment',{'payment_id':'first_paid_payment'})
        with patch('operations.services.account',return_value=provider):
            reconcile_payment(job)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.plan_id,self.plan.id)
        self.assertEqual(self.sub.license_source,'subscription')
        self.assertEqual(self.sub.external_id,'new_paid_sub')
        self.assertLess(self.sub.current_period_end,vip_end)

    def test_ambiguous_customer_creation_recovers_via_read_only_lookup(self):
        source=enqueue('billing.subscribe',self.tenant,'source-customer',{'document':'12345678901'})
        source.status='needs_review';source.save()
        recovery=enqueue('billing.recover_customer',self.tenant,'customer-recovery',{'source_job_id':str(source.pk)})
        provider=Mock();provider.find_customers.return_value={'data':[{'id':'cus_1','cpfCnpj':'12345678901','externalReference':'pdv2:'+str(self.tenant.pk)}]}
        with patch('operations.services.account',return_value=provider):
            recover_customer(recovery);recover_customer(recovery)
        source.refresh_from_db();self.assertEqual(source.result['customer_id'],'cus_1');self.assertEqual(source.status,'queued')
        provider.create_customer.assert_not_called();provider.create_subscription.assert_not_called()

    def test_platform_metrics_are_global_and_financials_are_role_scoped(self):
        from django.contrib.auth.models import Group
        self.assertEqual(self.client.get('/api/v1/ops/platform/metrics').status_code,403)
        self.user.is_staff=True;self.user.save();self.user.groups.add(Group.objects.get(name='platform_support'))
        Tenant.objects.bulk_create([Tenant(name='Metric '+str(i)) for i in range(55)])
        data=self.client.get('/api/v1/ops/platform/metrics').json()
        self.assertEqual(data['tenants_total'],57);self.assertIsNone(data['mrr_cents'])
        self.user.is_superuser=True;self.user.save()
        self.assertEqual(self.client.get('/api/v1/ops/platform/metrics').json()['mrr_cents'],0)
        self.sub.status='active';self.sub.license_source='vip'
        self.sub.current_period_end=timezone.now()+timedelta(days=30);self.sub.save()
        metrics=self.client.get('/api/v1/ops/platform/metrics').json()
        self.assertEqual(metrics['subscriptions_vip'],1)
        self.assertEqual(metrics['mrr_cents'],0)

    def test_release_status_reports_running_cloud_and_published_terminal(self):
        from django.contrib.auth.models import Group
        self.user.is_staff=True;self.user.save();self.user.groups.add(Group.objects.get(name='platform_ops'))
        ProductRelease.objects.create(component='terminal',channel='stable',version='2.0.1',
            url='https://updates.example.test/terminal.zip',sha256='a'*64,size_bytes=10)
        response=self.client.get('/api/v1/ops/releases/status')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(response.json()['terminal']['published_version'],'2.0.1')
        self.assertTrue(response.json()['terminal']['manifest_available'])
        self.assertIn('running_version',response.json()['cloud'])

    def test_ambiguous_mutation_is_never_automatically_repeated(self):
        job=enqueue('billing.subscribe',self.tenant,'ambiguous',{})
        handler=Mock(side_effect=ProviderError('Resultado incerto',outcome_unknown=True))
        with patch.dict('operations.services.HANDLERS',{'billing.subscribe':handler}):
            self.assertTrue(run_one());self.assertFalse(run_one())
        job.refresh_from_db();self.assertEqual(job.status,'needs_review');handler.assert_called_once()
        self.assertEqual(self.post('jobs/'+str(job.pk)+'/reconcile',{}).status_code,409)

    def test_safe_recovery_is_scoped_and_csrf_is_required(self):
        job=enqueue('billing.reconcile',self.other,'other-job',{'payment_id':'pay_2'})
        job.status='needs_review';job.save()
        self.assertEqual(self.post('jobs/'+str(job.pk)+'/reconcile',{}).status_code,404)
        client=Client(enforce_csrf_checks=True);client.force_login(self.user)
        response=client.post('/api/v1/ops/integrations/focusnfe','{}',content_type='application/json',**self.headers)
        self.assertEqual(response.status_code,403)
