"""Optimización de la ruta diaria de un chofer (TSP con ventanas de tiempo).

Reparto de responsabilidades, deliberado:
- OpenAI SOLO interpreta texto libre del chofer y lo convierte en restricciones
  estructuradas (ventana de tiempo, prioridad). Nunca calcula tiempos ni distancias.
- Google Maps Routes API SIEMPRE calcula los tiempos/distancias reales entre paradas.
- Este módulo combina ambos con una heurística de vecino más cercano + 2-opt para
  encontrar un orden de visita razonable. No depende de los modelos de Django: opera
  sobre dataclasses/dicts simples para poder probarse sin red ni base de datos.
"""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

ROUTES_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"

# Tiempo que el chofer tarda en cada parada (carga/descarga/firma) antes de partir a la siguiente.
STOP_SERVICE_MINUTES = 15

# Hora de salida por defecto cuando ninguna parada trae una ventana que la determine.
DEFAULT_DAY_START = datetime.time(8, 0)

# Penalización (en minutos equivalentes) por llegar después del fin de una ventana.
LATE_PENALTY_MINUTES = 240

# Sesgo para preferir paradas de alta prioridad al construir la ruta inicial.
PRIORITY_BONUS_MINUTES = {"ALTA": -30, "NORMAL": 0, "BAJA": 15}


class RouteEngineError(Exception):
    """No se pudo calcular la ruta; el mensaje explica qué falta corregir."""


@dataclass(frozen=True)
class RouteStopInput:
    order_id: str
    address: str
    time_window_start: datetime.time | None = None
    time_window_end: datetime.time | None = None
    priority: str = "NORMAL"


def parse_constraint_from_text(raw_note: str) -> dict:
    """Interpreta una nota libre del chofer y devuelve restricciones estructuradas.

    Ante cualquier falla (sin API key, error de red, respuesta inesperada) se degrada
    a "sin restricciones detectadas": nunca bloquea que el chofer guarde su nota.
    """
    normalized_note = str(raw_note or "").strip()
    if not normalized_note or not settings.OPENAI_API_KEY:
        return {}

    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extraes restricciones operativas de logística a partir de una nota "
                        "de un chofer de reparto, en español de México. Responde solo el JSON "
                        "pedido. No inventes horarios ni prioridades que no estén implícitos "
                        "en la nota; usa null cuando no se mencionen."
                    ),
                },
                {"role": "user", "content": normalized_note},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "route_constraint",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "ventana_inicio": {
                                "type": ["string", "null"],
                                "description": "Hora de inicio en formato HH:MM (24h), o null.",
                            },
                            "ventana_fin": {
                                "type": ["string", "null"],
                                "description": "Hora de fin en formato HH:MM (24h), o null.",
                            },
                            "prioridad": {
                                "type": ["string", "null"],
                                "enum": ["ALTA", "NORMAL", "BAJA", None],
                            },
                        },
                        "required": ["ventana_inicio", "ventana_fin", "prioridad"],
                        "additionalProperties": False,
                    },
                },
            },
        )
        parsed = json.loads(response.choices[0].message.content)
    except Exception:
        logger.exception("No se pudo interpretar la restricción del chofer con OpenAI.")
        return {}

    result: dict = {}
    start = _parse_hhmm(parsed.get("ventana_inicio"))
    end = _parse_hhmm(parsed.get("ventana_fin"))
    if start:
        result["time_window_start"] = start
    if end:
        result["time_window_end"] = end
    priority = parsed.get("prioridad")
    if priority in ("ALTA", "NORMAL", "BAJA"):
        result["priority"] = priority
    return result


def _parse_hhmm(value) -> datetime.time | None:
    if not value:
        return None
    try:
        hours, minutes = str(value).strip().split(":")
        return datetime.time(int(hours), int(minutes))
    except (ValueError, TypeError):
        return None


def fetch_route_matrix(origin_address: str, stop_addresses: list[str]) -> list[list[dict]]:
    """Matriz real de tiempos/distancias entre la bodega y cada parada (Google Routes API).

    Devuelve una matriz (n+1)x(n+1): índice 0 es el origen, 1..n son `stop_addresses` en el
    mismo orden recibido. Cada celda es {"durationMinutes": float, "distanceKm": float}.
    Lanza `RouteEngineError` si no se puede obtener — sin tiempos reales no hay optimización.
    """
    if not settings.GOOGLE_MAPS_API_KEY:
        raise RouteEngineError(
            "Falta configurar GOOGLE_MAPS_API_KEY en el servidor para calcular la ruta."
        )

    waypoints = [{"waypoint": {"address": origin_address}}] + [
        {"waypoint": {"address": address}} for address in stop_addresses
    ]

    payload = {
        "origins": waypoints,
        "destinations": waypoints,
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings.GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": "originIndex,destinationIndex,duration,distanceMeters,condition",
    }

    try:
        response = requests.post(ROUTES_MATRIX_URL, json=payload, headers=headers, timeout=20)
    except requests.RequestException as error:
        logger.exception("No se pudo contactar a Google Maps.")
        raise RouteEngineError("No se pudo contactar al servicio de mapas.") from error

    if response.status_code != 200:
        detail = _describe_google_error(response)
        logger.error("Google Routes API respondió %s: %s", response.status_code, response.text)
        raise RouteEngineError(f"El servicio de mapas rechazó la solicitud: {detail}")

    try:
        rows = response.json()
    except ValueError as error:
        logger.exception("Respuesta ilegible de Google Maps.")
        raise RouteEngineError("El servicio de mapas devolvió una respuesta inesperada.") from error

    size = len(waypoints)
    matrix: list[list[dict | None]] = [[None] * size for _ in range(size)]
    for row in rows:
        if row.get("condition") != "ROUTE_EXISTS":
            continue
        # Proto3 omite los campos con valor 0 en JSON, por eso el default explícito.
        origin_index = row.get("originIndex", 0)
        destination_index = row.get("destinationIndex", 0)
        try:
            duration_seconds = float(str(row["duration"]).rstrip("s"))
        except (KeyError, ValueError):
            continue
        matrix[origin_index][destination_index] = {
            "durationMinutes": duration_seconds / 60,
            "distanceKm": row.get("distanceMeters", 0) / 1000,
        }

    if any(cell is None for row in matrix for cell in row):
        logger.warning("La matriz de Google Maps quedó incompleta; se descarta la optimización.")
        raise RouteEngineError(
            "El mapa no pudo trazar la ruta entre todas las paradas. "
            "Revisa que las direcciones sean localizables."
        )

    return matrix


def _describe_google_error(response) -> str:
    """Extrae el mensaje legible del error que devuelve la Routes API."""
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"

    # La API puede responder un objeto o una lista de errores.
    if isinstance(payload, list):
        payload = payload[0] if payload else {}

    message = (payload.get("error") or {}).get("message") if isinstance(payload, dict) else None
    return message or f"HTTP {response.status_code}"


def optimize_stops(
    stops: list[RouteStopInput],
    matrix: list[list[dict]],
) -> dict:
    """Heurística TSPTW (vecino más cercano + 2-opt) sobre una matriz de tiempos real.

    No calcula tiempos/distancias por sí misma: siempre parte de `matrix` (Google Maps).
    """
    if not stops:
        return {
            "stops": [],
            "recommendedDeparture": DEFAULT_DAY_START.strftime("%H:%M"),
            "firstStopEta": None,
            "totalDurationMinutes": 0,
            "totalDistanceKm": 0.0,
        }

    business_start = _minutes_since_midnight(DEFAULT_DAY_START)
    order = _nearest_neighbor_order(stops, matrix, business_start)
    order = _two_opt(order, matrix, stops, business_start)
    departure_minutes = _recommended_departure_minutes(order[0], matrix, stops, business_start)
    return _build_result(order, matrix, stops, departure_minutes)


def _minutes_since_midnight(value: datetime.time) -> int:
    return value.hour * 60 + value.minute


def _time_from_minutes(minutes: float) -> datetime.time:
    total_minutes = max(0, round(minutes)) % (24 * 60)
    return datetime.time(total_minutes // 60, total_minutes % 60)


def _nearest_neighbor_order(
    stops: list[RouteStopInput], matrix: list[list[dict]], departure_minutes: float
) -> list[int]:
    remaining = list(range(len(stops)))
    order: list[int] = []
    previous_index = 0
    current_minutes = departure_minutes

    while remaining:
        best_choice = None
        best_score = None
        for candidate in remaining:
            travel = matrix[previous_index][candidate + 1]["durationMinutes"]
            arrival = current_minutes + travel
            stop = stops[candidate]
            score = travel
            if stop.time_window_end:
                window_end = _minutes_since_midnight(stop.time_window_end)
                if arrival > window_end:
                    score += LATE_PENALTY_MINUTES
            if stop.time_window_start:
                window_start = _minutes_since_midnight(stop.time_window_start)
                if arrival < window_start:
                    score += (window_start - arrival) * 0.5
            score += PRIORITY_BONUS_MINUTES.get(stop.priority, 0)
            if best_score is None or score < best_score:
                best_score = score
                best_choice = candidate

        order.append(best_choice)
        remaining.remove(best_choice)
        travel = matrix[previous_index][best_choice + 1]["durationMinutes"]
        arrival = current_minutes + travel
        stop = stops[best_choice]
        if stop.time_window_start:
            arrival = max(arrival, _minutes_since_midnight(stop.time_window_start))
        current_minutes = arrival + STOP_SERVICE_MINUTES
        previous_index = best_choice + 1

    return order


def _route_cost(
    order: list[int],
    matrix: list[list[dict]],
    stops: list[RouteStopInput],
    departure_minutes: float,
) -> float:
    cost = 0.0
    current_minutes = departure_minutes
    previous_index = 0
    for stop_index in order:
        travel = matrix[previous_index][stop_index + 1]["durationMinutes"]
        arrival = current_minutes + travel
        stop = stops[stop_index]
        if stop.time_window_start:
            window_start = _minutes_since_midnight(stop.time_window_start)
            if arrival < window_start:
                cost += window_start - arrival
                arrival = window_start
        if stop.time_window_end:
            window_end = _minutes_since_midnight(stop.time_window_end)
            if arrival > window_end:
                cost += LATE_PENALTY_MINUTES
        cost += travel
        current_minutes = arrival + STOP_SERVICE_MINUTES
        previous_index = stop_index + 1
    return cost


def _two_opt(
    order: list[int],
    matrix: list[list[dict]],
    stops: list[RouteStopInput],
    departure_minutes: float,
) -> list[int]:
    best_order = order
    best_cost = _route_cost(best_order, matrix, stops, departure_minutes)
    n = len(order)
    improved = True

    while improved and n > 3:
        improved = False
        for i in range(n - 1):
            for j in range(i + 1, n):
                candidate = best_order[:i] + list(reversed(best_order[i : j + 1])) + best_order[j + 1 :]
                candidate_cost = _route_cost(candidate, matrix, stops, departure_minutes)
                if candidate_cost < best_cost:
                    best_order = candidate
                    best_cost = candidate_cost
                    improved = True

    return best_order


def _recommended_departure_minutes(
    first_stop_index: int,
    matrix: list[list[dict]],
    stops: list[RouteStopInput],
    business_start_minutes: int,
) -> float:
    stop = stops[first_stop_index]
    if not stop.time_window_start:
        return business_start_minutes
    travel = matrix[0][first_stop_index + 1]["durationMinutes"]
    window_start = _minutes_since_midnight(stop.time_window_start)
    return max(business_start_minutes, round(window_start - travel))


def _build_result(
    order: list[int],
    matrix: list[list[dict]],
    stops: list[RouteStopInput],
    departure_minutes: float,
) -> dict:
    stop_results = []
    current_minutes = departure_minutes
    previous_index = 0
    total_distance_km = 0.0

    for sequence, stop_index in enumerate(order, start=1):
        stop = stops[stop_index]
        leg = matrix[previous_index][stop_index + 1]
        arrival = current_minutes + leg["durationMinutes"]
        alert = None
        if stop.time_window_start:
            window_start = _minutes_since_midnight(stop.time_window_start)
            if arrival < window_start:
                arrival = window_start
        if stop.time_window_end:
            window_end = _minutes_since_midnight(stop.time_window_end)
            if arrival > window_end:
                alert = "Fuera de la ventana comprometida"

        total_distance_km += leg["distanceKm"]
        stop_results.append(
            {
                "orderId": stop.order_id,
                "sequence": sequence,
                "eta": _time_from_minutes(arrival).strftime("%H:%M"),
                "distanceFromPreviousKm": round(leg["distanceKm"], 1),
                "alert": alert,
            }
        )
        current_minutes = arrival + STOP_SERVICE_MINUTES
        previous_index = stop_index + 1

    total_duration_minutes = 0
    if stop_results:
        total_duration_minutes = max(
            round(current_minutes - departure_minutes - STOP_SERVICE_MINUTES), 0
        )

    return {
        "stops": stop_results,
        "recommendedDeparture": _time_from_minutes(departure_minutes).strftime("%H:%M"),
        "firstStopEta": stop_results[0]["eta"] if stop_results else None,
        "totalDurationMinutes": total_duration_minutes,
        "totalDistanceKm": round(total_distance_km, 1),
    }
