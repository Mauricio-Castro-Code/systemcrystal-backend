from __future__ import annotations

import datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import route_optimization
from .models import (
    Client,
    Order,
    OrderRouteConstraint,
    OrderWorkflowEvent,
    Quotation,
    QuotationItem,
    RouteOptimizationRun,
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
def assign_order_driver(
    order: Order,
    *,
    driver=None,
    maps_url: str | None = None,
    changed_by=None,
) -> Order:
    """Asigna (o desasigna) chofer y/o ubicación de Maps a una orden.

    `driver` puede ser un User, o None para desasignar.
    `maps_url=None` deja la URL intacta; "" la limpia.
    """
    update_fields = ["updated_at"]
    previous_driver = order.assigned_driver

    if driver is not previous_driver:
        order.assigned_driver = driver
        update_fields.append("assigned_driver")

        def _driver_label(user) -> str:
            if user is None:
                return "Sin asignar"
            full_name = (user.get_full_name() or "").strip()
            return full_name or user.username

        create_order_workflow_event(
            order,
            category=OrderWorkflowEvent.Category.ASSIGNMENT,
            from_status=_driver_label(previous_driver),
            to_status=_driver_label(driver),
            changed_by=changed_by,
        )

    if maps_url is not None:
        normalized_url = str(maps_url or "").strip()
        if normalized_url != order.maps_url:
            order.maps_url = normalized_url
            update_fields.append("maps_url")

    if len(update_fields) > 1:
        order.save(update_fields=update_fields)

    return order


def _order_stop_address(order: Order) -> str:
    """Dirección de una parada, lista para mandarse a Google Maps.

    Las notas rara vez incluyen ciudad/estado (solo calle + colonia), y sin
    esos datos Google no logra geocodificar aunque la calle esté bien escrita
    (confirmado con casos reales: agregar ", Puebla, Pue." resolvió direcciones
    que fallaban tal cual). Por eso se agrega automáticamente si falta.
    """
    quotation = order.quotation
    address_parts = [quotation.address, quotation.neighborhood]
    address = ", ".join(part for part in address_parts if part)
    if not address:
        return ""

    locality = settings.DRIVER_ROUTE_DEFAULT_LOCALITY
    if locality and locality.split(",")[0].strip().lower() not in address.lower():
        address = f"{address}, {locality}"
    return address


@transaction.atomic
def set_order_route_constraint(
    order: Order,
    *,
    time_window_start: datetime.time | None = None,
    time_window_end: datetime.time | None = None,
    priority: str | None = None,
    raw_note: str = "",
    changed_by=None,
) -> OrderRouteConstraint:
    """Guarda la restricción operativa que el chofer agrega a una parada.

    Si viene una nota libre y no se especificaron ventana/prioridad explícitas, se le
    pide a la IA que la interprete (nunca calcula tiempos de viaje, solo estructura texto).
    """
    normalized_note = str(raw_note or "").strip()
    parsed_by_ai = False

    if normalized_note and time_window_start is None and time_window_end is None and priority is None:
        parsed = route_optimization.parse_constraint_from_text(normalized_note)
        time_window_start = parsed.get("time_window_start")
        time_window_end = parsed.get("time_window_end")
        priority = parsed.get("priority")
        parsed_by_ai = bool(parsed)

    constraint, _ = OrderRouteConstraint.objects.get_or_create(order=order)
    constraint.time_window_start = time_window_start
    constraint.time_window_end = time_window_end
    constraint.priority = priority or OrderRouteConstraint.Priority.NORMAL
    constraint.raw_note = normalized_note
    constraint.parsed_by_ai = parsed_by_ai
    constraint.created_by = changed_by if getattr(changed_by, "pk", None) else None
    constraint.save()
    return constraint


def clear_order_route_constraint(order: Order) -> None:
    OrderRouteConstraint.objects.filter(order=order).delete()


def run_route_optimization(
    driver,
    route_date,
    orders: list[Order],
    *,
    origin_lat: float | None = None,
    origin_lng: float | None = None,
) -> RouteOptimizationRun:
    """Optimiza la ruta del día de un chofer y persiste el resultado.

    `origin_lat`/`origin_lng` son la ubicación GPS del chofer al momento de pedir la
    optimización (capturada por el navegador) -- se usan como punto de partida real en
    vez de la bodega si el chofer dio permiso de ubicación. Sin ellas, se usa
    `DRIVER_ROUTE_START_ADDRESS` como respaldo.

    Lanza `RouteEngineError` con un mensaje accionable si falta configuración,
    alguna parada no tiene dirección, o el motor de mapas no pudo responder.
    """
    if origin_lat is not None and origin_lng is not None:
        origin = route_optimization.Origin(latitude=origin_lat, longitude=origin_lng)
    else:
        origin_address = settings.DRIVER_ROUTE_START_ADDRESS
        if not origin_address:
            raise route_optimization.RouteEngineError(
                "No se pudo obtener tu ubicación y falta configurar la dirección de "
                "salida (DRIVER_ROUTE_START_ADDRESS) en el servidor."
            )
        origin = route_optimization.Origin(address=origin_address)

    stop_addresses = [_order_stop_address(order) for order in orders]

    # Si administración/el chofer ya capturó el link de Maps que mandó el cliente, se
    # usan esas coordenadas exactas en vez de adivinar con el texto de la dirección
    # (que puede ser ambiguo: "Barrio de San Juan" existe en más de un municipio).
    stop_waypoints: list[route_optimization.Waypoint] = []
    stop_labels: list[str] = []
    for order, address in zip(orders, stop_addresses):
        coordinates = (
            route_optimization.resolve_maps_url_coordinates(order.maps_url)
            if order.maps_url
            else None
        )
        if coordinates:
            stop_waypoints.append(
                route_optimization.Waypoint(latitude=coordinates[0], longitude=coordinates[1])
            )
        else:
            stop_waypoints.append(route_optimization.Waypoint(address=address))
        stop_labels.append(address or order.maps_url or order.order_id)

    missing = [
        order.order_id
        for order, waypoint in zip(orders, stop_waypoints)
        if not waypoint.address and waypoint.latitude is None
    ]
    if missing:
        raise route_optimization.RouteEngineError(
            "Estas notas no tienen dirección ni link de ubicación capturado: " + ", ".join(missing)
        )

    try:
        matrix = route_optimization.fetch_route_matrix(origin, stop_waypoints, stop_labels)
    except route_optimization.UnroutableStopsError as error:
        # Traducimos etiqueta -> folio para que el mensaje diga qué nota corregir.
        label_to_order_id = dict(zip(stop_labels, (o.order_id for o in orders)))
        broken = [f"{label_to_order_id.get(label, '?')} ({label})" for label in error.addresses]
        raise route_optimization.RouteEngineError(
            "El mapa no pudo ubicar estas notas -- corrige su dirección o pega el link de "
            "ubicación que mandó el cliente, e intenta de nuevo: " + ", ".join(broken)
        ) from error

    stop_inputs = []
    for order, address in zip(orders, stop_addresses):
        constraint = getattr(order, "route_constraint", None)
        stop_inputs.append(
            route_optimization.RouteStopInput(
                order_id=order.order_id,
                address=address,
                time_window_start=constraint.time_window_start if constraint else None,
                time_window_end=constraint.time_window_end if constraint else None,
                priority=constraint.priority if constraint else OrderRouteConstraint.Priority.NORMAL,
            )
        )

    # Se parte de la hora real (no de un "inicio de jornada" fijo): el chofer ya está
    # en movimiento -- el resultado siempre respeta que no se puede salir antes de ahora.
    now = timezone.localtime().time()
    result = route_optimization.optimize_stops(stop_inputs, matrix, departure_reference=now)

    recommended_departure = _parse_hhmm_or_none(result["recommendedDeparture"])
    first_stop_eta = _parse_hhmm_or_none(result["firstStopEta"])

    run, _ = RouteOptimizationRun.objects.update_or_create(
        driver=driver,
        route_date=route_date,
        defaults={
            "recommended_departure": recommended_departure,
            "first_stop_eta": first_stop_eta,
            "total_duration_minutes": result["totalDurationMinutes"],
            "total_distance_km": result["totalDistanceKm"],
            "stops": result["stops"],
        },
    )
    return run


def _parse_hhmm_or_none(value: str | None) -> datetime.time | None:
    if not value:
        return None
    hours, minutes = value.split(":")
    return datetime.time(int(hours), int(minutes))


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
    folio_value: int | None = None,
) -> Order:
    quotation.status = Quotation.Status.CONFIRMED
    quotation.save(update_fields=["status", "updated_at"])

    order = Order.objects.filter(quotation=quotation).first()

    if order is None:
        order = Order(quotation=quotation)
        order.save(folio_strategy=folio_strategy, folio_value=folio_value)
        create_initial_order_workflow(order, changed_by)

    return order


@transaction.atomic
def create_order_from_note(
    note: dict,
    changed_by=None,
    folio_strategy: str = "fill",
    folio_value: int | None = None,
) -> Order:
    quotation = Quotation(status=Quotation.Status.CONFIRMED, client_name="")
    quotation = apply_note_to_quotation(quotation, note)
    order = Order(quotation=quotation)
    order.save(folio_strategy=folio_strategy, folio_value=folio_value)
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
    # La nota cambió (cantidades, artículos, etc.): lo impreso ya no coincide.
    order.printed_at = None
    order.save(update_fields=["printed_at"])
    return order


@transaction.atomic
def delete_order_and_quotation(order: Order) -> None:
    quotation = order.quotation
    order.delete()
    quotation.delete()
