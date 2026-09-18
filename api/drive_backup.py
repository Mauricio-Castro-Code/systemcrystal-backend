"""Subida privada a una carpeta concreta de Drive usando OAuth del propietario."""
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import requests

DRIVE_API = "https://www.googleapis.com/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class DriveBackupError(Exception):
    pass


class DriveBackupUploader:
    def __init__(self, folder_id, credentials, session=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", folder_id or ""):
            raise DriveBackupError("Configura BACKUP_DRIVE_FOLDER_ID con el ID de la carpeta.")
        if not all(credentials.get(key) for key in ("client_id", "client_secret", "refresh_token")):
            raise DriveBackupError("Faltan credenciales OAuth de Google para el respaldo.")
        self.folder_id = folder_id
        self.credentials = credentials
        self.session = session or requests.Session()

    @classmethod
    def from_environment(cls):
        try:
            credentials = json.loads(os.environ.get("BACKUP_GOOGLE_OAUTH_JSON", "{}"))
        except (ValueError, TypeError):
            raise DriveBackupError("BACKUP_GOOGLE_OAUTH_JSON no es JSON válido.") from None
        if not isinstance(credentials, dict):
            raise DriveBackupError("BACKUP_GOOGLE_OAUTH_JSON debe ser un objeto.")
        return cls(os.environ.get("BACKUP_DRIVE_FOLDER_ID", ""), credentials)

    def request(self, method, url, **kwargs):
        try:
            response = self.session.request(method, url, timeout=(20, 180), allow_redirects=False, **kwargs)
        except requests.RequestException:
            # No imprimir excepciones HTTP: podrían incluir tokens o URLs privadas.
            raise DriveBackupError("No se pudo conectar con Google Drive. Se conserva cualquier respaldo anterior.") from None
        if not 200 <= response.status_code < 300:
            status = response.status_code
            response.close()
            raise DriveBackupError(f"Google rechazó el respaldo (HTTP {status}). Revisa autorización, carpeta y espacio disponible.")
        return response

    def authenticate(self):
        with self.request("POST", TOKEN_URL, data={
            "client_id": self.credentials["client_id"],
            "client_secret": self.credentials["client_secret"],
            "refresh_token": self.credentials["refresh_token"],
            "grant_type": "refresh_token",
        }) as response:
            token = response.json().get("access_token")
        if not token:
            raise DriveBackupError("Google no devolvió una autorización válida.")
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def validate_folder(self):
        with self.request("GET", f"{DRIVE_API}/files/{self.folder_id}", params={
            "fields": "id,mimeType,trashed,capabilities(canAddChildren)", "supportsAllDrives": "true",
        }) as response:
            folder = response.json()
        if (folder.get("trashed") or folder.get("mimeType") != "application/vnd.google-apps.folder"
                or not folder.get("capabilities", {}).get("canAddChildren")):
            raise DriveBackupError("La carpeta de respaldo no existe o no permite agregar archivos.")
        token = None
        while True:
            params = {"fields": "nextPageToken,permissions(type)", "supportsAllDrives": "true"}
            if token:
                params["pageToken"] = token
            with self.request("GET", f"{DRIVE_API}/files/{self.folder_id}/permissions", params=params) as response:
                permissions = response.json()
            if any(permission.get("type") in {"anyone", "domain"} for permission in permissions.get("permissions", [])):
                raise DriveBackupError("Usa una carpeta restringida a personas concretas, sin acceso público ni de todo el dominio.")
            token = permissions.get("nextPageToken")
            if not token:
                break

    def upload(self, path: Path):
        path = Path(path)
        self.authenticate()
        self.validate_folder()
        with self.request("POST", "https://www.googleapis.com/upload/drive/v3/files", params={
            "uploadType": "resumable", "supportsAllDrives": "true", "fields": "id,size,md5Checksum",
        }, headers={"X-Upload-Content-Type": "application/zip", "X-Upload-Content-Length": str(path.stat().st_size)},
            json={"name": path.name, "parents": [self.folder_id],
                  "appProperties": {"application": "systemcrystal", "kind": "daily-backup"}}) as response:
            upload_url = response.headers.get("Location", "")
        parsed = urlsplit(upload_url)
        if parsed.scheme != "https" or parsed.hostname != "www.googleapis.com" or parsed.port not in (None, 443):
            raise DriveBackupError("Google devolvió un destino de subida inesperado.")
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "md5").hexdigest()
            stream.seek(0)
            with self.request("PUT", upload_url, data=stream, headers={"Content-Type": "application/zip"}) as response:
                result = response.json()
        if (not result.get("id") or str(result.get("size")) != str(path.stat().st_size)
                or result.get("md5Checksum") != checksum):
            raise DriveBackupError("No se pudo verificar la integridad del respaldo en Drive.")
        return result["id"]

    def close(self):
        self.session.close()
