from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .models import Client, Order, Quotation, normalize_text


def resolve_client_groups(clients: Iterable[Client]) -> list[list[Client]]:
    client_list = list(clients)

    if not client_list:
        return []

    parent = list(range(len(client_list)))
    phone_index: dict[str, int] = {}
    name_index: dict[str, int] = {}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]

        return index

    def union(first_index: int, second_index: int) -> None:
        first_root = find(first_index)
        second_root = find(second_index)

        if first_root != second_root:
            parent[second_root] = first_root

    for index, client in enumerate(client_list):
        phone_digits = str(client.phone_digits or "").strip()
        name_key = str(client.name_key or "").strip()

        if phone_digits:
            previous_index = phone_index.get(phone_digits)

            if previous_index is not None:
                union(index, previous_index)
            else:
                phone_index[phone_digits] = index

        if name_key:
            previous_index = name_index.get(name_key)

            if previous_index is not None:
                union(index, previous_index)
            else:
                name_index[name_key] = index

    grouped_clients: dict[int, list[Client]] = defaultdict(list)

    for index, client in enumerate(client_list):
        grouped_clients[find(index)].append(client)

    return sorted(
        grouped_clients.values(),
        key=lambda group: (
            resolve_group_value(group, "client_name", "Cliente sin nombre").lower(),
            select_primary_client(group).code,
        ),
    )


def resolve_client_group_by_code(
    clients: Iterable[Client],
    client_code: str,
) -> list[Client] | None:
    normalized_code = str(client_code or "").strip()

    for group in resolve_client_groups(clients):
        if any(client.code == normalized_code for client in group):
            return group

    return None


def build_client_directory_entries(clients: Iterable[Client]) -> list[dict]:
    return [build_client_directory_entry(group) for group in resolve_client_groups(clients)]


def build_client_directory_entry(group: list[Client]) -> dict:
    primary_client = select_primary_client(group)

    return {
        "id": primary_client.code,
        "clientName": resolve_group_value(group, "client_name", "Cliente sin nombre"),
        "contactPerson": resolve_group_value(group, "contact_person", "Pendiente"),
        "phoneNumber": resolve_group_value(group, "phone_number", ""),
        "email": resolve_group_value(group, "email", ""),
        "address": resolve_group_value(group, "address", ""),
        "mergedRecords": len(group),
    }


def build_client_profile(
    group: list[Client],
    quotations: Iterable[Quotation],
    orders: Iterable[Order],
) -> dict:
    primary_client = select_primary_client(group)
    sorted_clients = sort_clients_by_priority(group)
    sorted_quotations = sorted(
        quotations,
        key=lambda quotation: (quotation.updated_at, quotation.created_at),
        reverse=True,
    )
    sorted_orders = sorted(
        orders,
        key=lambda order: (order.confirmed_at, order.created_at),
        reverse=True,
    )

    return {
        "id": primary_client.code,
        "clientName": resolve_group_value(group, "client_name", "Cliente sin nombre"),
        "contactPerson": resolve_group_value(group, "contact_person", "Pendiente"),
        "phoneNumber": resolve_group_value(group, "phone_number", ""),
        "email": resolve_group_value(group, "email", ""),
        "address": resolve_group_value(group, "address", ""),
        "mergedRecords": len(group),
        "mergedClientCodes": [client.code for client in sorted_clients],
        "addresses": build_address_history(sorted_clients, sorted_quotations),
        "orderHistory": build_order_history(sorted_orders),
        "prefill": build_client_prefill(sorted_clients, sorted_quotations),
    }


def build_address_history(clients: list[Client], quotations: list[Quotation]) -> list[dict]:
    addresses: dict[str, dict] = {}

    def register_address(
        address: str,
        reference: str,
        last_used_at,
        address_line: str = "",
        neighborhood: str = "",
        freight=None,
    ) -> None:
        normalized_key = normalize_text(address)

        if not normalized_key:
            return

        existing_entry = addresses.get(normalized_key)

        if existing_entry is None:
            addresses[normalized_key] = {
                "address": address,
                "addressLine": address_line,
                "neighborhood": neighborhood,
                "reference": reference,
                "lastUsedAt": last_used_at,
                "usageCount": 1,
                "freight": freight,
                "freightAt": last_used_at if freight is not None else None,
            }
            return

        existing_entry["usageCount"] += 1

        if last_used_at > existing_entry["lastUsedAt"]:
            existing_entry["lastUsedAt"] = last_used_at

        if reference and not existing_entry["reference"]:
            existing_entry["reference"] = reference

        # Si la entrada existente no tiene colonia separada pero la nueva sí, la actualizamos
        if neighborhood and not existing_entry["neighborhood"]:
            existing_entry["neighborhood"] = neighborhood
            if address_line:
                existing_entry["addressLine"] = address_line

        # Actualizar flete con el más reciente que tenga valor
        if freight is not None:
            if existing_entry["freightAt"] is None or last_used_at > existing_entry["freightAt"]:
                existing_entry["freight"] = freight
                existing_entry["freightAt"] = last_used_at

    for client in clients:
        address = str(client.address or "").strip()

        if address:
            register_address(address, "", client.updated_at, address_line=address)

    for quotation in quotations:
        address = compose_address(quotation.address, quotation.neighborhood)
        reference = str(quotation.reference or "").strip()
        freight = float(quotation.freight) if quotation.freight else None

        if address:
            register_address(
                address,
                reference,
                quotation.updated_at,
                address_line=str(quotation.address or "").strip(),
                neighborhood=str(quotation.neighborhood or "").strip(),
                freight=freight,
            )

    return [
        {
            "address": entry["address"],
            "addressLine": entry["addressLine"],
            "neighborhood": entry["neighborhood"],
            "reference": entry["reference"],
            "lastUsedAt": entry["lastUsedAt"].isoformat(),
            "usageCount": entry["usageCount"],
            "freight": entry.get("freight"),
        }
        for entry in sorted(
            addresses.values(),
            key=lambda entry: (entry["lastUsedAt"], entry["address"]),
            reverse=True,
        )
    ]


def build_order_history(orders: list[Order]) -> list[dict]:
    history = []

    for order in orders:
        quotation = order.quotation

        history.append(
            {
                "orderId": order.order_id,
                "status": order.status,
                "confirmedAt": order.confirmed_at.isoformat(),
                "deliveryDate": quotation.delivery_date.isoformat()
                if quotation.delivery_date
                else None,
                "eventDate": quotation.event_date.isoformat() if quotation.event_date else None,
                "collectionDate": quotation.collection_date.isoformat()
                if quotation.collection_date
                else None,
                "totalEstimated": quotation.total_estimated,
                "address": compose_address(quotation.address, quotation.neighborhood)
                or "Domicilio pendiente",
                "reference": quotation.reference,
                "deliveryInstructions": quotation.delivery_instructions,
            }
        )

    return history


def build_client_prefill(clients: list[Client], quotations: list[Quotation]) -> dict:
    if quotations:
        latest_quotation = quotations[0]

        return {
            "clientInfo": {
                "fullName": latest_quotation.client_name,
                "phoneNumber": latest_quotation.phone_number,
                "birthDate": latest_quotation.birth_date.isoformat()
                if latest_quotation.birth_date
                else None,
                "address": latest_quotation.address,
                "neighborhood": latest_quotation.neighborhood,
                "reference": latest_quotation.reference,
                "deliveryInstructions": latest_quotation.delivery_instructions,
            }
        }

    primary_client = select_primary_client(clients)

    return {
        "clientInfo": {
            "fullName": primary_client.client_name,
            "phoneNumber": primary_client.phone_number,
            "birthDate": None,
            "address": primary_client.address,
            "neighborhood": "",
            "reference": "",
            "deliveryInstructions": "",
        }
    }


def select_primary_client(group: list[Client]) -> Client:
    return sort_clients_by_priority(group)[0]


def sort_clients_by_priority(group: list[Client]) -> list[Client]:
    return sorted(group, key=build_client_priority, reverse=True)


def build_client_priority(client: Client):
    return (
        int(bool(str(client.phone_digits or "").strip())),
        int(bool(str(client.address or "").strip())),
        int(bool(str(client.email or "").strip())),
        int(
            bool(str(client.contact_person or "").strip())
            and normalize_text(client.contact_person) != normalize_text(client.client_name)
        ),
        client.updated_at,
        client.created_at,
        client.code,
    )


def resolve_group_value(group: list[Client], field_name: str, fallback: str) -> str:
    for client in sort_clients_by_priority(group):
        value = str(getattr(client, field_name, "") or "").strip()

        if value:
            return value

    return fallback


def compose_address(address: str, neighborhood: str) -> str:
    parts = [
        str(address or "").strip(),
        str(neighborhood or "").strip(),
    ]
    return ", ".join(part for part in parts if part)
