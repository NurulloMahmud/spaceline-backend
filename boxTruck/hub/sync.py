"""Receive a driver from the Driver Hub (POST /api/hub/drivers/).

Since release 1.4 the carrier owns the driver record (SPEC-1.4 section 2):
name, phone, email, mailing address, company, unit number and vehicles are
edited here and pushed to the hub (hub.profiles), never taken from it. The
hub's push links the driver, reports its position and brings the driver's
own app documents (license, medical card, other).

Matching is by hub_driver_id first, then by the email the invite went to, so
the Driver row the dispatcher invited is the one that gets linked. Only when
no row matches (the hub knows a member we lost) is a Driver created, from the
hub's name, email and phone, with no company and no vehicle.

The sync never touches a file a dispatcher uploaded, and nothing it saves is
pushed back to the hub (hub.profiles.syncing).
"""

import logging
import os
import re
from datetime import date, datetime
from typing import Any

import requests
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Model
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.text import get_valid_filename

from hiring.models import Driver, DriverFile, DriverStatus
from hiring.places import place_for_position
from mobile.models import DriverLocation

from . import payouts, profiles, registry
from .profiles import APP_FILE_SUFFIX, APP_FILES

logger = logging.getLogger(__name__)

# The driver documents the app manages. Insurance and registration (sent by
# hub builds before 1.4) belong to the carrier's vehicle and are ignored.
DRIVER_DOC_KINDS = ("license", "medical_card", "other")
DOC_TITLES = {
    "license": "Driver License",
    "medical_card": "Medical Card",
}
# Documents the driver uploaded in the app. "carrier" ones came from a carrier
# (maybe another one) and are not stored; no source is an old hub: the app's.
APP_SOURCES = ("", "app")
# DriverLocation.device for positions the hub reported.
APP_DEVICE = "driver app"
INVITE_STATUSES = ("declined", "accepted")


def fetch_file(url):
    """Download a hub upload. Returns bytes or None; a missing file never blocks the sync."""
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("hub document %s could not be fetched: %s", url, exc)
        return None
    return resp.content


def _text(value, limit=255):
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = parse_datetime(str(value))
    if parsed:
        return parsed.date()
    return parse_date(str(value)[:10])


def default_status():
    return (
        DriverStatus.objects.filter(name__iexact="pending").first()
        or DriverStatus.objects.order_by("id").first()
        or DriverStatus.objects.create(name="Pending")
    )


def find_driver(hub_driver_id, email):
    driver = Driver.objects.filter(hub_driver_id=hub_driver_id).first()
    if driver or not email:
        return driver
    return (
        Driver.objects.filter(email__iexact=email, hub_driver_id__isnull=True)
        .order_by("id")
        .first()
    )


def _new_driver(hub_driver_id: int, data: dict[str, Any], email: str) -> Driver:
    """A member the carrier has no row for: what the hub knows, nothing else."""
    full_name = " ".join(
        p for p in (_text(data.get("first_name")), _text(data.get("last_name"))) if p
    )
    return Driver(
        status=default_status(),
        full_name=full_name or email or f"Driver {hub_driver_id}",
        email=email or None,
        phone_number=_text(data.get("phone")) or None,
    )


def upsert_driver(payload: dict[str, Any]) -> Driver:
    """Link the driver, record the position and store the app documents.

    Accepts the hub's payload from before 1.4 too: its `vehicle` and the
    driver's company and address keys are ignored.
    """
    hub_driver_id = _int(payload.get("hub_driver_id"))
    if not hub_driver_id:
        raise ValueError("hub_driver_id is required")
    data = payload.get("driver") or {}
    if not isinstance(data, dict):
        data = {}
    email = _text(data.get("email"))

    with profiles.syncing(), transaction.atomic():
        driver = find_driver(hub_driver_id, email)
        if driver is None:
            driver = _new_driver(hub_driver_id, data, email)
        before = _snapshot(driver)
        linked_before = driver.hub_driver_id

        driver.hub_driver_id = hub_driver_id
        driver.hub_invite_status = "active"

        position = _position(data)
        moved = position is not None and _is_new_position(driver, position)
        if moved:
            driver.current_latitude, driver.current_longitude = position
            driver.current_location_updated_at = timezone.now()
            # The place the Tracking page shows, from the nearest known ZIP.
            # The mailing address stays the carrier's.
            for key, value in place_for_position(*position).items():
                setattr(driver, key, value)

        # Most pushes only move the position: rows whose values did not change
        # are not written.
        _save_changed(driver, before)
        if moved:
            DriverLocation.objects.create(
                driver=driver,
                latitude=position[0],
                longitude=position[1],
                device=APP_DEVICE,
            )

        # The hub always sends the key (null when the driver has no documents).
        # Without it we cannot tell a removed document from a short payload.
        reconcile = "documents" in payload
        _apply_documents(driver, payload.get("documents") or [], reconcile=reconcile)

        if linked_before != hub_driver_id:
            # Newly linked: the app shows the payouts the driver already has
            # here, and this carrier's name, phone and documents.
            payouts.push_driver_payouts_later(driver.pk)
            profiles.push_profile_later(driver.pk, with_name=True)
    registry.forget(email, driver.email, hub_driver_ids=[hub_driver_id, linked_before])
    return driver


def _position(data: dict[str, Any]) -> tuple[float, float] | None:
    """The app's last known position, or None when it sent none (or 0, 0)."""
    try:
        latitude, longitude = float(data.get("lat")), float(data.get("lng"))
    except (TypeError, ValueError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    if latitude == 0 and longitude == 0:
        return None
    return latitude, longitude


def _is_new_position(driver: Driver, position: tuple[float, float]) -> bool:
    """False when the hub resends the position it sent last time.

    The hub also pushes on document changes, with the last position again: a
    repeated position is old news and must not mark the location fresh or
    undo a ZIP a dispatcher typed since.
    """
    if driver.pk is None:
        return True
    last = (
        DriverLocation.objects.filter(driver=driver, device=APP_DEVICE)
        .order_by("-created_at", "-id")
        .first()
    )
    if last is None:
        return True
    return (
        abs(last.latitude - position[0]) > 1e-6
        or abs(last.longitude - position[1]) > 1e-6
    )


def _snapshot(instance: Model) -> dict[str, Any]:
    """The row's column values, to tell later whether anything changed."""
    return {
        f.name: getattr(instance, f.attname) for f in instance._meta.concrete_fields
    }


def _save_changed(instance: Model, before: dict[str, Any]) -> bool:
    """Save a new row, or only the columns that changed (and auto_now ones). True if written."""
    if instance.pk is None:
        instance.save()
        return True
    fields = {f.name: f for f in instance._meta.concrete_fields}
    changed = [
        name
        for name, value in before.items()
        if getattr(instance, fields[name].attname) != value
    ]
    if not changed:
        return False
    stamps = [
        name
        for name, field in fields.items()
        if getattr(field, "auto_now", False) and name not in changed
    ]
    instance.save(update_fields=changed + stamps)
    return True


class _Stored:
    """What one push did with its documents, for removing the ones it no longer has."""

    def __init__(self) -> None:
        self.driver_files: set[int] = set()
        # Kinds whose new file could not be downloaded: their old files stay.
        self.unfetched: set[str] = set()
        self.names: set[str] = set()

    def keep(self, pk: int) -> None:
        self.driver_files.add(pk)

    def unique_name(self, title: str) -> str:
        """'<title> (driver app)', numbered when two documents share a title."""
        title = title[:200]
        name = f"{title}{APP_FILE_SUFFIX}"
        number = 1
        while name in self.names:
            number += 1
            name = f"{title} {number}{APP_FILE_SUFFIX}"
        self.names.add(name)
        return name


def is_app_document(doc: dict[str, Any]) -> bool:
    """A driver document the driver uploaded in the app (or from a hub without sources)."""
    kind = _text(doc.get("kind"), 40) or "other"
    source = _text(doc.get("source"), 40).lower()
    return kind in DRIVER_DOC_KINDS and source in APP_SOURCES


def _apply_documents(
    driver: Driver,
    documents: list[dict[str, Any]],
    reconcile: bool = True,
) -> None:
    driver_updates = {}
    stored = _Stored()
    for doc in documents:
        if not isinstance(doc, dict) or not is_app_document(doc):
            continue
        kind = _text(doc.get("kind"), 40) or "other"
        expires = _date(doc.get("expires_at"))
        if kind == "license" and expires:
            driver_updates["cdl_expiration"] = expires
        elif kind == "medical_card" and expires:
            driver_updates["medical_exp_date"] = expires
        _store_document(driver, kind, doc, stored)
    driver_updates = {
        k: v for k, v in driver_updates.items() if getattr(driver, k) != v
    }
    if driver_updates:
        Driver.objects.filter(pk=driver.pk).update(**driver_updates)
        for key, value in driver_updates.items():
            setattr(driver, key, value)
    if reconcile:
        _remove_dropped_documents(driver, stored)


def _store_document(
    driver: Driver,
    kind: str,
    doc: dict[str, Any],
    stored: _Stored,
) -> None:
    url = _text(doc.get("file_url"), 500)
    title = _text(doc.get("title")) or DOC_TITLES.get(
        kind, kind.replace("_", " ").title()
    )
    name = stored.unique_name(title)
    existing = (
        DriverFile.objects.filter(APP_FILES, name=name, driver=driver)
        .order_by("-id")
        .first()
    )

    if not url:
        # The document is still in the app, just without a file: keep ours.
        if existing:
            stored.keep(existing.pk)
        return
    filename = _upload_name(url, kind)
    if (
        existing
        and existing.document
        and _same_upload(existing.document.name, filename)
    ):
        if existing.hub_kind != kind:
            DriverFile.objects.filter(pk=existing.pk).update(hub_kind=kind)
        stored.keep(existing.pk)
        return
    content = fetch_file(url)
    if content is None:
        stored.unfetched.add(kind)
        if existing:
            stored.keep(existing.pk)
        return
    DriverFile.objects.filter(APP_FILES, name=name, driver=driver).delete()
    created = DriverFile.objects.create(
        name=name,
        document=ContentFile(content, name=filename),
        hub_kind=kind,
        driver=driver,
    )
    stored.keep(created.pk)


def _upload_name(url: str, kind: str) -> str:
    """The file name of a hub link, cleaned the way storage cleans it."""
    try:
        return get_valid_filename(url.split("?")[0].rsplit("/", 1)[-1])
    except SuspiciousFileOperation:
        return f"{kind}.bin"


def _same_upload(stored_name: str, filename: str) -> bool:
    """Whether a stored file is this upload.

    When the name is taken, storage adds "_" and 7 random characters before
    the extension (AWS_S3_FILE_OVERWRITE is off).
    """
    stored = stored_name.rsplit("/", 1)[-1]
    root, ext = os.path.splitext(filename)
    return (
        re.fullmatch(f"{re.escape(root)}(_[A-Za-z0-9]{{7}})?{re.escape(ext)}", stored)
        is not None
    )


def _remove_dropped_documents(driver: Driver, stored: _Stored) -> None:
    """Remove the app's files for documents the driver removed in the app.

    Only driver files the sync stored itself. Vehicle files it stored before
    1.4 (insurance, registration) now belong to the carrier and stay. A kind
    whose new file could not be downloaded keeps its old files, and so do the
    unmarked older files (whose kind is unknown) when any download failed.
    """
    dropped = DriverFile.objects.filter(APP_FILES, driver=driver).exclude(
        pk__in=stored.driver_files
    )
    if stored.unfetched:
        dropped = dropped.exclude(hub_kind__in=stored.unfetched).exclude(hub_kind="")
    for row in dropped:
        logger.info(
            "hub driver %s removed %s in the app: DriverFile %s (%s) deleted",
            driver.hub_driver_id,
            row.hub_kind or "a document",
            row.pk,
            row.name,
        )
        # The stored object is left in storage, as when a dispatcher deletes a file.
        row.delete()


def set_invite_status(invite_id: Any, email: Any, invite_status: str) -> Driver | None:
    """Record the driver's answer to an invite. None when no driver has that invite.

    By the hub invite id, else by email among drivers without an invite id. A
    driver already connected keeps its state: the profile push settled it.
    """
    if invite_status not in INVITE_STATUSES:
        raise ValueError(f'status must be one of {", ".join(INVITE_STATUSES)}')
    invite = _int(invite_id)
    driver = (
        Driver.objects.filter(hub_invite_id=invite).order_by("id").first()
        if invite
        else None
    )
    address = _text(email)
    if driver is None and address:
        driver = (
            Driver.objects.filter(email__iexact=address, hub_invite_id__isnull=True)
            .order_by("id")
            .first()
        )
    if driver is None:
        return None
    if driver.hub_driver_id:
        return driver
    driver.hub_invite_status = invite_status
    fields = ["hub_invite_status", "updated_at"]
    if invite and not driver.hub_invite_id:
        driver.hub_invite_id = invite
        fields.append("hub_invite_id")
    driver.save(update_fields=fields)
    registry.forget(address, driver.email)
    return driver


def _squash(name):
    """"Out of Service", "out-of-service" and "OUT OF SERVICE" all read "outofservice"."""
    return re.sub(r"[\s\-]+", "", str(name or "")).lower()


def out_of_service_status():
    """The existing out-of-service driver status, or None (never created here)."""
    for status in DriverStatus.objects.order_by("id"):
        if _squash(status.name) == "outofservice":
            return status
    return None


def remove_driver(hub_driver_id):
    """The driver left this carrier (or deleted the app account): keep the record,
    drop the link and mark the driver out of service."""
    driver = Driver.objects.filter(hub_driver_id=_int(hub_driver_id)).first()
    if not driver:
        return None
    registry.forget(hub_driver_ids=[driver.hub_driver_id])
    driver.hub_driver_id = None
    driver.hub_invite_status = "left"
    driver.hub_invite_id = None
    fields = ["hub_driver_id", "hub_invite_status", "hub_invite_id"]
    out_of_service = out_of_service_status()
    if out_of_service:
        driver.status = out_of_service
        fields.append("status")
    else:
        logger.warning(
            "No out-of-service driver status exists; driver %s keeps its status",
            driver.pk,
        )
    driver.save(update_fields=fields)
    registry.forget(driver.email)
    return driver
