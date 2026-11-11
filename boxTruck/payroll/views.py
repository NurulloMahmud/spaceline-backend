from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from users.permissions import (
    IsAdminUser,
    IsPayroll,
    IsBilling,
)
from .models import (
    DeductionType,
    StatementStatus,
)
from .serializers import (
    StatementStatusSerializer,
    DeductionTypeSerializer,
)


class StatementStatusViewSet(viewsets.ModelViewSet):
    queryset = StatementStatus.objects.all().order_by("id")
    serializer_class = StatementStatusSerializer

    def get_permissions(self):
        if self.request.method == "GET":
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser]
        return [permission() for permission in permission_classes]


class DeductionTypeViewSet(viewsets.ModelViewSet):
    queryset = DeductionType.objects.all().order_by("name")
    serializer_class = DeductionTypeSerializer

    def get_permissions(self):
        if self.request.method == "GET":
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser | IsBilling | IsPayroll]
        return [permission() for permission in permission_classes]


