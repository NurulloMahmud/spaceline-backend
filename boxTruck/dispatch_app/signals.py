"""is_active changes and user deletes reach the hub, the Redis key and the device."""

from typing import Any

from django.contrib.auth import get_user_model
from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver

from . import services

User = get_user_model()


@receiver(pre_save, sender=User)
def remember_is_active(sender: Any, instance: Any, update_fields: Any = None, **kwargs: Any) -> None:
    instance._dispatch_was_active = None
    if not instance.pk:
        return
    if update_fields is not None and "is_active" not in update_fields:
        return
    instance._dispatch_was_active = (
        User.objects.filter(pk=instance.pk).values_list("is_active", flat=True).first()
    )


@receiver(post_save, sender=User)
def is_active_changed(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
    was = getattr(instance, "_dispatch_was_active", None)
    if created or was is None or bool(was) == bool(instance.is_active):
        return
    services.user_status_changed(instance.pk, bool(instance.is_active))


@receiver(pre_delete, sender=User)
def user_deleted(sender: Any, instance: Any, **kwargs: Any) -> None:
    services.user_deleted(instance.pk)
