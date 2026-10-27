"""Every load gets its payout and invoice the moment it is created, however it is created
(the Loads page, the rate con parser, batch imports), and both follow the load's pay and
status changes afterwards."""
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from billing.models import Load

from .services import create_for_load, follow_load_change


@receiver(pre_save, sender=Load)
def remember_old_load(sender, instance, **kwargs):
    if not instance.pk:
        instance._accounting_old = None
        return
    old = Load.objects.filter(pk=instance.pk).select_related('status').first()
    instance._accounting_old = {
        'driver_pay': old.driver_pay, 'carrier_pay': old.carrier_pay,
        'status_id': old.status_id, 'status_name': old.status.name if old.status else None,
    } if old else None


@receiver(post_save, sender=Load)
def keep_accounting_in_step(sender, instance, created, **kwargs):
    if created:
        create_for_load(instance, user=instance.created_by)
        return
    old = getattr(instance, '_accounting_old', None)
    if old:
        follow_load_change(instance, old, user=instance.updated_by)
