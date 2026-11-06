from typing import Any

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver

from accounting.models import Payout, PayoutExpense
from billing.models import BrokerBlacklist, Load
from hiring.models import Driver
from users.models import Company

from . import payouts
from .blacklist import push_blacklist
from .services import driver_hub
from .tenant import push_tenant_name

# The load fields a driver-app payout shows or follows (the payout's gross and
# fee are set with QuerySet.update when the driver pay or pay type changes).
LOAD_FIELDS = (
    "driver_id",
    "driver_pay",
    "payment_type",
    "pickup_date",
    "drop_date",
    "load_number",
    "shipment",
)


@receiver(post_save, sender=BrokerBlacklist)
@receiver(post_delete, sender=BrokerBlacklist)
def blacklist_changed(sender, **kwargs):
    # After commit, so the hub reads the list as it is now, not mid-transaction.
    transaction.on_commit(push_blacklist)


@receiver(post_save, sender=Payout)
def payout_saved(sender: type[Payout], instance: Payout, **kwargs: Any) -> None:
    payouts.schedule_push(instance.pk)


@receiver(pre_delete, sender=Payout)
def payout_deleted(sender: type[Payout], instance: Payout, **kwargs: Any) -> None:
    # Before the delete, while the load and its driver can still be read.
    if not driver_hub.configured():
        return
    if Load.objects.filter(
        pk=instance.load_id, driver__hub_driver_id__isnull=False
    ).exists():
        payouts.schedule_delete(instance.pk)


@receiver(post_save, sender=PayoutExpense)
@receiver(post_delete, sender=PayoutExpense)
def payout_expense_changed(
    sender: type[PayoutExpense], instance: PayoutExpense, **kwargs: Any
) -> None:
    payouts.schedule_push(instance.payout_id)


@receiver(pre_save, sender=Load)
def remember_load_for_hub(sender: type[Load], instance: Load, **kwargs: Any) -> None:
    instance._hub_old = None
    if instance.pk and driver_hub.configured():
        instance._hub_old = (
            Load.objects.filter(pk=instance.pk).values(*LOAD_FIELDS).first()
        )


@receiver(post_save, sender=Load)
def load_changed_for_hub(
    sender: type[Load], instance: Load, created: bool, **kwargs: Any
) -> None:
    old = getattr(instance, "_hub_old", None)
    if created or not old:
        return
    changed = [f for f in LOAD_FIELDS if old.get(f) != getattr(instance, f)]
    if not changed:
        return
    payout_id = (
        Payout.objects.filter(load_id=instance.pk).values_list("id", flat=True).first()
    )
    if not payout_id:
        return
    if "driver_id" in changed and old.get("driver_id"):
        # Off the old driver's app first; the push then goes to the new one.
        if Driver.objects.filter(
            pk=old["driver_id"], hub_driver_id__isnull=False
        ).exists():
            payouts.schedule_delete(payout_id)
    payouts.schedule_push(payout_id)


@receiver(pre_save, sender=Company)
def remember_company_name(
    sender: type[Company], instance: Company, **kwargs: Any
) -> None:
    instance._hub_old_name = (
        Company.objects.filter(pk=instance.pk).values_list("name", flat=True).first()
        if instance.pk and driver_hub.configured()
        else None
    )


@receiver(post_save, sender=Company)
def company_name_changed(
    sender: type[Company], instance: Company, **kwargs: Any
) -> None:
    if not driver_hub.configured():
        return
    if getattr(instance, "_hub_old_name", None) == instance.name:
        return
    name = instance.name
    transaction.on_commit(lambda: push_tenant_name(name))
