"""Which drivers have a driver-app account, for the Tracking page's list.

Only a driver registered in the app can be invited, so the list asks the hub
about the drivers on the page in one batched lookup and shows the rest as
"not_registered". Answers are cached for a minute; when the hub is down the
list keeps the stored status and `hub_registered` is None.

A driver already connected is asked about by hub driver id (the `members`
answer), never by email: a dispatcher may change the email here, and that
must not unlink anyone. Only that answer unlinks a connected driver (account
deleted, or left this carrier). Drivers without a hub id are asked about by
email: registered or not, invited, declined, and linked when the hub has them
as active with us (their payouts are then sent).
"""

import hashlib
import logging
from typing import Any, Iterable

from django.core.cache import cache
from django.db import IntegrityError, transaction

from hiring.models import Driver

from . import payouts
from .services import driver_hub

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60
# After a failed lookup the hub is not asked again for this long, so a hub
# outage costs one slow page, not every page.
DOWN_SECONDS = 30
DOWN_KEY = "hub:lookup:down"


def _email_key(email: str) -> str:
    return "hub:lookup:email:" + hashlib.sha256(email.encode()).hexdigest()


def _member_key(hub_driver_id: int) -> str:
    return f"hub:lookup:member:{int(hub_driver_id)}"


def _norm(email: str | None) -> str:
    return (email or "").strip().lower()


def forget(*emails: str | None, hub_driver_ids: Iterable[int | None] = ()) -> None:
    """Drop cached answers after we changed what the hub knows about these drivers."""
    keys = [_email_key(_norm(e)) for e in emails if _norm(e)]
    keys += [_member_key(i) for i in hub_driver_ids if i]
    if keys:
        cache.delete_many(keys)


class Answer:
    """The hub's rows for one page, and which of them were asked just now."""

    def __init__(self) -> None:
        self.by_email: dict[str, dict[str, Any]] = {}
        self.by_member: dict[int, dict[str, Any]] = {}
        self.fresh_emails: set[str] = set()
        self.fresh_members: set[int] = set()


def lookup(
    emails: Iterable[str | None], hub_driver_ids: Iterable[int]
) -> Answer | None:
    """The hub's rows for these emails and hub driver ids: one request for what is not cached.

    None when the hub is not configured or cannot answer. Emails are lowered.
    """
    if not driver_hub.configured():
        return None
    wanted_emails = list(dict.fromkeys(_norm(e) for e in emails if _norm(e)))
    wanted_ids = list(dict.fromkeys(int(i) for i in hub_driver_ids if i))
    answer = Answer()
    if not wanted_emails and not wanted_ids:
        return answer
    if cache.get(DOWN_KEY):
        return None
    email_keys = {email: _email_key(email) for email in wanted_emails}
    member_keys = {pk: _member_key(pk) for pk in wanted_ids}
    cached = cache.get_many([*email_keys.values(), *member_keys.values()])
    answer.by_email = {e: cached[k] for e, k in email_keys.items() if k in cached}
    answer.by_member = {i: cached[k] for i, k in member_keys.items() if k in cached}
    missing_emails = [e for e in wanted_emails if e not in answer.by_email]
    missing_ids = [i for i in wanted_ids if i not in answer.by_member]
    if not missing_emails and not missing_ids:
        return answer
    try:
        reply = driver_hub.lookup_drivers(missing_emails, missing_ids)
    except driver_hub.DriverHubError as exc:
        logger.warning("driver-app registration lookup failed: %s", exc)
        cache.set(DOWN_KEY, True, DOWN_SECONDS)
        return None
    fresh_emails = _match(
        missing_emails, reply["drivers"], lambda r: _norm(r.get("email"))
    )
    fresh_members = _match(missing_ids, reply["members"], driver_hub_id)
    cache.set_many(
        {
            **{email_keys[e]: row for e, row in fresh_emails.items()},
            **{member_keys[i]: row for i, row in fresh_members.items()},
        },
        CACHE_SECONDS,
    )
    answer.by_email.update(fresh_emails)
    answer.by_member.update(fresh_members)
    answer.fresh_emails = set(fresh_emails)
    answer.fresh_members = set(fresh_members)
    return answer


def _match(
    asked: list[Any], rows: list[dict[str, Any]], key_of: Any
) -> dict[Any, dict]:
    """Rows by what was asked: by their own key, else by position (they come in order)."""
    by_key = {key_of(r): r for r in rows}
    matched = {}
    for position, key in enumerate(asked):
        row = by_key.get(key)
        if row is None and position < len(rows):
            row = rows[position]
        if row is not None:
            matched[key] = row
    return matched


def apply_registrations(drivers: list[Driver]) -> None:
    """Set hub_registered on each driver and correct stored fields from fresh answers."""
    connected = [d for d in drivers if d.hub_driver_id]
    others = [d for d in drivers if not d.hub_driver_id]
    answer = lookup((d.email for d in others), (d.hub_driver_id for d in connected))
    if answer is None:
        for driver in drivers:
            driver.hub_registered = None
        return
    for driver in connected:
        row = answer.by_member.get(int(driver.hub_driver_id))
        if row is None:
            driver.hub_registered = None
            continue
        driver.hub_registered = bool(row.get("exists"))
        if int(driver.hub_driver_id) in answer.fresh_members:
            reconcile_member(driver, row)
    for driver in others:
        email = _norm(driver.email)
        if not email:
            driver.hub_registered = False
            continue
        row = answer.by_email.get(email)
        if row is None:
            driver.hub_registered = None
            continue
        driver.hub_registered = bool(row.get("registered"))
        if email in answer.fresh_emails:
            reconcile(driver, row)


def reconcile_member(driver: Driver, row: dict[str, Any]) -> None:
    """A connected driver, from the hub's row for its hub driver id.

    Unlinked when the account was deleted (`exists` false; the hub's remove
    call normally got here first, this is the safety net) or the driver left
    this carrier. Anything else, "active" above all, keeps the driver connected.
    """
    gone = row.get("exists") is False
    if not gone and str(row.get("status") or "") != "left":
        return
    _store(
        driver,
        {"hub_driver_id": None, "hub_invite_id": None, "hub_invite_status": "left"},
        "account deleted in the app" if gone else "left this carrier in the app",
    )


def reconcile(driver: Driver, row: dict[str, Any]) -> None:
    """A driver without a hub id, from the hub's row for its email.

    Only what the hub states outright changes anything; "none" changes nothing.
    """
    hub_status = str(row.get("status") or "")
    hub_driver_id = driver_hub_id(row)
    changes: dict[str, Any] = {}
    if driver.hub_driver_id:
        # Decided by the members answer only, never by email.
        return
    if hub_status == "active" and hub_driver_id:
        taken = (
            Driver.objects.filter(hub_driver_id=hub_driver_id)
            .exclude(pk=driver.pk)
            .exists()
        )
        if not taken:
            changes = {"hub_driver_id": hub_driver_id, "hub_invite_status": "active"}
    elif hub_status in ("declined", "left") and driver.hub_invite_status != hub_status:
        changes = {"hub_invite_status": hub_status}
    elif hub_status == "invited" and driver.hub_invite_status not in (
        "pending",
        "accepted",
    ):
        changes = {"hub_invite_status": "pending"}
    if changes and _store(driver, changes, f"hub says {hub_status}"):
        if changes.get("hub_driver_id"):
            payouts.push_driver_payouts_later(driver.pk)


def _store(driver: Driver, changes: dict[str, Any], why: str) -> bool:
    try:
        with transaction.atomic():
            Driver.objects.filter(pk=driver.pk).update(**changes)
    except IntegrityError:
        # Another row took the hub driver id meanwhile; the next lookup retries.
        logger.warning(
            "driver %s hub fields not changed (%s): %s", driver.pk, why, changes
        )
        return False
    for field, value in changes.items():
        setattr(driver, field, value)
    logger.info("driver %s hub fields reconciled (%s): %s", driver.pk, why, changes)
    return True


def driver_hub_id(row: dict[str, Any]) -> int | None:
    try:
        value = int(row.get("hub_driver_id") or 0)
    except (TypeError, ValueError):
        return None
    return value or None
