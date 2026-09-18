from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management.base import BaseCommand, CommandError

from api.backups import create_backup_archive
from api.drive_backup import DriveBackupError, DriveBackupUploader


class Command(BaseCommand):
    help = "Respalda clientes y notas en Google Drive; no modifica datos ni elimina copias anteriores."

    def add_arguments(self, parser):
        parser.add_argument("--local-only", action="store_true", help="Generar sin subir a Google.")
        parser.add_argument("--output-dir", type=Path, help="Destino local, obligatorio con --local-only.")

    def handle(self, *args, **options):
        if options["local_only"]:
            if not options["output_dir"]:
                raise CommandError("Indica --output-dir junto con --local-only.")
            archive = create_backup_archive(options["output_dir"])
            self.stdout.write(self.style.SUCCESS(f"Respaldo local generado: {archive.name}"))
            return

        uploader = None
        try:
            # Validar credenciales antes de consultar datos privados.
            uploader = DriveBackupUploader.from_environment()
            with TemporaryDirectory(prefix="crystal-drive-backup-") as temporary:
                archive = create_backup_archive(Path(temporary))
                uploader.upload(archive)
                self.stdout.write(self.style.SUCCESS("Respaldo subido y verificado en Google Drive."))
        except DriveBackupError as error:
            raise CommandError(str(error)) from None
        except Exception:
            # Los logs del job no deben revelar SQL, registros de clientes ni credenciales.
            raise CommandError("Falló la generación o verificación del respaldo. Revisa conexión y plantilla; no se eliminaron copias anteriores.") from None
        finally:
            if uploader:
                uploader.close()
