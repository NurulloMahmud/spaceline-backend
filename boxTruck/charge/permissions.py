from rest_framework.permissions import BasePermission

# departments allowed to change the company's payment method / see billing detail
_BILLING_DEPARTMENTS = {"management", "billing"}


def _department_name(user):
    dept = getattr(user, "department", None)
    return (dept.name.lower() if dept and dept.name else "")


class CanManageBilling(BasePermission):
    """Authenticated, belongs to a company, and is in management or billing."""

    message = "Only management or billing staff can manage the company's payment method."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and getattr(user, "company_id", None)
            and _department_name(user) in _BILLING_DEPARTMENTS
        )
