"""Descargas completas de cada sección, con un Excel tabular dentro de un ZIP."""
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from django.http import FileResponse
from django.utils import timezone
from rest_framework.views import APIView

from .backups import BACKUP_TIME_ZONE, BackupSnapshot, client_tables, note_tables, save_tables
from .models import Client, ClientAddress, Order
from .permissions import IsAdminOrVentas


def excel_archive_response(tables, name):
    workbook = BytesIO()
    save_tables(workbook, tables)
    archive = BytesIO()
    with ZipFile(archive, "w", ZIP_DEFLATED) as output:
        output.writestr(f"{name}.xlsx", workbook.getvalue())
    archive.seek(0)
    stamp = timezone.now().astimezone(BACKUP_TIME_ZONE).strftime("%Y-%m-%d_%H-%M-%S")
    response = FileResponse(
        archive, as_attachment=True, filename=f"{name}_{stamp}.zip",
        content_type="application/zip",
    )
    response["Cache-Control"] = "private, no-store"
    return response


class ClientDirectoryExportView(APIView):
    permission_classes = [IsAdminOrVentas]

    def get(self, request):
        snapshot = BackupSnapshot(
            captured_at=timezone.now(),
            clients=list(Client.objects.order_by("pk")),
            addresses=list(ClientAddress.objects.select_related("client").order_by("pk")),
            orders=[],
        )
        return excel_archive_response(client_tables(snapshot), "Clientes")


class ActiveOrdersExportView(APIView):
    permission_classes = [IsAdminOrVentas]
    archived = False
    filename = "Notas_Activas"

    def get(self, request):
        orders = Order.objects.select_related("quotation", "quotation__client").prefetch_related(
            "quotation__equipment_items", "extra_costs", "workflow_events",
        ).order_by("pk")
        archived_status = {"operational_status": Order.OperationalStatus.RECOGIDO}
        orders = orders.filter(**archived_status) if self.archived else orders.exclude(**archived_status)
        return excel_archive_response(note_tables(list(orders)), self.filename)


class ArchivedOrdersExportView(ActiveOrdersExportView):
    archived = True
    filename = "Registro_Notas"
