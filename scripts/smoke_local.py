#!/usr/bin/env python3
"""Exercise the disposable seeded demo through real HTTP services.

--offline temporarily stops only this compose project's cloud container.
Never run this against a customer's environment.
"""
import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path
import requests

parser=argparse.ArgumentParser();parser.add_argument('--offline',action='store_true');args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
env=dict(line.split('=',1) for line in (root/'.runtime/local.env').read_text().splitlines() if '=' in line)
compose=['docker','compose','--env-file',str(root/'.runtime/local.env'),'-f',str(root/'compose.local.yaml')]
cloud=requests.Session();local=requests.Session()
base='http://127.0.0.1:8000/api/v1';terminal='http://127.0.0.1:8010'
def call(session,method,url,**kwargs):
    response=session.request(method,url,timeout=45,**kwargs)
    if response.status_code>=400:
        try:message=response.json().get('error','Erro HTTP')
        except ValueError:message='Erro HTTP'
        raise RuntimeError(f'{method} {url}: HTTP {response.status_code}: {message}')
    return response.json()
def post(path,body):return call(local,'POST',terminal+path,json=body)
token=call(cloud,'GET',base+'/csrf')['csrfToken'];cloud.headers['X-CSRFToken']=token
result=call(cloud,'POST',base+'/auth/login',json={'email':'gestor@pdv2.local','password':env['PDV_DEMO_PASSWORD']})
member=next(x for x in result['memberships'] if x['tenant_name']=='Mercado Exemplo Maricá')
cloud.headers.update({'X-Tenant-ID':member['tenant_id'],'X-CSRFToken':cloud.cookies['csrftoken']})
shop=call(cloud,'GET',base+'/shops')['results'][0]
session=call(local,'GET',terminal+'/api/session');local.headers['X-CSRF-Token']=session['csrf']
if not session['paired']:
    pair=call(cloud,'POST',base+'/terminals',json={'shop_id':shop['id'],'name':'Caixa piloto local'})
    post('/api/pair',{'cloud_url':'http://cloud:8080','code':pair['pairing_code']})
    session=call(local,'GET',terminal+'/api/session')
operator=next(x for x in session['operators'] if x['id']==member['id'])
login=post('/api/login',{'operator_id':operator['id'],'pin':env['PDV_DEMO_PIN']});local.headers['X-CSRF-Token']=login['csrf']
post('/api/sync/now',{})
state=call(local,'GET',terminal+'/api/cash')['cash']
if state:raise RuntimeError('O caixa precisa estar fechado antes deste teste de demonstração')
product=next(x for x in call(local,'GET',terminal+'/api/products')['products'] if x['sku']=='ARROZ-1KG')
post('/api/cash/open',{'opening_cents':10000})
stopped=False
try:
    if args.offline:
        subprocess.run(compose+['stop','cloud'],check=True,capture_output=True);stopped=True
        response=requests.get('http://127.0.0.1:8000/health/',timeout=5)
        if response.status_code<500:raise RuntimeError('Nuvem deveria estar indisponível durante teste offline')
    body={'request_id':'smoke-'+uuid.uuid4().hex,'items':[{'product_id':product['id'],'quantity_milli':1000}],
          'discount_cents':0,'payments':[{'method':'cash','amount_cents':product['price_cents']}]}
    sale=post('/api/sales',body)
    duplicate=post('/api/sales',body)
    assert duplicate['id']==sale['id'],'Venda duplicada no reenvio local'
    print('Venda local persistida e reenvio idempotente: PASS')
finally:
    if stopped:subprocess.run(compose+['start','cloud'],check=True,capture_output=True)
for attempt in range(30):
    try:
        if requests.get('http://127.0.0.1:8000/health/',timeout=3).status_code==200:break
    except requests.RequestException:pass
    time.sleep(1)
post('/api/sync/now',{})
remote=call(cloud,'GET',base+'/sales/'+sale['id'])
assert remote['total_cents']==product['price_cents'] and remote['status']=='completed'
post('/api/sales/'+sale['id']+'/void',{'reason':'Estorno do teste automatizado local'})
post('/api/cash/close',{'counted_cents':10000})
post('/api/sync/now',{})
remote=call(cloud,'GET',base+'/sales/'+sale['id']);assert remote['status']=='cancelled'
report={'offline_cloud_stopped':args.offline,'sale_id':sale['id'],'result':'PASS','sale_status':'cancelled'}
(root/'.runtime/http-smoke-report.json').write_text(json.dumps(report,indent=2)+'\n')
print('Sincronização PostgreSQL, estorno e fechamento de caixa: PASS')
