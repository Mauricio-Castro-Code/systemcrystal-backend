from rest_framework.permissions import BasePermission

from .models import UserProfile, get_user_role


class IsChofer(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated
            and get_user_role(request.user) == UserProfile.Role.CHOFER
        )


class IsAdminOrVentas(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated
            and get_user_role(request.user) in (UserProfile.Role.ADMIN, UserProfile.Role.VENTAS)
        )
