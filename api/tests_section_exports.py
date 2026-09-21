from copy import copy
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from openpyxl import load_workbook
from rest_framework.test import APITestCase

from .models import Client, ClientAddress, Order, QuotationItem, set_user_role
from .services import create_order_from_note
from .tests import build_quotation_payload


class SectionExportTests(APITestCase):
    endpoints = ("clients-export-excel", "active-orders-export-excel", "archived-orders-export-excel")

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="export-sales")
        set_user_role(self.user, "ventas")
        self.client.force_authenticate(self.user)
        template = Path(__file__).resolve().parent.parent / "templates/excel/Nota.xlsx"
        override = self.settings(NOTE_EXCEL_TEMPLATE_PATH=template)
        override.enable()
        self.addCleanup(override.disable)

    def export(self, endpoint, params=None):
        with patch("api.excel_exports._generate_pdf") as pdf:
            response = self.client.get(reverse(endpoint), params or {})
            self.assertEqual(response.status_code, 200)
            expected_type = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                             if endpoint == self.endpoints[0] else "application/zip")
            self.assertEqual(response["Content-Type"], expected_type)
            self.assertIn("attachment;", response["Content-Disposition"])
            self.assertEqual(response["Cache-Control"], "private, no-store")
            content = b"".join(response.streaming_content)
            response.close()
            pdf.assert_not_called()
        return content

    def workbook(self, content):
        book = load_workbook(BytesIO(content), data_only=True)
        self.addCleanup(book.close)
        return book

    def test_all_clients_and_addresses_in_one_excel_without_pagination_or_search(self):
        for index in range(32):
            Client.objects.create(client_name=f"Cliente {index}")
        customer = Client.objects.create(client_name='=SUM(1,2)', phone_number="0012345678")
        ClientAddress.objects.create(client=customer, address_line="Dirección alternativa")
        book = self.workbook(self.export(self.endpoints[0], {"page": 2, "search": "inexistente"}))
        self.assertEqual(book["Clientes"].max_row, 34)
        self.assertEqual(book["Clientes"]["C34"].value, "=SUM(1,2)")
        self.assertEqual(book["Clientes"]["C34"].data_type, "s")
        self.assertEqual(book["Clientes"]["E34"].value, "0012345678")
        self.assertEqual(book["Direcciones"]["C2"].value, "Dirección alternativa")
        self.assertEqual(book["Clientes"].freeze_panes, "A2")

    def test_each_note_keeps_template_format_and_section_membership(self):
        active = create_order_from_note(build_quotation_payload())
        cancelled = create_order_from_note(build_quotation_payload())
        cancelled.is_cancelled = True
        cancelled.save()
        archived = create_order_from_note(build_quotation_payload())
        archived.operational_status = Order.OperationalStatus.RECOGIDO
        archived.save()
        before = list(Order.objects.values())
        template = load_workbook(settings.NOTE_EXCEL_TEMPLATE_PATH)
        self.addCleanup(template.close)
        for endpoint, orders in [(self.endpoints[1], [active, cancelled]), (self.endpoints[2], [archived])]:
            with ZipFile(BytesIO(self.export(endpoint, {"folder": "por-cobrar", "page": 10}))) as archive:
                self.assertEqual(archive.namelist(), [f"{order.order_id}.xlsx" for order in orders])
                for order in orders:
                    content = archive.read(f"{order.order_id}.xlsx")
                    book = self.workbook(content)
                    sheet = book.active
                    self.assertEqual(sheet["I15"].value, order.order_id)
                    self.assertEqual(sheet["B9"].value, order.quotation.client_name.upper())
                    self.assertEqual(sheet["J49"].value, 3650)
                    self.assertEqual(sheet.merged_cells, template.active.merged_cells)
                    self.assertEqual(copy(sheet["B9"].font), copy(template.active["B9"].font))
                    with ZipFile(BytesIO(content)) as note, ZipFile(settings.NOTE_EXCEL_TEMPLATE_PATH) as original:
                        media = [name for name in original.namelist() if name.startswith("xl/media/")]
                        self.assertTrue(media)
                        for name in media:
                            self.assertEqual(note.read(name), original.read(name))
        self.assertEqual(before, list(Order.objects.values()))

    def test_seventy_active_notes_produce_seventy_complete_workbooks(self):
        orders = [create_order_from_note(build_quotation_payload()) for _ in range(70)]
        with ZipFile(BytesIO(self.export(self.endpoints[1], {"page": 2, "search": "no-match"}))) as archive:
            self.assertEqual(len(archive.namelist()), 70)
            for order in orders:
                book = self.workbook(archive.read(f"{order.order_id}.xlsx"))
                self.assertEqual(book.active["I15"].value, order.order_id)
                self.assertEqual(book.active["J49"].value, 3650)

    def test_folios_cannot_escape_or_overwrite_another_note(self):
        first = create_order_from_note(build_quotation_payload())
        second = create_order_from_note(build_quotation_payload())
        Order.objects.filter(pk=first.pk).update(order_id="../one")
        Order.objects.filter(pk=second.pk).update(order_id="..\\one")
        with ZipFile(BytesIO(self.export(self.endpoints[1]))) as archive:
            names = archive.namelist()
            self.assertEqual(len(set(names)), 2)
            self.assertTrue(all("/" not in name and "\\" not in name and ".." not in name for name in names))

    def test_unrenderable_note_returns_error_instead_of_partial_or_tabular_backup(self):
        create_order_from_note(build_quotation_payload())
        long_note = create_order_from_note(build_quotation_payload())
        for index in range(22):
            QuotationItem.objects.create(quotation=long_note.quotation, equipment=f"Extra {index}", quantity=1)
        response = self.client.get(reverse(self.endpoints[1]))
        self.assertEqual(response.status_code, 400)
        self.assertIn(long_note.order_id, str(response.data))
        self.assertIn("21 renglones", str(response.data))

    def test_missing_template_returns_error(self):
        create_order_from_note(build_quotation_payload())
        with self.settings(NOTE_EXCEL_TEMPLATE_PATH="/nonexistent/nota.xlsx"):
            self.assertEqual(self.client.get(reverse(self.endpoints[1])).status_code, 404)

    def test_empty_sections_return_empty_zip_or_client_headers(self):
        book = self.workbook(self.export(self.endpoints[0]))
        self.assertTrue(all(sheet.max_row == 1 for sheet in book))
        for endpoint in self.endpoints[1:]:
            with ZipFile(BytesIO(self.export(endpoint))) as archive:
                self.assertEqual(archive.namelist(), [])

    def test_anonymous_and_drivers_cannot_export(self):
        for role in (None, "chofer"):
            if role:
                set_user_role(self.user, role)
                self.user.refresh_from_db()
            self.client.force_authenticate(self.user if role else None)
            for endpoint in self.endpoints:
                with self.subTest(role=role, endpoint=endpoint):
                    self.assertIn(self.client.get(reverse(endpoint)).status_code, (401, 403))

    def test_admin_can_export_every_section(self):
        set_user_role(self.user, "admin")
        self.user.refresh_from_db()
        for endpoint in self.endpoints:
            self.export(endpoint)
