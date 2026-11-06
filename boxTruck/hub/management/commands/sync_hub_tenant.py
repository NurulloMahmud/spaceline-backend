from typing import Any

from django.core.management.base import BaseCommand

from hub.services import driver_hub
from hub.tenant import push_tenant_name


class Command(BaseCommand):
    help = "Send this carrier's company name to the Driver Hub, for the driver app. Run on deploy."

    def handle(self, *args: Any, **options: Any) -> None:
        if not driver_hub.configured():
            self.stdout.write(
                "Driver hub is not configured (DRIVER_HUB_URL, DRIVER_HUB_TOKEN); nothing sent."
            )
            return
        name = push_tenant_name()
        if name is None:
            self.stderr.write("The hub did not take the company name; see the log.")
            return
        self.stdout.write(f"The driver app shows this carrier as {name!r}.")
