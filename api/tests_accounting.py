from datetime import date, datetime, timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APITestCase

from .models import Order, Quotation


class AccountingFreightTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client.force_authenticate(
            get_user_model().objects.create_user(username="accountant", is_staff=True)
        )
        self.today = patch("api.views.timezone.localdate", return_value=date(2026, 9, 25))
        self.today.start()
        self.addCleanup(self.today.stop)

    def create_order(self, event_date, freight, cancelled=False):
        quotation = Quotation.objects.create(
            client_name="Cliente de prueba",
            status=Quotation.Status.CONFIRMED,
            event_date=event_date,
            freight=freight,
            total_estimated="1000.00",
        )
        return Order.objects.create(
            quotation=quotation,
            is_cancelled=cancelled,
            confirmed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

    def summary(self, year=2026):
        response = self.client.get(reverse("accounting-overview"), {"year": year})
        self.assertEqual(response.status_code, 200)
        return response.data["summary"]

    def test_freight_accumulates_by_event_date_through_today(self):
        self.create_order(date(2026, 1, 1), "100.10")
        self.create_order(date(2026, 9, 1), "200.20")
        self.create_order(date(2026, 9, 25), "50.05")
        self.create_order(date(2026, 9, 26), "800.00")
        self.create_order(date(2026, 10, 1), "900.00")
        self.create_order(date(2025, 9, 1), "700.00")
        self.create_order(date(2026, 9, 10), "600.00", cancelled=True)
        Quotation.objects.create(client_name="Sin confirmar", freight="500", total_estimated="1000")

        summary = self.summary()

        self.assertEqual(summary["yearFreight"], 350.35)
        self.assertEqual(summary["monthFreight"], 250.25)

    def test_selected_past_year_includes_full_year_and_matching_month(self):
        self.create_order(date(2025, 9, 30), "120.50")
        self.create_order(date(2025, 12, 31), "80.25")
        self.create_order(date(2026, 9, 1), "999.00")

        summary = self.summary(2025)

        self.assertEqual(summary["yearFreight"], 200.75)
        self.assertEqual(summary["monthFreight"], 120.50)

    def test_empty_period_returns_zero(self):
        summary = self.summary()
        self.assertEqual(summary["yearFreight"], 0)
        self.assertEqual(summary["monthFreight"], 0)

    def test_missing_event_date_uses_confirmation_date(self):
        self.create_order(None, "42.50")
        summary = self.summary()
        self.assertEqual(summary["yearFreight"], 42.50)
        self.assertEqual(summary["monthFreight"], 42.50)
