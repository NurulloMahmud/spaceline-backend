from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from .models import DriverAuthToken


class DriverTokenAuthentication(BaseAuthentication):
    def authenticate(self, request):
        token = request.headers.get('Driver-Authorization')
        if not token:
            return None

        try:
            auth_token = DriverAuthToken.objects.select_related('driver').get(token=token)
        except DriverAuthToken.DoesNotExist:
            raise AuthenticationFailed('Invalid token.')
        if not auth_token.is_valid():
            raise AuthenticationFailed('Token expired.')
        return (auth_token.driver, auth_token)
