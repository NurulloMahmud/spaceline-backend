from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import IsDeveloper

from . import logs


def _int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


class ErrorLogsView(APIView):
    """Errors from every service's log file, grouped, newest first. Developers only."""

    permission_classes = [IsDeveloper]

    def get(self, request):
        params = request.query_params
        service = (params.get('service') or '').strip()
        if service and service not in logs.SERVICE_KEYS:
            return Response({'detail': 'Unknown service.'}, status=400)
        data = logs.collect(
            service=service or None,
            q=(params.get('q') or '').strip(),
            hours=_int(params.get('hours'), 24, 1, 24 * 30),
            limit=_int(params.get('limit'), 200, 1, 1000),
        )
        return Response(data)
