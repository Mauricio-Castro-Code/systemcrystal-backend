import os
import runpy
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    SECURE_SSL_REDIRECT=True,
    CORS_ALLOWED_ORIGINS=["https://systemcrystal-frontend.vercel.app"],
)
class RailwayProxyTests(SimpleTestCase):
    origin = "https://systemcrystal-frontend.vercel.app"
    login_url = "/api/auth/login/"

    def proxy_setting(self, **environment):
        # Ejecutar la configuración real sin leer .env ni abrir conexiones.
        with patch.dict(os.environ, {
            "DJANGO_LOAD_ENV": "False", "DJANGO_DEBUG": "True", **environment,
        }, clear=True):
            config = runpy.run_path(str(Path(__file__).resolve().parents[1] / "config/settings.py"))
        return config["SECURE_PROXY_SSL_HEADER"]

    def preflight(self, **headers):
        return self.client.options(
            self.login_url, HTTP_ORIGIN=self.origin,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type", **headers,
        )

    def test_railway_https_preflight_is_not_redirected(self):
        with override_settings(SECURE_PROXY_SSL_HEADER=self.proxy_setting(RAILWAY_PROJECT_ID="project")):
            response = self.preflight(HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Location", response)
        self.assertEqual(response["Access-Control-Allow-Origin"], self.origin)

    def test_railway_https_login_reaches_validation(self):
        with override_settings(SECURE_PROXY_SSL_HEADER=self.proxy_setting(RAILWAY_PROJECT_ID="project")):
            response = self.client.post(
                self.login_url, data="{}", content_type="application/json",
                HTTP_ORIGIN=self.origin, HTTP_X_FORWARDED_PROTO="https",
            )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Location", response)
        self.assertEqual(response["Access-Control-Allow-Origin"], self.origin)

    def test_non_railway_does_not_trust_forwarded_header_by_default(self):
        with override_settings(SECURE_PROXY_SSL_HEADER=self.proxy_setting()):
            self.assertEqual(self.preflight(HTTP_X_FORWARDED_PROTO="https").status_code, 301)

    def test_explicit_proxy_override_is_respected(self):
        self.assertIsNone(self.proxy_setting(
            RAILWAY_PROJECT_ID="project", DJANGO_TRUST_PROXY_SSL_HEADER="False",
        ))
        self.assertEqual(self.proxy_setting(DJANGO_TRUST_PROXY_SSL_HEADER="True"),
                         ("HTTP_X_FORWARDED_PROTO", "https"))

    def test_plain_http_still_redirects_to_https(self):
        with override_settings(SECURE_PROXY_SSL_HEADER=self.proxy_setting(RAILWAY_PROJECT_ID="project")):
            response = self.preflight()
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "https://testserver/api/auth/login/")
