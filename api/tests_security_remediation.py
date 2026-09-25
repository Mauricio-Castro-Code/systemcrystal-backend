from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from .models import Order, UserProfile, set_user_role
from .tests import build_quotation_payload


class SecurityRemediationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = get_user_model().objects.create_user(username='owner', password='Roble!9274Nube', is_staff=True)
        self.driver = get_user_model().objects.create_user(username='driver')
        self.other = get_user_model().objects.create_user(username='other')
        for user in [self.driver, self.other]:
            set_user_role(user, UserProfile.Role.CHOFER)

    def create_order(self, driver=None):
        self.client.force_authenticate(self.admin)
        response = self.client.post(reverse('order-list'), build_quotation_payload(), format='json')
        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(order_id=response.data['orderId'])
        order.assigned_driver = driver
        order.save(update_fields=['assigned_driver'])
        return order

    def test_driver_cannot_take_another_drivers_order(self):
        order = self.create_order(self.other)
        self.client.force_authenticate(self.driver)
        response = self.client.post(reverse('order-my-route-add-order'), {'orderId': order.order_id})
        self.assertEqual(response.status_code, 403)
        order.refresh_from_db()
        self.assertEqual(order.assigned_driver_id, self.other.pk)

    def test_driver_can_take_unassigned_order(self):
        order = self.create_order()
        self.client.force_authenticate(self.driver)
        response = self.client.post(reverse('order-my-route-add-order'), {'orderId': order.order_id})
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.assigned_driver_id, self.driver.pk)

    def test_office_can_reassign_order(self):
        order = self.create_order(self.other)
        office = get_user_model().objects.create_user(username='office')
        set_user_role(office, UserProfile.Role.VENTAS)
        self.client.force_authenticate(office)
        response = self.client.post(reverse('order-assign', kwargs={'order_id': order.order_id}), {'driverId': self.driver.pk})
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.assigned_driver_id, self.driver.pk)

    @override_settings(AUTH_TOKEN_TTL_SECONDS=3600)
    def test_expired_token_is_rejected(self):
        token = Token.objects.create(user=self.admin)
        Token.objects.filter(pk=token.pk).update(created=timezone.now()-timedelta(hours=2))
        self.client.credentials(HTTP_AUTHORIZATION='Token '+token.key)
        self.assertEqual(self.client.get(reverse('api-me')).status_code, 401)

    @override_settings(AUTH_TOKEN_TTL_SECONDS=3600)
    def test_login_replaces_expired_token(self):
        token = Token.objects.create(user=self.admin)
        Token.objects.filter(pk=token.pk).update(created=timezone.now()-timedelta(hours=2))
        response = self.client.post(reverse('api-login'), {'identifier': 'owner', 'password': 'Roble!9274Nube'})
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.data['token'], token.key)
        self.client.credentials(HTTP_AUTHORIZATION='Token '+response.data['token'])
        self.assertEqual(self.client.get(reverse('api-me')).status_code, 200)

    @override_settings(SECURITY_RATE_LIMITS={'route_optimize': [(2, 60)]})
    def test_optimization_limit_is_shared_after_cache_reset_and_scoped_to_user(self):
        self.client.force_authenticate(self.driver)
        url = reverse('order-my-route-optimize')
        self.assertEqual(self.client.post(url, {}).status_code, 400)
        cache.clear()
        self.assertEqual(self.client.post(url, {}).status_code, 400)
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.post(url, {}).status_code, 400)
