"""HTTP client for the internal **billing service** (FastAPI + Stripe).

The billing service owns cards, charges and monthly recurring billing. This backend is a
proxy: the frontend calls our ``/api/charge/*`` endpoints, and those call the billing service
server-side with an API key. The frontend never talks to billing directly.

One billing "project" == this backend (one API key, in settings.BILLING_API_KEY). Within it a
payer is identified by ``external_user_id`` — we use the paying company's id.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 10


class BillingError(Exception):
    """A non-2xx response (or transport failure) from the billing service."""

    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"billing {status_code}: {detail}")


class BillingClient:
    def __init__(self, base_url=None, api_key=None):
        self.base_url = (base_url or settings.BILLING_BASE_URL).rstrip("/")
        self.api_key = api_key or settings.BILLING_API_KEY

    # -- low level ---------------------------------------------------------

    def _request(self, method, path, *, params=None, json=None):
        if not self.api_key:
            raise BillingError(503, "BILLING_API_KEY is not configured")
        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(
                method,
                url,
                params=params,
                json=json,
                headers={"X-API-Key": self.api_key},
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.warning("billing %s %s unreachable: %s", method, path, exc)
            raise BillingError(502, "billing service unreachable") from exc

        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            logger.warning("billing %s %s -> %s: %s", method, path, resp.status_code, detail)
            raise BillingError(resp.status_code, detail)

        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _get(self, path, **params):
        return self._request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def _post(self, path, json=None):
        return self._request("POST", path, json=json)

    def _delete(self, path):
        return self._request("DELETE", path)

    # -- payer reads -----------------------------------------------------

    def summary(self, eid):
        return self._get(f"/v1/users/{eid}/summary")

    def upcoming(self, eid):
        return self._get(f"/v1/users/{eid}/upcoming")

    def transactions(self, eid, *, type=None, period=None, limit=50, offset=0):
        return self._get(
            "/v1/transactions",
            external_user_id=eid, type=type, period=period, limit=limit, offset=offset,
        )

    def recurring(self, eid, *, status=None):
        return self._get("/v1/recurring", external_user_id=eid, status=status)

    def charges(self, eid, *, status=None, period=None, limit=50):
        return self._get(
            "/v1/charges", external_user_id=eid, status=status, period=period, limit=limit
        )

    # -- cards ----------------------------------------------------------

    def list_cards(self, eid):
        return self._get(f"/v1/users/{eid}/cards")

    def create_card_setup_session(self, eid, *, return_url, email=None, name=None, client_reference=None):
        return self._post(
            "/v1/card-setup-sessions",
            {
                "external_user_id": eid,
                "return_url": return_url,
                "email": email,
                "name": name,
                "client_reference": client_reference,
            },
        )

    def get_card_setup_session(self, session_id):
        return self._get(f"/v1/card-setup-sessions/{session_id}")

    def set_default_card(self, eid, card_id):
        return self._post(f"/v1/users/{eid}/cards/{card_id}/default")

    def delete_card(self, eid, card_id):
        return self._delete(f"/v1/users/{eid}/cards/{card_id}")


client = BillingClient()
