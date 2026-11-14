"""The hiring board: drivers from the invite link as four columns.

    Lead    Pending driver, hiring_stage null or "lead" (filled in the invite form)
    Called  Pending driver, hiring_stage "called" (the team talked to them)
    Ready   Pending driver, hiring_stage "ready" (cleared to start)
    Active  Active driver with no load yet; one moved to Active on the board stays
            for ACTIVE_GRACE even if a load is already on file, so the card does
            not vanish the moment it is dropped there.

These are the same drivers the Onboarding page lists: a move on the board sets
the driver's status too (Active for the Active column, Pending for the others).

Deleting a driver who has loads (payouts hang off loads), expenses or
statements would cascade into or orphan accounting data, so `remove_driver`
sets such a driver Out of service instead. Every delete path goes through it.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.db.models import Exists, OuterRef, Prefetch, Q, QuerySet
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from users.models import CustomUser

from .models import Driver, DriverFile, DriverHistory, DriverStatus, HiringMove, Vehicle

STAGES = ("lead", "called", "ready", "active")
LABELS = {"lead": "Lead", "called": "Called", "ready": "Ready", "active": "Active"}
ACTIVE_GRACE = timedelta(hours=1)
LICENSE_NAME = "driver license"


def _squash(name: Any) -> str:
    return re.sub(r"[\s\-_]+", "", str(name or "")).lower()


def status_named(name: str) -> DriverStatus | None:
    """The driver status with this name (spaces, dashes and case ignored)."""
    want = _squash(name)
    for status in DriverStatus.objects.order_by("id"):
        if _squash(status.name) == want:
            return status
    return None


def _has_load() -> Exists:
    from billing.models import Load

    return Exists(Load.objects.filter(driver=OuterRef("pk")))


def with_records(qs: QuerySet[Driver]) -> QuerySet[Driver]:
    """Annotates `has_load` (payouts hang off loads) and `has_expense` and
    `has_statement` (payroll rows that would cascade away with the driver)."""
    from payroll.models import Deduction, Statement

    return qs.annotate(
        has_load=_has_load(),
        has_expense=Exists(Deduction.objects.filter(driver=OuterRef("pk"))),
        has_statement=Exists(Statement.objects.filter(driver=OuterRef("pk"))),
    )


def _records(d: Any) -> bool:
    return bool(d.has_load or d.has_expense or d.has_statement)


def driver_has_records(driver: Driver) -> bool:
    row = with_records(Driver.objects.filter(pk=driver.pk)).first()
    return bool(row and _records(row))


def column_of(driver: Driver) -> str | None:
    key = _squash(driver.status.name if driver.status_id else "")
    if key == "active":
        return "active"
    if key == "pending":
        return driver.hiring_stage if driver.hiring_stage in ("lead", "called", "ready") else "lead"
    return None


def board_queryset(now: Any = None) -> QuerySet[Driver]:
    now = now or timezone.now()
    pending = status_named("Pending")
    active = status_named("Active")
    qs = with_records(Driver.objects.all())
    shown = Q(pk__in=[])
    if pending:
        shown |= Q(status=pending)
    if active:
        shown |= Q(status=active) & (
            Q(has_load=False) | Q(hiring_stage="active", hiring_moved_at__gte=now - ACTIVE_GRACE)
        )
    licenses = DriverFile.objects.filter(Q(name__iexact=LICENSE_NAME) | Q(hub_kind="license")).order_by("-created_at", "-id")
    return (
        qs.filter(shown)
        .select_related("status", "referral_by", "hiring_moved_by")
        .prefetch_related(
            Prefetch("driverfile_set", queryset=licenses, to_attr="licenses"),
            Prefetch("hiring_moves", queryset=HiringMove.objects.select_related("changed_by")),
            Prefetch("vehicles", queryset=Vehicle.objects.order_by("id")),
            "second_driver",
        )
    )


def _person(user: CustomUser | None) -> dict[str, Any] | None:
    if user is None:
        return None
    name = " ".join(p for p in (user.first_name, user.last_name) if p) or user.username
    return {"id": user.id, "name": name}


def _file_url(f: DriverFile) -> str | None:
    try:
        return f.document.url if f.document else None
    except ValueError:
        return None


def card(driver: Driver, request: Any = None) -> dict[str, Any]:
    """What the board shows for one driver (the dialog reads the same object)."""
    licenses = getattr(driver, "licenses", None)
    if licenses is None:
        licenses = list(
            driver.driverfile_set.filter(Q(name__iexact=LICENSE_NAME) | Q(hub_kind="license")).order_by("-created_at", "-id")
        )
    lic = licenses[0] if licenses else None
    url = _file_url(lic) if lic else None
    if url and url.startswith("/") and request is not None:
        url = request.build_absolute_uri(url)
    vehicle = next(iter(driver.vehicles.all()), None)
    team = bool(vehicle and vehicle.second_driver_id) or bool(list(driver.second_driver.all()))
    moves = [
        {"stage": m.stage, "by": _person(m.changed_by), "at": m.created_at}
        for m in driver.hiring_moves.all()
    ]
    has_records = _records(driver) if hasattr(driver, "has_load") else driver_has_records(driver)
    return {
        "id": driver.id,
        "full_name": driver.full_name,
        "phone_number": driver.phone_number,
        "email": driver.email,
        "city": driver.city,
        "state": driver.state,
        "cdl_number": driver.cdl_number,
        "cdl_state": driver.cdl_state,
        "cdl_class": driver.cdl_class,
        "cdl_expiration": driver.cdl_expiration,
        "medical_exp_date": driver.medical_exp_date,
        "created_at": driver.created_at,
        "status": driver.status.name if driver.status_id else None,
        "stage": column_of(driver),
        "invited_by": _person(driver.referral_by),
        "moved_by": _person(driver.hiring_moved_by),
        "moved_at": driver.hiring_moved_at,
        "cdl_file": {"url": url, "name": lic.document.name.rsplit("/", 1)[-1] if lic.document else ""} if url else None,
        "vehicle": {"type": vehicle.vehicle_type, "length": vehicle.length} if vehicle else None,
        "team": team,
        "has_records": has_records,
        "moves": moves,
    }


def _sort_key(c: dict[str, Any]) -> tuple:
    # Newest first: a Lead by when the driver signed up, every other column by
    # when the card was last moved (falling back to sign-up).
    when = c["created_at"] if c["stage"] == "lead" else (c["moved_at"] or c["created_at"])
    return (when.timestamp() if when else 0, c["id"])


def board(request: Any = None) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {s: [] for s in STAGES}
    for driver in board_queryset():
        c = card(driver, request)
        if c["stage"] in out:
            out[c["stage"]].append(c)
    for cards in out.values():
        cards.sort(key=_sort_key, reverse=True)
    return out


def move(driver: Driver, stage: str, user: CustomUser) -> Driver:
    """Moves the driver to a column; the status follows (Active or Pending)."""
    if stage not in STAGES:
        raise ValidationError({"stage": f"Must be one of {', '.join(STAGES)}."})
    target = status_named("Active" if stage == "active" else "Pending")
    if target is None:
        raise ValidationError({"detail": "The driver status for this column does not exist."})
    before = column_of(driver) or (driver.status.name if driver.status_id else "")
    with transaction.atomic():
        driver.status = target
        driver.hiring_stage = stage
        driver.hiring_moved_at = timezone.now()
        driver.hiring_moved_by = user
        driver.save(update_fields=["status", "hiring_stage", "hiring_moved_at", "hiring_moved_by", "updated_at"])
        HiringMove.objects.create(driver=driver, stage=stage, changed_by=user)
        DriverHistory.objects.create(
            driver=driver,
            changed_by=user,
            description=f"Hiring board: {LABELS.get(before, before) or 'none'} -> {LABELS[stage]}",
        )
    return driver


def remove_driver(driver: Driver, user: CustomUser | None) -> str:
    """Deletes the driver, or sets them Out of service when they have loads,
    payouts, expenses or statements. Returns "deleted" or "out_of_service"."""
    if not driver_has_records(driver):
        with transaction.atomic():
            driver.delete()
        return "deleted"
    from hub.sync import out_of_service_status

    out = out_of_service_status()
    if out is None:
        raise ValidationError(
            {"detail": "This driver has loads or payouts, so they cannot be deleted, and no Out of service status exists to set instead."}
        )
    with transaction.atomic():
        driver.status = out
        driver.save(update_fields=["status", "updated_at"])
        if user is not None:
            DriverHistory.objects.create(
                driver=driver,
                changed_by=user,
                description="Set to Out of service instead of deleted: the driver has loads or payouts on file.",
            )
    return "out_of_service"
