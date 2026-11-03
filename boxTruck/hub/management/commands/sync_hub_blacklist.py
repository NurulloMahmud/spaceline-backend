from django.core.management.base import BaseCommand

from hub.blacklist import push_blacklist
from hub.services import driver_hub


class Command(BaseCommand):
    help = 'Replace the Driver Hub copy of the broker blacklist with ours. Run on deploy.'

    def handle(self, *args, **options):
        if not driver_hub.configured():
            self.stdout.write('Driver hub is not configured (DRIVER_HUB_URL, DRIVER_HUB_TOKEN); nothing sent.')
            return
        rows = push_blacklist()
        if rows is None:
            self.stderr.write('The hub did not accept the blacklist; see the log.')
            return
        self.stdout.write(f'Sent {len(rows)} blacklisted broker(s) to the hub.')
