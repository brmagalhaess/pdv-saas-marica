from django.conf import settings
from django.db import connection
from django.http import FileResponse, HttpResponse, JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie


@ensure_csrf_cookie
def page(request, name):
    filename = {"landing": "landing.html", "login": "login.html",
                "app": "app.html", "platform": "app.html"}[name]
    path = settings.WEB_DIR / filename
    if not path.is_file():
        return JsonResponse({"error": "Página indisponível"}, status=404)
    return FileResponse(path.open("rb"), content_type="text/html; charset=utf-8")


def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return JsonResponse({"status": "ok"})


def public_asset(request, name):
    files = {"manifest": ("manifest.webmanifest", "application/manifest+json"),
             "sw": ("sw.js", "application/javascript"),
             "offline": ("offline.html", "text/html; charset=utf-8")}
    filename, content_type = files[name]
    path = settings.WEB_DIR / filename
    if not path.is_file():
        return JsonResponse({"error": "Arquivo indisponível"}, status=404)
    response = FileResponse(path.open("rb"), content_type=content_type)
    if name == "sw":
        response["Service-Worker-Allowed"] = "/"
    return response


PRIVATE_PATHS = ("/app/", "/platform/", "/login/", "/api/", "/control/")


def _public_origin():
    return getattr(settings, "PDV_PUBLIC_URL", "https://pdv2.crowntech.com.br").rstrip("/")


def robots(request):
    lines = ["User-agent: *", "Allow: /", *(f"Disallow: {path}" for path in PRIVATE_PATHS),
             "", f"Sitemap: {_public_origin()}/sitemap.xml", ""]
    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")


def sitemap(request):
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"  <url><loc>{_public_origin()}/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>\n"
            "</urlset>\n")
    return HttpResponse(body, content_type="application/xml; charset=utf-8")
