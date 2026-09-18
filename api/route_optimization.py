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
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

ROUTES_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"

# Evita pagar dos veces por la misma matriz si el chofer/admin le da doble clic a
# "Optimizar ruta" o si se reintenta tras un error -- las paradas del día no
# cambian de un minuto a otro, así que un caché corto es seguro.
MATRIX_CACHE_TTL_SECONDS = 300

# Tiempo que el chofer tarda en cada parada (carga/descarga/firma) antes de partir a la siguiente.
STOP_SERVICE_MINUTES = 15

# Distancia máxima plausible (en línea de conducción) desde la bodega para una
# parada normal del día. Direcciones ambiguas ("Barrio de San Juan" existe en
# más de un municipio de Puebla, por ejemplo) a veces geocodifican a un lugar
# real y con ruta válida, pero a decenas de km del correcto -- Google no lo
# reporta como error porque técnicamente sí hay camino. Este límite atrapa
# ese caso en vez de inflar la ruta del día en silencio.
MAX_PLAUSIBLE_DISTANCE_KM = 60

# Hora de salida por defecto cuando ninguna parada trae una ventana que la determine.
DEFAULT_DAY_START = datetime.time(8, 0)

# Penalización (en minutos equivalentes) por llegar después del fin de una ventana.
LATE_PENALTY_MINUTES = 240

# Sesgo para preferir paradas de alta prioridad al construir la ruta inicial.
PRIORITY_BONUS_MINUTES = {"ALTA": -30, "NORMAL": 0, "BAJA": 15}


class RouteEngineError(Exception):
    """No se pudo calcular la ruta; el mensaje explica qué falta corregir."""


class UnroutableStopsError(RouteEngineError):
    """Google no pudo trazar ruta hacia/desde una o más paradas (o las ubicó implausiblemente lejos).

    `addresses` trae la etiqueta (dirección o folio) de cada una para que quien
    atrapa el error pueda identificar a qué pedido pertenece.
    """

    def __init__(self, addresses: list[str]):
        self.addresses = addresses
        joined = "; ".join(addresses)
        super().__init__(f"El mapa no pudo ubicar estas paradas: {joined}.")


@dataclass(frozen=True)
class RouteStopInput:
    order_id: str
    address: str
    time_window_start: datetime.time | None = None
    time_window_end: datetime.time | None = None
    priority: str = "NORMAL"


@dataclass(frozen=True)
class Waypoint:
    """Un punto para Google Maps: coordenadas exactas (preferido) o una dirección
    de texto a geocodificar. Se usa tanto para el origen (GPS del chofer o bodega)
    como para las paradas (coordenadas de un link de Maps, o la dirección de la nota).
    """

    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    def as_waypoint(self) -> dict:
        if self.latitude is not None and self.longitude is not None:
            return {
                "waypoint": {
                    "location": {"latLng": {"latitude": self.latitude, "longitude": self.longitude}}
                }
            }
        return {"waypoint": {"address": self.address}}

    def cache_key_part(self) -> str:
        if self.latitude is not None and self.longitude is not None:
            # Redondeado a ~11m: agrupa el caché sin perder precisión relevante.
            return f"{round(self.latitude, 4)},{round(self.longitude, 4)}"
        return self.address or ""


# Alias por legibilidad en el punto de partida (misma forma que una parada).
Origin = Waypoint


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


# Coordenadas del pin exacto ("!3d<lat>!4d<lng>") o, si no aparece, del centro del
# mapa en el momento de compartir ("@<lat>,<lng>,<zoom>") -- el pin es más preciso.
_PRECISE_PIN_PATTERN = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
_VIEWPORT_PATTERN = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")
_QUERY_PATTERN = re.compile(r"[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)")

def is_allowed_maps_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and parts.hostname in {
                "maps.app.goo.gl", "goo.gl", "maps.google.com",
                "www.google.com", "google.com", "www.google.com.mx", "maps.google.com.mx",
            }
            and parts.port in (None, 443)
            and parts.username is None and parts.password is None
        )
    except ValueError:
        return False


MAPS_LINK_CACHE_TTL_SECONDS = 60 * 60 * 24  # el link de un cliente no cambia de coordenadas


def resolve_maps_url_coordinates(url: str) -> tuple[float, float] | None:
    """Extrae lat/lng de un link de Google Maps (largo o acortado tipo maps.app.goo.gl).

    Sirve para cuando el cliente comparte su ubicación por WhatsApp: en vez de adivinar
    con el texto de la dirección (que puede ser ambiguo entre municipios), se usa el
    punto exacto que el cliente marcó. Devuelve None si no se pudo resolver -- nunca
    bloquea: quien llama simplemente cae de vuelta a geocodificar la dirección de texto.
    """
    normalized_url = str(url or "").strip()
    if not is_allowed_maps_url(normalized_url):
        return None

    cache_key = f"maps_url_coords:{hashlib.sha256(normalized_url.encode('utf-8')).hexdigest()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return None if cached == "none" else tuple(cached)

    coordinates = None
    try:
        final_url = normalized_url
        for _ in range(5):
            if not is_allowed_maps_url(final_url):
                return None
            response = requests.get(
                final_url, allow_redirects=False, stream=True, timeout=10,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    final_url = urljoin(final_url, response.headers.get("Location", ""))
                    continue
                response.raise_for_status()
                final_url = response.url
                if not is_allowed_maps_url(final_url):
                    return None
                break
            finally:
                response.close()
        else:
            return None
        match = (
            _PRECISE_PIN_PATTERN.search(final_url)
            or _VIEWPORT_PATTERN.search(final_url)
            or _QUERY_PATTERN.search(final_url)
        )
        if match:
            coordinates = (float(match.group(1)), float(match.group(2)))
    except requests.RequestException:
        logger.warning("No se pudo resolver el enlace de Maps.")

    cache.set(cache_key, list(coordinates) if coordinates else "none", MAPS_LINK_CACHE_TTL_SECONDS)
    return coordinates


GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"
GEOCODE_CHECK_CACHE_TTL_SECONDS = 60 * 60 * 24  # el resultado de geocodificar un texto no cambia

# Estos location_type indican que Google no encontró el punto exacto, sino el
# centro aproximado de una calle/colonia/ciudad completa -- no confiable para
# saber a qué tan cerca queda una entrega puntual.
_IMPRECISE_LOCATION_TYPES = {"GEOMETRIC_CENTER", "APPROXIMATE"}


def check_geocode_confidence(address: str) -> str | None:
    """Detecta -antes de optimizar- si Google no está seguro de una dirección.

    Compute Route Matrix no expone esta señal (solo dice si encontró *alguna* ruta,
    no si está seguro de dónde). La Geocoding API sí la da: `partial_match` significa
    que tuvo que ignorar parte de lo escrito (ej. la colonia no coincidió), y
    location_type aproximado significa que solo ubicó el centro de una zona amplia,
    no un punto puntual. Devuelve un mensaje de advertencia, o None si está confiado.
    Nunca bloquea: es una alerta, no un error -- algunas direcciones normales también
    dan partial_match sin estar realmente mal.
    """
    normalized_address = str(address or "").strip()
    if not normalized_address or not settings.GOOGLE_MAPS_API_KEY:
        return None

    cache_key = f"geocode_confidence:{hashlib.sha256(normalized_address.encode('utf-8')).hexdigest()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return None if cached == "none" else cached

    warning = None
    try:
        response = requests.get(
            GEOCODING_URL,
            params={"address": normalized_address, "key": settings.GOOGLE_MAPS_API_KEY},
            timeout=10,
        )
        data = response.json()
        if data.get("status") == "OK" and data.get("results"):
            result = data["results"][0]
            location_type = result.get("geometry", {}).get("location_type")
            formatted = result.get("formatted_address", "")
            if result.get("partial_match"):
                warning = f"Google no encontró una coincidencia exacta; la ubicó en: {formatted}"
            elif location_type in _IMPRECISE_LOCATION_TYPES:
                warning = f"Google solo ubicó el área aproximada, no el domicilio exacto: {formatted}"
    except requests.RequestException:
        logger.warning("No se pudo verificar la confianza de geocodificación.")

    cache.set(cache_key, warning or "none", GEOCODE_CHECK_CACHE_TTL_SECONDS)
    return warning


def fetch_route_matrix(
    origin: Origin, stops: list[Waypoint], stop_labels: list[str]
) -> list[list[dict]]:
    """Matriz real de tiempos/distancias entre el origen y cada parada (Google Routes API).

    `origin` es la ubicación GPS del chofer al momento de optimizar (preferido) o la
    dirección fija de la bodega como respaldo. `stops` es un `Waypoint` por parada
    (coordenadas exactas si hay un link de Maps guardado, o su dirección de texto).
    `stop_labels` es solo para mensajes de error (qué mostrar si esa parada falla).
    Devuelve una matriz (n+1)x(n+1): índice 0 es el origen, 1..n son `stops` en el mismo
    orden recibido. Cada celda es {"durationMinutes": float, "distanceKm": float}.
    Lanza `RouteEngineError` si no se puede obtener — sin tiempos reales no hay optimización.
    """
    if not settings.GOOGLE_MAPS_API_KEY:
        raise RouteEngineError(
            "Falta configurar GOOGLE_MAPS_API_KEY en el servidor para calcular la ruta."
        )

    cache_key = _matrix_cache_key(origin, stops)
    cached_matrix = cache.get(cache_key)
    if cached_matrix is not None:
        return cached_matrix

    waypoints = [origin.as_waypoint()] + [stop.as_waypoint() for stop in stops]

    payload = {
        "origins": waypoints,
        "destinations": waypoints,
        "travelMode": "DRIVE",
        # TRAFFIC_AWARE cae en el SKU "Pro" de Compute Route Matrix: misma cuota
        # gratis mensual que "Essentials" (10,000 elementos), pero el precio por
        # elemento después de agotarla es el doble ($10 vs $5 por 1,000). Se prioriza
        # la puntualidad real sobre el ahorro, ya que con 1-2 camionetas es muy
        # improbable exceder la cuota gratis de todas formas.
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
        logger.error("Google Routes API respondió %s.", response.status_code)
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
        # Un punto sin ruta hacia/desde la bodega está desconectado del mapa vial
        # (dirección con error, incompleta, o inexistente): identificamos cuál es
        # para que se pueda corregir esa nota puntual, no solo "algo falló".
        broken_indices = [
            index
            for index in range(1, size)
            if matrix[0][index] is None or matrix[index][0] is None
        ]
        broken_labels = [stop_labels[index - 1] for index in broken_indices] or stop_labels
        logger.warning("Paradas sin ruta en Google Maps: %s", broken_labels)
        raise UnroutableStopsError(broken_labels)

    # El chequeo de distancia implausible solo aplica a paradas geocodificadas por
    # texto (riesgo de ambigüedad); si ya tenemos coordenadas exactas de un link de
    # Maps, la distancia es real y no hay nada que "corregir" -- puede ser una
    # entrega legítimamente lejana.
    implausible_indices = [
        index
        for index in range(1, size)
        if stops[index - 1].latitude is None
        and (
            matrix[0][index]["distanceKm"] > MAX_PLAUSIBLE_DISTANCE_KM
            or matrix[index][0]["distanceKm"] > MAX_PLAUSIBLE_DISTANCE_KM
        )
    ]
    if implausible_indices:
        implausible_labels = [stop_labels[index - 1] for index in implausible_indices]
        logger.warning(
            "Paradas geocodificadas a un lugar implausiblemente lejano: %s",
            implausible_labels,
        )
        raise UnroutableStopsError(implausible_labels)

    cache.set(cache_key, matrix, MATRIX_CACHE_TTL_SECONDS)
    return matrix


def _matrix_cache_key(origin: Origin, stops: list[Waypoint]) -> str:
    raw = origin.cache_key_part() + "|" + "|".join(stop.cache_key_part() for stop in stops)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"route_matrix:{digest}"


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
    departure_reference: datetime.time | None = None,
) -> dict:
    """Heurística TSPTW (vecino más cercano + 2-opt) sobre una matriz de tiempos real.

    No calcula tiempos/distancias por sí misma: siempre parte de `matrix` (Google Maps).
    `departure_reference` es la hora real en que se pide la optimización (normalmente
    "ahora", ya que se parte de la ubicación GPS del chofer en ese instante) -- nunca se
    parte antes de esa hora. Si no se especifica, se usa `DEFAULT_DAY_START` como piso.
    """
    if not stops:
        return {
            "stops": [],
            "recommendedDeparture": (departure_reference or DEFAULT_DAY_START).strftime("%H:%M"),
            "firstStopEta": None,
            "totalDurationMinutes": 0,
            "totalDistanceKm": 0.0,
        }

    business_start = _minutes_since_midnight(departure_reference or DEFAULT_DAY_START)
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
