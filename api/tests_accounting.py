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


class ExpenseCategoryTests(AccountingFreightTests):
    def test_categories_are_saved_and_summed_by_event_year(self):
        from .models import OrderExtraCost
        order = self.create_order(date(2026, 9, 1), '0')
        url = reverse('order-costs', args=[order.order_id])
        for category, amount in [('CARPAS', 100), ('MESEROS', 200), ('SUBARRENDOS', 50), ('OTROS', 25)]:
            response = self.client.post(url, {'categoria': category, 'concepto': 'Proveedor', 'monto': amount})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.data['extraCosts'][-1]['categoria'], category)
        OrderExtraCost.objects.create(order=order, concepto='Histórico', monto=10)
        other_year = self.create_order(date(2025, 9, 1), '0')
        OrderExtraCost.objects.create(order=other_year, categoria='CARPAS', concepto='Anterior', monto=999)
        cancelled = self.create_order(date(2026, 9, 1), '0', cancelled=True)
        OrderExtraCost.objects.create(order=cancelled, categoria='CARPAS', concepto='Cancelado', monto=999)
        response = self.client.get(reverse('accounting-overview'), {'year': 2026})
        totals = {row['category']: row['total'] for row in response.data['expensesByCategory']}
        self.assertEqual(totals, {'CARPAS': 100, 'MESEROS': 200, 'SUBARRENDOS': 50, 'OTROS': 25, 'UNCLASSIFIED': 10})
        self.assertEqual(response.data['summary']['yearExtraCosts'], 385)

    def test_deposit_and_discount_are_excluded_and_retained_deposit_counted_once(self):
        order = self.create_order(date(2026, 9, 1), '100')
        order.quotation.security_deposit = 200
        order.quotation.discount = 50
        order.quotation.save()
        self.assertEqual(self.summary()['yearRevenue'], 750)
        url = reverse('order-costs', args=[order.order_id])
        response = self.client.patch(url, {'retainedDeposit': 75}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['accountingRevenue'], 825)
        response = self.client.get(reverse('accounting-overview'), {'year': 2026})
        self.assertEqual(response.data['summary']['yearRevenue'], 825)
        self.assertEqual(response.data['summary']['yearUtility'], 825)
        self.assertEqual(response.data['monthlySales'][8]['value'], 825)
        self.assertEqual(response.data['topClients'][0]['totalSales'], 825)
        self.assertEqual(response.data['summary']['yearExtraCosts'], 0)
        self.client.patch(url, {'retainedDeposit': 0}, format='json')
        self.assertEqual(self.summary()['yearRevenue'], 750)

    def test_rejects_invalid_category_and_retention_outside_deposit(self):
        order = self.create_order(date(2026, 9, 1), '0')
        url = reverse('order-costs', args=[order.order_id])
        for category in ['DESCUENTOS', 'DEVOLUCION_DEPOSITO', 'invalido']:
            self.assertEqual(self.client.post(url, {'categoria': category, 'concepto': category, 'monto': 10}).status_code, 400)
        for amount in [-1, 1, 'NaN']:
            self.assertEqual(self.client.patch(url, {'retainedDeposit': amount}, format='json').status_code, 400)

    def test_cost_creation_and_deletion_refresh_accounting(self):
        order = self.create_order(date(2026, 9, 1), '0')
        self.assertEqual(self.summary()['yearExtraCosts'], 0)
        response = self.client.post(reverse('order-costs', args=[order.order_id]),
                                    {'categoria': 'CARPAS', 'concepto': 'Carpa', 'monto': 100})
        self.assertEqual(self.summary()['yearExtraCosts'], 100)
        cost_id = response.data['extraCosts'][0]['id']
        self.client.delete(reverse('order-cost-delete', args=[order.order_id, cost_id]))
        self.assertEqual(self.summary()['yearExtraCosts'], 0)

    def test_reclassifying_historical_cost_keeps_description_and_amount(self):
        from .models import OrderExtraCost
        order = self.create_order(date(2026, 9, 1), '0')
        cost = OrderExtraCost.objects.create(order=order, concepto='Carpa proveedor anterior', monto=100)
        self.summary()
        response = self.client.patch(reverse('order-cost-delete', args=[order.order_id, cost.pk]),
                                     {'categoria': 'CARPAS'}, format='json')
        self.assertEqual(response.status_code, 200)
        cost.refresh_from_db()
        self.assertEqual(cost.categoria, 'CARPAS')
        self.assertEqual(cost.concepto, 'Carpa proveedor anterior')
        self.assertEqual(cost.monto, 100)
        response = self.client.get(reverse('accounting-overview'), {'year': 2026})
        self.assertEqual(next(row['total'] for row in response.data['expensesByCategory'] if row['category'] == 'CARPAS'), 100)

    def test_editing_note_cannot_reduce_deposit_below_retained_amount(self):
        from decimal import Decimal
        from rest_framework.exceptions import ValidationError
        from .services import update_order_from_note
        order = self.create_order(date(2026, 9, 1), '0')
        order.retained_deposit = Decimal('50')
        with self.assertRaises(ValidationError):
            update_order_from_note(order, {"logistics": {"securityDeposit": Decimal('25')}})
