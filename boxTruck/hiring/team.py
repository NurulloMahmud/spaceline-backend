"""Two drivers on one truck (SPEC-1.4 section 2.5).

A team is `Vehicle.second_driver` on the first driver's vehicle (the first by
id). A driver drives as second driver on one truck at a time: picking one who
is already on another truck moves them, and that other team is cleared.
"""

from typing import Any

from django.db import transaction

from users.models import CustomUser

from .models import Driver, Vehicle, VehicleHistory


class NoVehicle(Exception):
    """The driver has no vehicle to put a second driver on."""


def _person(driver: Driver | None) -> dict[str, Any] | None:
    return {"id": driver.id, "full_name": driver.full_name} if driver else None


def team_vehicle(driver: Driver) -> Vehicle | None:
    return driver.vehicles.order_by("id").first()


def _set_second(
    vehicle: Vehicle, second: Driver | None, user: CustomUser | None, why: str
) -> None:
    before = vehicle.second_driver
    vehicle.second_driver = second
    vehicle.save(update_fields=["second_driver", "updated_at"])
    if user is not None and getattr(user, "is_authenticated", False):
        VehicleHistory.objects.create(
            vehicle=vehicle,
            changed_by=user,
            description=(
                f"Second Driver: {before.full_name if before else None} -> "
                f"{second.full_name if second else None} ({why})"
            ),
        )


@transaction.atomic
def set_team(
    driver: Driver, second: Driver | None, user: CustomUser | None
) -> dict[str, Any]:
    """Put `second` on `driver`'s truck, or clear it (None). Raises NoVehicle.

    Answers {driver_id, vehicle_id, second_driver, moved_from, replaced}:
    moved_from is the owner of the truck `second` was taken off, replaced the
    second driver this truck had before (also on a clear).
    """
    first = team_vehicle(driver)
    if first is None:
        raise NoVehicle()
    vehicle = (
        Vehicle.objects.select_for_update()
        .select_related("second_driver")
        .get(pk=first.pk)
    )
    moved_from: Driver | None = None
    replaced: Driver | None = None
    if second is None or vehicle.second_driver_id != second.id:
        if second is not None:
            others = (
                Vehicle.objects.select_for_update()
                .select_related("driver", "second_driver")
                .filter(second_driver=second)
                .exclude(pk=vehicle.pk)
                .order_by("id")
            )
            for other in others:
                if moved_from is None:
                    moved_from = other.driver
                _set_second(other, None, user, f"moved to {driver.full_name}'s truck")
        replaced = vehicle.second_driver
        if replaced is not None or second is not None:
            _set_second(vehicle, second, user, "team")
    return {
        "driver_id": driver.id,
        "vehicle_id": vehicle.id,
        "second_driver": _person(second),
        "moved_from": _person(moved_from),
        "replaced": _person(replaced),
    }
