from typing import Any

from django.core.management.base import BaseCommand

from hub.payouts import resend_all
from hub.services import driver_hub


class Command(BaseCommand):
    help = (
        "Resend every payout of the drivers in the driver app to the Driver Hub. "
        "Run on deploy, so a change made while the hub was unreachable is not lost."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        if not driver_hub.configured():
            self.stdout.write(
                "Driver hub is not configured (DRIVER_HUB_URL, DRIVER_HUB_TOKEN); nothing sent."
            )
            return
        counts = resend_all(stdout=self.stderr.write)
        self.stdout.write(f"Sent {counts['sent']} payout(s) to the hub.")
        if counts["failed"]:
            self.stderr.write(f"{counts['failed']} payout(s) not sent; see the log.")
