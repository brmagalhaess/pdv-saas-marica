from django.conf import settings
from django.db import connection
from django.http import FileResponse, HttpResponse, JsonResponse
from django.utils.html import escape
from django.views.decorators.csrf import ensure_csrf_cookie


@ensure_csrf_cookie
def page(request, name):
    filename = {"landing": "landing.html", "login": "login.html",
                "app": "app.html", "platform": "app.html", "privacy": "privacidade.html",
                "support": "suporte.html", "account_deletion": "excluir-conta.html"}[name]
    path = settings.WEB_DIR / filename
    if not path.is_file():
        return JsonResponse({"error": "Página indisponível"}, status=404)
    if name in ("privacy", "support", "account_deletion"):
        # The public pages show the support e-mail and the site address configured for this server.
        body = (path.read_text(encoding="utf-8")
                .replace("__PDV_SUPPORT_EMAIL__", escape(settings.PDV_SUPPORT_EMAIL))
                .replace("__PDV_PUBLIC_URL__", escape(_public_origin())))
        return HttpResponse(body, content_type="text/html; charset=utf-8")
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


# Public pages linked from the PDV2 Gestão app and the store listings.
PUBLIC_PAGES = ("/privacidade/", "/suporte/", "/excluir-conta/")


def robots(request):
    lines = ["User-agent: *", "Allow: /", *(f"Disallow: {path}" for path in PRIVATE_PATHS),
             "", f"Sitemap: {_public_origin()}/sitemap.xml", ""]
    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")


def sitemap(request):
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"  <url><loc>{_public_origin()}/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>\n"
            + "".join(f"  <url><loc>{_public_origin()}{path}</loc><changefreq>yearly</changefreq>"
                      "<priority>0.3</priority></url>\n" for path in PUBLIC_PAGES)
            + "</urlset>\n")
    return HttpResponse(body, content_type="application/xml; charset=utf-8")
