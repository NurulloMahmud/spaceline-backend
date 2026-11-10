from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand

from hub.profiles import linked_driver_ids, send
from hub.services import driver_hub


class Command(BaseCommand):
    help = (
        "Push the carrier's record (name, phone, license and medical card) of every "
        "driver active in the driver app to the Driver Hub. Run once on the 1.4 "
        "rollout; afterwards a manual tool to resend after a hub outage."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--driver", type=int, help="Only this Driver id (it must be linked)."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not driver_hub.configured():
            self.stdout.write(
                "Driver hub is not configured (DRIVER_HUB_URL, DRIVER_HUB_TOKEN); nothing sent."
            )
            return
        ids = linked_driver_ids()
        if options.get("driver"):
            ids = [i for i in ids if i == options["driver"]]
        counts = send(ids)
        self.stdout.write(
            f"Sent {counts['sent']} driver profile(s) to the hub"
            f" ({counts['skipped']} skipped)."
        )
        if counts["failed"]:
            self.stderr.write(f"{counts['failed']} profile(s) not sent; see the log.")
