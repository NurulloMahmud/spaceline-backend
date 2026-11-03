from django.conf import settings
from rest_framework.permissions import BasePermission


class IsHubService(BasePermission):
    """The Driver Hub calling us with the service token it issued to this deployment."""

    def has_permission(self, request, view):
        token = settings.DRIVER_HUB_TOKEN
        return bool(token) and request.headers.get('X-Hub-Token') == token
