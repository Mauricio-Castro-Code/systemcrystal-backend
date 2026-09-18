from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import (
    InventoryProduct,
    Order,
    OrderRouteConstraint,
    UserProfile,
    normalize_text,
)

TWO_DECIMAL_PLACES = Decimal("0.01")
TAX_RATE = Decimal("0.16")


def to_money(value) -> Decimal:
    return Decimal(value or 0).quantize(TWO_DECIMAL_PLACES, rounding=ROUND_HALF_UP)


class LoginSerializer(serializers.Serializer):
    identifier = serializers.CharField(max_length=150)
    password = serializers.CharField(trim_whitespace=False, write_only=True)


class PasswordValidationMixin:
    def validate(self, attrs):
        attrs = super().validate(attrs)
        password = attrs.get("password")
        if password:
            user = self.instance or get_user_model()(
                username=attrs.get("email", ""), email=attrs.get("email", ""),
                first_name=attrs.get("displayName", ""),
            )
            try:
                validate_password(password, user=user)
            except DjangoValidationError as error:
                raise serializers.ValidationError({"password": error.messages}) from error
        return attrs


class RegisterSerializer(PasswordValidationMixin, serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(min_length=8, trim_whitespace=False, write_only=True)
    registrationKey = serializers.CharField(min_length=6, trim_whitespace=True, write_only=True)
    role = serializers.ChoiceField(choices=["ventas"], default="ventas")


class TeamMemberCreateSerializer(PasswordValidationMixin, serializers.Serializer):
    displayName = serializers.CharField(max_length=120, allow_blank=True, required=False)
    email = serializers.EmailField()
    password = serializers.CharField(min_length=8, trim_whitespace=False, write_only=True)
    role = serializers.ChoiceField(
        choices=UserProfile.Role.choices,
        default=UserProfile.Role.VENTAS,
    )


class TeamMemberUpdateSerializer(PasswordValidationMixin, serializers.Serializer):
    displayName = serializers.CharField(max_length=120, allow_blank=True, required=False)
    role = serializers.ChoiceField(choices=UserProfile.Role.choices, required=False)
    isActive = serializers.BooleanField(required=False)
    password = serializers.CharField(
        min_length=8,
        trim_whitespace=False,
        write_only=True,
        required=False,
        allow_blank=True,
    )


class InventoryItemSerializer(serializers.ModelSerializer):
    name = serializers.CharField(max_length=120)
    quantity = serializers.IntegerField(min_value=0)
    unitPrice = serializers.DecimalField(
        source="unit_price",
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.00"),
    )
    category = serializers.ChoiceField(
        choices=InventoryProduct.Category.choices,
        default=InventoryProduct.Category.OTROS,
    )

    class Meta:
        model = InventoryProduct
        fields = ("id", "name", "quantity", "unitPrice", "category")

    def validate_name(self, value: str) -> str:
        normalized_name = str(value or "").strip()

        if not normalized_name:
            raise serializers.ValidationError("El nombre del producto es requerido.")

        queryset = InventoryProduct.objects.filter(name_key=normalize_text(normalized_name))

        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)

        if queryset.exists():
            raise serializers.ValidationError("Ya existe un producto con este nombre.")

        return normalized_name


class QuotationClientInfoSerializer(serializers.Serializer):
    fullName = serializers.CharField(max_length=120)
    phoneNumber = serializers.CharField(max_length=25)
    birthDate = serializers.DateField(allow_null=True, required=False)
    address = serializers.CharField(max_length=180, allow_blank=True, required=False)
    neighborhood = serializers.CharField(max_length=120, allow_blank=True, required=False)
    reference = serializers.CharField(max_length=220, allow_blank=True, required=False)
    deliveryInstructions = serializers.CharField(
        max_length=220,
        allow_blank=True,
        required=False,
    )


class QuotationScheduleSerializer(serializers.Serializer):
    deliveryDate = serializers.DateField(allow_null=True)
    eventDate = serializers.DateField(allow_null=True)
    collectionDate = serializers.DateField(allow_null=True)


class QuotationLogisticsSerializer(serializers.Serializer):
    freight = serializers.DecimalField(max_digits=12, decimal_places=2)
    securityDeposit = serializers.DecimalField(max_digits=12, decimal_places=2)
    applyTax = serializers.BooleanField(required=False, default=False)


class QuotationEquipmentItemSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=0)
    equipment = serializers.CharField(max_length=120, allow_blank=True)
    unitPrice = serializers.DecimalField(max_digits=12, decimal_places=2)
    total = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    productId = serializers.IntegerField(required=False, allow_null=True, default=None)


class QuotationSummarySerializer(serializers.Serializer):
    subtotal = serializers.DecimalField(max_digits=12, decimal_places=2)
    freight = serializers.DecimalField(max_digits=12, decimal_places=2)
    taxAmount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
    )
    securityDeposit = serializers.DecimalField(max_digits=12, decimal_places=2)
    discount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
    )
    advancePayment = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
    )
    totalEstimated = serializers.DecimalField(max_digits=12, decimal_places=2)
    balanceDue = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
    )


class QuotationNoteSerializer(serializers.Serializer):
    clientInfo = QuotationClientInfoSerializer()
    schedule = QuotationScheduleSerializer()
    logistics = QuotationLogisticsSerializer()
    equipmentItems = QuotationEquipmentItemSerializer(many=True)
    summary = QuotationSummarySerializer()

    def validate(self, attrs):
        self.ensure_valid_schedule(attrs["schedule"])
        normalized_items = self.normalize_items(attrs["equipmentItems"])
        logistics = attrs["logistics"]
        freight = to_money(logistics["freight"])
        security_deposit = to_money(logistics["securityDeposit"])
        apply_tax = bool(logistics.get("applyTax"))

        if normalized_items:
            subtotal = sum((item["total"] for item in normalized_items), start=Decimal("0"))
        else:
            subtotal = to_money(attrs["summary"]["subtotal"])

        taxable_amount = subtotal + freight
        tax_amount = to_money(taxable_amount * TAX_RATE if apply_tax else 0)
        discount = to_money(attrs["summary"].get("discount", 0))
        advance_payment = to_money(attrs["summary"].get("advancePayment", 0))
        total_estimated = subtotal + freight + tax_amount + security_deposit
        balance_due = total_estimated - discount - advance_payment

        if balance_due < Decimal("0.00"):
            raise serializers.ValidationError(
                {
                    "summary": (
                        "El descuento y el anticipo no pueden exceder el total a pagar."
                    ),
                }
            )

        attrs["equipmentItems"] = normalized_items
        attrs["logistics"] = {
            "freight": freight,
            "securityDeposit": security_deposit,
            "applyTax": apply_tax,
        }
        attrs["summary"] = {
            "subtotal": subtotal.quantize(TWO_DECIMAL_PLACES, rounding=ROUND_HALF_UP),
            "freight": freight,
            "taxAmount": tax_amount,
            "securityDeposit": security_deposit,
            "discount": discount,
            "advancePayment": advance_payment,
            "totalEstimated": total_estimated.quantize(
                TWO_DECIMAL_PLACES,
                rounding=ROUND_HALF_UP,
            ),
            "balanceDue": balance_due.quantize(
                TWO_DECIMAL_PLACES,
                rounding=ROUND_HALF_UP,
            ),
        }

        return attrs

    def ensure_valid_schedule(self, schedule: dict) -> None:
        delivery_date = schedule.get("deliveryDate")
        event_date = schedule.get("eventDate")
        collection_date = schedule.get("collectionDate")

        if not delivery_date or not event_date or not collection_date:
            return

        if delivery_date > event_date or event_date > collection_date:
            raise serializers.ValidationError(
                {
                    "schedule": "Las fechas deben seguir el orden entrega, evento y recoleccion.",
                }
            )

    def normalize_items(self, equipment_items: list[dict]) -> list[dict]:
        normalized_items = []
        requested_product_ids = {
            item["productId"] for item in equipment_items if item.get("productId")
        }
        valid_product_ids = set(
            InventoryProduct.objects.filter(pk__in=requested_product_ids).values_list(
                "pk", flat=True
            )
        )

        for item in equipment_items:
            quantity = int(item["quantity"])
            equipment = item["equipment"].strip()
            unit_price = to_money(item["unitPrice"])
            total = (Decimal(quantity) * unit_price).quantize(
                TWO_DECIMAL_PLACES,
                rounding=ROUND_HALF_UP,
            )
            product_id = item.get("productId")
            product_id = product_id if product_id in valid_product_ids else None

            if not equipment and quantity == 0 and unit_price == Decimal("0.00"):
                continue

            normalized_items.append(
                {
                    "quantity": quantity,
                    "equipment": equipment,
                    "unitPrice": unit_price,
                    "total": total,
                    "productId": product_id,
                }
            )

        return normalized_items


class OrderExtraCostSerializer(serializers.Serializer):
    concepto = serializers.CharField(max_length=220, allow_blank=False)
    monto = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))


class OrderAssignmentSerializer(serializers.Serializer):
    # driverId = null/None desasigna; ausente deja al chofer intacto.
    driverId = serializers.IntegerField(required=False, allow_null=True)
    mapsUrl = serializers.URLField(
        max_length=500,
        required=False,
        allow_blank=True,
    )

    def validate_mapsUrl(self, value):
        from .route_optimization import is_allowed_maps_url

        if value and not is_allowed_maps_url(value):
            raise serializers.ValidationError("Usa un enlace HTTPS de Google Maps.")
        return value

    def validate(self, attrs):
        if "driverId" not in attrs and "mapsUrl" not in attrs:
            raise serializers.ValidationError(
                "Debes enviar driverId y/o mapsUrl.",
            )
        return attrs


class OrderStatusUpdateSerializer(serializers.Serializer):
    operationalStatus = serializers.ChoiceField(
        choices=Order.OperationalStatus.choices,
        required=False,
    )
    billingStatus = serializers.ChoiceField(
        choices=Order.BillingStatus.choices,
        required=False,
    )
    comment = serializers.CharField(
        max_length=500,
        required=False,
        allow_blank=True,
    )

    def validate(self, attrs):
        if "operationalStatus" not in attrs and "billingStatus" not in attrs:
            raise serializers.ValidationError(
                "Debes enviar al menos un estado para actualizar la nota.",
            )

        attrs["comment"] = str(attrs.get("comment", "")).strip()
        return attrs


class OrderRouteConstraintSerializer(serializers.Serializer):
    timeWindowStart = serializers.TimeField(required=False, allow_null=True)
    timeWindowEnd = serializers.TimeField(required=False, allow_null=True)
    priority = serializers.ChoiceField(
        choices=OrderRouteConstraint.Priority.choices,
        required=False,
        allow_null=True,
    )
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if (
            "timeWindowStart" not in attrs
            and "timeWindowEnd" not in attrs
            and "priority" not in attrs
            and not attrs.get("note")
        ):
            raise serializers.ValidationError(
                "Debes enviar una ventana, prioridad o nota para la restricción.",
            )
        return attrs


class DriverRouteAddOrderSerializer(serializers.Serializer):
    orderId = serializers.CharField(max_length=20)


class ClientCreateSerializer(serializers.Serializer):
    clientName = serializers.CharField(max_length=120)
    phoneNumber = serializers.CharField(max_length=25, allow_blank=True, default="")
    email = serializers.EmailField(allow_blank=True, required=False, default="")


class ClientUpdateSerializer(serializers.Serializer):
    clientName = serializers.CharField(max_length=120, required=False)
    phoneNumber = serializers.CharField(max_length=25, allow_blank=True, required=False)
    email = serializers.EmailField(allow_blank=True, required=False)


class ClientAddressSerializer(serializers.Serializer):
    addressLine = serializers.CharField(max_length=220)
    neighborhood = serializers.CharField(max_length=120, allow_blank=True, default="")
    reference = serializers.CharField(max_length=220, allow_blank=True, default="")
