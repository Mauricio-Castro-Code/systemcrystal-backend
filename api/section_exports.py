"""Respaldos manuales: notas individuales con plantilla y directorio de clientes."""
import re
from io import BytesIO
from pathlib import Path
from tempfile import SpooledTemporaryFile, TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

from django.http import FileResponse
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from .backups import (
    BACKUP_TIME_ZONE, BackupSnapshot, client_tables, save_tables, write_individual_note,
)
from .excel_exports import ExcelTemplateExportError
from .models import Client, ClientAddress, Order
from .permissions import IsAdminOrVentas

EXCEL_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def download_response(file, filename, content_type):
    file.seek(0)
    response = FileResponse(file, as_attachment=True, filename=filename, content_type=content_type)
    response["Cache-Control"] = "private, no-store"
    return response


def notes_archive_response(orders, name):
    archive = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    try:
        with TemporaryDirectory(prefix="crystal-section-export-") as directory:
            path = Path(directory) / "nota.xlsx"
            used_names = set()
            with ZipFile(archive, "w", ZIP_DEFLATED) as output:
                for order in orders:
                    folio = re.sub(r"[^A-Za-z0-9_-]", "_", order.order_id)[:100] or "nota"
                    filename = f"{folio}.xlsx"
                    while filename.casefold() in used_names:
                        folio += f"_{order.pk}"
                        filename = f"{folio}.xlsx"
                    used_names.add(filename.casefold())
                    try:
                        write_individual_note(order, path, allow_tabular_fallback=False)
                    except ExcelTemplateExportError as error:
                        raise ValidationError(
                            f"No se pudo exportar la nota {order.order_id}: {error} "
                            "No se descargó un respaldo incompleto."
                        ) from error
                    output.write(path, filename)
        stamp = timezone.now().astimezone(BACKUP_TIME_ZONE).strftime("%Y-%m-%d_%H-%M-%S")
        return download_response(archive, f"{name}_{stamp}.zip", "application/zip")
    except Exception:
        archive.close()
        raise


class ClientDirectoryExportView(APIView):
    permission_classes = [IsAdminOrVentas]

    def get(self, request):
        snapshot = BackupSnapshot(
            captured_at=timezone.now(),
            clients=list(Client.objects.order_by("pk")),
            addresses=list(ClientAddress.objects.select_related("client").order_by("pk")),
            orders=[],
        )
        workbook = BytesIO()
        save_tables(workbook, client_tables(snapshot))
        return download_response(workbook, "Clientes.xlsx", EXCEL_CONTENT_TYPE)


class ActiveOrdersExportView(APIView):
    permission_classes = [IsAdminOrVentas]
    archived = False
    filename = "Notas_Activas"

    def get(self, request):
        orders = Order.objects.select_related("quotation").prefetch_related(
            "quotation__equipment_items",
        ).order_by("pk")
        archived_status = {"operational_status": Order.OperationalStatus.RECOGIDO}
        orders = orders.filter(**archived_status) if self.archived else orders.exclude(**archived_status)
        try:
            return notes_archive_response(orders, self.filename)
        except FileNotFoundError as error:
            raise NotFound("No se encontró la plantilla de notas. No se generó el respaldo.") from error


class ArchivedOrdersExportView(ActiveOrdersExportView):
    archived = True
    filename = "Registro_Notas"
