"""Push the carrier's record of a driver to the Driver Hub (SPEC-1.4 section 2.2).

The carrier owns the driver's name, phone and its own driver documents. After
every change that moves them, and right after a driver is linked, the record
goes to the hub with PUT /t/v1/drivers/{hub_driver_id}/profile:

    first_name, last_name   full_name split at the first space
    phone                   phone_number
    documents               the dispatcher's license and medical card files

The hub takes any name or phone it is sent as the latest change across all
the driver's carriers, so they are sent only on the link push, by the
command, and when full_name or phone_number really changed. A push caused by
a document or an expiry date leaves them out (the hub keeps its values).

Only drivers linked to the app and active there are pushed. Nothing is pushed
from inside the hub sync itself (see `syncing`), so a document the driver
uploaded in the app never bounces back. Pushes run after the transaction
commits, in the background; a hub error is logged and never fails the
dispatcher's save. `manage.py push_hub_profiles` sends every driver again.
"""

import logging
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timezone as dt_timezone
from typing import Any, Callable

from django.db import connections, transaction
from django.db.models import Q

from hiring.models import Driver, DriverFile

from .services import driver_hub

logger = logging.getLogger(__name__)

# What the sync stores is marked with hub_kind, or (older rows) only named
# "<title> (driver app)". Kept here as well as in hub.sync, which imports this module.
APP_FILE_SUFFIX = " (driver app)"
APP_FILES = ~Q(hub_kind="") | Q(name__endswith=APP_FILE_SUFFIX)

# The Driver columns the hub shows; a change to one of them is pushed.
PROFILE_FIELDS = ("full_name", "phone_number", "cdl_expiration", "medical_exp_date")
# Of those, the ones the hub shows as the driver's name and phone.
NAME_FIELDS = ("full_name", "phone_number")
# Signed file links must outlive the hub's download (at least 10 minutes).
FILE_URL_SECONDS = 3600

_LICENSE_WORDS = re.compile(r"\b(cdl|dl)\b")

_syncing: ContextVar[bool] = ContextVar("hub_syncing", default=False)


@contextmanager
def syncing() -> Iterator[None]:
    """Mark the hub sync running on this thread: no saves inside push back to the hub."""
    token = _syncing.set(True)
    try:
        yield
    finally:
        _syncing.reset(token)


def is_syncing() -> bool:
    return _syncing.get()


def classify(name: str | None) -> str | None:
    """The hub kind of a dispatcher's driver file, from its name. None: not sent.

    Case-insensitive: "medical" -> medical_card; "license", "licence" or the
    word "cdl" or "dl" -> license. Anything else (a contract, a W-9, an SSN
    card, a driving record) stays with the carrier.
    """
    text = (name or "").lower()
    if "medical" in text:
        return "medical_card"
    if "license" in text or "licence" in text or _LICENSE_WORDS.search(text):
        return "license"
    return None


def is_app_file(row: DriverFile) -> bool:
    """Whether the hub sync stored this file (a driver-app document)."""
    return bool(row.hub_kind) or (row.name or "").endswith(APP_FILE_SUFFIX)


def split_name(full_name: str | None) -> tuple[str, str]:
    """'Roy  Lee Johnson' -> ('Roy', 'Lee Johnson'): the first word, then the rest."""
    parts = (full_name or "").split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def file_url(row: DriverFile) -> str:
    """A link the hub can download the file from for at least FILE_URL_SECONDS.

    On S3 a presigned GET (the configured custom domain gives unsigned links,
    which only work while the bucket is public); elsewhere the storage URL.
    """
    storage = row.document.storage
    name = row.document.name
    bucket = getattr(storage, "bucket_name", None)
    if bucket:
        try:
            client = storage.connection.meta.client
            key = storage._normalize_name(name)
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=FILE_URL_SECONDS,
            )
        except Exception:
            logger.exception("driver file %s: no presigned link, using its url", row.pk)
    return row.document.url


def _updated_at(row: DriverFile) -> str | None:
    if row.updated_at is None:
        return None
    return (
        row.updated_at.astimezone(dt_timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def carrier_documents(driver: Driver) -> list[dict[str, Any]]:
    """The driver documents this carrier sends: its newest license and medical card file.

    Files the hub sync stored are never sent back, and nor is a file without
    an upload. The hub keeps one document per kind, so only the newest file of
    each kind goes (by last change, then id).
    """
    newest: dict[str, DriverFile] = {}
    rows = (
        DriverFile.objects.filter(driver=driver)
        .exclude(APP_FILES)
        .order_by("-updated_at", "-id")
    )
    for row in rows:
        kind = classify(row.name)
        if kind is None or kind in newest or not row.document:
            continue
        newest[kind] = row
    expiry = {
        "license": driver.cdl_expiration,
        "medical_card": driver.medical_exp_date,
    }
    documents = []
    for kind in ("license", "medical_card"):
        row = newest.get(kind)
        if row is None:
            continue
        expires = expiry[kind]
        documents.append(
            {
                "external_id": f"driverfile-{row.pk}",
                "kind": kind,
                "title": row.name,
                "file_url": file_url(row),
                "expires_at": expires.isoformat() if expires else None,
                "updated_at": _updated_at(row),
            }
        )
    return documents


def profile_body(driver: Driver, with_name: bool = True) -> dict[str, Any]:
    """What PUT /t/v1/drivers/{id}/profile gets.

    with_name False leaves out first_name, last_name and phone; empty ones
    are always left out (an absent key leaves the hub's value alone).
    """
    body: dict[str, Any] = {}
    if with_name:
        first_name, last_name = split_name(driver.full_name)
        for key, value in (
            ("first_name", first_name),
            ("last_name", last_name),
            ("phone", (driver.phone_number or "").strip()),
        ):
            if value:
                body[key] = value
    # Always present: the full set of this carrier's documents, [] removes them.
    body["documents"] = carrier_documents(driver)
    return body


def is_pushed(driver: Driver) -> bool:
    return bool(driver.hub_driver_id) and driver.hub_invite_status == "active"


def push_profile(driver_id: int, with_name: bool = True) -> dict[str, Any] | None:
    """Send one driver's record now. None when the driver is not in the app (nothing sent).

    A driver the hub does not have as a member is a 404 (DriverHubError):
    logged by the caller and not retried; the next change or the command
    tries again.
    """
    driver = Driver.objects.filter(pk=driver_id).first()
    if driver is None or not is_pushed(driver):
        return None
    reply = driver_hub.put_driver_profile(
        driver.hub_driver_id, profile_body(driver, with_name=with_name)
    )
    failed = [
        d
        for d in (reply or {}).get("documents") or []
        if isinstance(d, dict) and d.get("status") == "failed"
    ]
    if failed:
        logger.warning(
            "driver %s: the hub could not download %s", driver_id, failed
        )
    return reply if reply is not None else {}


def _in_background(work: Callable[[], Any]) -> None:
    """Run off the request thread, with its own database connection."""

    def run() -> None:
        try:
            work()
        except Exception:
            logger.exception("driver profiles not sent to the hub")
        finally:
            connections.close_all()

    threading.Thread(target=run, daemon=True).start()


class _Pending:
    """The drivers one transaction changed, pushed once when it commits.

    drivers maps the Driver id to whether its name and phone go too.
    """

    def __init__(self) -> None:
        self.drivers: dict[int, bool] = {}
        self.done = False

    def add(self, driver_id: int, with_name: bool) -> None:
        self.drivers[driver_id] = self.drivers.get(driver_id, False) or with_name

    def __call__(self) -> None:
        self.done = True
        drivers = dict(sorted(self.drivers.items()))
        _in_background(lambda: send(list(drivers), with_name=drivers))


def send(
    driver_ids: list[int], with_name: bool | dict[int, bool] = True
) -> dict[str, int]:
    """Push each driver; a down hub stops the rest (the next change or the command resends).

    with_name: for all drivers, or per Driver id.
    """
    counts = {"sent": 0, "skipped": 0, "failed": 0}
    for position, pk in enumerate(driver_ids):
        named = with_name.get(pk, False) if isinstance(with_name, dict) else with_name
        try:
            if push_profile(pk, with_name=named) is None:
                counts["skipped"] += 1
            else:
                counts["sent"] += 1
        except driver_hub.DriverHubError as exc:
            counts["failed"] += 1
            if exc.status_code == 404:
                # Not a member there (any more): nothing to retry.
                logger.info("driver %s is not a member on the hub: %s", pk, exc)
                continue
            logger.warning("driver %s profile not sent to the hub: %s", pk, exc)
            if exc.status_code >= 500:
                left = len(driver_ids) - position - 1
                counts["failed"] += left
                if left:
                    logger.warning(
                        "%s more driver profile(s) not sent; push_hub_profiles resends",
                        left,
                    )
                break
        except Exception:
            counts["failed"] += 1
            logger.exception("driver %s profile not sent to the hub", pk)
    return counts


def push_profile_later(driver_id: int | None, with_name: bool = False) -> None:
    """Push this driver after the transaction commits, in the background.

    with_name: also send name and phone (a link, or a name or phone change).
    One callback per transaction however many saves; outside a transaction it
    starts at once. The caller decides whether the change is pushed: the
    signals skip saves made by the hub sync.
    """
    if not driver_id or not driver_hub.configured():
        return
    connection = transaction.get_connection()
    if connection.in_atomic_block:
        for _savepoints, callback, _robust in connection.run_on_commit:
            if isinstance(callback, _Pending) and not callback.done:
                callback.add(int(driver_id), with_name)
                return
    pending = _Pending()
    pending.add(int(driver_id), with_name)
    transaction.on_commit(pending)


def linked_driver_ids() -> list[int]:
    return list(
        Driver.objects.filter(hub_driver_id__isnull=False, hub_invite_status="active")
        .order_by("id")
        .values_list("id", flat=True)
    )
