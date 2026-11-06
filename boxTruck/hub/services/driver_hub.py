"""HTTP client for the Driver Hub (btm-driver-app), tenant side.

Every call goes to ``{DRIVER_HUB_URL}/t/v1/...`` with the deployment's service
token as a bearer token. Callers that must not fail because the hub is down
(bid relays, payouts, blacklist and name pushes, the registration lookup)
catch DriverHubError and log it; the invite endpoint lets it through so the
dispatcher sees why nothing happened.
"""

import logging
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

logger = logging.getLogger(__name__)

TIMEOUT = 10
# The registration lookup runs inside a page request, so it gives up sooner.
LOOKUP_TIMEOUT = 4
LOOKUP_LIMIT = 500

BID_STATUSES = ("reviewing", "placed", "declined", "won", "lost", "blacklisted")
PAYOUT_STATUSES = ("pending", "hold", "confirmed", "paid")
# The hub's `code` when an invite names an email with no driver-app account.
NOT_REGISTERED = "not_registered"


class DriverHubError(Exception):
    def __init__(self, status_code: int, detail: Any, code: str = "") -> None:
        self.status_code = status_code
        self.detail = detail
        self.code = code
        super().__init__(f"driver hub {status_code}: {detail}")


def configured():
    return bool(settings.DRIVER_HUB_URL and settings.DRIVER_HUB_TOKEN)


def _request(method, path, json=None, timeout: float = TIMEOUT):
    if not configured():
        raise DriverHubError(
            503, "DRIVER_HUB_URL and DRIVER_HUB_TOKEN are not configured"
        )
    url = f"{settings.DRIVER_HUB_URL.rstrip('/')}{path}"
    try:
        resp = requests.request(
            method,
            url,
            json=json,
            headers={"Authorization": f"Bearer {settings.DRIVER_HUB_TOKEN}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.warning("driver hub %s %s unreachable: %s", method, path, exc)
        raise DriverHubError(502, "driver hub unreachable") from exc

    if resp.status_code >= 400:
        code = ""
        try:
            reply = resp.json()
            detail = reply.get("error", resp.text)
            code = str(reply.get("code") or "")
        except (ValueError, AttributeError):
            detail = resp.text
        logger.warning(
            "driver hub %s %s -> %s: %s", method, path, resp.status_code, detail
        )
        raise DriverHubError(resp.status_code, detail, code)

    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


def invite(email: str, invited_by: str) -> dict[str, Any] | None:
    """POST /t/v1/invites -> {invite_id, status, hub_driver_id}.

    Only for an email with a driver-app account: any other is a 409 with
    code `not_registered` (DriverHubError.code).
    """
    return _request(
        "POST", "/t/v1/invites", json={"email": email, "invited_by": invited_by}
    )


def lookup_drivers(
    emails: list[str], hub_driver_ids: list[int] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """POST /t/v1/drivers/lookup, both lists in one request (batches of LOOKUP_LIMIT).

    Answers {"drivers": one row per email, "members": one row per hub driver
    id}, each in the order asked:
      drivers: {email, registered, hub_driver_id, status}
      members: {hub_driver_id, exists, status}; exists false: account deleted
        (or never existed), with status none
    status is active, invited, declined, left or none (towards this carrier).
    """
    ids = [int(i) for i in hub_driver_ids or [] if int(i) > 0]
    answer: dict[str, list[dict[str, Any]]] = {"drivers": [], "members": []}
    # Nothing to ask is no request: the hub answers 400 to two empty lists.
    for start in range(0, max(len(emails), len(ids)), LOOKUP_LIMIT):
        reply = _request(
            "POST",
            "/t/v1/drivers/lookup",
            json={
                "emails": emails[start : start + LOOKUP_LIMIT],
                "hub_driver_ids": ids[start : start + LOOKUP_LIMIT],
            },
            timeout=LOOKUP_TIMEOUT,
        )
        for key in answer:
            rows = reply.get(key) if isinstance(reply, dict) else None
            if isinstance(rows, list):
                answer[key].extend(r for r in rows if isinstance(r, dict))
    return answer


def invite_status(invite_id):
    """GET /t/v1/invites/{id} -> {status: pending, accepted, declined}."""
    return _request("GET", f"/t/v1/invites/{int(invite_id)}")


def link_driver(hub_driver_id, tenant_driver_id):
    return _request(
        "POST",
        f"/t/v1/drivers/{int(hub_driver_id)}/link",
        json={"tenant_driver_id": int(tenant_driver_id)},
    )


def update_bid_target(
    target_id,
    status,
    carrier_amount=None,
    driver_amount=None,
    placed_by=None,
    tenant_bid_id=None,
    note=None,
):
    """POST /t/v1/bid-targets/{id}/status. The hub notifies the driver on each change."""
    if status not in BID_STATUSES:
        raise ValueError(f"unknown bid target status {status!r}")
    body = {"status": status}
    if carrier_amount is not None:
        body["carrier_amount"] = float(carrier_amount)
    if driver_amount is not None:
        body["driver_amount"] = float(driver_amount)
    if placed_by:
        body["placed_by"] = str(placed_by)
    if tenant_bid_id:
        body["tenant_bid_id"] = str(tenant_bid_id)
    if note:
        body["note"] = str(note)
    return _request("POST", f"/t/v1/bid-targets/{int(target_id)}/status", json=body)


def upsert_payout(body: dict[str, Any]) -> dict[str, Any] | None:
    """POST /t/v1/payouts, upsert by external_id. See hub.payouts.payout_body."""
    if body.get("status") not in PAYOUT_STATUSES:
        raise ValueError(f"unknown payout status {body.get('status')!r}")
    return _request("POST", "/t/v1/payouts", json=body)


def delete_payout(external_id: str) -> None:
    """DELETE /t/v1/payouts/{external_id}. One the hub never had is not an error."""
    try:
        _request("DELETE", f"/t/v1/payouts/{quote(str(external_id), safe='')}")
    except DriverHubError as exc:
        if exc.status_code != 404:
            raise


def get_tenant() -> dict[str, Any] | None:
    """GET /t/v1/tenant -> this carrier as the hub knows it, with its name."""
    return _request("GET", "/t/v1/tenant")


def update_tenant(name: str) -> dict[str, Any] | None:
    """PUT /t/v1/tenant: the carrier name the driver app shows."""
    return _request("PUT", "/t/v1/tenant", json={"name": name})


def replace_blacklist(brokers):
    """PUT /t/v1/blacklist with the full list: [{name, email, mc}]."""
    rows = [
        {
            "name": str(b.get("name") or ""),
            "email": str(b.get("email") or ""),
            "mc": str(b.get("mc") or ""),
        }
        for b in brokers
    ]
    return _request("PUT", "/t/v1/blacklist", json={"brokers": rows})


def rfc3339(value: date | datetime | str | None) -> str | None:
    """RFC 3339 for the hub: a date is UTC midnight, a datetime keeps its offset.

    date(2026, 10, 26) -> "2026-10-26T00:00:00Z"; an aware datetime ->
    "2026-10-26T14:30:00-05:00" (a naive one is read in TIME_ZONE). A string is
    parsed the same way, and passed through unchanged if it is neither.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = parse_date(text) if len(text) == 10 else parse_datetime(text)
        except ValueError:
            parsed = None
        if parsed is None:
            return text
        value = parsed
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            value = timezone.make_aware(value)
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return f"{value.isoformat()}T00:00:00Z"
    return str(value)
