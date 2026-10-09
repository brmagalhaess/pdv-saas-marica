"""Django settings for PDV2. Production refuses implicit secrets, hosts or DB."""
import os
import sys
from ipaddress import ip_network
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BASE_DIR.parent
WEB_DIR = ROOT_DIR / "web"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

DEBUG = os.environ.get("PDV_DEBUG", "0") == "1" and os.environ.get("PDV_ENV") != "production"
PLATFORM_MFA_REQUIRED = os.environ.get("PDV_PLATFORM_MFA_REQUIRED", "1" if not DEBUG else "0") == "1"
SECRET_KEY = os.environ.get("PDV_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "development-only-change-before-deploying-pdv2"
    else:
        raise RuntimeError("Configure PDV_SECRET_KEY em produção")
if not DEBUG and len(SECRET_KEY) < 50:
    raise RuntimeError("PDV_SECRET_KEY deve conter ao menos 50 caracteres em produção")
if not DEBUG and not (os.environ.get("PDV_LICENSE_PRIVATE_KEY") or
                      os.environ.get("PDV_LICENSE_PRIVATE_KEY_FILE")):
    raise RuntimeError("Configure a chave privada de licença em produção")

hosts = os.environ.get("PDV_ALLOWED_HOSTS", "")
if not hosts and not DEBUG:
    raise RuntimeError("Configure PDV_ALLOWED_HOSTS em produção")
ALLOWED_HOSTS = [x.strip() for x in hosts.split(",") if x.strip()] or ["localhost", "127.0.0.1", "testserver"]
CSRF_TRUSTED_ORIGINS = [x.strip() for x in os.environ.get("PDV_CSRF_TRUSTED_ORIGINS", "").split(",") if x.strip()]

database_url = os.environ.get("DATABASE_URL", "")
if database_url:
    parsed = urlparse(database_url)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise RuntimeError("DATABASE_URL deve usar PostgreSQL")
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "localhost",
        "PORT": parsed.port or 5432,
        "CONN_MAX_AGE": 60,
        "OPTIONS": {key: values[-1] for key, values in parse_qs(parsed.query).items()
                    if key in ("sslmode", "options")},
    }}
elif DEBUG:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}
else:
    raise RuntimeError("Configure DATABASE_URL PostgreSQL em produção")

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "core", "operations.apps.OperationsConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "operations.middleware.ResponsePolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "pdvcloud.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [WEB_DIR],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "pdvcloud.wsgi.application"
ASGI_APPLICATION = "pdvcloud.asgi.application"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/assets/"
STATICFILES_DIRS = [WEB_DIR / "assets"] if (WEB_DIR / "assets").exists() else []
STATIC_ROOT = ROOT_DIR / "staticfiles"
STORAGES = {"staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 8 * 3600
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
SECURE_REDIRECT_EXEMPT = [r"^health/?$"]
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
PDV_TRUST_PROXY = os.environ.get("PDV_TRUST_PROXY", "0") == "1"
PDV_TRUSTED_PROXY_NETWORKS = tuple(
    ip_network(value.strip(), strict=False)
    for value in os.environ.get("PDV_TRUSTED_PROXY_CIDRS", "").split(",") if value.strip()
)
if PDV_TRUST_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

CSRF_FAILURE_VIEW = "core.api_common.csrf_failure"
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.environ.get("PDV_EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("PDV_EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("PDV_EMAIL_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("PDV_EMAIL_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("PDV_EMAIL_USE_TLS", "1") == "1"
DEFAULT_FROM_EMAIL = os.environ.get("PDV_DEFAULT_FROM_EMAIL", "PDV2 <no-reply@pdv2.local>")
