from django.core.management.base import BaseCommand
from django.db import transaction

from users.models import CustomUser, Department, Page, UserPageAccess


PAGES = [
    ('dashboard', 'Dashboard'),
    ('companies', 'Companies'),
    ('users', 'Users'),
    ('drivers', 'Drivers'),
    ('statuses', 'Statuses'),
    ('tags', 'Tags'),
    ('billing-invoices', 'Billing / Invoices'),
    ('loads', 'Loads'),
    ('tracking', 'Tracking'),
    ('load-board', 'Load board'),
    ('bid-board', 'Bid board'),
    ('negotiations', 'Negotiations'),
    ('onboarding', 'Onboarding'),
    ('analytics', 'Analytics'),
    ('dispatcher-salaries', 'Dispatcher salaries'),
    ('payroll', 'Payroll'),
    ('expenses', 'Expenses'),
    ('bm-nexus', 'BM-NEXUS'),
    ('settings', 'Settings'),
]

ALL_KEYS = {key for key, _ in PAGES}

PAYROLL_BILLING_EXCLUDED = {
    'dashboard', 'companies', 'users', 'statuses', 'tags', 'dispatcher-salaries', 'settings',
}

# department name (lowercase) -> set of page keys the department's users get full CRUD on
DEPARTMENT_PAGE_KEYS = {
    'management': ALL_KEYS,
    'payroll': ALL_KEYS - PAYROLL_BILLING_EXCLUDED,
    'billing': ALL_KEYS - PAYROLL_BILLING_EXCLUDED,
    'dispatch manager': {
        'users', 'drivers', 'tags', 'billing-invoices', 'loads',
        'analytics', 'dispatcher-salaries', 'payroll', 'settings',
    },
    'dispatch': {'drivers', 'billing-invoices', 'loads', 'analytics'},
    'updater': {'drivers', 'billing-invoices', 'loads', 'analytics'},
}


class Command(BaseCommand):
    help = "Seeds the Page registry and grants UserPageAccess (full CRUD) per department defaults."

    @transaction.atomic
    def handle(self, *args, **options):
        pages_by_key = {}
        for key, name in PAGES:
            page, created = Page.objects.get_or_create(key=key, defaults={'name': name})
            if not created and page.name != name:
                page.name = name
                page.save(update_fields=['name'])
            pages_by_key[key] = page
            self.stdout.write(f"{'Created' if created else 'Exists'} page: {name}")

        for department_name, page_keys in DEPARTMENT_PAGE_KEYS.items():
            department = Department.objects.filter(name__iexact=department_name).first()
            if not department:
                self.stdout.write(self.style.WARNING(f"Department '{department_name}' not found, skipping."))
                continue

            users = CustomUser.objects.filter(department=department)
            if not users.exists():
                self.stdout.write(self.style.WARNING(f"No users in department '{department_name}'."))
                continue

            for user in users:
                for key in page_keys:
                    UserPageAccess.objects.update_or_create(
                        user=user,
                        page=pages_by_key[key],
                        defaults={
                            'can_view': True,
                            'can_create': True,
                            'can_edit': True,
                            'can_delete': True,
                        },
                    )
                self.stdout.write(f"Set {len(page_keys)} pages for {user.username} ({department_name})")

        self.stdout.write(self.style.SUCCESS("Page access seeding complete."))
