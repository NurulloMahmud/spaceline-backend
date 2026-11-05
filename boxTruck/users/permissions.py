from rest_framework.permissions import BasePermission
from config import settings


# Developers can do everything Management can, plus the things only they may
# touch (other developers' accounts, the posted-loads listener mailbox).
MANAGEMENT_DEPARTMENTS = ('management', 'developer')
DEVELOPER = 'developer'


def department_name(user):
    dept = getattr(user, 'department', None)
    return (dept.name if dept and dept.name else '').strip().lower()


def is_management(user):
    return bool(user and user.is_authenticated and department_name(user) in MANAGEMENT_DEPARTMENTS)


def is_developer(user):
    return bool(user and user.is_authenticated and department_name(user) == DEVELOPER)


class IsAdminUser(BasePermission):
    """Management or Developer."""
    def has_permission(self, request, view):
        return is_management(request.user)


class IsDeveloper(BasePermission):
    def has_permission(self, request, view):
        return is_developer(request.user)


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