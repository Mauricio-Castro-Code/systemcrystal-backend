import os

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

ENV_FILE = BASE_DIR / ".env"


def load_env_file() -> None:
    if not ENV_FILE.exists():
        return

    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        normalized_value = value.strip().strip('"').strip("'")
        normalized_key = key.strip()

        # El .env siempre gana sobre variables vacias o ausentes del shell.
        # Si la variable ya esta exportada con un valor no vacio, respetamos
        # el shell (util para overrides en CI/produccion).
        existing = os.environ.get(normalized_key, "")
        if not existing.strip():
            os.environ[normalized_key] = normalized_value


def get_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def get_list(name: str, default: list[str]) -> list[str]:
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    return [item.strip() for item in raw_value.split(",") if item.strip()]


def get_path(name: str, default: Path) -> Path:
    raw_value = os.getenv(name)

    if raw_value is None or not raw_value.strip():
        return default

    return Path(raw_value.strip()).expanduser()


def get_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    try:
        return int(raw_value.strip())
    except ValueError:
        return default


load_env_file()

SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "django-insecure-change-me-before-production",
)

DEBUG = get_bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = get_list("DJANGO_ALLOWED_HOSTS", ["localhost", "127.0.0.1"])


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "rest_framework.authtoken",
    "api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


def build_database_config() -> dict:
    """
    Si SUPABASE_DATABASE_URL (o DATABASE_URL) esta definido, lo usamos.
    En cualquier otro caso, caemos a SQLite local para desarrollo.

    Para Supabase: pega en .env la cadena que aparece en
    Project Settings -> Database -> Connection string -> URI.
    Recomendado el "Session pooler" (puerto 5432) o "Transaction pooler" (6543).
    Ej: postgresql://postgres.<ref>:<password>@aws-0-us-east-1.pooler.supabase.com:5432/postgres
    """
    database_url = (
        os.getenv("SUPABASE_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()

    if not database_url:
        print(
            "[SystemCrystal] AVISO: SUPABASE_DATABASE_URL vacia. "
            "Usando SQLite local (db.sqlite3) — solo desarrollo.",
        )
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }

    print(
        "[SystemCrystal] Conectando a Supabase Postgres "
        f"({database_url.split('@', 1)[-1].split('/', 1)[0]}).",
    )

    import dj_database_url

    config = dj_database_url.parse(
        database_url,
        conn_max_age=get_int("DB_CONN_MAX_AGE", 60),
        conn_health_checks=True,
        ssl_require=get_bool("DB_SSL_REQUIRE", True),
    )
    config.setdefault("OPTIONS", {})
    config["OPTIONS"].setdefault("sslmode", "require")
    return config


DATABASES = {"default": build_database_config()}


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


LANGUAGE_CODE = "es-mx"

TIME_ZONE = "America/Mexico_City"

USE_I18N = True

USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "COERCE_DECIMAL_TO_STRING": False,
}

CORS_ALLOWED_ORIGINS = get_list(
    "DJANGO_CORS_ALLOWED_ORIGINS",
    ["http://localhost:4200"],
)
CORS_EXPOSE_HEADERS = ["Content-Disposition"]

CSRF_TRUSTED_ORIGINS = get_list(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    ["http://localhost:4200"],
)

REGISTRATION_ACCESS_KEY = os.getenv(
    "REGISTRATION_ACCESS_KEY",
    "CrystalRegister2026",
)

DEFAULT_NOTE_EXCEL_TEMPLATE_PATH = BASE_DIR / "templates" / "excel" / "Nota.xlsx"

NOTE_EXCEL_TEMPLATE_PATH = get_path(
    "NOTE_EXCEL_TEMPLATE_PATH",
    DEFAULT_NOTE_EXCEL_TEMPLATE_PATH,
)

QUOTATION_EXCEL_TEMPLATE_PATH = get_path(
    "QUOTATION_EXCEL_TEMPLATE_PATH",
    NOTE_EXCEL_TEMPLATE_PATH,
)

LIBREOFFICE_BINARY = os.getenv("LIBREOFFICE_BINARY", "").strip()

DOCUMENT_RENDERER_BACKEND = os.getenv("DOCUMENT_RENDERER_BACKEND", "auto").strip() or "auto"

DOCUMENT_RENDER_TIMEOUT_SECONDS = get_int(
    "DOCUMENT_RENDER_TIMEOUT_SECONDS",
    120,
)
