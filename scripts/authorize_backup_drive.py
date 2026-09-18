"""Autoriza Drive en el navegador local; escribe credenciales privadas sin imprimirlas."""
import argparse
import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests


def authorize(client_config: Path, output: Path):
    if output.exists():
        raise ValueError("El archivo de salida ya existe; elige otro nombre para no sobrescribir credenciales.")
    client = json.loads(client_config.read_text(encoding="utf-8")).get("installed", {})
    if not client.get("client_id") or not client.get("client_secret"):
        raise ValueError("Usa el JSON de un cliente OAuth de tipo Aplicación de escritorio.")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            if not secrets.compare_digest(query.get("state", [""])[0], state):
                self.send_error(400, "Solicitud no reconocida")
                return
            result.update({"code": query.get("code", [""])[0], "error": query.get("error", [""])[0]})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Autorización recibida. Puedes cerrar esta pestaña y volver a la terminal.".encode())

        def log_message(self, *args):
            pass  # La URL del callback contiene un código de autorización privado.

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        redirect_uri = f"http://127.0.0.1:{server.server_port}/"
        params = {
            "client_id": client["client_id"], "redirect_uri": redirect_uri,
            "response_type": "code", "scope": "https://www.googleapis.com/auth/drive",
            "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        }
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
        print("Autoriza la cuenta propietaria de la carpeta en el navegador.")
        if not webbrowser.open(url):
            print("Abre esta URL en este mismo equipo:\n" + url)
        deadline = time.monotonic() + 300
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result.get("code"):
        raise ValueError("No se completó la autorización de Google en cinco minutos.")
    response = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": client["client_id"], "client_secret": client["client_secret"],
        "code": result["code"], "redirect_uri": redirect_uri,
        "grant_type": "authorization_code", "code_verifier": verifier,
    }, timeout=30, allow_redirects=False)
    if response.status_code != 200:
        raise ValueError("Google rechazó el intercambio de autorización; revisa el cliente OAuth.")
    refresh_token = response.json().get("refresh_token")
    if not refresh_token:
        raise ValueError("Google no entregó un refresh token. Repite el consentimiento.")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"client_id": client["client_id"], "client_secret": client["client_secret"],
                   "refresh_token": refresh_token}, stream)
    print(f"Credenciales guardadas de forma privada en {output}. No las compartas en chats ni en Git.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("var/backup-google-oauth.json"))
    arguments = parser.parse_args()
    try:
        authorize(arguments.client_config, arguments.output)
    except (ValueError, OSError, requests.RequestException):
        parser.exit(1, "No se completó la autorización. Comprueba el JSON de escritorio, la API de Drive y el consentimiento.\n")
