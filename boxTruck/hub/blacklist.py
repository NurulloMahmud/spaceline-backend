"""Keep the hub's copy of the broker blacklist equal to ours."""
import logging

from billing.models import Broker, BrokerBlacklist

from .services import driver_hub

logger = logging.getLogger(__name__)


def blacklist_rows():
    entries = list(BrokerBlacklist.objects.order_by('id').values('name', 'mc'))
    mcs = [e['mc'] for e in entries if e['mc']]
    emails = {}
    if mcs:
        for mc, email in Broker.objects.filter(mc__in=mcs).values_list('mc', 'email'):
            if email:
                emails[mc] = email
    return [{'name': e['name'], 'mc': e['mc'], 'email': emails.get(e['mc'], '')} for e in entries]


def push_blacklist():
    """Full replace on the hub. Returns the rows sent, or None when the hub is off or down."""
    if not driver_hub.configured():
        return None
    rows = blacklist_rows()
    try:
        driver_hub.replace_blacklist(rows)
    except driver_hub.DriverHubError as exc:
        logger.warning('blacklist not pushed to the hub: %s', exc)
        return None
    return rows
