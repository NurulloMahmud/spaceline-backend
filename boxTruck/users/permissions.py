from rest_framework.permissions import BasePermission
from config import settings


class IsAdminUser(BasePermission):
    def has_permission(self, request, view):
        if request.user.is_authenticated and request.user.department.name.lower() == 'management':
            return True
        return False


class IsDispatch(BasePermission):
    def has_permission(self, request, view):
        if request.user.is_authenticated and request.user.department.name.lower() in ['dispatch', 'updater']:
            return True
        return False


class IsBilling(BasePermission):
    def has_permission(self, request, view):
        if request.user.is_authenticated and request.user.department.name.lower() == 'billing':
            return True
        return False


class IsDispatchManager(BasePermission):
    def has_permission(self, request, view):
        if request.user.is_authenticated and request.user.department.name.lower() == 'dispatch manager':
            return True
        return False


class IsPayroll(BasePermission):
    def has_permission(self, request, view):
        if request.user.is_authenticated and request.user.department.name.lower() == 'payroll':
            return True
        return False


class IsUpdater(BasePermission):
    def has_permission(self, request, view):
        if request.user.is_authenticated and request.user.department.name.lower() == 'updater':
            return True
        return False


class IsInternalService(BasePermission):
    def has_permission(self, request, view):
        secret = request.headers.get('X-Internal-Secret')
        return secret == settings.INTERNAL_SERVICE_SECRET