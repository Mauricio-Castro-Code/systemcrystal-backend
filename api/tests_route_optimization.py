"""Tests puros (sin base de datos) para la optimización de ruta del chofer.

Se corren aparte de `api/tests.py` (que sí usa base de datos) para poder
validar la heurística y el parseo de restricciones sin crear/borrar una base
de datos de prueba contra Supabase. Ejecutar con:

    python manage.py test api.tests_route_optimization
"""

from __future__ import annotations

import datetime
import json
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from api import route_optimization as ro


def _addr_stops(addresses: list[str]) -> list[ro.Waypoint]:
    return [ro.Waypoint(address=address) for address in addresses]


def _matrix(times: list[list[float]], distances: list[list[float]] | None = None) -> list[list[dict]]:
    distances = distances or [[t * 0.5 for t in row] for row in times]
    return [
        [
            {"durationMinutes": times[i][j], "distanceKm": distances[i][j]}
            for j in range(len(times[i]))
        ]
        for i in range(len(times))
    ]


class OptimizeStopsTests(SimpleTestCase):
    databases = set()

    def test_empty_stops_returns_zeroed_result(self):
        result = ro.optimize_stops([], matrix=[])
        self.assertEqual(result["stops"], [])
        self.assertEqual(result["totalDurationMinutes"], 0)
        self.assertEqual(result["totalDistanceKm"], 0.0)
        self.assertIsNone(result["firstStopEta"])

    def test_respects_time_windows_over_naive_distance_order(self):
        # Origen(0) -> A(1) 10min, -> B(2) 40min directo, pero B ya en camino a A tarda poco.
        # A tiene ventana que fuerza visitarla después de B pese a estar más cerca del origen.
        stops = [
            ro.RouteStopInput(
                order_id="A",
                address="Calle A",
                time_window_start=datetime.time(11, 0),
                time_window_end=datetime.time(12, 0),
            ),
            ro.RouteStopInput(
                order_id="B",
                address="Calle B",
                time_window_start=datetime.time(8, 0),
                time_window_end=datetime.time(9, 0),
            ),
        ]
        # index 0 = origen, 1 = A, 2 = B
        matrix = _matrix(
            [
                [0, 10, 40],
                [10, 0, 15],
                [40, 15, 0],
            ]
        )

        result = ro.optimize_stops(stops, matrix)
        order_ids = [stop["orderId"] for stop in result["stops"]]
        self.assertEqual(order_ids, ["B", "A"])
        self.assertEqual(result["stops"][0]["sequence"], 1)
        self.assertEqual(result["stops"][1]["sequence"], 2)

    def test_flags_alert_when_arrival_after_window_end(self):
        stops = [
            ro.RouteStopInput(
                order_id="A",
                address="Calle A",
                time_window_start=datetime.time(8, 0),
                time_window_end=datetime.time(8, 15),
            ),
        ]
        matrix = _matrix([[0, 300], [300, 0]])  # 5 horas de viaje: siempre llega tarde

        result = ro.optimize_stops(stops, matrix)
        self.assertEqual(result["stops"][0]["alert"], "Fuera de la ventana comprometida")

    def test_no_window_stops_get_no_alert_and_default_departure(self):
        stops = [ro.RouteStopInput(order_id="A", address="Calle A")]
        matrix = _matrix([[0, 20], [20, 0]])

        result = ro.optimize_stops(stops, matrix)
        self.assertIsNone(result["stops"][0]["alert"])
        self.assertEqual(result["recommendedDeparture"], ro.DEFAULT_DAY_START.strftime("%H:%M"))
        self.assertEqual(result["firstStopEta"], "08:20")

    def test_departs_from_real_time_not_a_fixed_default(self):
        # El chofer ya está en movimiento (GPS): la salida es "ahora", no 08:00 fijo.
        stops = [ro.RouteStopInput(order_id="A", address="Calle A")]
        matrix = _matrix([[0, 20], [20, 0]])

        result = ro.optimize_stops(stops, matrix, departure_reference=datetime.time(13, 30))

        self.assertEqual(result["recommendedDeparture"], "13:30")
        self.assertEqual(result["firstStopEta"], "13:50")


class ParseConstraintFromTextTests(SimpleTestCase):
    databases = set()

    @override_settings(OPENAI_API_KEY="")
    def test_returns_empty_dict_without_api_key(self):
        self.assertEqual(ro.parse_constraint_from_text("entregar entre 11 y 2"), {})

    @override_settings(OPENAI_API_KEY="test-key")
    def test_returns_empty_dict_for_blank_note(self):
        self.assertEqual(ro.parse_constraint_from_text("   "), {})

    @override_settings(OPENAI_API_KEY="test-key")
    @patch("openai.OpenAI")
    def test_parses_structured_response(self, mock_openai_cls):
        payload = {"ventana_inicio": "11:00", "ventana_fin": "14:00", "prioridad": "ALTA"}
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content=json.dumps(payload)))
        ]
        mock_openai_cls.return_value = mock_client

        result = ro.parse_constraint_from_text("debe entregarse entre 11 y 2, es prioritario")

        self.assertEqual(result["time_window_start"], datetime.time(11, 0))
        self.assertEqual(result["time_window_end"], datetime.time(14, 0))
        self.assertEqual(result["priority"], "ALTA")

    @override_settings(OPENAI_API_KEY="test-key")
    @patch("openai.OpenAI")
    def test_fails_open_when_openai_raises(self, mock_openai_cls):
        mock_openai_cls.side_effect = RuntimeError("boom")

        result = ro.parse_constraint_from_text("nota cualquiera")

        self.assertEqual(result, {})


class FetchRouteMatrixTests(SimpleTestCase):
    databases = set()

    def setUp(self):
        # fetch_route_matrix cachea por (origen, direcciones); sin esto, un test
        # deja en caché la matriz que el siguiente test cree que está mockeando.
        cache.clear()

    @override_settings(GOOGLE_MAPS_API_KEY="")
    def test_raises_without_api_key(self):
        with self.assertRaises(ro.RouteEngineError):
            ro.fetch_route_matrix(ro.Origin(address="Bodega"), _addr_stops(["Calle A"]), ["Calle A"])

    @override_settings(GOOGLE_MAPS_API_KEY="test-key")
    @patch("api.route_optimization.requests.post")
    def test_surfaces_google_error_message(self, mock_post):
        error_body = [
            {"error": {"code": 403, "message": "Routes API has not been used in project 123"}}
        ]
        mock_post.return_value = MagicMock(status_code=403, json=lambda: error_body, text="403")

        with self.assertRaises(ro.RouteEngineError) as ctx:
            ro.fetch_route_matrix(ro.Origin(address="Bodega"), _addr_stops(["Calle A"]), ["Calle A"])

        self.assertIn("Routes API has not been used", str(ctx.exception))

    @override_settings(GOOGLE_MAPS_API_KEY="test-key")
    @patch("api.route_optimization.requests.post")
    def test_builds_matrix_from_response(self, mock_post):
        # 2 puntos (origen + 1 parada) => 4 celdas; el índice 0 se omite en JSON (proto3).
        rows = [
            {"destinationIndex": 0, "duration": "0s", "distanceMeters": 0, "condition": "ROUTE_EXISTS"},
            {"destinationIndex": 1, "duration": "600s", "distanceMeters": 5000, "condition": "ROUTE_EXISTS"},
            {"originIndex": 1, "destinationIndex": 0, "duration": "600s", "distanceMeters": 5000, "condition": "ROUTE_EXISTS"},
            {"originIndex": 1, "destinationIndex": 1, "duration": "0s", "distanceMeters": 0, "condition": "ROUTE_EXISTS"},
        ]
        mock_post.return_value = MagicMock(status_code=200, json=lambda: rows)

        matrix = ro.fetch_route_matrix(ro.Origin(address="Bodega"), _addr_stops(["Calle A"]), ["Calle A"])

        self.assertEqual(matrix[0][1]["durationMinutes"], 10)
        self.assertEqual(matrix[0][1]["distanceKm"], 5)
        self.assertEqual(matrix[1][0]["durationMinutes"], 10)

    @override_settings(GOOGLE_MAPS_API_KEY="test-key")
    @patch("api.route_optimization.requests.post")
    def test_second_call_with_same_stops_reuses_cache(self, mock_post):
        rows = [
            {"destinationIndex": 0, "duration": "0s", "distanceMeters": 0, "condition": "ROUTE_EXISTS"},
            {"destinationIndex": 1, "duration": "600s", "distanceMeters": 5000, "condition": "ROUTE_EXISTS"},
            {"originIndex": 1, "destinationIndex": 0, "duration": "600s", "distanceMeters": 5000, "condition": "ROUTE_EXISTS"},
            {"originIndex": 1, "destinationIndex": 1, "duration": "0s", "distanceMeters": 0, "condition": "ROUTE_EXISTS"},
        ]
        mock_post.return_value = MagicMock(status_code=200, json=lambda: rows)

        first = ro.fetch_route_matrix(ro.Origin(address="Bodega"), _addr_stops(["Calle A"]), ["Calle A"])
        second = ro.fetch_route_matrix(ro.Origin(address="Bodega"), _addr_stops(["Calle A"]), ["Calle A"])

        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(first, second)

    @override_settings(GOOGLE_MAPS_API_KEY="test-key")
    @patch("api.route_optimization.requests.post")
    def test_raises_when_matrix_incomplete(self, mock_post):
        rows = [
            {"destinationIndex": 1, "duration": "600s", "distanceMeters": 5000, "condition": "ROUTE_EXISTS"},
        ]
        mock_post.return_value = MagicMock(status_code=200, json=lambda: rows)

        with self.assertRaises(ro.RouteEngineError):
            ro.fetch_route_matrix(ro.Origin(address="Bodega"), _addr_stops(["Calle A"]), ["Calle A"])

    @override_settings(GOOGLE_MAPS_API_KEY="test-key")
    @patch("api.route_optimization.requests.post")
    def test_names_the_specific_unroutable_address(self, mock_post):
        # 3 puntos: bodega(0), A(1) con ruta normal, B(2) sin conexión con nada.
        rows = [
            {"destinationIndex": 0, "duration": "0s", "distanceMeters": 0, "condition": "ROUTE_EXISTS"},
            {"destinationIndex": 1, "duration": "600s", "distanceMeters": 5000, "condition": "ROUTE_EXISTS"},
            {"destinationIndex": 2, "duration": "0s", "distanceMeters": 0, "condition": "ROUTE_NOT_FOUND"},
            {"originIndex": 1, "destinationIndex": 0, "duration": "600s", "distanceMeters": 5000, "condition": "ROUTE_EXISTS"},
            {"originIndex": 1, "destinationIndex": 1, "duration": "0s", "distanceMeters": 0, "condition": "ROUTE_EXISTS"},
            {"originIndex": 1, "destinationIndex": 2, "duration": "0s", "distanceMeters": 0, "condition": "ROUTE_NOT_FOUND"},
            {"originIndex": 2, "destinationIndex": 0, "duration": "0s", "distanceMeters": 0, "condition": "ROUTE_NOT_FOUND"},
            {"originIndex": 2, "destinationIndex": 1, "duration": "0s", "distanceMeters": 0, "condition": "ROUTE_NOT_FOUND"},
            {"originIndex": 2, "destinationIndex": 2, "duration": "0s", "distanceMeters": 0, "condition": "ROUTE_EXISTS"},
        ]
        mock_post.return_value = MagicMock(status_code=200, json=lambda: rows)

        with self.assertRaises(ro.UnroutableStopsError) as ctx:
            ro.fetch_route_matrix(ro.Origin(address="Bodega"), _addr_stops(["Calle A", "Calle B rota"]), ["Calle A", "Calle B rota"])

        self.assertEqual(ctx.exception.addresses, ["Calle B rota"])

    @override_settings(GOOGLE_MAPS_API_KEY="test-key")
    @patch("api.route_optimization.requests.post")
    def test_rejects_stop_geocoded_implausibly_far(self, mock_post):
        # Google sí encuentra ruta hacia la parada ambigua, pero está a 150km:
        # un caso real (una colonia con el mismo nombre en otro municipio).
        rows = [
            {"destinationIndex": 0, "duration": "0s", "distanceMeters": 0, "condition": "ROUTE_EXISTS"},
            {"destinationIndex": 1, "duration": "36000s", "distanceMeters": 150_000, "condition": "ROUTE_EXISTS"},
            {"originIndex": 1, "destinationIndex": 0, "duration": "36000s", "distanceMeters": 150_000, "condition": "ROUTE_EXISTS"},
            {"originIndex": 1, "destinationIndex": 1, "duration": "0s", "distanceMeters": 0, "condition": "ROUTE_EXISTS"},
        ]
        mock_post.return_value = MagicMock(status_code=200, json=lambda: rows)

        with self.assertRaises(ro.UnroutableStopsError) as ctx:
            ro.fetch_route_matrix(ro.Origin(address="Bodega"), _addr_stops(["Barrio de San Juan, otro municipio"]), ["Barrio de San Juan, otro municipio"])

        self.assertEqual(ctx.exception.addresses, ["Barrio de San Juan, otro municipio"])

    @override_settings(GOOGLE_MAPS_API_KEY="test-key")
    @patch("api.route_optimization.requests.post")
    def test_does_not_reject_far_stop_when_coordinates_are_exact(self, mock_post):
        # Misma distancia de 150km que el test anterior, pero esta vez viene de
        # coordenadas exactas (link del cliente), no de geocodificar texto ambiguo:
        # puede ser una entrega legítimamente lejana, no se rechaza.
        rows = [
            {"destinationIndex": 0, "duration": "0s", "distanceMeters": 0, "condition": "ROUTE_EXISTS"},
            {"destinationIndex": 1, "duration": "36000s", "distanceMeters": 150_000, "condition": "ROUTE_EXISTS"},
            {"originIndex": 1, "destinationIndex": 0, "duration": "36000s", "distanceMeters": 150_000, "condition": "ROUTE_EXISTS"},
            {"originIndex": 1, "destinationIndex": 1, "duration": "0s", "distanceMeters": 0, "condition": "ROUTE_EXISTS"},
        ]
        mock_post.return_value = MagicMock(status_code=200, json=lambda: rows)

        matrix = ro.fetch_route_matrix(
            ro.Origin(address="Bodega"),
            [ro.Waypoint(latitude=19.24, longitude=-97.14)],
            ["1251-26 (link del cliente)"],
        )

        self.assertEqual(matrix[0][1]["distanceKm"], 150)


class ResolveMapsUrlCoordinatesTests(SimpleTestCase):
    databases = set()

    def setUp(self):
        cache.clear()

    @patch("api.route_optimization.requests.get")
    def test_extracts_precise_pin_over_viewport_center(self, mock_get):
        mock_get.return_value = MagicMock(
            url="https://www.google.com/maps/place/X/@19.05,-98.20,17z/data=!4m5!3m4!1s0x0:0x0!8m2!3d19.0577123!4d-98.2374456"
        )

        result = ro.resolve_maps_url_coordinates("https://maps.app.goo.gl/abc123")

        self.assertEqual(result, (19.0577123, -98.2374456))

    @patch("api.route_optimization.requests.get")
    def test_falls_back_to_viewport_center(self, mock_get):
        mock_get.return_value = MagicMock(url="https://www.google.com/maps/@19.0577,-98.2374,15z")

        result = ro.resolve_maps_url_coordinates("https://maps.app.goo.gl/abc123")

        self.assertEqual(result, (19.0577, -98.2374))

    @patch("api.route_optimization.requests.get")
    def test_returns_none_when_link_has_no_coordinates(self, mock_get):
        mock_get.return_value = MagicMock(url="https://www.google.com/maps/place/Some+Place")

        self.assertIsNone(ro.resolve_maps_url_coordinates("https://maps.app.goo.gl/abc123"))

    @patch("api.route_optimization.requests.get")
    def test_returns_none_and_does_not_raise_on_network_error(self, mock_get):
        import requests

        mock_get.side_effect = requests.RequestException("boom")

        self.assertIsNone(ro.resolve_maps_url_coordinates("https://maps.app.goo.gl/abc123"))

    def test_returns_none_for_blank_url(self):
        self.assertIsNone(ro.resolve_maps_url_coordinates(""))

    @patch("api.route_optimization.requests.get")
    def test_caches_resolution_across_calls(self, mock_get):
        mock_get.return_value = MagicMock(url="https://www.google.com/maps/@19.0577,-98.2374,15z")

        ro.resolve_maps_url_coordinates("https://maps.app.goo.gl/same-link")
        ro.resolve_maps_url_coordinates("https://maps.app.goo.gl/same-link")

        self.assertEqual(mock_get.call_count, 1)
