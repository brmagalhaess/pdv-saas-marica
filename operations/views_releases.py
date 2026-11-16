from django.db import IntegrityError,transaction
from django.conf import settings
from django.http import JsonResponse
from core.api_common import endpoint,method,platform_user,payload,fields,text,APIError,audit
from shared.licensing import LicenseError
from shared.releases import validate_release,sign_release,version_tuple,load_release_private_key,load_release_public_key
from .models import ProductRelease

def manifest(row):
    data={'schema':1,'purpose':'pdv2-release','component':row.component,'channel':row.channel,
          'version':row.version,'url':row.url,'sha256':row.sha256,'size_bytes':row.size_bytes,
          'notes':row.notes,'published_at':row.published_at.isoformat()}
    # Optional keys stay out of the signed manifest when unset.
    if row.kind:data['kind']=row.kind
    if row.min_version:data['min_version']=row.min_version
    return data

@endpoint
def current(request):
    method(request,'GET')
    component=request.GET.get('component','terminal');channel=request.GET.get('channel','stable')
    if component not in ('terminal','cloud') or channel not in ('stable','pilot'):raise APIError('Canal inválido')
    rows=list(ProductRelease.objects.filter(component=component,channel=channel,active=True))
    if 'current' in request.GET:
        # Never offer a package the caller cannot install from its running version.
        try:running=version_tuple(request.GET['current'])
        except LicenseError:raise APIError('Versão atual inválida')
        rows=[x for x in rows if not x.min_version or version_tuple(x.min_version)<=running]
    row=max(rows,key=lambda x:tuple(map(int,x.version.split('.'))),default=None)
    if not row:return JsonResponse({'release':None})
    try:return JsonResponse({'release':sign_release(manifest(row),load_release_private_key()),'public_key':load_release_public_key()})
    except LicenseError:raise APIError('Assinatura da versão indisponível',503)

@endpoint
def status(request):
    method(request,'GET');platform_user(request,roles=('platform_ops',))
    rows=list(ProductRelease.objects.filter(active=True))
    def newest(component, channel='stable'):
        matches=[row for row in rows if row.component==component and row.channel==channel]
        return max(matches,key=lambda row:tuple(map(int,row.version.split('.'))),default=None)
    cloud=newest('cloud');terminal=newest('terminal')
    cloud_version=str(getattr(settings,'PDV_VERSION',''))
    return JsonResponse({'cloud':{'running_version':cloud_version,
                                  'published_version':cloud.version if cloud else None,
                                  'deployment_pending':bool(cloud and cloud.version != cloud_version)},
                         'terminal':{'published_version':terminal.version if terminal else None,
                                     'manifest_available':bool(terminal)}})

@endpoint
def releases(request):
    method(request,'GET','POST');platform_user(request,roles=('platform_ops',))
    if request.method=='GET':
        return JsonResponse({'results':[{'id':str(x.pk),'active':x.active,**manifest(x)} for x in ProductRelease.objects.order_by('-published_at')[:100]]})
    data=payload(request)
    fields(data,('component','channel','version','url','sha256','size_bytes','notes','kind','min_version'),('component','channel','version','url','sha256','size_bytes'))
    for key in ('kind','min_version'):
        if data.get(key) in ('',None):data.pop(key,None)
    try:validate_release({'schema':1,'purpose':'pdv2-release',**data})
    except (LicenseError,ValueError) as exc:raise APIError(str(exc))
    for key,size in [('component',20),('channel',20),('version',50),('url',2000),('sha256',64)]:data[key]=text(data[key],key,size)
    data['notes']=text(data.get('notes',''),'Notas',4000,optional=True)
    try:
        with transaction.atomic():
            row=ProductRelease.objects.create(**data);audit(None,request.user,'release.published',row,{'version':row.version,'sha256':row.sha256})
    except IntegrityError:raise APIError('Versão já publicada; publique uma nova versão para alterar o pacote',409)
    return JsonResponse({'id':str(row.pk),**manifest(row)},status=201)

@endpoint
def withdraw(request,pk):
    method(request,'POST');platform_user(request,roles=('platform_ops',))
    with transaction.atomic():
        row=ProductRelease.objects.select_for_update().filter(pk=pk).first()
        if not row:raise APIError('Versão não encontrada',404)
        row.active=False;row.save(update_fields=['active']);audit(None,request.user,'release.withdrawn',row)
    return JsonResponse({'id':str(row.pk),'active':False})
