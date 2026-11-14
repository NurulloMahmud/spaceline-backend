"""Which saves make the hiring board change (see hiring/live.py).

Only fields the board shows or sorts by count, so the frequent location updates
from tracking and the driver app never wake the boards.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from . import live
from .models import Driver, DriverFile

BOARD_FIELDS = (
    "status_id",
    "hiring_stage",
    "hiring_moved_at",
    "hiring_moved_by_id",
    "referral_by_id",
    "full_name",
    "phone_number",
    "email",
    "city",
    "state",
    "cdl_number",
    "cdl_state",
    "cdl_class",
    "cdl_expiration",
    "medical_exp_date",
)


def _later(driver_id: int | None) -> None:
    if driver_id:
        transaction.on_commit(lambda: live.notify(driver_id))


@receiver(pre_save, sender=Driver)
def remember_board_fields(sender: type[Driver], instance: Driver, update_fields: Any = None, **kwargs: Any) -> None:
    instance._board_old = None
    if not instance.pk or not live.enabled():
        return
    if update_fields is not None and not ({f.removesuffix("_id") for f in BOARD_FIELDS} | set(BOARD_FIELDS)) & set(update_fields):
        instance._board_old = "unchanged"
        return
    instance._board_old = Driver.objects.filter(pk=instance.pk).values(*BOARD_FIELDS).first()


@receiver(post_save, sender=Driver)
def driver_saved(sender: type[Driver], instance: Driver, created: bool, **kwargs: Any) -> None:
    if not live.enabled():
        return
    old = getattr(instance, "_board_old", None)
    if created or old is None:
        _later(instance.pk)
    elif old != "unchanged" and any(old.get(f) != getattr(instance, f) for f in BOARD_FIELDS):
        _later(instance.pk)


@receiver(post_delete, sender=Driver)
def driver_deleted(sender: type[Driver], instance: Driver, **kwargs: Any) -> None:
    if live.enabled():
        _later(instance.pk)


def _is_license(f: DriverFile) -> bool:
    return f.hub_kind == "license" or str(f.name or "").strip().lower() == "driver license"


@receiver(post_save, sender=DriverFile)
@receiver(post_delete, sender=DriverFile)
def license_changed(sender: type[DriverFile], instance: DriverFile, **kwargs: Any) -> None:
    if live.enabled() and _is_license(instance):
        _later(instance.driver_id)


def _load_saved(sender: Any, instance: Any, created: bool = False, **kwargs: Any) -> None:
    # A first load takes an Active driver off the board.
    if live.enabled() and instance.driver_id and (created or kwargs.get("signal") is post_delete):
        _later(instance.driver_id)


def connect_load_signals() -> None:
    from billing.models import Load

    post_save.connect(_load_saved, sender=Load, dispatch_uid="hiring_live_load_saved")
    post_delete.connect(_load_saved, sender=Load, dispatch_uid="hiring_live_load_deleted")

