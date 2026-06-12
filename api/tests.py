from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from .excel_exports import build_note_like_cell_writes
from .models import Client, InventoryProduct, Order, OrderWorkflowEvent, Quotation


def build_quotation_payload() -> dict:
    return {
        "clientInfo": {
            "fullName": "Grupo Marea Eventos",
            "phoneNumber": "+52 55 1345 8890",
            "birthDate": None,
            "address": "Av. Insurgentes Sur 845",
            "neighborhood": "Napoles",
            "reference": "Frente al parque",
            "deliveryInstructions": "Lunes antes de las 12 PM",
        },
        "schedule": {
            "deliveryDate": "2026-03-18",
            "eventDate": "2026-03-19",
            "collectionDate": "2026-03-20",
        },
        "logistics": {
            "freight": "450.00",
            "applyTax": False,
            "securityDeposit": "1500.00",
        },
        "equipmentItems": [
            {
                "quantity": 20,
                "equipment": "Silla Tiffany",
                "unitPrice": "85.00",
                "total": "1700.00",
            }
        ],
        "summary": {
            "subtotal": "1700.00",
            "freight": "450.00",
            "taxAmount": "0.00",
            "securityDeposit": "1500.00",
            "discount": "0.00",
            "advancePayment": "0.00",
            "totalEstimated": "3650.00",
            "balanceDue": "3650.00",
        },
    }


def create_client_quotation(
    client: Client,
    *,
    status: str,
    address: str,
    neighborhood: str,
    reference: str,
    subtotal: str,
    total_estimated: str,
    delivery_instructions: str = "",
) -> Quotation:
    return Quotation.objects.create(
        client=client,
        status=status,
        client_name=client.client_name,
        phone_number=client.phone_number,
        birth_date=None,
        address=address,
        neighborhood=neighborhood,
        reference=reference,
        delivery_instructions=delivery_instructions,
        delivery_date=date(2026, 3, 18),
        event_date=date(2026, 3, 19),
        collection_date=date(2026, 3, 20),
        freight="0.00",
        security_deposit="0.00",
        subtotal=subtotal,
        total_estimated=total_estimated,
    )


def extract_order_sequence(order_id: str) -> int:
    return int(str(order_id).split("-", 1)[0])


class AuthApiTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="admin",
            email="admin@orderflow.com",
            password="OrderFlow123",
            first_name="Administrador",
            last_name="OrderFlow",
        )

    def test_login_accepts_username(self):
        response = self.client.post(
            reverse("api-login"),
            {
                "identifier": "admin",
                "password": "OrderFlow123",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["displayName"], "Administrador OrderFlow")
        self.assertEqual(response.data["email"], "admin@orderflow.com")
        self.assertTrue(response.data["token"])

    def test_login_accepts_email(self):
        response = self.client.post(
            reverse("api-login"),
            {
                "identifier": "admin@orderflow.com",
                "password": "OrderFlow123",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["email"], "admin@orderflow.com")

    @override_settings(REGISTRATION_ACCESS_KEY="ClaveCrystal2026")
    def test_register_creates_user_with_valid_access_key(self):
        response = self.client.post(
            reverse("api-register"),
            {
                "email": "nuevo@orderflow.com",
                "password": "OrderFlow123",
                "registrationKey": "ClaveCrystal2026",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["email"], "nuevo@orderflow.com")
        self.assertTrue(response.data["token"])

    @override_settings(REGISTRATION_ACCESS_KEY="ClaveCrystal2026")
    def test_register_rejects_invalid_access_key(self):
        response = self.client.post(
            reverse("api-register"),
            {
                "email": "bloqueado@orderflow.com",
                "password": "OrderFlow123",
                "registrationKey": "ClaveIncorrecta",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("registrationKey", response.data)


class AuthenticatedApiTestCase(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="admin",
            email="admin@orderflow.com",
            password="OrderFlow123",
            first_name="Administrador",
            last_name="OrderFlow",
        )
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")


class QuotationWorkflowApiTests(AuthenticatedApiTestCase):
    def test_create_and_confirm_quotation(self):
        create_response = self.client.post(
            reverse("quotation-list"),
            build_quotation_payload(),
            format="json",
        )

        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(create_response.data["clientName"], "Grupo Marea Eventos")
        self.assertEqual(
            create_response.data["quotation"]["clientInfo"]["deliveryInstructions"],
            "Lunes antes de las 12 PM",
        )
        self.assertEqual(Quotation.objects.count(), 1)
        self.assertEqual(Client.objects.count(), 1)

        quotation_id = create_response.data["quotationId"]
        confirm_response = self.client.post(
            reverse("quotation-confirm", kwargs={"quotation_id": quotation_id}),
            {},
            format="json",
        )

        self.assertEqual(confirm_response.status_code, 201)
        self.assertEqual(confirm_response.data["status"], "Confirmado")
        self.assertEqual(Order.objects.count(), 1)

        list_response = self.client.get(reverse("quotation-list"))
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.data), 0)

    def test_dashboard_overview_uses_current_records(self):
        quotation_response = self.client.post(
            reverse("quotation-list"),
            build_quotation_payload(),
            format="json",
        )
        quotation_id = quotation_response.data["quotationId"]

        self.client.post(
            reverse("quotation-confirm", kwargs={"quotation_id": quotation_id}),
            {},
            format="json",
        )

        overview_response = self.client.get(reverse("dashboard-overview"))

        self.assertEqual(overview_response.status_code, 200)
        self.assertEqual(len(overview_response.data["stats"]), 3)
        self.assertEqual(overview_response.data["stats"][1]["value"], "1")

    def test_dashboard_overview_filters_delivery_range(self):
        today = timezone.localdate()

        in_range_payload = build_quotation_payload()
        in_range_payload["clientInfo"]["fullName"] = "Entrega en rango"
        in_range_payload["schedule"]["deliveryDate"] = (today + timedelta(days=7)).isoformat()
        in_range_payload["schedule"]["eventDate"] = (today + timedelta(days=8)).isoformat()
        in_range_payload["schedule"]["collectionDate"] = (today + timedelta(days=9)).isoformat()

        out_of_range_payload = build_quotation_payload()
        out_of_range_payload["clientInfo"]["fullName"] = "Entrega fuera de rango"
        out_of_range_payload["schedule"]["deliveryDate"] = (today + timedelta(days=12)).isoformat()
        out_of_range_payload["schedule"]["eventDate"] = (today + timedelta(days=13)).isoformat()
        out_of_range_payload["schedule"]["collectionDate"] = (today + timedelta(days=14)).isoformat()

        in_range_order = self.client.post(
            reverse("order-list"),
            in_range_payload,
            format="json",
        )
        self.client.post(
            reverse("order-list"),
            out_of_range_payload,
            format="json",
        )

        overview_response = self.client.get(
            reverse("dashboard-overview"),
            {
                "deliveryDateFrom": (today + timedelta(days=6)).isoformat(),
                "deliveryDateTo": (today + timedelta(days=9)).isoformat(),
            },
        )

        self.assertEqual(overview_response.status_code, 200)
        self.assertEqual(
            overview_response.data["deliveryRange"]["group"]["orders"][0]["id"],
            in_range_order.data["orderId"],
        )
        self.assertEqual(
            len(overview_response.data["deliveryRange"]["group"]["orders"]),
            1,
        )

    def test_document_cell_mapping_uses_uppercase_and_balance_formula(self):
        create_response = self.client.post(
            reverse("quotation-list"),
            build_quotation_payload(),
            format="json",
        )

        quotation_id = create_response.data["quotationId"]
        quotation = Quotation.objects.get(quotation_id=quotation_id)
        cell_writes = {
            write.cell_reference: write
            for write in build_note_like_cell_writes(
                document_id=quotation_id,
                created_at=timezone.localtime(quotation.created_at),
                quotation=quotation,
            )
        }

        self.assertEqual(cell_writes["B9"].kind, "text")
        self.assertEqual(cell_writes["B9"].value, "GRUPO MAREA EVENTOS")
        self.assertEqual(cell_writes["B10"].value, "AV. INSURGENTES SUR 845")
        self.assertEqual(cell_writes["B11"].value, "NAPOLES")
        self.assertEqual(cell_writes["B12"].value, "FRENTE AL PARQUE")
        self.assertEqual(cell_writes["I10"].value, "+52 55 1345 8890")
        self.assertEqual(cell_writes["I15"].value, quotation_id)
        self.assertEqual(cell_writes["C21"].value, "SILLA TIFFANY")
        self.assertEqual(cell_writes["B43"].value, "LUNES ANTES DE LAS 12 PM")
        self.assertEqual(cell_writes["J42"].value, Decimal("1700.00"))
        self.assertEqual(cell_writes["J43"].value, Decimal("450.00"))
        self.assertEqual(cell_writes["J44"].value, Decimal("0.00"))
        self.assertEqual(cell_writes["J45"].value, Decimal("1500.00"))
        self.assertEqual(cell_writes["J46"].value, Decimal("3650.00"))
        self.assertEqual(cell_writes["J47"].value, Decimal("0.00"))
        self.assertEqual(cell_writes["J48"].value, Decimal("0.00"))
        self.assertEqual(cell_writes["J49"].kind, "formula")
        self.assertEqual(cell_writes["J49"].value, "=J46-J47-J48")
        self.assertEqual(
            cell_writes["I12"].value,
            timezone.localtime(quotation.created_at).date(),
        )
        self.assertEqual(cell_writes["B17"].value, date(2026, 3, 20))

    def test_tax_discount_and_advance_are_calculated_when_invoice_is_requested(self):
        payload = build_quotation_payload()
        payload["logistics"]["applyTax"] = True
        payload["summary"]["taxAmount"] = "0.00"
        payload["summary"]["discount"] = "150.00"
        payload["summary"]["advancePayment"] = "500.00"
        payload["summary"]["totalEstimated"] = "0.00"
        payload["summary"]["balanceDue"] = "0.00"

        create_response = self.client.post(
            reverse("quotation-list"),
            payload,
            format="json",
        )

        self.assertEqual(create_response.status_code, 201)
        quotation_id = create_response.data["quotationId"]
        quotation = Quotation.objects.get(quotation_id=quotation_id)

        self.assertTrue(quotation.apply_tax)
        self.assertEqual(quotation.tax_amount, Decimal("344.00"))
        self.assertEqual(quotation.discount, Decimal("150.00"))
        self.assertEqual(quotation.advance_payment, Decimal("500.00"))
        self.assertEqual(quotation.total_estimated, Decimal("3994.00"))

        summary = create_response.data["quotation"]["summary"]
        self.assertEqual(summary["taxAmount"], 344.0)
        self.assertEqual(summary["discount"], 150.0)
        self.assertEqual(summary["advancePayment"], 500.0)
        self.assertEqual(summary["totalEstimated"], 3994.0)
        self.assertEqual(summary["balanceDue"], 3344.0)

    @patch("api.views.export_quotation_excel")
    def test_export_quotation_excel_returns_generated_workbook(self, export_quotation_excel_mock):
        create_response = self.client.post(
            reverse("quotation-list"),
            build_quotation_payload(),
            format="json",
        )

        quotation_id = create_response.data["quotationId"]
        export_quotation_excel_mock.return_value = (
            b"excel-binary",
            f"{quotation_id}.xlsx",
        )

        export_response = self.client.get(
            reverse("quotation-export-excel", kwargs={"quotation_id": quotation_id}),
        )

        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn(f'{quotation_id}.xlsx', export_response["Content-Disposition"])

    @patch("api.views.export_quotation_pdf")
    def test_export_quotation_pdf_returns_generated_pdf(self, export_quotation_pdf_mock):
        create_response = self.client.post(
            reverse("quotation-list"),
            build_quotation_payload(),
            format="json",
        )

        quotation_id = create_response.data["quotationId"]
        export_quotation_pdf_mock.return_value = (b"pdf-binary", f"{quotation_id}.pdf")

        export_response = self.client.get(
            reverse("quotation-export-pdf", kwargs={"quotation_id": quotation_id}),
        )

        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(export_response["Content-Type"], "application/pdf")
        self.assertIn(f'{quotation_id}.pdf', export_response["Content-Disposition"])


class InventoryApiTests(AuthenticatedApiTestCase):
    def test_inventory_crud_flow(self):
        create_response = self.client.post(
            reverse("inventory-list"),
            {
                "name": "Silla Crossback",
                "quantity": 120,
                "unitPrice": "11.00",
            },
            format="json",
        )

        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(create_response.data["name"], "Silla Crossback")
        self.assertEqual(InventoryProduct.objects.count(), 1)

        product_id = create_response.data["id"]

        list_response = self.client.get(reverse("inventory-list"))
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.data), 1)

        update_response = self.client.put(
            reverse("inventory-detail", kwargs={"product_id": product_id}),
            {
                "name": "Silla Crossback",
                "quantity": 95,
                "unitPrice": "12.50",
            },
            format="json",
        )

        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.data["quantity"], 95)

        delete_response = self.client.delete(
            reverse("inventory-detail", kwargs={"product_id": product_id}),
        )

        self.assertEqual(delete_response.status_code, 204)
        self.assertEqual(InventoryProduct.objects.count(), 0)

    def test_inventory_rejects_duplicate_name_case_insensitive(self):
        InventoryProduct.objects.create(
            name="Silla Tiffany",
            quantity=40,
            unit_price="11.00",
        )

        response = self.client.post(
            reverse("inventory-list"),
            {
                "name": "  silla tiffany  ",
                "quantity": 10,
                "unitPrice": "10.50",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.data)


class OrderWorkflowApiTests(AuthenticatedApiTestCase):
    def test_create_update_and_delete_order(self):
        create_response = self.client.post(
            reverse("order-list"),
            build_quotation_payload(),
            format="json",
        )

        self.assertEqual(create_response.status_code, 201)
        order_id = create_response.data["orderId"]

        updated_payload = build_quotation_payload()
        updated_payload["clientInfo"]["fullName"] = "Producciones Nova"
        updated_payload["summary"]["subtotal"] = "1800.00"
        updated_payload["summary"]["totalEstimated"] = "3750.00"
        updated_payload["equipmentItems"][0]["total"] = "1800.00"
        updated_payload["equipmentItems"][0]["unitPrice"] = "90.00"

        update_response = self.client.put(
            reverse("order-detail", kwargs={"order_id": order_id}),
            updated_payload,
            format="json",
        )

        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.data["clientName"], "Producciones Nova")

        delete_response = self.client.delete(
            reverse("order-detail", kwargs={"order_id": order_id}),
        )

        self.assertEqual(delete_response.status_code, 204)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(Quotation.objects.count(), 0)

    def test_deleted_order_folio_is_reused_before_assigning_a_new_one(self):
        first_response = self.client.post(
            reverse("order-list"),
            build_quotation_payload(),
            format="json",
        )
        second_response = self.client.post(
            reverse("order-list"),
            build_quotation_payload(),
            format="json",
        )
        third_response = self.client.post(
            reverse("order-list"),
            build_quotation_payload(),
            format="json",
        )

        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(second_response.status_code, 201)
        self.assertEqual(third_response.status_code, 201)

        second_order_id = second_response.data["orderId"]
        third_order_id = third_response.data["orderId"]

        delete_response = self.client.delete(
            reverse("order-detail", kwargs={"order_id": second_order_id}),
        )

        self.assertEqual(delete_response.status_code, 204)

        reused_gap_response = self.client.post(
            reverse("order-list"),
            build_quotation_payload(),
            format="json",
        )
        next_sequential_response = self.client.post(
            reverse("order-list"),
            build_quotation_payload(),
            format="json",
        )

        self.assertEqual(reused_gap_response.status_code, 201)
        self.assertEqual(next_sequential_response.status_code, 201)
        self.assertEqual(reused_gap_response.data["orderId"], second_order_id)
        self.assertEqual(
            extract_order_sequence(next_sequential_response.data["orderId"]),
            extract_order_sequence(third_order_id) + 1,
        )

    def test_order_status_workflow_updates_operational_and_billing_states(self):
        create_response = self.client.post(
            reverse("order-list"),
            build_quotation_payload(),
            format="json",
        )

        self.assertEqual(create_response.status_code, 201)
        order_id = create_response.data["orderId"]
        self.assertEqual(
            create_response.data["operationalStatus"],
            Order.OperationalStatus.PROGRAMADA,
        )
        self.assertEqual(
            create_response.data["billingStatus"],
            Order.BillingStatus.AL_CORRIENTE,
        )

        update_status_response = self.client.post(
            reverse("order-status-update", kwargs={"order_id": order_id}),
            {
                "operationalStatus": Order.OperationalStatus.POR_RECOGER,
                "billingStatus": Order.BillingStatus.POR_COBRAR,
                "comment": "Pendiente de recoleccion y con saldo abierto.",
            },
            format="json",
        )

        self.assertEqual(update_status_response.status_code, 200)
        self.assertEqual(
            update_status_response.data["operationalStatus"],
            Order.OperationalStatus.POR_RECOGER,
        )
        self.assertEqual(
            update_status_response.data["billingStatus"],
            Order.BillingStatus.POR_COBRAR,
        )
        self.assertIn("por-recoger", update_status_response.data["folderKeys"])
        self.assertIn("por-cobrar", update_status_response.data["folderKeys"])
        self.assertEqual(len(update_status_response.data["workflowHistory"]), 4)
        self.assertEqual(OrderWorkflowEvent.objects.count(), 4)

        detail_response = self.client.get(
            reverse("order-detail", kwargs={"order_id": order_id}),
        )

        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(
            detail_response.data["workflowHistory"][0]["comment"],
            "Pendiente de recoleccion y con saldo abierto.",
        )

    def test_order_lists_separate_active_notes_from_archive(self):
        active_payload = build_quotation_payload()
        active_payload["clientInfo"]["fullName"] = "Evento Activo"

        archive_payload = build_quotation_payload()
        archive_payload["clientInfo"]["fullName"] = "Evento Archivado"

        active_response = self.client.post(
            reverse("order-list"),
            active_payload,
            format="json",
        )
        archive_response = self.client.post(
            reverse("order-list"),
            archive_payload,
            format="json",
        )

        self.assertEqual(active_response.status_code, 201)
        self.assertEqual(archive_response.status_code, 201)

        archived_order_id = archive_response.data["orderId"]
        archive_status_response = self.client.post(
            reverse("order-status-update", kwargs={"order_id": archived_order_id}),
            {
                "operationalStatus": Order.OperationalStatus.RECOGIDO,
                "billingStatus": Order.BillingStatus.COBRADO,
            },
            format="json",
        )

        self.assertEqual(archive_status_response.status_code, 200)

        active_list_response = self.client.get(reverse("order-list"))
        archived_list_response = self.client.get(reverse("order-archive-list"))

        self.assertEqual(active_list_response.status_code, 200)
        self.assertEqual(archived_list_response.status_code, 200)
        self.assertEqual(len(active_list_response.data), 1)
        self.assertEqual(len(archived_list_response.data), 1)
        self.assertEqual(active_list_response.data[0]["clientName"], "Evento Activo")
        self.assertEqual(archived_list_response.data[0]["clientName"], "Evento Archivado")
        self.assertIn("pagado", archived_list_response.data[0]["folderKeys"])

    def test_order_archive_filters_recogido_notes_by_billing_status(self):
        create_response = self.client.post(
            reverse("order-list"),
            build_quotation_payload(),
            format="json",
        )

        self.assertEqual(create_response.status_code, 201)
        order_id = create_response.data["orderId"]

        update_status_response = self.client.post(
            reverse("order-status-update", kwargs={"order_id": order_id}),
            {
                "operationalStatus": Order.OperationalStatus.RECOGIDO,
                "billingStatus": Order.BillingStatus.POR_COBRAR,
            },
            format="json",
        )

        self.assertEqual(update_status_response.status_code, 200)

        por_cobrar_response = self.client.get(
            reverse("order-archive-list"),
            {"folder": "por-cobrar"},
        )
        pagado_response = self.client.get(
            reverse("order-archive-list"),
            {"folder": "pagado"},
        )

        self.assertEqual(por_cobrar_response.status_code, 200)
        self.assertEqual(len(por_cobrar_response.data), 1)
        self.assertEqual(por_cobrar_response.data[0]["orderId"], order_id)
        self.assertEqual(pagado_response.status_code, 200)
        self.assertEqual(len(pagado_response.data), 0)

    def test_delivered_orders_are_grouped_under_entregado_folder(self):
        create_response = self.client.post(
            reverse("order-list"),
            build_quotation_payload(),
            format="json",
        )

        self.assertEqual(create_response.status_code, 201)
        order_id = create_response.data["orderId"]

        update_status_response = self.client.post(
            reverse("order-status-update", kwargs={"order_id": order_id}),
            {
                "operationalStatus": Order.OperationalStatus.ENTREGADO,
            },
            format="json",
        )

        self.assertEqual(update_status_response.status_code, 200)
        self.assertEqual(
            update_status_response.data["operationalStatus"],
            Order.OperationalStatus.ENTREGADO,
        )
        self.assertIn("entregado", update_status_response.data["folderKeys"])
        self.assertIn("Entregado", update_status_response.data["folderLabels"])

        filtered_response = self.client.get(
            reverse("order-list"),
            {"folder": "entregado"},
        )

        self.assertEqual(filtered_response.status_code, 200)
        self.assertEqual(len(filtered_response.data), 1)
        self.assertEqual(filtered_response.data[0]["orderId"], order_id)

    @patch("api.views.export_order_excel")
    def test_export_order_excel_returns_generated_workbook(self, export_order_excel_mock):
        create_response = self.client.post(
            reverse("order-list"),
            build_quotation_payload(),
            format="json",
        )

        order_id = create_response.data["orderId"]
        export_order_excel_mock.return_value = (b"excel-binary", f"{order_id}.xlsx")

        export_response = self.client.get(
            reverse("order-export-excel", kwargs={"order_id": order_id}),
        )

        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn(f'{order_id}.xlsx', export_response["Content-Disposition"])

    @patch("api.views.export_order_pdf")
    def test_export_order_pdf_returns_generated_pdf(self, export_order_pdf_mock):
        create_response = self.client.post(
            reverse("order-list"),
            build_quotation_payload(),
            format="json",
        )

        order_id = create_response.data["orderId"]
        export_order_pdf_mock.return_value = (b"pdf-binary", f"{order_id}.pdf")

        export_response = self.client.get(
            reverse("order-export-pdf", kwargs={"order_id": order_id}),
        )

        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(export_response["Content-Type"], "application/pdf")
        self.assertIn(f'{order_id}.pdf', export_response["Content-Disposition"])


class ClientDirectoryApiTests(AuthenticatedApiTestCase):
    def test_client_list_merges_duplicates_and_detail_returns_history(self):
        primary_client = Client.objects.create(
            client_name="Grupo Marea Eventos",
            contact_person="Laura Marea",
            phone_number="5513458890",
            email="ventas@marea.com",
            address="Av. Insurgentes Sur 845, Napoles",
        )
        duplicate_client = Client.objects.create(
            client_name="Grupo Marea Eventos",
            contact_person="Laura Marea",
            phone_number="5513458890",
            email="",
            address="Montecito 38, Napoles",
        )

        create_client_quotation(
            primary_client,
            status=Quotation.Status.DRAFT,
            address="Av. Insurgentes Sur 845",
            neighborhood="Napoles",
            reference="Frente al parque",
            delivery_instructions="Entregar antes del mediodia",
            subtotal="1700.00",
            total_estimated="1700.00",
        )
        confirmed_quotation = create_client_quotation(
            duplicate_client,
            status=Quotation.Status.CONFIRMED,
            address="Montecito 38",
            neighborhood="Napoles",
            reference="Salon principal",
            delivery_instructions="Acceso por la puerta de servicio",
            subtotal="2400.00",
            total_estimated="2400.00",
        )
        confirmed_order = Order.objects.create(quotation=confirmed_quotation)

        list_response = self.client.get(reverse("client-list"))

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.data), 1)
        self.assertEqual(list_response.data[0]["mergedRecords"], 2)
        self.assertEqual(list_response.data[0]["phoneNumber"], "5513458890")

        client_id = list_response.data[0]["id"]
        detail_response = self.client.get(
            reverse("client-detail", kwargs={"client_id": client_id}),
        )

        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.data["mergedRecords"], 2)
        self.assertEqual(len(detail_response.data["mergedClientCodes"]), 2)
        self.assertEqual(len(detail_response.data["addresses"]), 2)
        self.assertEqual(len(detail_response.data["orderHistory"]), 1)
        self.assertEqual(
            detail_response.data["orderHistory"][0]["orderId"],
            confirmed_order.order_id,
        )
        self.assertEqual(
            detail_response.data["prefill"]["clientInfo"]["address"],
            "Montecito 38",
        )
        self.assertEqual(
            detail_response.data["prefill"]["clientInfo"]["neighborhood"],
            "Napoles",
        )
        self.assertEqual(
            detail_response.data["prefill"]["clientInfo"]["deliveryInstructions"],
            "Acceso por la puerta de servicio",
        )
