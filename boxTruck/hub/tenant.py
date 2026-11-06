"""The carrier name the driver app shows: this deployment's own Company."""

import logging

from users.models import Company

from .services import driver_hub

logger = logging.getLogger(__name__)


def push_tenant_name(name: str | None = None) -> str | None:
    """Send the company name to the hub. Returns the name sent, None if not sent."""
    if not driver_hub.configured():
        return None
    name = (name if name is not None else Company.current().name or "").strip()
    if not name:
        return None
    try:
        driver_hub.update_tenant(name)
    except driver_hub.DriverHubError as exc:
        logger.warning("carrier name not pushed to the hub: %s", exc)
        return None
    except Exception:
        logger.exception("carrier name not pushed to the hub")
        return None
    return name
