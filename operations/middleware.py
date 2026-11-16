"""Response hardening without storing customer data in browser/proxy caches."""
import contextvars
import logging
import uuid

_request_id=contextvars.ContextVar('pdv_request_id',default='-')

class RequestIdFilter(logging.Filter):
    """Adds the current request id (or '-') to every log record."""
    def filter(self,record):
        record.request_id=_request_id.get()
        return True

class RequestIdMiddleware:
    """Tags log lines and the response of one request with the same id."""
    def __init__(self,get_response):self.get_response=get_response
    def __call__(self,request):
        request_id=uuid.uuid4().hex[:16]
        token=_request_id.set(request_id)
        try:
            response=self.get_response(request)
        finally:
            _request_id.reset(token)
        response['X-Request-ID']=request_id
        return response

class ResponsePolicyMiddleware:
    def __init__(self,get_response):self.get_response=get_response
    def __call__(self,request):
        response=self.get_response(request)
        if request.path.startswith(('/api/','/app/','/platform/','/login/','/control/')):
            response['Cache-Control']='private, no-store'
            response['Pragma']='no-cache'
        if not request.path.startswith('/control/'):
            response['Content-Security-Policy']=("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; font-src 'self'; connect-src 'self'; worker-src 'self'; "
                "manifest-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        response['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
        return response
