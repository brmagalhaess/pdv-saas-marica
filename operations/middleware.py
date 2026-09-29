"""Response hardening without storing customer data in browser/proxy caches."""
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
