from __future__ import annotations

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.utils import timezone


def normalize_text(value: str) -> str:
    return str(value or "").strip().lower()


def only_digits(value: str) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


def extract_order_sequence_value(order_id: str, year_suffix: str) -> int | None:
    normalized_order_id = str(order_id or "").strip()

    if not normalized_order_id.endswith(f"-{year_suffix}"):
        return None

    value_part, separator, suffix = normalized_order_id.partition("-")

    if separator != "-" or suffix != year_suffix or not value_part.isdigit():
        return None

    return int(value_part)


def used_order_values(year_suffix: str) -> list[int]:
    return sorted(
        value
        for value in (
            extract_order_sequence_value(order_id, year_suffix)
            for order_id in Order.objects.filter(
                order_id__endswith=f"-{year_suffix}",
            ).values_list("order_id", flat=True)
        )
        if value is not None
    )


def next_available_order_value(year_suffix: str) -> int:
    """Menor folio libre (rellena huecos para mantener la secuencia compacta)."""
    next_value = 1

    for used_value in used_order_values(year_suffix):
        if used_value < next_value:
            continue

        if used_value == next_value:
            next_value += 1
            continue

        break

    return next_value


def next_sequential_order_value(year_suffix: str) -> int:
    """Siguiente folio estrictamente secuencial (ignora huecos): max usado + 1."""
    used_values = used_order_values(year_suffix)
    return (used_values[-1] if used_values else 0) + 1


def available_order_gaps(year_suffix: str) -> list[int]:
    """Todos los folios libres por debajo del último usado (huecos por borrados)."""
    used_values = used_order_values(year_suffix)

    if not used_values:
        return []

    used_set = set(used_values)
    return [value for value in range(1, used_values[-1]) if value not in used_set]


def order_folio_options(reference_date=None) -> dict:
    """Opciones de folio para una nueva orden: huecos disponibles vs. secuencial."""
    if reference_date is None:
        reference_date = timezone.localdate()

    year_suffix = reference_date.strftime("%y")
    fill_value = next_available_order_value(year_suffix)
    sequential_value = next_sequential_order_value(year_suffix)
    gaps = available_order_gaps(year_suffix)

    return {
        "yearSuffix": year_suffix,
        "fillValue": fill_value,
        "sequentialValue": sequential_value,
        "fillFolio": f"{fill_value:04d}-{year_suffix}",
        "sequentialFolio": f"{sequential_value:04d}-{year_suffix}",
        "hasGap": bool(gaps),
        "gaps": [
            {"value": value, "folio": f"{value:04d}-{year_suffix}"}
            for value in gaps
        ],
    }


class FolioUnavailableError(Exception):
    """El folio elegido ya está en uso o no es válido."""


def next_document_code(
    sequence_key: str,
    reference_date=None,
    *,
    order_strategy: str = "fill",
    explicit_order_value: int | None = None,
) -> str:
    year_suffix = ""

    if reference_date is None:
        reference_date = timezone.localdate()

    if sequence_key in {
        DocumentSequence.Key.QUOTATION,
        DocumentSequence.Key.ORDER,
    }:
        year_suffix = reference_date.strftime("%y")

    next_value = None

    for _ in range(3):
        with transaction.atomic():
            sequence = (
                DocumentSequence.objects.select_for_update()
                .filter(key=sequence_key, year_suffix=year_suffix)
                .first()
            )

            if sequence is None:
                try:
                    sequence = DocumentSequence.objects.create(
                        key=sequence_key,
                        year_suffix=year_suffix,
                    )
                except IntegrityError:
                    continue

            if sequence_key == DocumentSequence.Key.ORDER:
                # Folio explícito = hueco concreto elegido por el usuario.
                # "sequential" = max usado + 1; "fill" = rellena el primer hueco
                # disponible para mantener la secuencia anual compacta.
                if explicit_order_value is not None:
                    if explicit_order_value in set(used_order_values(year_suffix)):
                        raise FolioUnavailableError(
                            f"El folio {explicit_order_value:04d}-{year_suffix} "
                            "ya está en uso. Elige otro.",
                        )
                    next_value = explicit_order_value
                elif order_strategy == "sequential":
                    next_value = next_sequential_order_value(year_suffix)
                else:
                    next_value = next_available_order_value(year_suffix)
                sequence.last_value = max(sequence.last_value, next_value)
            else:
                sequence.last_value += 1
                next_value = sequence.last_value

            sequence.save(update_fields=["last_value"])
            break

    if next_value is None:
        raise RuntimeError("No fue posible generar el siguiente folio.")

    if sequence_key == DocumentSequence.Key.CLIENT:
        return f"CLI-{next_value:03d}"

    if sequence_key == DocumentSequence.Key.QUOTATION:
        return f"{next_value:03d}COTI-{year_suffix}"

    return f"{next_value:04d}-{year_suffix}"


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class DocumentSequence(models.Model):
    class Key(models.TextChoices):
        CLIENT = "CLIENT", "Client"
        QUOTATION = "QUOTATION", "Quotation"
        ORDER = "ORDER", "Order"

    key = models.CharField(max_length=20, choices=Key.choices)
    year_suffix = models.CharField(max_length=2, blank=True)
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("key", "year_suffix"),
                name="unique_document_sequence_per_year",
            )
        ]
        ordering = ("key", "year_suffix")

    def __str__(self) -> str:
        return f"{self.key}:{self.year_suffix or 'NA'} -> {self.last_value}"


class Client(TimestampedModel):
    code = models.CharField(max_length=20, unique=True, editable=False)
    client_name = models.CharField(max_length=120)
    contact_person = models.CharField(max_length=120)
    phone_number = models.CharField(max_length=25, blank=True)
    phone_digits = models.CharField(max_length=20, blank=True, db_index=True, editable=False)
    name_key = models.CharField(max_length=120, blank=True, db_index=True, editable=False)
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=180, blank=True)

    class Meta:
        ordering = ("client_name", "code")

    def save(self, *args, **kwargs):
        self.client_name = str(self.client_name or "").strip() or "Cliente sin nombre"
        self.contact_person = str(self.contact_person or "").strip() or self.client_name
        self.phone_number = str(self.phone_number or "").strip()
        self.phone_digits = only_digits(self.phone_number)
        self.name_key = normalize_text(self.client_name)

        if not self.code:
            self.code = next_document_code(DocumentSequence.Key.CLIENT)

        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.code} - {self.client_name}"


class InventoryProduct(TimestampedModel):
    name = models.CharField(max_length=120)
    name_key = models.CharField(max_length=120, unique=True, editable=False)
    quantity = models.PositiveIntegerField(default=0)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ("name", "id")

    def save(self, *args, **kwargs):
        self.name = str(self.name or "").strip() or "Producto sin nombre"
        self.name_key = normalize_text(self.name)

        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name


class Quotation(TimestampedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        CONFIRMED = "CONFIRMED", "Confirmed"

    quotation_id = models.CharField(max_length=20, unique=True, editable=False)
    client = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        related_name="quotations",
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    client_name = models.CharField(max_length=120)
    phone_number = models.CharField(max_length=25, blank=True)
    birth_date = models.DateField(null=True, blank=True)
    address = models.CharField(max_length=180, blank=True)
    neighborhood = models.CharField(max_length=120, blank=True)
    reference = models.CharField(max_length=220, blank=True)
    delivery_instructions = models.CharField(max_length=220, blank=True)
    delivery_date = models.DateField(null=True, blank=True)
    event_date = models.DateField(null=True, blank=True)
    collection_date = models.DateField(null=True, blank=True)
    freight = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    apply_tax = models.BooleanField(default=False)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    security_deposit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    advance_payment = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_estimated = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ("-created_at",)

    def save(self, *args, **kwargs):
        self.client_name = str(self.client_name or "").strip() or "Cliente sin nombre"
        self.phone_number = str(self.phone_number or "").strip()
        self.address = str(self.address or "").strip()
        self.neighborhood = str(self.neighborhood or "").strip()
        self.reference = str(self.reference or "").strip()
        self.delivery_instructions = str(self.delivery_instructions or "").strip()

        if not self.quotation_id:
            self.quotation_id = next_document_code(DocumentSequence.Key.QUOTATION)

        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.quotation_id


class QuotationItem(models.Model):
    quotation = models.ForeignKey(
        Quotation,
        related_name="equipment_items",
        on_delete=models.CASCADE,
    )
    quantity = models.PositiveIntegerField(default=0)
    equipment = models.CharField(max_length=120)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ("id",)

    def __str__(self) -> str:
        return f"{self.equipment} x {self.quantity}"


class Order(TimestampedModel):
    class Status(models.TextChoices):
        CONFIRMED = "Confirmado", "Confirmado"

    class OperationalStatus(models.TextChoices):
        PROGRAMADA = "PROGRAMADA", "Programada"
        EN_CAMINO = "EN_CAMINO", "En camino"
        ENTREGADO = "ENTREGADO", "Entregado"
        POR_RECOGER = "POR_RECOGER", "Por recoger"
        CLIENTE_ENTREGA = "CLIENTE_ENTREGA", "Cliente entrega"
        RECOGIDO = "RECOGIDO", "Recogido"

    class BillingStatus(models.TextChoices):
        AL_CORRIENTE = "AL_CORRIENTE", "Al corriente"
        POR_COBRAR = "POR_COBRAR", "Por cobrar"
        COBRADO = "COBRADO", "Cobrado"

    order_id = models.CharField(max_length=20, unique=True, editable=False)
    quotation = models.OneToOneField(
        Quotation,
        related_name="order",
        on_delete=models.CASCADE,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.CONFIRMED,
    )
    operational_status = models.CharField(
        max_length=20,
        choices=OperationalStatus.choices,
        default=OperationalStatus.PROGRAMADA,
    )
    billing_status = models.CharField(
        max_length=20,
        choices=BillingStatus.choices,
        default=BillingStatus.AL_CORRIENTE,
    )
    confirmed_at = models.DateTimeField(default=timezone.now)
    is_cancelled = models.BooleanField(default=False)
    assigned_driver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="assigned_orders",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    maps_url = models.URLField(max_length=500, blank=True)

    class Meta:
        ordering = ("-confirmed_at", "-created_at")

    def save(
        self,
        *args,
        folio_strategy: str = "fill",
        folio_value: int | None = None,
        **kwargs,
    ):
        if not self.order_id:
            self.order_id = next_document_code(
                DocumentSequence.Key.ORDER,
                self.confirmed_at.date(),
                order_strategy=folio_strategy,
                explicit_order_value=folio_value,
            )

        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.order_id


class FreightZone(models.Model):
    name = models.CharField(max_length=150)
    name_key = models.CharField(max_length=150, unique=True, editable=False)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    notes = models.CharField(max_length=220, blank=True)

    class Meta:
        ordering = ("name",)

    def save(self, *args, **kwargs):
        self.name = str(self.name or "").strip()
        self.name_key = normalize_text(self.name)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.name} — ${self.price}"


class OrderWorkflowEvent(TimestampedModel):
    class Category(models.TextChoices):
        OPERATIONAL = "OPERATIONAL", "Operativo"
        BILLING = "BILLING", "Cobro"
        ASSIGNMENT = "ASSIGNMENT", "Asignación"

    order = models.ForeignKey(
        Order,
        related_name="workflow_events",
        on_delete=models.CASCADE,
    )
    category = models.CharField(max_length=20, choices=Category.choices)
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20)
    comment = models.TextField(blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="order_workflow_events",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ("-created_at", "-id")

    def __str__(self) -> str:
        return f"{self.order.order_id} {self.category} -> {self.to_status}"


class UserProfile(TimestampedModel):
    class Role(models.TextChoices):
        ADMIN = "admin", "Administrador"
        VENTAS = "ventas", "Ventas"
        CHOFER = "chofer", "Chofer"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        related_name="profile",
        on_delete=models.CASCADE,
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.VENTAS,
    )

    def __str__(self) -> str:
        return f"{self.user.username} ({self.role})"


def get_user_role(user) -> str:
    """Rol del usuario; si no tiene perfil, se deriva de is_staff."""
    try:
        return user.profile.role
    except UserProfile.DoesNotExist:
        return UserProfile.Role.ADMIN if user.is_staff else UserProfile.Role.VENTAS


def set_user_role(user, role: str) -> None:
    """Asigna el rol (crea el perfil si falta) y sincroniza is_staff."""
    normalized_role = role if role in UserProfile.Role.values else UserProfile.Role.VENTAS

    UserProfile.objects.update_or_create(
        user=user,
        defaults={"role": normalized_role},
    )

    is_admin = normalized_role == UserProfile.Role.ADMIN

    if user.is_staff != is_admin:
        user.is_staff = is_admin
        user.save(update_fields=["is_staff"])
