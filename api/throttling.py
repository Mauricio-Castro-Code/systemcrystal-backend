import hashlib
import math
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.throttling import BaseThrottle

from .models import SecurityRateBucket


class DatabaseRateThrottle(BaseThrottle):
    """Fixed-window quotas shared across workers, with atomic increments."""

    def allow_request(self, request, view):
        scope = getattr(view, 'throttle_scope', None)
        limits = settings.SECURITY_RATE_LIMITS.get(scope, [])
        identity = f'user:{request.user.pk}' if request.user.is_authenticated else f'ip:{self.get_ident(request)}'
        now = timezone.now()
        self.retry_after = 0
        for limit, seconds in limits:
            window = int(now.timestamp()) // seconds
            key = hashlib.sha256(f'{scope}:{identity}:{seconds}:{window}'.encode()).hexdigest()
            expires = datetime.fromtimestamp((window + 1) * seconds, tz=datetime_timezone.utc)
            with transaction.atomic():
                bucket, created = SecurityRateBucket.objects.select_for_update().get_or_create(
                    key=key, defaults={'expires_at': expires},
                )
                if bucket.count >= limit:
                    self.retry_after = max(1, math.ceil((expires - now).total_seconds()))
                    return False
                bucket.count += 1
                bucket.save(update_fields=['count'])
            if created:
                SecurityRateBucket.objects.filter(expires_at__lt=now - timedelta(days=1)).delete()
        return True

    def wait(self):
        return self.retry_after
