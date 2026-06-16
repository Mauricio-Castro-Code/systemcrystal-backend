from __future__ import annotations

from django.db import transaction

from .models import (
    Client,
    Order,
    OrderWorkflowEvent,
    Quotation,
    QuotationItem,
    normalize_text,
    only_digits,
)


def build_client_address(client_info: dict) -> str:
    address_parts = [
        str(client_info.get("address", "")).strip(),
        str(client_info.get("neighborhood", "")).strip(),
    ]
    return ", ".join(part for part in address_parts if part)


def upsert_client_from_note(note: dict) -> Client:
    client_info = note["clientInfo"]
    full_name = str(client_info.get("fullName", "")).strip() or "Cliente sin nombre"
    phone_number = str(client_info.get("phoneNumber", "")).strip()
    phone_digits = only_digits(phone_number)
    name_key = normalize_text(full_name)

    client = None

    if phone_digits:
        client = Client.objects.filter(phone_digits=phone_digits).first()

    if client is None and name_key:
        client = Client.objects.filter(name_key=name_key).first()

    if client is None:
        client = Client(
            client_name=full_name,
            contact_person=full_name,
        )

    client.client_name = full_name
    client.contact_person = full_name

    if phone_number or not client.pk:
        client.phone_number = phone_number

    resolved_address = build_client_address(client_info)

    if resolved_address or not client.pk:
        client.address = resolved_address

    client.save()
    return client


def apply_note_to_quotation(quotation: Quotation, note: dict) -> Quotation:
    client_info = note["clientInfo"]
    schedule = note["schedule"]
    logistics = note["logistics"]
    summary = note["summary"]
    client = upsert_client_from_note(note)

    quotation.client = client
    quotation.client_name = str(client_info.get("fullName", "")).strip() or "Cliente sin nombre"
    quotation.phone_number = str(client_info.get("phoneNumber", "")).strip()
    quotation.birth_date = client_info.get("birthDate")
    quotation.address = str(client_info.get("address", "")).strip()
    quotation.neighborhood = str(client_info.get("neighborhood", "")).strip()
    quotation.reference = str(client_info.get("reference", "")).strip()
    quotation.delivery_instructions = str(
        client_info.get("deliveryInstructions", ""),
    ).strip()
    quotation.delivery_date = schedule.get("deliveryDate")
    quotation.event_date = schedule.get("eventDate")
    quotation.collection_date = schedule.get("collectionDate")
    quotation.freight = logistics["freight"]
    quotation.apply_tax = logistics["applyTax"]
    quotation.tax_amount = summary["taxAmount"]
    quotation.security_deposit = logistics["securityDeposit"]
    quotation.discount = summary["discount"]
    quotation.advance_payment = summary["advancePayment"]
    quotation.subtotal = summary["subtotal"]
    quotation.total_estimated = summary["totalEstimated"]
    quotation.save()

    replace_equipment_items(quotation, note["equipmentItems"])
    return quotation


def replace_equipment_items(quotation: Quotation, equipment_items: list[dict]) -> None:
    quotation.equipment_items.all().delete()

    if not equipment_items:
        return

    QuotationItem.objects.bulk_create(
        [
            QuotationItem(
                quotation=quotation,
                quantity=item["quantity"],
                equipment=item["equipment"],
                unit_price=item["unitPrice"],
                total=item["total"],
            )
            for item in equipment_items
        ]
    )


def create_order_workflow_event(
    order: Order,
    *,
    category: str,
    from_status: str = "",
    to_status: str,
    changed_by=None,
    comment: str = "",
) -> OrderWorkflowEvent:
    return OrderWorkflowEvent.objects.create(
        order=order,
        category=category,
        from_status=from_status,
        to_status=to_status,
        comment=str(comment or "").strip(),
        changed_by=changed_by if getattr(changed_by, "pk", None) else None,
    )


def create_initial_order_workflow(order: Order, changed_by=None) -> None:
    create_order_workflow_event(
        order,
        category=OrderWorkflowEvent.Category.OPERATIONAL,
        to_status=order.operational_status,
        changed_by=changed_by,
        comment="Estado inicial de la nota.",
    )
    create_order_workflow_event(
        order,
        category=OrderWorkflowEvent.Category.BILLING,
        to_status=order.billing_status,
        changed_by=changed_by,
        comment="Estado inicial de cobranza.",
    )


@transaction.atomic
def update_order_statuses(
    order: Order,
    *,
    operational_status: str | None = None,
    billing_status: str | None = None,
    changed_by=None,
    comment: str = "",
) -> Order:
    update_fields = ["updated_at"]
    normalized_comment = str(comment or "").strip()

    if operational_status and operational_status != order.operational_status:
        previous_status = order.operational_status
        order.operational_status = operational_status
        update_fields.append("operational_status")
        create_order_workflow_event(
            order,
            category=OrderWorkflowEvent.Category.OPERATIONAL,
            from_status=previous_status,
            to_status=operational_status,
            changed_by=changed_by,
            comment=normalized_comment,
        )

    if billing_status and billing_status != order.billing_status:
        previous_status = order.billing_status
        order.billing_status = billing_status
        if "billing_status" not in update_fields:
            update_fields.append("billing_status")
        create_order_workflow_event(
            order,
            category=OrderWorkflowEvent.Category.BILLING,
            from_status=previous_status,
            to_status=billing_status,
            changed_by=changed_by,
            comment=normalized_comment,
        )

    if len(update_fields) > 1:
        order.save(update_fields=update_fields)

    return order


@transaction.atomic
def create_quotation_from_note(note: dict) -> Quotation:
    quotation = Quotation(status=Quotation.Status.DRAFT, client_name="")
    return apply_note_to_quotation(quotation, note)


@transaction.atomic
def update_quotation_from_note(quotation: Quotation, note: dict) -> Quotation:
    return apply_note_to_quotation(quotation, note)


@transaction.atomic
def confirm_quotation_as_order(
    quotation: Quotation,
    changed_by=None,
    folio_strategy: str = "fill",
) -> Order:
    quotation.status = Quotation.Status.CONFIRMED
    quotation.save(update_fields=["status", "updated_at"])

    order = Order.objects.filter(quotation=quotation).first()

    if order is None:
        order = Order(quotation=quotation)
        order.save(folio_strategy=folio_strategy)
        create_initial_order_workflow(order, changed_by)

    return order


@transaction.atomic
def create_order_from_note(
    note: dict,
    changed_by=None,
    folio_strategy: str = "fill",
) -> Order:
    quotation = Quotation(status=Quotation.Status.CONFIRMED, client_name="")
    quotation = apply_note_to_quotation(quotation, note)
    order = Order(quotation=quotation)
    order.save(folio_strategy=folio_strategy)
    create_initial_order_workflow(order, changed_by)
    return order


@transaction.atomic
def create_order_from_imported_note(
    note: dict,
    *,
    order_id: str = "",
    changed_by=None,
    operational_status: str = Order.OperationalStatus.PROGRAMADA,
    billing_status: str = Order.BillingStatus.AL_CORRIENTE,
    confirmed_at=None,
) -> Order:
    """Crea una orden confirmada a partir de una nota importada desde Excel.

    Reutiliza el upsert de cliente (así un cliente que solo estaba en la nota
    de Excel queda dado de alta en el directorio). Si `order_id` viene vacío se
    genera el siguiente folio disponible; si viene, se respeta tal cual.
    """
    quotation = Quotation(status=Quotation.Status.CONFIRMED, client_name="")
    quotation = apply_note_to_quotation(quotation, note)

    order = Order(
        quotation=quotation,
        operational_status=operational_status,
        billing_status=billing_status,
    )
    if order_id:
        order.order_id = order_id
    if confirmed_at:
        order.confirmed_at = confirmed_at

    order.save(folio_strategy="fill")
    create_initial_order_workflow(order, changed_by)
    return order


@transaction.atomic
def update_order_from_note(order: Order, note: dict) -> Order:
    apply_note_to_quotation(order.quotation, note)
    return order


@transaction.atomic
def delete_order_and_quotation(order: Order) -> None:
    quotation = order.quotation
    order.delete()
    quotation.delete()
