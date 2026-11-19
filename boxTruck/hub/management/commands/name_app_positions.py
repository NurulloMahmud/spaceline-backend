from typing import Any

from django.core.management.base import BaseCommand
from django.db.models import Q

from hiring.models import Driver
from hiring.places import place_for_position
from hub.sync import APP_DEVICE
from mobile.models import DriverLocation

PLACE_FIELDS = ("current_zip", "current_city", "current_state", "current_address")


class Command(BaseCommand):
    help = (
        "Name driver-app positions that have no ZIP yet as City, ST ZIP. "
        "Run on deploy, so a position Google could not name is tried again."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        named = left = 0
        drivers = Driver.objects.filter(
            Q(current_zip__isnull=True) | Q(current_zip=""),
            hub_driver_id__isnull=False,
            current_latitude__isnull=False,
            current_longitude__isnull=False,
        )
        for driver in drivers.only(
            "id", "current_latitude", "current_longitude", *PLACE_FIELDS
        ):
            if not _from_the_app(driver):
                continue
            place = place_for_position(
                driver.current_latitude, driver.current_longitude
            )
            if not place["current_address"]:
                left += 1
                continue
            # A plain update: no signal pushes this back to the hub, and the
            # time the position was taken stays as it was.
            Driver.objects.filter(pk=driver.pk).update(**place)
            named += 1
            self.stdout.write(f"Driver {driver.pk}: {place['current_address']}")
        self.stdout.write(f"Named {named} position(s); {left} could not be named.")


def _from_the_app(driver: Driver) -> bool:
    """True when the driver's coordinates are the last ones the app sent, not a typed ZIP's."""
    last = (
        DriverLocation.objects.filter(driver=driver, device=APP_DEVICE)
        .order_by("-created_at", "-id")
        .first()
    )
    return (
        last is not None
        and abs(last.latitude - driver.current_latitude) <= 1e-6
        and abs(last.longitude - driver.current_longitude) <= 1e-6
    )
