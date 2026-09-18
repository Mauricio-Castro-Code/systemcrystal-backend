"""Configuración del exportador independiente; no levanta la API ni lee .env."""
import os
from pathlib import Path
from urllib.parse import urlsplit

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
database_url = os.environ.get("BACKUP_DATABASE_URL", "").strip()
if not database_url or urlsplit(database_url).scheme not in {"postgres", "postgresql"}:
    raise ImproperlyConfigured("Configura BACKUP_DATABASE_URL con la conexión PostgreSQL de lectura.")

DATABASES = {"default": dj_database_url.parse(database_url, ssl_require=True)}
DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = True
DATABASES["default"]["OPTIONS"].update({
    "connect_timeout": 20,
    "options": "-c default_transaction_read_only=on -c statement_timeout=120000",
})
# No se usa para firmar sesiones: este proceso solo genera archivos.
SECRET_KEY = "backup-exporter-no-http-sessions"
INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes", "api"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
TIME_ZONE = "America/Mexico_City"
USE_TZ = True
LANGUAGE_CODE = "es-mx"
NOTE_EXCEL_TEMPLATE_PATH = BASE_DIR / "templates" / "excel" / "Nota.xlsx"
