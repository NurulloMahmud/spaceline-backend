import hmac

from django.conf import settings
from rest_framework.permissions import BasePermission


def secret_matches(given: str | None, expected: str | None) -> bool:
    """Constant-time compare; an empty configured secret never matches."""
    if not expected or not given:
        return False
    return hmac.compare_digest(str(given).encode(), str(expected).encode())


class IsHubService(BasePermission):
    """The Driver Hub calling us with the service token it issued to this deployment."""

    def has_permission(self, request, view):
        return secret_matches(request.headers.get('X-Hub-Token'), settings.DRIVER_HUB_TOKEN)
