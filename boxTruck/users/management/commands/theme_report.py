from collections import Counter
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from users.models import CustomUser, Theme


class Command(BaseCommand):
    """Who uses which theme, as the app last reported it (CustomUser.theme).

    Active users only. A user the app has not reported for yet shows as "not reported": they have
    not opened a signed-in page since the reporting shipped."""

    help = 'List the theme each active user has on screen, with a count per theme.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--counts', action='store_true', help='Print the counts only.')

    def handle(self, *args: Any, **options: Any) -> None:
        names = dict(Theme.choices)
        users = list(
            CustomUser.objects.filter(is_active=True)
            .select_related('department')
            .order_by('theme', 'username')
        )
        counts = Counter(names.get(u.theme, 'not reported') for u in users)

        if not options['counts']:
            now = timezone.now()
            for u in users:
                seen = u.theme_seen_at
                ago = f'{(now - seen).total_seconds() / 3600:.1f} h ago' if seen else '-'
                dept = u.department.name if u.department else '-'
                name = f'{u.first_name} {u.last_name}'.strip() or '-'
                theme = names.get(u.theme, 'not reported')
                self.stdout.write(f'{theme:14} {u.username:22} {name:28} {dept:14} {ago}')
            self.stdout.write('')

        for label in [*names.values(), 'not reported']:
            self.stdout.write(f'{label:14} {counts.get(label, 0)}')
        self.stdout.write(f'{"active users":14} {len(users)}')
