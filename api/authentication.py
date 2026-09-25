from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import AuthenticationFailed


def token_expired(token):
    return timezone.now() >= token.created + timedelta(seconds=settings.AUTH_TOKEN_TTL_SECONDS)


class ExpiringTokenAuthentication(TokenAuthentication):
    def authenticate_credentials(self, key):
        user, token = super().authenticate_credentials(key)
        if token_expired(token):
            Token.objects.filter(key=token.key).delete()
            raise AuthenticationFailed('Tu sesión venció. Vuelve a iniciar sesión.')
        return user, token


@transaction.atomic
def get_login_token(user):
    # Serializes simultaneous logins, including the first token creation.
    type(user).objects.select_for_update().get(pk=user.pk)
    token, _ = Token.objects.get_or_create(user=user)
    if token_expired(token):
        token.delete()
        token = Token.objects.create(user=user)
    return token
