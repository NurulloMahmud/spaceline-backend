"""Invite-form leads: dealt to the lead team in turn, posted to Telegram, announced in the app.

Rotation: when the invite form CREATES a driver, the lead goes to the next member of the
lead team by `position` after the one who got the last lead (`LeadSettings.last_position`),
then back to the top. New members join the end of the line. The LeadSettings row is locked
while the next member is picked, so two drivers submitting at once get two people.

After the assignment, a background thread posts the lead to the Telegram group (helpers in
hiring/telegram.py) and sends a `lead_assigned` event through the email agent's event stream
(the same route hiring/live.py uses). Neither can fail or slow the form submit.

Roles: admins are the Management, Developer and Dispatch manager departments. A team member
who is not an admin sees and handles only the leads assigned to them.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import re
import sys
import threading
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from decouple import config
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from users.models import CustomUser
from users.permissions import department_name

from . import board as hiring_board
from . import live
from .models import (
    Driver,
    DriverFile,
    LeadAssignment,
    LeadPost,
    LeadSettings,
    LeadTeamMember,
    VehicleFile,
)

logger = logging.getLogger("hiring.leads")

ADMIN_DEPARTMENTS = ("management", "developer", "dispatch manager")
CENTRAL = ZoneInfo("America/Chicago")
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
CHAT_ID_RE = re.compile(r"^-?\d{5,20}$")
EVENT_TYPE = "lead_assigned"
TEST_MESSAGE = (
    "<b>Test message from Box Truck Manage.</b>\n"
    "New leads from the invite form will be posted here."
)
CARGO_PHOTO = "cargo space photo"
LEFT_PHOTO = "photo left side"
RIGHT_PHOTO = "photo right side"
MASK_BULLETS = 22


# --- roles ------------------------------------------------------------------


def is_admin(user: Any) -> bool:
    return bool(
        user and user.is_authenticated and department_name(user) in ADMIN_DEPARTMENTS
    )


def on_team(user: Any) -> bool:
    return bool(
        user
        and user.is_authenticated
        and LeadTeamMember.objects.filter(user_id=user.pk).exists()
    )


def team_only(user: Any) -> bool:
    """A team member who is not an admin: sees and handles only their own leads."""
    return on_team(user) and not is_admin(user)


def can_touch(user: Any, driver: Driver) -> bool:
    """Whether the user may move or delete this driver's card."""
    return not team_only(user) or driver.assigned_to_id == user.pk


def viewer(user: Any) -> dict[str, Any]:
    return {
        "user_id": user.pk if user and user.is_authenticated else None,
        "is_admin": is_admin(user),
        "on_team": on_team(user),
    }


def admin_users() -> QuerySet[CustomUser]:
    q = Q(pk__in=[])
    for name in ADMIN_DEPARTMENTS:
        q |= Q(department__name__iexact=name)
    return CustomUser.objects.filter(q, is_active=True)


def person(user: CustomUser | None) -> dict[str, Any] | None:
    return hiring_board._person(user)


# --- lead column count --------------------------------------------------------


def lead_column_q() -> Q | None:
    """Drivers in the board's Lead column (see board.column_of)."""
    pending = hiring_board.status_named("Pending")
    if pending is None:
        return None
    return Q(status=pending) & ~Q(hiring_stage__in=("called", "ready"))


def lead_count(user: Any) -> dict[str, Any]:
    if is_admin(user):
        scope = "all"
    elif on_team(user):
        scope = "mine"
    else:
        return {"count": 0, "scope": "none"}
    q = lead_column_q()
    if q is None:
        return {"count": 0, "scope": scope}
    qs = Driver.objects.filter(q)
    if scope == "mine":
        qs = qs.filter(assigned_to=user)
    return {"count": qs.count(), "scope": scope}


# --- rotation -----------------------------------------------------------------


def rotation_members() -> list[LeadTeamMember]:
    """Members who can get leads, in turn order. A deactivated account is skipped."""
    return list(
        LeadTeamMember.objects.select_related("user", "user__department")
        .filter(user__is_active=True)
        .order_by("position", "id")
    )


def next_member(
    last_position: int, members: list[LeadTeamMember] | None = None
) -> LeadTeamMember | None:
    members = rotation_members() if members is None else members
    for m in members:
        if m.position > last_position:
            return m
    return members[0] if members else None


def assign_by_rotation(driver_id: int) -> CustomUser | None:
    """Gives the driver to the next member in turn. None for an empty team."""
    LeadSettings.get()  # the row has to exist before it can be locked
    with transaction.atomic():
        row = LeadSettings.objects.select_for_update().get(pk=1)
        driver = Driver.objects.select_for_update().filter(pk=driver_id).first()
        if driver is None:
            return None
        if driver.assigned_to_id:
            return driver.assigned_to
        member = next_member(row.last_position)
        if member is None:
            return None
        driver.assigned_to = member.user
        driver.assigned_at = timezone.now()
        driver.save(update_fields=["assigned_to", "assigned_at", "updated_at"])
        LeadAssignment.objects.create(
            driver=driver, user=member.user, by=None, reason=LeadAssignment.ROTATION
        )
        row.last_position = member.position
        row.save(update_fields=["last_position"])
        return member.user


def reassign(driver: Driver, user: CustomUser, by: CustomUser) -> Driver:
    """An admin gives the lead to someone on the team. No Telegram post; the
    rotation is not affected. The new person gets the in-app announcement."""
    with transaction.atomic():
        driver.assigned_to = user
        driver.assigned_at = timezone.now()
        driver.save(update_fields=["assigned_to", "assigned_at", "updated_at"])
        LeadAssignment.objects.create(
            driver=driver, user=user, by=by, reason=LeadAssignment.MANUAL
        )
        driver_id, user_id = driver.pk, user.pk
        transaction.on_commit(
            lambda: in_background(send_event, driver_id, "reassigned", [user_id]),
            robust=True,
        )
    return driver


def lead_submitted(driver_id: int) -> None:
    """Called inside the invite submit's transaction for a NEW driver."""
    transaction.on_commit(lambda: new_lead(driver_id), robust=True)


def new_lead(driver_id: int) -> None:
    try:
        assign_by_rotation(driver_id)
    except Exception:  # noqa: BLE001 - the driver is saved; the lead stays unassigned
        logger.exception("Lead %s was not assigned", driver_id)
    in_background(announce_new_lead, driver_id)


def announce_new_lead(driver_id: int) -> None:
    driver = Driver.objects.filter(pk=driver_id).only("assigned_to_id").first()
    if driver is None:
        return
    ids = [driver.assigned_to_id] if driver.assigned_to_id else []
    ids += list(admin_users().values_list("id", flat=True))
    send_event(driver_id, "new", ids)
    post_lead(driver_id)


# --- background ---------------------------------------------------------------


def _inline() -> bool:
    # The test runner runs the background work in place, so tests can see it.
    return "test" in sys.argv[1:2]


def in_background(fn: Callable[..., Any], *args: Any) -> None:
    """Runs fn on a daemon thread; nothing it does reaches the caller."""
    if _inline():
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            logger.exception("Lead background work failed")
        return

    def run() -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            logger.exception("Lead background work failed")
        finally:
            connection.close()

    threading.Thread(target=run, name="hiring-leads", daemon=True).start()


# --- live event -------------------------------------------------------------------


def _license(driver: Driver) -> DriverFile | None:
    return (
        DriverFile.objects.filter(driver=driver)
        .filter(Q(name__iexact=hiring_board.LICENSE_NAME) | Q(hub_kind="license"))
        .order_by("-created_at", "-id")
        .first()
    )


def link_base() -> str:
    return str(config("LEAD_LINK_BASE", default="https://spaceline.boxtruckmanage.com")).rstrip("/")


def card_link(driver_id: int) -> str:
    return f"{link_base()}/hiring?driver={driver_id}"


def _is_pdf(name: str, data: bytes = b"") -> bool:
    return name.lower().endswith(".pdf") or data[:5] == b"%PDF-"


def lead_payload(driver: Driver) -> dict[str, Any]:
    lic = _license(driver)
    url = hiring_board._file_url(lic) if lic else None
    if url and url.startswith("/"):
        url = link_base() + url
    return {
        "id": driver.pk,
        "full_name": driver.full_name,
        "phone_number": driver.phone_number,
        "city": driver.city,
        "state": driver.state,
        "cdl_url": url,
        "cdl_is_pdf": bool(lic and lic.document and _is_pdf(lic.document.name)),
        "assigned_to": person(driver.assigned_to),
    }


def send_event(driver_id: int, reason: str, user_ids: list[int]) -> None:
    """Posts `lead_assigned` to the email agent for these users. Never raises."""
    if not live.enabled():
        return
    ids = list(dict.fromkeys(int(i) for i in user_ids if i))
    if not ids:
        return
    try:
        driver = Driver.objects.select_related("assigned_to").filter(pk=driver_id).first()
        if driver is None:
            return
        live.post_event(
            EVENT_TYPE, {"user_ids": ids, "reason": reason, "lead": lead_payload(driver)}
        )
    except Exception as exc:  # noqa: BLE001 - the Hiring badge still refreshes every minute
        logger.warning("Lead event for driver %s not sent: %s", driver_id, exc)


# --- bot token ------------------------------------------------------------------


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def bot_token(row: LeadSettings | None = None) -> str:
    row = row or LeadSettings.get()
    if not row.bot_token_enc:
        return ""
    try:
        return _fernet().decrypt(row.bot_token_enc.encode()).decode()
    except (InvalidToken, ValueError):
        logger.error("The saved Telegram bot token cannot be decrypted (SECRET_KEY changed?)")
        return ""


def masked(token: str) -> str:
    bot_id, _, secret = token.partition(":")
    return f"{bot_id}:{secret[:4]}" + "•" * MASK_BULLETS + secret[-3:]


def bot_json(row: LeadSettings) -> dict[str, Any]:
    token = bot_token(row)
    return {
        "configured": bool(token),
        "username": row.bot_username if token else "",
        "masked": masked(token) if token else "",
        "saved_at": row.token_saved_at if token else None,
        "saved_by": person(row.token_saved_by) if token else None,
    }


def chat_json(row: LeadSettings) -> dict[str, Any]:
    return {
        "chat_id": row.chat_id,
        "chat_title": row.chat_title,
        "connected": bool(row.chat_id and row.chat_title and row.bot_token_enc),
    }


# --- Telegram post --------------------------------------------------------------


def _read(field: Any) -> tuple[bytes, str] | None:
    if not field or not field.name:
        return None
    try:
        with field.storage.open(field.name, "rb") as fh:
            data = fh.read()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lead file %s could not be read: %s", field.name, exc)
        return None
    return data, field.name.rsplit("/", 1)[-1]


def _picture(field: Any) -> tuple[bytes | None, bool]:
    """(JPEG bytes or None, whether the file was a PDF)."""
    from . import telegram

    got = _read(field)
    if got is None:
        return None, False
    data, name = got
    try:
        photo = telegram.to_photo(data, name, mimetypes.guess_type(name)[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lead file %s could not be turned into a picture: %s", name, exc)
        photo = None
    return photo, _is_pdf(name, data)


def collect_photos(driver: Driver) -> tuple[list[bytes], list[str]]:
    """Up to three pictures (CDL, cargo space, left or right side) and the caption notes."""
    photos: list[bytes] = []
    notes: list[str] = []

    lic = _license(driver)
    if lic is None or not lic.document:
        notes.append("No CDL uploaded")
    else:
        photo, pdf = _picture(lic.document)
        if photo is None:
            notes.append("CDL could not be shown")
        else:
            photos.append(photo)
            if pdf:
                notes.append("CDL came as a PDF, first page shown")

    files = list(
        VehicleFile.objects.filter(vehicle__driver=driver).order_by("-created_at", "-id")
    )

    def latest(name: str) -> VehicleFile | None:
        return next((f for f in files if str(f.name or "").strip().lower() == name), None)

    def picture(name: str) -> bytes | None:
        f = latest(name)
        return _picture(f.document)[0] if f is not None else None

    cargo = picture(CARGO_PHOTO)
    if cargo is None:
        notes.append("No cargo space photo")
    else:
        photos.append(cargo)

    left = picture(LEFT_PHOTO)
    if left is not None:
        photos.append(left)
    else:
        right = picture(RIGHT_PHOTO)
        if right is not None:
            photos.append(right)
            notes.append("Right side photo (no left side)")
        else:
            notes.append("No side photo")
    return photos, notes


def post_lead(driver_id: int) -> LeadPost | None:
    """Posts the lead to the group and records the outcome. Never raises."""
    from . import telegram

    driver = Driver.objects.select_related("assigned_to").filter(pk=driver_id).first()
    if driver is None:
        return None
    row = LeadSettings.get()
    token = bot_token(row)
    if not token or not row.chat_id:
        return LeadPost.objects.create(
            driver=driver,
            kind=LeadPost.KIND_LEAD,
            status=LeadPost.SKIPPED,
            error="No bot token is saved." if not token else "No group chat ID is saved.",
        )
    photos: list[bytes] = []
    try:
        photos, notes = collect_photos(driver)
        member = (
            LeadTeamMember.objects.filter(user_id=driver.assigned_to_id).first()
            if driver.assigned_to_id
            else None
        )
        assignee = person(driver.assigned_to)
        caption = telegram.build_caption(
            full_name=driver.full_name,
            phone=driver.phone_number or "",
            dispatcher_name=assignee["name"] if assignee else "",
            dispatcher_username=member.telegram_username if member else "",
            link=card_link(driver.pk),
            notes=notes,
        )
        result = telegram.send_lead(token, row.chat_id, photos, caption)
    except telegram.TelegramError as exc:
        return LeadPost.objects.create(
            driver=driver,
            kind=LeadPost.KIND_LEAD,
            status=LeadPost.FAILED,
            error=str(exc) or "Telegram refused the post.",
            photos=len(photos),
            attempts=int(getattr(exc, "attempts", 0) or 0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Lead %s was not posted to Telegram", driver_id)
        return LeadPost.objects.create(
            driver=driver,
            kind=LeadPost.KIND_LEAD,
            status=LeadPost.FAILED,
            error=str(exc) or type(exc).__name__,
            photos=len(photos),
        )
    return LeadPost.objects.create(
        driver=driver,
        kind=LeadPost.KIND_LEAD,
        status=LeadPost.SENT,
        photos=len(photos),
        attempts=int(result.get("attempts") or 1),
        telegram_message_id=str(result.get("message_id") or ""),
    )


def send_test() -> tuple[bool, str]:
    """Sends the test message to the group. (ok, error detail)."""
    from . import telegram

    row = LeadSettings.get()
    token = bot_token(row)
    if not token or not row.chat_id:
        detail = (
            "No bot token is saved. A developer adds it in the Dev console."
            if not token
            else "Save a group chat ID first."
        )
        LeadPost.objects.create(kind=LeadPost.KIND_TEST, status=LeadPost.SKIPPED, error=detail)
        return False, detail
    try:
        message_id = telegram.send_text(token, row.chat_id, TEST_MESSAGE)
    except telegram.TelegramError as exc:
        detail = str(exc) or "Telegram refused the message."
        LeadPost.objects.create(
            kind=LeadPost.KIND_TEST,
            status=LeadPost.FAILED,
            error=detail,
            attempts=int(getattr(exc, "attempts", 0) or 0),
        )
        return False, detail
    LeadPost.objects.create(
        kind=LeadPost.KIND_TEST,
        status=LeadPost.SENT,
        attempts=1,
        telegram_message_id=str(message_id or ""),
    )
    return True, ""


# --- team ---------------------------------------------------------------------------


def clean_username(value: Any) -> str:
    """Strips spaces and a leading @. Raises ValueError with a plain message."""
    name = str(value or "").strip().lstrip("@").strip()
    if name and not USERNAME_RE.match(name):
        raise ValueError(
            "A Telegram username is 5 to 32 letters, digits or underscores and starts with a letter."
        )
    return name


def month_start() -> datetime:
    now = timezone.localtime(timezone.now(), CENTRAL)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_counts(user_ids: list[int]) -> dict[int, int]:
    rows = (
        Driver.objects.filter(assigned_to_id__in=user_ids, assigned_at__gte=month_start())
        .values("assigned_to_id")
        .annotate(n=Count("id"))
    )
    return {r["assigned_to_id"]: r["n"] for r in rows}


def member_json(
    m: LeadTeamMember, counts: dict[int, int] | None = None, next_user_id: int | None = None
) -> dict[str, Any]:
    counts = month_counts([m.user_id]) if counts is None else counts
    dept = m.user.department.name if m.user.department_id else None
    return {
        "user": {**(person(m.user) or {}), "department": dept},
        "telegram_username": m.telegram_username,
        "position": m.position,
        "month_count": counts.get(m.user_id, 0),
        "next": next_user_id is not None and m.user_id == next_user_id,
    }


def team_json() -> dict[str, Any]:
    row = LeadSettings.get()
    members = list(
        LeadTeamMember.objects.select_related("user", "user__department").order_by("position", "id")
    )
    nxt = next_member(row.last_position, [m for m in members if m.user.is_active])
    next_user_id = nxt.user_id if nxt else None
    counts = month_counts([m.user_id for m in members])
    return {
        "members": [member_json(m, counts, next_user_id) for m in members],
        "next_user_id": next_user_id,
        "chat": chat_json(row),
        "bot": {"configured": bool(bot_token(row)), "username": row.bot_username},
    }


def add_member(user: CustomUser, username: str, by: CustomUser) -> LeadTeamMember:
    """Adds the user at the end of the line. Positions only grow, so a new member
    never lands before the rotation pointer, even after the last member left."""
    LeadSettings.get()
    with transaction.atomic():
        row = LeadSettings.objects.select_for_update().get(pk=1)
        top = (
            LeadTeamMember.objects.order_by("-position")
            .values_list("position", flat=True)
            .first()
            or 0
        )
        return LeadTeamMember.objects.create(
            user=user,
            telegram_username=username,
            position=max(top, row.last_position) + 1,
            added_by=by,
        )


# --- page access ----------------------------------------------------------------------

HIRING_PAGE = {"key": "hiring", "name": "Hiring", "path": "/hiring"}


def _hiring_page() -> tuple[int | None, dict[str, str]]:
    from users.models import Page

    page = (
        Page.objects.filter(Q(key__iexact="hiring") | Q(path="/hiring"), is_active=True)
        .order_by("id")
        .first()
    )
    if page is None:
        return None, dict(HIRING_PAGE)
    return page.id, {"key": page.key, "name": page.name, "path": page.path or "/hiring"}


def with_hiring_page(user: Any, rows: list[dict[str, Any]], *, profile: bool = False) -> list[dict[str, Any]]:
    """Page access rows plus the Hiring page (view, edit, delete) while the user is on
    the lead team. Nothing is written: taking them off the team takes it away again.

    `profile` picks the shape: the profile's page_access rows (page_key, page_name)
    or the /users/my-pages/ rows (key, name, path)."""
    if not on_team(user):
        return rows
    page_id, page = _hiring_page()
    key_field = "page_key" if profile else "key"
    rows = [dict(r) for r in rows]
    for r in rows:
        if r.get(key_field) == page["key"]:
            r.update(can_view=True, can_edit=True, can_delete=True)
            return rows
    if profile:
        rows.append(
            {
                "id": None,
                "user": user.pk,
                "username": user.username,
                "page": page_id,
                "page_key": page["key"],
                "page_name": page["name"],
                "can_view": True,
                "can_create": False,
                "can_edit": True,
                "can_delete": True,
            }
        )
    else:
        rows.append({**page, "can_view": True, "can_create": False, "can_edit": True, "can_delete": True})
    return rows
