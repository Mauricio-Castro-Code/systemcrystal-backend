from __future__ import annotations

from collections import defaultdict
from datetime import date as calendar_date, datetime, time, timedelta
from io import BytesIO
import re

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import AuthenticationFailed, NotFound, ValidationError
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView


class IsAdminUser(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)

from .client_directory import (
    build_client_directory_entries,
    build_client_profile,
    resolve_client_group_by_code,
)
from .excel_exports import (
    ExcelTemplateExportError,
    export_order_excel,
    export_order_pdf,
    export_quotation_excel,
    export_quotation_pdf,
)
from .models import (
    Client,
    FreightZone,
    InventoryProduct,
    Order,
    Quotation,
    QuotationItem,
    UserProfile,
    order_folio_options,
    set_user_role,
)
from .note_import import NoteImportError, read_note_excel
from .presenters import (
    build_dashboard_overview,
    build_order_record,
    build_quotation_record,
    build_team_member,
    build_user_session,
)
from .serializers import (
    InventoryItemSerializer,
    LoginSerializer,
    OrderStatusUpdateSerializer,
    QuotationNoteSerializer,
    RegisterSerializer,
    TeamMemberCreateSerializer,
    TeamMemberUpdateSerializer,
)
from .services import (
    confirm_quotation_as_order,
    create_order_from_imported_note,
    create_order_from_note,
    create_quotation_from_note,
    delete_order_and_quotation,
    update_order_statuses,
    update_order_from_note,
    update_quotation_from_note,
)


User = get_user_model()
def get_order_base_queryset():
    from django.db.models.expressions import OrderBy, RawSQL

    # Sort: year suffix desc (26 > 25), then folio number desc (1977 > 0001).
    # REGEXP_REPLACE strips non-digits before casting so malformed folios
    # (0578*-25, 0496--25, 0340-26-1, etc.) are handled without errors.
    # NULLIF('','') → NULL → NULLS LAST keeps them at the bottom.
    _year_sql = (
        "CAST(NULLIF(REGEXP_REPLACE(SPLIT_PART(order_id,'-',2),'[^0-9]','','g'),'') AS INTEGER)"
    )
    _num_sql = (
        "CAST(NULLIF(REGEXP_REPLACE(SPLIT_PART(order_id,'-',1),'[^0-9]','','g'),'') AS INTEGER)"
    )

    return (
        Order.objects.select_related("quotation")
        .prefetch_related("quotation__equipment_items", "workflow_events__changed_by")
        .order_by(
            OrderBy(RawSQL(_year_sql, []), descending=True, nulls_last=True),
            OrderBy(RawSQL(_num_sql,  []), descending=True, nulls_last=True),
        )
    )


def request_folder_key(request):
    return (request.query_params.get("folder") or "").strip().lower()


def request_folio_strategy(request):
    value = str(request.data.get("folioStrategy") or "").strip().lower()
    return "sequential" if value == "sequential" else "fill"


def filter_orders_by_folder(order_list, folder_key: str):
    if not folder_key or folder_key == "all":
        return order_list

    if folder_key == "por-cobrar":
        return [
            order
            for order in order_list
            if order.billing_status == Order.BillingStatus.POR_COBRAR
        ]

    if folder_key == "pagado":
        return [
            order
            for order in order_list
            if order.billing_status == Order.BillingStatus.COBRADO
        ]

    operational_folder_map = {
        "programada": Order.OperationalStatus.PROGRAMADA,
        "programadas": Order.OperationalStatus.PROGRAMADA,
        "entregado": Order.OperationalStatus.ENTREGADO,
        "entregados": Order.OperationalStatus.ENTREGADO,
        "por-recoger": Order.OperationalStatus.POR_RECOGER,
        "cliente-entrega": Order.OperationalStatus.CLIENTE_ENTREGA,
        "recogido": Order.OperationalStatus.RECOGIDO,
    }

    operational_status = operational_folder_map.get(folder_key)

    if not operational_status:
        return order_list

    return [
        order
        for order in order_list
        if order.operational_status == operational_status
    ]


def parse_iso_date_param(raw_value: str, field_name: str):
    try:
        return calendar_date.fromisoformat(raw_value)
    except ValueError as error:
        raise ValidationError(
            {field_name: "Usa el formato YYYY-MM-DD para la fecha."},
        ) from error


def resolve_dashboard_delivery_range(request):
    today = timezone.localdate()
    raw_start = (request.query_params.get("deliveryDateFrom") or "").strip()
    raw_end = (request.query_params.get("deliveryDateTo") or "").strip()

    if raw_start:
        delivery_start = parse_iso_date_param(raw_start, "deliveryDateFrom")
    elif raw_end:
        delivery_start = parse_iso_date_param(raw_end, "deliveryDateFrom")
    else:
        delivery_start = today

    if raw_end:
        delivery_end = parse_iso_date_param(raw_end, "deliveryDateTo")
    elif raw_start:
        delivery_end = delivery_start
    else:
        delivery_end = today + timedelta(days=5)

    if delivery_start > delivery_end:
        raise ValidationError(
            {
                "deliveryDateTo": (
                    "La fecha final no puede ser anterior a la fecha inicial."
                )
            },
        )

    return delivery_start, delivery_end


def resolve_user_from_identifier(identifier: str):
    normalized_identifier = identifier.strip()

    if "@" in normalized_identifier:
        return User.objects.filter(email__iexact=normalized_identifier).first()

    return User.objects.filter(username__iexact=normalized_identifier).first()


def build_name_from_email(email: str) -> str:
    local_part = email.split("@", 1)[0]
    name_parts = [part for part in re.split(r"[._-]+", local_part) if part]

    if not name_parts:
        return email

    return " ".join(part.capitalize() for part in name_parts)


def build_excel_download_response(file_bytes: bytes, filename: str) -> HttpResponse:
    response = HttpResponse(
        file_bytes,
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def build_pdf_download_response(
    file_bytes: bytes,
    filename: str,
    *,
    inline: bool = False,
) -> HttpResponse:
    response = HttpResponse(file_bytes, content_type="application/pdf")
    disposition = "inline" if inline else "attachment"
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    return response


class HealthCheckView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response({"status": "ok"})


class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        identifier = serializer.validated_data["identifier"]
        password = serializer.validated_data["password"]
        candidate = resolve_user_from_identifier(identifier)

        if not candidate:
            raise AuthenticationFailed("Credenciales invalidas.")

        user = authenticate(
            request=request,
            username=candidate.username,
            password=password,
        )

        if not user:
            raise AuthenticationFailed("Credenciales invalidas.")

        token, _ = Token.objects.get_or_create(user=user)
        return Response(build_user_session(user, token.key))


class RegisterView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"].strip().lower()
        password = serializer.validated_data["password"]
        registration_key = serializer.validated_data["registrationKey"].strip()
        role = serializer.validated_data.get("role", "ventas")

        if registration_key != settings.REGISTRATION_ACCESS_KEY:
            raise ValidationError(
                {"registrationKey": "La clave de registro no es valida."},
            )

        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError({"email": "Ya existe una cuenta con este correo."})

        if User.objects.filter(username__iexact=email).exists():
            raise ValidationError({"email": "Ya existe una cuenta con este correo."})

        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            first_name=build_name_from_email(email),
            is_staff=(role == "admin"),
        )
        token = Token.objects.create(user=user)

        return Response(
            build_user_session(user, token.key),
            status=status.HTTP_201_CREATED,
        )


class CurrentSessionView(APIView):
    def get(self, request):
        token_key = (
            request.auth.key
            if request.auth
            else Token.objects.get_or_create(user=request.user)[0].key
        )
        return Response(build_user_session(request.user, token_key))


class LogoutView(APIView):
    authentication_classes = [TokenAuthentication]

    def post(self, request):
        if request.auth:
            request.auth.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


class TeamMemberListCreateView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        users = User.objects.select_related("profile").order_by(
            "-is_staff", "first_name", "username",
        )
        return Response([build_team_member(user) for user in users])

    def post(self, request):
        serializer = TeamMemberCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"].strip().lower()
        password = serializer.validated_data["password"]
        role = serializer.validated_data.get("role", UserProfile.Role.VENTAS)
        display_name = str(
            serializer.validated_data.get("displayName", ""),
        ).strip() or build_name_from_email(email)

        if User.objects.filter(email__iexact=email).exists() or User.objects.filter(
            username__iexact=email,
        ).exists():
            raise ValidationError({"email": "Ya existe una cuenta con este correo."})

        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            first_name=display_name,
        )
        set_user_role(user, role)
        user.refresh_from_db()

        return Response(build_team_member(user), status=status.HTTP_201_CREATED)


class TeamMemberDetailView(APIView):
    permission_classes = [IsAdminUser]

    def patch(self, request, user_id: int):
        user = get_object_or_404(User, pk=user_id)
        serializer = TeamMemberUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if "displayName" in data:
            user.first_name = str(data["displayName"]).strip()

        if "isActive" in data:
            if user.pk == request.user.pk and not data["isActive"]:
                raise ValidationError(
                    {"isActive": "No puedes desactivar tu propia cuenta."},
                )
            user.is_active = bool(data["isActive"])

        user.save()

        if data.get("password"):
            user.set_password(data["password"])
            user.save(update_fields=["password"])

        if "role" in data:
            if user.pk == request.user.pk and data["role"] != UserProfile.Role.ADMIN:
                raise ValidationError(
                    {"role": "No puedes quitarte a ti mismo el rol de administrador."},
                )
            set_user_role(user, data["role"])

        user.refresh_from_db()
        return Response(build_team_member(user))

    def delete(self, request, user_id: int):
        user = get_object_or_404(User, pk=user_id)

        if user.pk == request.user.pk:
            raise ValidationError("No puedes eliminar tu propia cuenta.")

        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ClientListView(APIView):
    def get(self, request):
        queryset = self.get_queryset()
        return Response(build_client_directory_entries(queryset))

    def get_queryset(self):
        return Client.objects.all()


class ClientDetailView(APIView):
    def get(self, request, client_id: str):
        client_group = resolve_client_group_by_code(Client.objects.all(), client_id)

        if not client_group:
            raise NotFound("Cliente no encontrado.")

        client_ids = [client.pk for client in client_group]
        quotations = Quotation.objects.filter(client_id__in=client_ids)
        orders = Order.objects.select_related("quotation").filter(quotation__client_id__in=client_ids)

        return Response(build_client_profile(client_group, quotations, orders))


class InventoryListCreateView(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminUser()]
        return super().get_permissions()

    def get(self, request):
        serializer = InventoryItemSerializer(self.get_queryset(), many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = InventoryItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        inventory_item = serializer.save()

        return Response(
            InventoryItemSerializer(inventory_item).data,
            status=status.HTTP_201_CREATED,
        )

    def get_queryset(self):
        return InventoryProduct.objects.all()


class InventoryDetailView(APIView):
    permission_classes = [IsAdminUser]

    def put(self, request, product_id: int):
        inventory_item = self.get_object(product_id)
        serializer = InventoryItemSerializer(inventory_item, data=request.data)
        serializer.is_valid(raise_exception=True)
        updated_item = serializer.save()

        return Response(InventoryItemSerializer(updated_item).data)

    def delete(self, request, product_id: int):
        inventory_item = self.get_object(product_id)
        inventory_item.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def get_object(self, product_id: int) -> InventoryProduct:
        return get_object_or_404(InventoryProduct, pk=product_id)


class QuotationListCreateView(APIView):
    def get(self, request):
        queryset = self.get_queryset()
        payload = [build_quotation_record(quotation) for quotation in queryset]
        return Response(payload)

    def post(self, request):
        serializer = QuotationNoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        quotation = create_quotation_from_note(serializer.validated_data)

        return Response(
            build_quotation_record(quotation),
            status=status.HTTP_201_CREATED,
        )

    def get_queryset(self):
        return Quotation.objects.filter(status=Quotation.Status.DRAFT).prefetch_related(
            "equipment_items",
        )


class QuotationDetailView(APIView):
    def get_permissions(self):
        if self.request.method == "DELETE":
            return [IsAdminUser()]
        return super().get_permissions()

    def get(self, request, quotation_id: str):
        quotation = self.get_object(quotation_id)
        return Response(build_quotation_record(quotation))

    def put(self, request, quotation_id: str):
        quotation = self.get_object(quotation_id)
        serializer = QuotationNoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated_quotation = update_quotation_from_note(quotation, serializer.validated_data)

        return Response(build_quotation_record(updated_quotation))

    def delete(self, request, quotation_id: str):
        quotation = self.get_object(quotation_id)
        quotation.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def get_object(self, quotation_id: str) -> Quotation:
        queryset = Quotation.objects.filter(status=Quotation.Status.DRAFT).prefetch_related(
            "equipment_items",
        )
        return get_object_or_404(queryset, quotation_id=quotation_id)


class QuotationExcelExportView(APIView):
    def get(self, request, quotation_id: str):
        quotation = get_object_or_404(
            Quotation.objects.filter(status=Quotation.Status.DRAFT).prefetch_related(
                "equipment_items",
            ),
            quotation_id=quotation_id,
        )

        try:
            file_bytes, filename = export_quotation_excel(quotation)
        except FileNotFoundError as error:
            raise NotFound(str(error)) from error
        except ExcelTemplateExportError as error:
            raise ValidationError(str(error)) from error

        return build_excel_download_response(file_bytes, filename)


class QuotationPdfExportView(APIView):
    def get(self, request, quotation_id: str):
        quotation = get_object_or_404(
            Quotation.objects.filter(status=Quotation.Status.DRAFT).prefetch_related(
                "equipment_items",
            ),
            quotation_id=quotation_id,
        )

        try:
            file_bytes, filename = export_quotation_pdf(quotation)
        except FileNotFoundError as error:
            raise NotFound(str(error)) from error
        except ExcelTemplateExportError as error:
            raise ValidationError(str(error)) from error

        return build_pdf_download_response(file_bytes, filename)


class QuotationConfirmView(APIView):
    def post(self, request, quotation_id: str):
        quotation = get_object_or_404(
            Quotation.objects.prefetch_related("equipment_items"),
            quotation_id=quotation_id,
            status=Quotation.Status.DRAFT,
        )
        order = confirm_quotation_as_order(
            quotation,
            changed_by=request.user,
            folio_strategy=request_folio_strategy(request),
        )

        return Response(
            build_order_record(order),
            status=status.HTTP_201_CREATED,
        )


class OrderFolioOptionsView(APIView):
    def get(self, request):
        return Response(order_folio_options())


class OrderImportView(APIView):
    """Importa una nota desde un archivo Excel (.xlsx) con la plantilla de Crystal."""

    permission_classes = [IsAdminUser]

    def post(self, request):
        upload = request.FILES.get("file")

        if upload is None:
            raise ValidationError({"file": "Adjunta un archivo de Excel (.xlsx)."})

        if not upload.name.lower().endswith(".xlsx"):
            raise ValidationError(
                {"file": "El archivo debe ser .xlsx (la plantilla de nota de Crystal)."},
            )

        try:
            parsed = read_note_excel(BytesIO(upload.read()))
        except NoteImportError as error:
            raise ValidationError({"file": str(error)}) from error

        note = parsed["note"]

        if not note["clientInfo"]["fullName"]:
            raise ValidationError(
                {"file": "El Excel no tiene nombre de cliente (celda B9). Revisa la nota."},
            )

        folio = parsed["folio"]

        if folio and Order.objects.filter(order_id=folio).exists():
            raise ValidationError(
                {"file": f"Ya existe una nota con el folio {folio} en el sistema."},
            )

        confirmed_at = None
        note_date = parsed["noteDate"]
        if note_date:
            confirmed_at = timezone.make_aware(
                datetime.combine(note_date, time.min),
            )

        order = create_order_from_imported_note(
            note,
            order_id=folio,
            changed_by=request.user,
            confirmed_at=confirmed_at,
        )

        refreshed_order = get_object_or_404(get_order_base_queryset(), pk=order.pk)
        return Response(
            build_order_record(refreshed_order),
            status=status.HTTP_201_CREATED,
        )


class OrderListCreateView(APIView):
    def get(self, request):
        payload = [build_order_record(order) for order in self.get_queryset()]
        return Response(payload)

    def post(self, request):
        serializer = QuotationNoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = create_order_from_note(
            serializer.validated_data,
            changed_by=request.user,
            folio_strategy=request_folio_strategy(request),
        )

        return Response(
            build_order_record(order),
            status=status.HTTP_201_CREATED,
        )

    def get_queryset(self):
        orders = list(get_order_base_queryset())
        folder_key = request_folder_key(self.request)

        filtered_orders = [
            order
            for order in orders
            if order.operational_status != Order.OperationalStatus.RECOGIDO
        ]
        return filter_orders_by_folder(filtered_orders, folder_key)


class OrderArchiveListView(APIView):
    def get(self, request):
        payload = [build_order_record(order) for order in self.get_queryset()]
        return Response(payload)

    def get_queryset(self):
        orders = list(get_order_base_queryset())
        folder_key = request_folder_key(self.request)
        return filter_orders_by_folder(orders, folder_key)


class OrderDetailView(APIView):
    def get_permissions(self):
        if self.request.method == "DELETE":
            return [IsAdminUser()]
        return super().get_permissions()

    def get(self, request, order_id: str):
        order = self.get_object(order_id)
        return Response(build_order_record(order))

    def put(self, request, order_id: str):
        order = self.get_object(order_id)
        serializer = QuotationNoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated_order = update_order_from_note(order, serializer.validated_data)

        return Response(build_order_record(updated_order))

    def delete(self, request, order_id: str):
        order = self.get_object(order_id)

        with transaction.atomic():
            delete_order_and_quotation(order)

        return Response(status=status.HTTP_204_NO_CONTENT)

    def get_object(self, order_id: str) -> Order:
        queryset = get_order_base_queryset()
        return get_object_or_404(queryset, order_id=order_id)


class OrderCancelView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, order_id: str):
        order = get_object_or_404(get_order_base_queryset(), order_id=order_id)
        order.is_cancelled = True
        order.save(update_fields=["is_cancelled"])
        return Response({"isCancelled": True})

    def delete(self, request, order_id: str):
        order = get_object_or_404(get_order_base_queryset(), order_id=order_id)
        order.is_cancelled = False
        order.save(update_fields=["is_cancelled"])
        return Response({"isCancelled": False})


class OrderRenameView(APIView):
    permission_classes = [IsAdminUser]

    def patch(self, request, order_id: str):
        order = get_object_or_404(get_order_base_queryset(), order_id=order_id)
        new_order_id = str(request.data.get("newOrderId", "")).strip()

        if not new_order_id:
            raise ValidationError({"newOrderId": "El nuevo folio no puede estar vacío."})

        if len(new_order_id) > 20:
            raise ValidationError({"newOrderId": "El folio no puede tener más de 20 caracteres."})

        if new_order_id == order_id:
            return Response(build_order_record(order))

        if Order.objects.filter(order_id=new_order_id).exists():
            raise ValidationError({"newOrderId": f"Ya existe una nota con el folio {new_order_id}."})

        Order.objects.filter(pk=order.pk).update(order_id=new_order_id)
        order.refresh_from_db()
        return Response(build_order_record(order))


class OrderExcelExportView(APIView):
    def get(self, request, order_id: str):
        order = get_object_or_404(get_order_base_queryset(), order_id=order_id)

        try:
            file_bytes, filename = export_order_excel(order)
        except FileNotFoundError as error:
            raise NotFound(str(error)) from error
        except ExcelTemplateExportError as error:
            raise ValidationError(str(error)) from error

        return build_excel_download_response(file_bytes, filename)


class OrderPdfExportView(APIView):
    def get(self, request, order_id: str):
        order = get_object_or_404(get_order_base_queryset(), order_id=order_id)

        try:
            file_bytes, filename = export_order_pdf(order)
        except FileNotFoundError as error:
            raise NotFound(str(error)) from error
        except ExcelTemplateExportError as error:
            raise ValidationError(str(error)) from error

        return build_pdf_download_response(file_bytes, filename)


class OrderStatusUpdateView(APIView):
    def post(self, request, order_id: str):
        order = get_object_or_404(get_order_base_queryset(), order_id=order_id)
        serializer = OrderStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        updated_order = update_order_statuses(
            order,
            operational_status=serializer.validated_data.get("operationalStatus"),
            billing_status=serializer.validated_data.get("billingStatus"),
            changed_by=request.user,
            comment=serializer.validated_data.get("comment", ""),
        )

        refreshed_order = get_object_or_404(get_order_base_queryset(), pk=updated_order.pk)
        return Response(build_order_record(refreshed_order))


class OrderBulkStatusUpdateView(APIView):
    def post(self, request):
        order_ids = request.data.get("orderIds", [])
        if not isinstance(order_ids, list) or not order_ids:
            raise ValidationError("orderIds debe ser una lista no vacía.")

        # Solo incluimos los estados realmente enviados; pasar None explicito
        # haria fallar el ChoiceField con "Este campo no puede ser nulo".
        status_data = {"comment": request.data.get("comment", "")}
        if request.data.get("operationalStatus") is not None:
            status_data["operationalStatus"] = request.data.get("operationalStatus")
        if request.data.get("billingStatus") is not None:
            status_data["billingStatus"] = request.data.get("billingStatus")

        serializer = OrderStatusUpdateSerializer(data=status_data)
        serializer.is_valid(raise_exception=True)

        orders = get_order_base_queryset().filter(order_id__in=order_ids)
        if not orders.exists():
            raise NotFound("No se encontraron órdenes con los IDs proporcionados.")

        for order in orders:
            update_order_statuses(
                order,
                operational_status=serializer.validated_data.get("operationalStatus"),
                billing_status=serializer.validated_data.get("billingStatus"),
                changed_by=request.user,
                comment=serializer.validated_data.get("comment", ""),
            )

        return Response({"success": True, "updated_count": orders.count()})


class DashboardOverviewView(APIView):
    def get(self, request):
        orders = (
            Order.objects.select_related("quotation")
            .prefetch_related("quotation__equipment_items")
            .all()
        )
        quotations = Quotation.objects.filter(status=Quotation.Status.DRAFT).all()
        delivery_range = resolve_dashboard_delivery_range(request)
        return Response(build_dashboard_overview(orders, quotations, delivery_range))


class AccountingOverviewView(APIView):
    permission_classes = [IsAdminUser]

    MONTH_NAMES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun",
                   "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

    def get(self, request):
        today = timezone.localdate()

        raw_year = (request.query_params.get("year") or "").strip()
        try:
            selected_year = int(raw_year)
        except ValueError:
            selected_year = today.year

        orders = list(
            Order.objects.select_related("quotation")
            .prefetch_related("quotation__equipment_items")
            .filter(quotation__total_estimated__gt=0, is_cancelled=False)
        )

        year_orders = [o for o in orders if self._order_date(o).year == selected_year]

        monthly_sales = self._build_monthly_sales(orders, selected_year)
        top_products = self._build_top_products(year_orders)
        top_colors = self._build_top_colors(year_orders)
        summary = self._build_summary(orders, today, selected_year)
        available_years = self._available_years(orders, today)

        return Response({
            "generatedAt": timezone.localtime().isoformat(),
            "selectedYear": selected_year,
            "availableYears": available_years,
            "summary": summary,
            "monthlySales": monthly_sales,
            "topProducts": top_products,
            "topColors": top_colors,
        })

    def _order_date(self, order) -> "calendar_date":
        return order.quotation.event_date or order.confirmed_at.date()

    def _build_monthly_sales(self, orders, year: int):
        buckets = {month: 0.0 for month in range(1, 13)}

        for order in orders:
            d = self._order_date(order)
            if d.year == year:
                buckets[d.month] += float(order.quotation.total_estimated)

        return [
            {
                "label": self.MONTH_NAMES[m - 1],
                "month": m,
                "value": round(v, 2),
            }
            for m, v in buckets.items()
        ]

    def _build_top_products(self, year_orders: list):
        qty_map: dict[str, int] = defaultdict(int)
        rev_map: dict[str, float] = defaultdict(float)

        for order in year_orders:
            for item in order.quotation.equipment_items.all():
                name = item.equipment or ""
                qty_map[name] += item.quantity or 0
                rev_map[name] += float(item.total or 0)

        # Return 30 sorted by qty so the frontend can re-sort by revenue without missing items
        top_names = sorted(qty_map, key=lambda n: qty_map[n], reverse=True)[:30]
        return [
            {
                "name": name,
                "totalQty": qty_map[name],
                "totalRevenue": round(rev_map[name], 2),
            }
            for name in top_names
        ]

    # ── Color extraction helpers ──────────────────────────────────────────────

    # Abbreviation replacements applied before color matching (order matters)
    _COLOR_ABBREVS: list[tuple[str, str]] = [
        (r'\bbco[sa]?\b', 'blanco'),
        (r'\bbca[s]?\b',  'blanco'),
        (r'\bturqueza\b', 'turquesa'),
        (r'\bturquesa[s]?\b', 'turquesa'),
        (r'\bfucsia\b',   'fucsia'),
        (r'\bguinda\b',   'guinda'),
    ]

    # Compound colors checked before their components (longest-match principle)
    _COMPOUND_COLORS: list[str] = [
        'azul rey', 'azul turquesa', 'azul cielo', 'azul marino', 'azul noche',
        'rosa pastel', 'rosa mexicano', 'rosa palo', 'rosa fucsia',
        'verde menta', 'verde olivo', 'verde esmeralda', 'verde militar',
        'rojo vino',
    ]

    _SIMPLE_COLORS: list[str] = [
        'blanco', 'negro', 'azul', 'rojo', 'rosa', 'morado', 'lila',
        'verde', 'amarillo', 'naranja', 'gris', 'dorado', 'plateado',
        'plata', 'beige', 'cafe', 'chocolate', 'champagne', 'coral',
        'salmon', 'terracota', 'turquesa', 'menta', 'marfil', 'crema',
        'vino', 'nude', 'fucsia', 'guinda', 'lavanda',
    ]

    # Canonical display names (normalize variants after matching)
    _COLOR_CANONICAL: dict[str, str] = {
        'plata': 'plateado',
        'cafe': 'café',
        'salmon': 'salmón',
    }

    _COLOR_PATTERNS: list[tuple] | None = None  # compiled lazily

    @classmethod
    def _compile_color_patterns(cls):
        if cls._COLOR_PATTERNS is not None:
            return cls._COLOR_PATTERNS
        patterns = []
        for color in cls._COMPOUND_COLORS + cls._SIMPLE_COLORS:
            patterns.append((re.compile(r'\b' + re.escape(color) + r'\b'), color))
        cls._COLOR_PATTERNS = patterns
        return patterns

    @classmethod
    def _extract_colors(cls, text: str) -> list[str]:
        normalized = text.lower()
        for pattern, replacement in cls._COLOR_ABBREVS:
            normalized = re.sub(pattern, replacement, normalized)

        found: list[str] = []
        remaining = normalized
        for pattern, color in cls._compile_color_patterns():
            if pattern.search(remaining):
                canonical = cls._COLOR_CANONICAL.get(color, color)
                found.append(canonical)
                # remove matched portion so compound colors don't double-count
                remaining = pattern.sub('', remaining)

        return found

    # Substrings that identify mantelería items (covers common typos: mantesl, mantle, matel)
    _TEXTILE_SUBSTRINGS = ('mant', 'matel', 'cubre', 'servilleta', 'camino')

    @classmethod
    def _is_manteleria(cls, equipment: str) -> bool:
        text = equipment.lower()
        return any(kw in text for kw in cls._TEXTILE_SUBSTRINGS)

    def _build_top_colors(self, year_orders: list):
        color_counts: dict[str, int] = defaultdict(int)

        for order in year_orders:
            for item in order.quotation.equipment_items.all():
                equip = item.equipment or ""
                if not self._is_manteleria(equip):
                    continue
                colors = self._extract_colors(equip)
                qty = item.quantity or 1
                for color in colors:
                    color_counts[color] += qty

        sorted_colors = sorted(color_counts.items(), key=lambda kv: kv[1], reverse=True)[:12]
        return [{"color": color.capitalize(), "count": count} for color, count in sorted_colors]

    def _build_summary(self, orders, today, selected_year: int):
        prev_year = selected_year - 1

        # Cutoff: start of current month — comparison always uses complete months.
        # e.g. today = May 12 → include Jan–Apr of each year (month < 5)
        def is_ytd(d) -> bool:
            return d.month < today.month

        year_revenue = 0.0       # full year (or YTD if current year)
        ytd_revenue = 0.0        # selected year up to today's month/day
        prev_ytd_revenue = 0.0   # previous year up to same month/day
        month_revenue = 0.0
        total_orders = 0

        for order in orders:
            total_orders += 1
            amount = float(order.quotation.total_estimated)
            d = self._order_date(order)

            if d.year == selected_year:
                year_revenue += amount
                if d.month == today.month:
                    month_revenue += amount
                if is_ytd(d):
                    ytd_revenue += amount
            elif d.year == prev_year:
                if is_ytd(d):
                    prev_ytd_revenue += amount

        if prev_ytd_revenue > 0:
            yoy_pct = round(((ytd_revenue - prev_ytd_revenue) / prev_ytd_revenue) * 100, 1)
        else:
            yoy_pct = None

        return {
            "yearRevenue": round(year_revenue, 2),
            "ytdRevenue": round(ytd_revenue, 2),
            "prevYtdRevenue": round(prev_ytd_revenue, 2),
            "monthRevenue": round(month_revenue, 2),
            "yoyPct": yoy_pct,
            "prevYear": prev_year,
            "totalOrders": total_orders,
        }

    def _available_years(self, orders, today) -> list[int]:
        years = {self._order_date(o).year for o in orders}
        years.add(today.year)
        return sorted(years)


class FreightZoneListCreateView(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminUser()]
        return super().get_permissions()

    def get(self, request):
        zones = FreightZone.objects.all()
        return Response([self._serialize(z) for z in zones])

    def post(self, request):
        name = str(request.data.get("name", "")).strip()
        price = request.data.get("price", 0)
        notes = str(request.data.get("notes", "")).strip()

        if not name:
            raise ValidationError({"name": "El nombre de la colonia es obligatorio."})

        from .models import normalize_text
        name_key = normalize_text(name)
        if FreightZone.objects.filter(name_key=name_key).exists():
            raise ValidationError({"name": "Ya existe una zona con ese nombre."})

        zone = FreightZone.objects.create(name=name, price=price, notes=notes)
        return Response(self._serialize(zone), status=status.HTTP_201_CREATED)

    @staticmethod
    def _serialize(zone: FreightZone) -> dict:
        return {
            "id": zone.pk,
            "name": zone.name,
            "price": float(zone.price),
            "notes": zone.notes,
        }


class FreightZoneDetailView(APIView):
    permission_classes = [IsAdminUser]

    def put(self, request, zone_id: int):
        zone = get_object_or_404(FreightZone, pk=zone_id)
        name = str(request.data.get("name", "")).strip()
        price = request.data.get("price", zone.price)
        notes = str(request.data.get("notes", "")).strip()

        if not name:
            raise ValidationError({"name": "El nombre de la colonia es obligatorio."})

        from .models import normalize_text
        name_key = normalize_text(name)
        if FreightZone.objects.filter(name_key=name_key).exclude(pk=zone_id).exists():
            raise ValidationError({"name": "Ya existe una zona con ese nombre."})

        zone.name = name
        zone.price = price
        zone.notes = notes
        zone.save()
        return Response(FreightZoneListCreateView._serialize(zone))

    def delete(self, request, zone_id: int):
        zone = get_object_or_404(FreightZone, pk=zone_id)
        zone.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
