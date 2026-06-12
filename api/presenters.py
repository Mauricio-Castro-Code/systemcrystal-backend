from __future__ import annotations

from datetime import timedelta

from django.utils import formats
from django.utils import timezone

from .models import Order, OrderWorkflowEvent


# Etiquetas visibles del estado operativo (el valor en BD sigue siendo
# POR_RECOGER; aqui se muestra como "En Ruta" por decision de negocio).
OPERATIONAL_STATUS_LABELS = {
    Order.OperationalStatus.PROGRAMADA: "Programada",
    Order.OperationalStatus.ENTREGADO: "Entregado",
    Order.OperationalStatus.POR_RECOGER: "En Ruta",
    Order.OperationalStatus.CLIENTE_ENTREGA: "Cliente entrega",
    Order.OperationalStatus.RECOGIDO: "Recogido",
}

OPERATIONAL_FOLDER_LABELS = {
    Order.OperationalStatus.PROGRAMADA: "Programadas",
    Order.OperationalStatus.ENTREGADO: "Entregado",
    Order.OperationalStatus.POR_RECOGER: "En Ruta",
    Order.OperationalStatus.CLIENTE_ENTREGA: "Cliente entrega",
    Order.OperationalStatus.RECOGIDO: "Recogido",
}

BILLING_FOLDER_LABELS = {
    Order.BillingStatus.POR_COBRAR: "Por cobrar",
    Order.BillingStatus.COBRADO: "Pagado",
}


def build_user_session(user, token_key: str) -> dict:
    full_name = user.get_full_name().strip()

    return {
        "id": str(user.pk),
        "displayName": full_name or user.username,
        "email": user.email or "",
        "token": token_key,
        "isAdmin": bool(user.is_staff),
    }


def build_quotation_note(quotation) -> dict:
    balance_due = quotation.total_estimated - quotation.discount - quotation.advance_payment

    equipment_items = [
        {
            "quantity": item.quantity,
            "equipment": item.equipment,
            "unitPrice": item.unit_price,
            "total": item.total,
        }
        for item in quotation.equipment_items.all()
    ]

    return {
        "clientInfo": {
            "fullName": quotation.client_name,
            "phoneNumber": quotation.phone_number,
            "birthDate": quotation.birth_date.isoformat() if quotation.birth_date else None,
            "address": quotation.address,
            "neighborhood": quotation.neighborhood,
            "reference": quotation.reference,
            "deliveryInstructions": quotation.delivery_instructions,
        },
        "schedule": {
            "deliveryDate": quotation.delivery_date.isoformat() if quotation.delivery_date else None,
            "eventDate": quotation.event_date.isoformat() if quotation.event_date else None,
            "collectionDate": quotation.collection_date.isoformat() if quotation.collection_date else None,
        },
        "logistics": {
            "freight": quotation.freight,
            "securityDeposit": quotation.security_deposit,
            "applyTax": quotation.apply_tax,
        },
        "equipmentItems": equipment_items,
        "summary": {
            "subtotal": quotation.subtotal,
            "freight": quotation.freight,
            "taxAmount": quotation.tax_amount,
            "securityDeposit": quotation.security_deposit,
            "discount": quotation.discount,
            "advancePayment": quotation.advance_payment,
            "totalEstimated": quotation.total_estimated,
            "balanceDue": balance_due,
        },
    }


def build_quotation_record(quotation) -> dict:
    return {
        "quotationId": quotation.quotation_id,
        "clientName": quotation.client_name,
        "date": quotation.created_at.isoformat(),
        "totalEstimated": quotation.total_estimated,
        "quotation": build_quotation_note(quotation),
    }


def build_order_record(order) -> dict:
    quotation = order.quotation

    payload = {
        "orderId": order.order_id,
        "clientName": quotation.client_name,
        "date": order.confirmed_at.isoformat(),
        "status": order.status,
        "operationalStatus": order.operational_status,
        "operationalStatusLabel": OPERATIONAL_STATUS_LABELS.get(
            order.operational_status, order.get_operational_status_display()
        ),
        "billingStatus": order.billing_status,
        "billingStatusLabel": order.get_billing_status_display(),
        "folderKeys": resolve_order_folder_keys(order),
        "folderLabels": resolve_order_folder_labels(order),
        "totalEstimated": quotation.total_estimated,
        "isCancelled": order.is_cancelled,
        "quotation": build_quotation_note(quotation),
    }

    workflow_events_manager = getattr(order, "workflow_events", None)

    if workflow_events_manager is not None:
        payload["workflowHistory"] = [
            build_order_workflow_event(event)
            for event in workflow_events_manager.all()
        ]

    return payload


def build_dashboard_overview(orders, quotations, delivery_range: tuple) -> dict:
    generated_at = timezone.localtime()
    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)
    delivery_start, delivery_end = delivery_range

    order_list = list(orders)
    draft_quotations_count = quotations.count()
    today_orders = resolve_orders_for_date(order_list, today)

    return {
        "generatedAt": generated_at.isoformat(),
        "stats": [
            {
                "id": "quotes",
                "title": "Total Cotizaciones",
                "value": str(draft_quotations_count),
                "helperText": "Borradores y oportunidades listas para seguimiento comercial.",
                "icon": "file-text",
            },
            {
                "id": "confirmed-orders",
                "title": "Pedidos Confirmados",
                "value": str(len(order_list)),
                "helperText": "Notas confirmadas con operacion activa dentro del sistema.",
                "icon": "clipboard-list",
            },
            {
                "id": "today-orders",
                "title": "Pedidos para Hoy",
                "value": str(len(today_orders)),
                "helperText": "Carga operativa inmediata para la jornada actual.",
                "icon": "clock",
            },
        ],
        "orderGroups": [
            {
                "id": "today",
                "title": "Pedidos para Hoy",
                "subtitle": "Eventos programados para la jornada actual.",
                "emptyMessage": "No hay pedidos con fecha de evento para hoy.",
                "orders": today_orders,
            },
            {
                "id": "tomorrow",
                "title": "Pedidos para Mañana",
                "subtitle": "Preparacion temprana para la siguiente salida operativa.",
                "emptyMessage": "No hay pedidos programados para manana.",
                "orders": resolve_orders_for_date(order_list, tomorrow),
            },
            {
                "id": "weekend",
                "title": "Pedidos para el Fin de Semana",
                "subtitle": "Vista anticipada de sabado y domingo para coordinacion logistica.",
                "emptyMessage": "No hay pedidos cargados para el fin de semana.",
                "orders": resolve_weekend_orders(order_list, today, tomorrow),
            },
        ],
        "deliveryRange": {
            "startDate": delivery_start.isoformat(),
            "endDate": delivery_end.isoformat(),
            "group": {
                "id": "delivery-range",
                "title": build_delivery_range_title(delivery_start, delivery_end),
                "subtitle": "Notas con fecha de entrega dentro del rango seleccionado.",
                "emptyMessage": "No hay notas por entregar dentro del rango seleccionado.",
                "orders": resolve_orders_for_delivery_range(
                    order_list,
                    delivery_start,
                    delivery_end,
                ),
            },
        },
    }


def resolve_order_folder_keys(order: Order) -> list[str]:
    operational_folder_key = order.operational_status.lower().replace("_", "-")

    folder_keys = [operational_folder_key]

    if order.billing_status == Order.BillingStatus.POR_COBRAR:
        folder_keys.append("por-cobrar")
    elif order.billing_status == Order.BillingStatus.COBRADO:
        folder_keys.append("pagado")

    return folder_keys


def resolve_order_folder_labels(order: Order) -> list[str]:
    labels = [OPERATIONAL_FOLDER_LABELS[order.operational_status]]

    if order.billing_status in BILLING_FOLDER_LABELS:
        labels.append(BILLING_FOLDER_LABELS[order.billing_status])

    return labels


def build_order_workflow_event(event: OrderWorkflowEvent) -> dict:
    return {
        "id": event.pk,
        "category": event.category,
        "categoryLabel": event.get_category_display(),
        "fromStatus": event.from_status,
        "fromStatusLabel": resolve_workflow_status_label(event.category, event.from_status),
        "toStatus": event.to_status,
        "toStatusLabel": resolve_workflow_status_label(event.category, event.to_status),
        "comment": event.comment,
        "createdAt": event.created_at.isoformat(),
        "changedBy": resolve_workflow_actor_name(event),
    }


def resolve_workflow_status_label(category: str, value: str) -> str:
    if not value:
        return "Sin estado previo"

    if category == OrderWorkflowEvent.Category.OPERATIONAL:
        return Order.OperationalStatus(value).label

    if category == OrderWorkflowEvent.Category.BILLING:
        return Order.BillingStatus(value).label

    return value


def resolve_workflow_actor_name(event: OrderWorkflowEvent) -> str:
    if not event.changed_by:
        return "Sistema"

    full_name = event.changed_by.get_full_name().strip()
    return full_name or event.changed_by.username


def resolve_orders_for_date(order_list, target_date) -> list[dict]:
    return [
        build_dashboard_agenda_order(order)
        for order in sorted(
            (
                order
                for order in order_list
                if order.quotation.event_date and order.quotation.event_date == target_date
            ),
            key=lambda order: order.order_id,
        )
    ]


def resolve_weekend_orders(order_list, today, tomorrow) -> list[dict]:
    filtered_orders = []

    for order in order_list:
        event_date = order.quotation.event_date

        if not event_date:
            continue

        day_difference = (event_date - today).days

        if day_difference < 0 or day_difference > 6:
            continue

        if event_date in {today, tomorrow}:
            continue

        if event_date.weekday() not in {5, 6}:
            continue

        filtered_orders.append(order)

    return [
        build_dashboard_agenda_order(order)
        for order in sorted(
            filtered_orders,
            key=lambda order: (
                order.quotation.event_date or today,
                order.order_id,
            ),
        )
    ]


def resolve_orders_for_delivery_range(order_list, delivery_start, delivery_end) -> list[dict]:
    return [
        build_dashboard_agenda_order(order)
        for order in sorted(
            (
                order
                for order in order_list
                if order.quotation.delivery_date
                and delivery_start <= order.quotation.delivery_date <= delivery_end
            ),
            key=lambda order: (
                order.quotation.delivery_date or delivery_start,
                order.order_id,
            ),
        )
    ]


def build_delivery_range_title(delivery_start, delivery_end) -> str:
    start_label = formats.date_format(delivery_start, "j M")
    end_label = formats.date_format(delivery_end, "j M")

    if delivery_start == delivery_end:
        return f"Entregas del {start_label}"

    return f"Entregas del {start_label} al {end_label}"


def build_dashboard_agenda_order(order) -> dict:
    address_parts = [
        order.quotation.address,
        order.quotation.neighborhood,
    ]
    normalized_address = ", ".join(part for part in address_parts if part) or "Domicilio pendiente"

    return {
        "id": order.order_id,
        "clientName": order.quotation.client_name,
        "address": normalized_address,
        "total": order.quotation.total_estimated,
    }
