from django.conf import settings
from django.db import connection
from django.http import FileResponse, JsonResponse
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
