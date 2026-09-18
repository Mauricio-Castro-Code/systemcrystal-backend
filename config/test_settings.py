"""Pruebas aisladas: no leer secretos ni conectar con Supabase."""
import os

os.environ["DJANGO_LOAD_ENV"] = "False"
os.environ["DJANGO_DEBUG"] = "True"
os.environ["SUPABASE_DATABASE_URL"] = ""
os.environ["DATABASE_URL"] = ""

from .settings import *  # noqa: E402,F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
SECURE_SSL_REDIRECT = False
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
OPENAI_API_KEY = ""
GOOGLE_MAPS_API_KEY = ""
