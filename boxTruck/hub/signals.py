from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from billing.models import BrokerBlacklist

from .blacklist import push_blacklist


@receiver(post_save, sender=BrokerBlacklist)
@receiver(post_delete, sender=BrokerBlacklist)
def blacklist_changed(sender, **kwargs):
    # After commit, so the hub reads the list as it is now, not mid-transaction.
    transaction.on_commit(push_blacklist)
