"""Create or update the hiring records from a hub driver profile.

The hub is the source of truth for a connected driver: on every profile,
vehicle or document change it posts the whole profile here and we overwrite
our copy. Matching is by hub_driver_id first, then by the email the invite
went to, so the Driver row the dispatcher invited is the one that gets linked.

What the sync never does: edit a DriverCompany other drivers share, write an
empty app value over one we have, or touch a file a dispatcher uploaded.
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
from django.db.models import Model, Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.text import get_valid_filename

from hiring.models import (
    Driver,
    DriverCompany,
    DriverFile,
    DriverStatus,
    Vehicle,
    VehicleEquipment,
    VehicleFile,
)
from hiring.places import place_for_position
from mobile.models import DriverLocation

from . import payouts, registry

logger = logging.getLogger(__name__)

DRIVER_DOC_KINDS = ("license", "medical_card", "other")
VEHICLE_DOC_KINDS = ("insurance", "registration")
DOC_TITLES = {
    "license": "Driver License",
    "medical_card": "Medical Card",
    "insurance": "Insurance",
    "registration": "Registration",
}
APP_FILE_SUFFIX = " (driver app)"
# Files this sync stored: marked with hub_kind, or, before that field existed,
# only named "<title> (driver app)". Nothing else is ever replaced or removed.
APP_FILES = ~Q(hub_kind="") | Q(name__endswith=APP_FILE_SUFFIX)
# DriverLocation.device for positions the hub reported.
APP_DEVICE = "driver app"
INVITE_STATUSES = ("declined", "accepted")

COMPANY_IDENTITY = ("mc", "employer_id", "name", "business_as")
# The app sends 0 for a number the driver left empty, so 0 means "not given".
VEHICLE_NUMBERS = (
    ("payload", "payload_lbs"),
    ("gvw", "gvw_lbs"),
    ("length", "cargo_length_in"),
    ("width", "cargo_width_in"),
    ("height", "cargo_height_in"),
    ("door_open_width", "door_width_in"),
    ("door_open_height", "door_height_in"),
)


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


def _positive_int(value: Any) -> int | None:
    number = _int(value)
    return number if number and number > 0 else None


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


@transaction.atomic
def upsert_driver(payload: dict[str, Any]) -> Driver:
    hub_driver_id = _int(payload.get("hub_driver_id"))
    if not hub_driver_id:
        raise ValueError("hub_driver_id is required")
    data = payload.get("driver") or {}
    email = _text(data.get("email"))

    driver = find_driver(hub_driver_id, email)
    if driver is None:
        driver = Driver(status=default_status())
    before = _snapshot(driver)
    linked_before = driver.hub_driver_id

    driver.hub_driver_id = hub_driver_id
    driver.hub_invite_status = "active"
    full_name = " ".join(
        p for p in (_text(data.get("first_name")), _text(data.get("last_name"))) if p
    )
    if full_name:
        driver.full_name = full_name
    elif not driver.full_name:
        driver.full_name = email or f"Driver {hub_driver_id}"
    if email:
        driver.email = email
    if data.get("phone"):
        driver.phone_number = _text(data.get("phone"))
    for ours, theirs, limit in (
        ("address", "address", 255),
        ("city", "city", 250),
        ("state", "state", 55),
        ("zip_code", "zip", 20),
    ):
        if data.get(theirs):
            setattr(driver, ours, _text(data.get(theirs), limit))

    position = _position(data)
    moved = position is not None and _is_new_position(driver, position)
    if moved:
        driver.current_latitude, driver.current_longitude = position
        driver.current_location_updated_at = timezone.now()
        # The place the Tracking page shows, from the nearest known ZIP. The
        # mailing address above stays what the driver typed.
        for key, value in place_for_position(*position).items():
            setattr(driver, key, value)

    company = _upsert_company(driver, data)
    if company:
        driver.driver_company = company
    # A push is the whole profile, and most pushes now only move the position:
    # rows whose values did not change are not written.
    _save_changed(driver, before)
    if linked_before != hub_driver_id:
        # Newly linked: the app shows the payouts the driver already has here.
        payouts.push_driver_payouts_later(driver.pk)
    if moved:
        DriverLocation.objects.create(
            driver=driver,
            latitude=position[0],
            longitude=position[1],
            device=APP_DEVICE,
        )

    vehicle = _upsert_vehicle(driver, payload.get("vehicle"))
    # The hub always sends the key (null when the driver has no documents).
    # Without it we cannot tell a removed document from a short payload.
    reconcile = "documents" in payload
    _apply_documents(
        driver, vehicle, payload.get("documents") or [], reconcile=reconcile
    )
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

    The hub pushes the profile on profile, vehicle and document changes, not
    on position updates, so a repeated position is old news: it must not mark
    the location fresh or undo a ZIP a dispatcher typed since.
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


def _company_fields(data: dict[str, Any]) -> dict[str, str]:
    return {
        "mc": _text(data.get("mc")),
        "employer_id": _text(data.get("employer_id")),
        "name": _text(data.get("company_name")),
        "business_as": _text(data.get("business_as")),
        "business_type": _text(data.get("business_type")),
        "zipcode": _text(data.get("zip")),
        "state": _text(data.get("state")),
        "city": _text(data.get("city")),
        "address": _text(data.get("address")),
    }


def _norm_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _norm_number(value: str | None) -> str:
    return re.sub(r"\D+", "", value or "")


def _same_company(company: DriverCompany, fields: dict[str, str]) -> bool:
    """Whether the app values describe this company (same MC or EIN, else same name)."""
    matched = False
    for key in ("mc", "employer_id"):
        ours, theirs = _norm_number(getattr(company, key)), _norm_number(fields[key])
        if ours and theirs:
            if ours != theirs:
                return False
            matched = True
    if matched:
        return True
    names = {
        n for n in (_norm_name(company.name), _norm_name(company.business_as)) if n
    }
    return any(
        _norm_name(fields[key]) in names
        for key in ("name", "business_as")
        if _norm_name(fields[key])
    )


def _is_shared(company: DriverCompany, driver: Driver) -> bool:
    return Driver.objects.filter(driver_company=company).exclude(pk=driver.pk).exists()


def _upsert_company(driver: Driver, data: dict[str, Any]) -> DriverCompany | None:
    """The driver's own company, as the app describes it.

    A company other drivers use (for example the carrier's own LLC that every
    company driver is attached to) is never edited from the app. If the app
    describes that same company the driver stays on it untouched; otherwise the
    driver gets a company of their own with the app values.
    """
    fields = _company_fields(data)
    if not any(fields[key] for key in COMPANY_IDENTITY):
        return driver.driver_company
    company = driver.driver_company
    if company is not None and _is_shared(company, driver):
        if _same_company(company, fields):
            return company
        logger.info(
            "hub driver %s: company %s is shared, the app company %r becomes its own",
            driver.hub_driver_id,
            company.pk,
            fields["name"] or fields["business_as"] or fields["mc"],
        )
        company = None
    if company is None:
        company = DriverCompany()
    before = _snapshot(company)
    for key, value in fields.items():
        # An empty app value never blanks one we have.
        if value:
            setattr(company, key, value)
    company.phone_number = _text(data.get("phone")) or company.phone_number or ""
    company.email = _text(data.get("email")) or company.email
    company.applicant_first_name = (
        _text(data.get("first_name")) or company.applicant_first_name
    )
    company.applicant_last_name = (
        _text(data.get("last_name")) or company.applicant_last_name
    )
    _save_changed(company, before)
    return company


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


def _upsert_vehicle(driver: Driver, data: dict[str, Any] | None) -> Vehicle | None:
    if not data:
        return driver.vehicles.order_by("id").first()
    vehicle = driver.vehicles.order_by("id").first() or Vehicle(
        driver=driver, payload=0, gvw=0, make="", model=""
    )
    before = _snapshot(vehicle)
    vehicle.make = _text(data.get("make")) or vehicle.make
    vehicle.model = _text(data.get("model")) or vehicle.model
    vehicle.vehicle_type = _text(data.get("vehicle_type")) or vehicle.vehicle_type
    year = _positive_int(data.get("year"))
    if year:
        vehicle.year = year
    for ours, theirs in VEHICLE_NUMBERS:
        value = _positive_int(data.get(theirs))
        if value:
            setattr(vehicle, ours, value)
    dock = _positive_int(data.get("dock_height_in"))
    dock_type = _text(data.get("dock_type"))
    if dock or dock_type:
        vehicle.dock_height = " ".join(
            p for p in (f"{dock} in" if dock else "", dock_type) if p
        )
    _save_changed(vehicle, before)

    unit = _text(data.get("unit_number"))
    if unit and driver.unit_number != unit:
        Driver.objects.filter(pk=driver.pk).update(unit_number=unit)
        driver.unit_number = unit

    equipment = [
        e.strip() for e in _text(data.get("equipment"), 1000).split(",") if e.strip()
    ]
    names = [e[:255] for e in equipment]
    current = list(
        VehicleEquipment.objects.filter(vehicle=vehicle)
        .order_by("id")
        .values_list("name", flat=True)
    )
    if names and names != current:
        VehicleEquipment.objects.filter(vehicle=vehicle).delete()
        VehicleEquipment.objects.bulk_create(
            [VehicleEquipment(vehicle=vehicle, name=name) for name in names]
        )
    return vehicle


class _Stored:
    """What one push did with its documents, for removing the ones it no longer has."""

    def __init__(self) -> None:
        self.driver_files: set[int] = set()
        self.vehicle_files: set[int] = set()
        # Kinds whose new file could not be downloaded: their old files stay.
        self.unfetched: set[str] = set()
        self.names: set[tuple[str, str]] = set()

    def keep(self, model: type[Model], pk: int) -> None:
        (self.vehicle_files if model is VehicleFile else self.driver_files).add(pk)

    def unique_name(self, model: type[Model], title: str) -> str:
        """'<title> (driver app)', numbered when two documents share a title."""
        title = title[:200]
        name = f"{title}{APP_FILE_SUFFIX}"
        number = 1
        while (model.__name__, name) in self.names:
            number += 1
            name = f"{title} {number}{APP_FILE_SUFFIX}"
        self.names.add((model.__name__, name))
        return name


def _apply_documents(
    driver: Driver,
    vehicle: Vehicle | None,
    documents: list[dict[str, Any]],
    reconcile: bool = True,
) -> None:
    driver_updates = {}
    vehicle_updates = {}
    stored = _Stored()
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        kind = _text(doc.get("kind"), 40) or "other"
        expires = _date(doc.get("expires_at"))
        if kind == "license" and expires:
            driver_updates["cdl_expiration"] = expires
        elif kind == "medical_card" and expires:
            driver_updates["medical_exp_date"] = expires
        elif kind == "insurance" and expires:
            vehicle_updates["insurance_expiry_date"] = expires
        elif kind == "registration" and expires:
            vehicle_updates["registration_expiry_date"] = expires
        _store_document(driver, vehicle, kind, doc, stored)
    driver_updates = {
        k: v for k, v in driver_updates.items() if getattr(driver, k) != v
    }
    if driver_updates:
        Driver.objects.filter(pk=driver.pk).update(**driver_updates)
        for key, value in driver_updates.items():
            setattr(driver, key, value)
    if vehicle:
        vehicle_updates = {
            k: v for k, v in vehicle_updates.items() if getattr(vehicle, k) != v
        }
        if vehicle_updates:
            Vehicle.objects.filter(pk=vehicle.pk).update(**vehicle_updates)
            for key, value in vehicle_updates.items():
                setattr(vehicle, key, value)
    if reconcile:
        _remove_dropped_documents(driver, vehicle, stored)


def _store_document(
    driver: Driver,
    vehicle: Vehicle | None,
    kind: str,
    doc: dict[str, Any],
    stored: _Stored,
) -> None:
    url = _text(doc.get("file_url"), 500)
    title = _text(doc.get("title")) or DOC_TITLES.get(
        kind, kind.replace("_", " ").title()
    )

    if kind in VEHICLE_DOC_KINDS:
        if vehicle is None:
            return
        model, owner = VehicleFile, {"vehicle": vehicle}
    else:
        model, owner = DriverFile, {"driver": driver}
    name = stored.unique_name(model, title)
    existing = (
        model.objects.filter(APP_FILES, name=name, **owner).order_by("-id").first()
    )

    if not url:
        # The document is still in the app, just without a file: keep ours.
        if existing:
            stored.keep(model, existing.pk)
        return
    filename = _upload_name(url, kind)
    if (
        existing
        and existing.document
        and _same_upload(existing.document.name, filename)
    ):
        if existing.hub_kind != kind:
            model.objects.filter(pk=existing.pk).update(hub_kind=kind)
        stored.keep(model, existing.pk)
        return
    content = fetch_file(url)
    if content is None:
        stored.unfetched.add(kind)
        if existing:
            stored.keep(model, existing.pk)
        return
    model.objects.filter(APP_FILES, name=name, **owner).delete()
    created = model.objects.create(
        name=name, document=ContentFile(content, name=filename), hub_kind=kind, **owner
    )
    stored.keep(model, created.pk)


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


def _remove_dropped_documents(
    driver: Driver, vehicle: Vehicle | None, stored: _Stored
) -> None:
    """Remove the app's files for documents the driver removed in the app.

    Only files the sync stored itself. A kind whose new file could not be
    downloaded keeps its old files, and so do the unmarked older files (whose
    kind is unknown) when any download failed.
    """
    targets: list[tuple[type[Model], dict[str, Any], set[int]]] = [
        (DriverFile, {"driver": driver}, stored.driver_files)
    ]
    if vehicle is not None:
        targets.append((VehicleFile, {"vehicle": vehicle}, stored.vehicle_files))
    for model, owner, kept in targets:
        dropped = model.objects.filter(APP_FILES, **owner).exclude(pk__in=kept)
        if stored.unfetched:
            dropped = dropped.exclude(hub_kind__in=stored.unfetched).exclude(
                hub_kind=""
            )
        for row in dropped:
            logger.info(
                "hub driver %s removed %s in the app: %s %s (%s) deleted",
                driver.hub_driver_id,
                row.hub_kind or "a document",
                model.__name__,
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


def remove_driver(hub_driver_id):
    """The driver left this carrier in the app: keep the record, drop the link."""
    driver = Driver.objects.filter(hub_driver_id=_int(hub_driver_id)).first()
    if not driver:
        return None
    registry.forget(hub_driver_ids=[driver.hub_driver_id])
    driver.hub_driver_id = None
    driver.hub_invite_status = "left"
    driver.hub_invite_id = None
    driver.save(update_fields=["hub_driver_id", "hub_invite_status", "hub_invite_id"])
    registry.forget(driver.email)
    return driver
