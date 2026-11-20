import time
import uuid
from datetime import timedelta
from typing import Any
from unittest import mock

import requests
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import APIException
from rest_framework.settings import api_settings as drf_settings
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from users.models import Company, CustomUser, Department
from users.serializers import CustomTokenObtainPairSerializer

from . import mailbox, services, state
from .authentication import DispatchJWTAuthentication
from .models import DispatchDevice

HUB = dict(DRIVER_HUB_URL="https://hub.test", DRIVER_HUB_TOKEN="tenant-token")
HUB_HEADERS = {"HTTP_X_HUB_TOKEN": "tenant-token"}
MONTH = "?month=2026-11"


class FakeRedis:
    """The few Redis commands dispatch_app uses, in memory."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.values[key] = value
        self.ttls[key] = ex if ex is not None else -1
        return True

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def delete(self, *keys: str) -> int:
        gone = 0
        for key in keys:
            gone += int(self.values.pop(key, None) is not None)
            self.ttls.pop(key, None)
        return gone


class BrokenRedis:
    def __getattr__(self, name: str) -> Any:
        def fail(*args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("redis is down")

        return fail


class FakeResponse:
    def __init__(self, status_code: int = 200, body: Any = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.content = b"{}"
        self.text = str(self._body)

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class HubCalls:
    """Stands in for requests.request inside the hub client; records (method, path, json)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.replies: dict[str, FakeResponse] = {}

    def __call__(self, method: str, url: str, json: Any = None, headers: Any = None, timeout: Any = None) -> FakeResponse:
        path = url.replace("https://hub.test", "")
        self.calls.append((method, path, json))
        reply = self.replies.get(path)
        if isinstance(reply, Exception):
            raise reply
        return reply or FakeResponse(200, {"ok": True})

    def paths(self) -> list[str]:
        return [path for _method, path, _json in self.calls]


class DispatchBase(TestCase):
    def setUp(self) -> None:
        self.company = Company.objects.create(name="SPACELINE LLC")
        self.dispatch = Department.objects.create(name="Dispatch")
        self.user = CustomUser.objects.create_user(
            username="dana", password="x", is_active=True, department=self.dispatch,
            first_name="Dana", last_name="Moss", email="dana@example.com",
        )
        self.other = CustomUser.objects.create_user(
            username="omar", password="x", is_active=True, department=self.dispatch,
        )
        self.redis = FakeRedis()
        patches = [
            mock.patch.object(state, "_client", self.redis),
            # Background hub calls run inline so the tests can see them.
            mock.patch("dispatch_app.background._start", lambda target: target()),
            mock.patch("dispatch_app.views.mailbox_for", return_value="dana@spaceline.test"),
        ]
        self.hub = HubCalls()
        patches.append(mock.patch("hub.services.driver_hub.requests.request", self.hub))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = APIClient()

    def web_token(self, user: CustomUser) -> str:
        return str(CustomTokenObtainPairSerializer.get_token(user).access_token)

    def link(self, user: CustomUser | None = None) -> tuple[DispatchDevice, str]:
        with self.captureOnCommitCallbacks(execute=True):
            return services.create_device((user or self.user).id, 9, "ios", "iPhone 15")

    def phone_token(self, device: DispatchDevice) -> str:
        return str(services.mint_access(device.user, device.id))

    def auth(self, token: str) -> None:
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")


class AuthenticationUnchangedTests(DispatchBase):
    """Tokens without dsid: the new class answers exactly as simplejwt's JWTAuthentication."""

    def outcome(self, auth_class: type, header: str | None) -> Any:
        factory = APIRequestFactory()
        extra = {"HTTP_AUTHORIZATION": header} if header is not None else {}
        request = factory.get("/api/dispatch-app/device/", **extra)
        try:
            result = auth_class().authenticate(request)
        except APIException as exc:
            return ("raised", type(exc).__name__, exc.status_code, exc.detail)
        if result is None:
            return None
        user, token = result
        return ("ok", user.pk, dict(token.payload))

    def test_same_outcome_for_every_kind_of_web_token(self) -> None:
        inactive = CustomUser.objects.create_user(username="ivy", password="x", is_active=False)
        expired = AccessToken.for_user(self.user)
        expired.set_exp(from_time=timezone.now() - timedelta(days=3))
        ghost = AccessToken.for_user(self.user)
        ghost["user_id"] = 999999
        no_user = AccessToken.for_user(self.user)
        del no_user["user_id"]
        headers = [
            None,
            "",
            "Basic abc",
            "Bearer",
            "Bearer a b",
            "Bearer not-a-jwt",
            f"Bearer {self.web_token(self.user)}",
            f"Bearer {self.web_token(inactive)}",
            f"Bearer {expired}",
            f"Bearer {ghost}",
            f"Bearer {no_user}",
            f"Bearer {RefreshToken.for_user(self.user)}",
        ]
        for header in headers:
            with self.subTest(header=(header or "")[:20]):
                self.assertEqual(
                    self.outcome(JWTAuthentication, header),
                    self.outcome(DispatchJWTAuthentication, header),
                )

    def test_web_token_does_no_extra_queries(self) -> None:
        header = f"Bearer {self.web_token(self.user)}"
        request = APIRequestFactory().get("/", HTTP_AUTHORIZATION=header)
        with self.assertNumQueries(1):
            DispatchJWTAuthentication().authenticate(request)

    def test_settings_use_the_new_class(self) -> None:
        self.assertEqual(drf_settings.DEFAULT_AUTHENTICATION_CLASSES, [DispatchJWTAuthentication])

    def test_inactive_web_user_still_refused_on_performance(self) -> None:
        self.user.is_active = False
        self.user.save()
        self.auth(self.web_token(self.user))
        reply = self.client.get("/api/salaries/leaderboard/" + MONTH)
        self.assertEqual(reply.status_code, 401)
        self.assertEqual(reply.data["code"], "user_inactive")

    def test_web_token_works(self) -> None:
        self.auth(self.web_token(self.user))
        self.assertEqual(self.client.get("/api/dispatch-app/device/").status_code, 200)
        self.assertEqual(self.client.get("/api/salaries/leaderboard/" + MONTH).status_code, 200)


class PhoneTokenTests(DispatchBase):
    def test_claims_are_the_web_logins_plus_dsid_for_ten_minutes(self) -> None:
        device, _ = self.link()
        web = AccessToken(self.web_token(self.user)).payload
        phone = AccessToken(self.phone_token(device)).payload
        self.assertEqual(set(phone) - set(web), {"dsid"})
        self.assertEqual(set(web) - set(phone), set())
        for claim in ("user_id", "department", "company", "company_id", "token_type"):
            self.assertEqual(phone[claim], web[claim], claim)
        self.assertEqual(phone["dsid"], str(device.id))
        self.assertEqual(phone["company"], "SPACELINE LLC")
        self.assertAlmostEqual(phone["exp"] - phone["iat"], 600, delta=2)

    def test_live_device_passes_and_last_seen_moves_once_a_minute(self) -> None:
        device, _ = self.link()
        self.auth(self.phone_token(device))
        self.assertEqual(self.client.get("/api/dispatch-app/device/").status_code, 200)
        device.refresh_from_db()
        first = device.last_seen_at
        self.assertIsNotNone(first)
        self.assertEqual(self.client.get("/api/salaries/leaderboard/" + MONTH).status_code, 200)
        device.refresh_from_db()
        self.assertEqual(device.last_seen_at, first)
        DispatchDevice.objects.filter(pk=device.pk).update(last_seen_at=first - timedelta(seconds=61))
        self.client.get("/api/dispatch-app/device/")
        device.refresh_from_db()
        self.assertGreater(device.last_seen_at, first)

    def test_revoked_device_is_401_device_revoked(self) -> None:
        device, _ = self.link()
        token = self.phone_token(device)
        services.revoke_user(self.user.id, DispatchDevice.UNLINKED)
        self.auth(token)
        reply = self.client.get("/api/salaries/leaderboard/" + MONTH)
        self.assertEqual(reply.status_code, 401)
        self.assertEqual(reply.data["code"], "device_revoked")

    def test_unknown_or_foreign_dsid_is_401(self) -> None:
        device, _ = self.link()
        stranger = services.mint_access(self.other, device.id)
        bogus = services.mint_access(self.user, uuid.uuid4())
        junk = services.mint_access(self.user, "nope")
        for token in (stranger, bogus, junk):
            self.auth(str(token))
            reply = self.client.get("/api/dispatch-app/device/")
            self.assertEqual(reply.status_code, 401)
            self.assertEqual(reply.data["code"], "device_revoked")

    def test_inactive_user_only_reaches_their_performance(self) -> None:
        device, _ = self.link()
        self.user.is_active = False
        with self.captureOnCommitCallbacks(execute=True):
            self.user.save()
        self.auth(self.phone_token(device))
        self.assertEqual(self.client.get("/api/salaries/leaderboard/" + MONTH).status_code, 200)
        own = self.client.get(f"/api/salaries/leaderboard/{self.user.id}/loads/" + MONTH)
        self.assertEqual(own.status_code, 200)
        for reply in (
            self.client.get(f"/api/salaries/leaderboard/{self.other.id}/loads/" + MONTH),
            self.client.get("/api/dispatch-app/device/"),
            self.client.post("/api/salaries/leaderboard/" + MONTH),
            self.client.get("/api/users/list/"),
        ):
            self.assertEqual(reply.status_code, 403)
            self.assertEqual(reply.data["code"], "user_inactive")


@override_settings(**HUB)
class HubEndpointTests(DispatchBase):
    def create(self, **body: Any) -> Any:
        payload = {"btm_user_id": self.user.id, "hub_workspace_id": 9, "platform": "ios", "device_label": "iPhone 15"}
        payload.update(body)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post("/api/dispatch-app/devices/", payload, format="json", **HUB_HEADERS)

    def test_needs_the_hub_token(self) -> None:
        for headers in ({}, {"HTTP_X_HUB_TOKEN": "wrong"}):
            for path in ("devices/", "session/", "devices/revoke/"):
                reply = self.client.post(f"/api/dispatch-app/{path}", {}, format="json", **headers)
                self.assertEqual(reply.status_code, 403, path)
        with override_settings(DRIVER_HUB_TOKEN=""):
            reply = self.client.post("/api/dispatch-app/devices/", {}, format="json", HTTP_X_HUB_TOKEN="")
            self.assertEqual(reply.status_code, 403)

    def test_create_device(self) -> None:
        reply = self.create()
        self.assertEqual(reply.status_code, 201)
        token = reply.data["device_token"]
        self.assertTrue(token.startswith("dsp_"))
        self.assertEqual(len(token), 44)
        device = DispatchDevice.objects.get(pk=reply.data["device_id"])
        self.assertEqual(device.token_hash, services.hash_token(token))
        self.assertNotIn(token, device.token_hash)
        self.assertEqual((device.platform, device.device_label, device.hub_workspace_id), ("ios", "iPhone 15", "9"))
        self.assertEqual(
            reply.data["user"],
            {"id": self.user.id, "first_name": "Dana", "last_name": "Moss", "email": "dana@example.com", "is_active": True},
        )
        self.assertEqual(reply.data["mailbox"], "dana@spaceline.test")
        self.assertEqual(self.redis.get(f"dispatch:dsid:{device.id}"), "active")
        self.assertEqual(self.redis.ttls[f"dispatch:dsid:{device.id}"], 1800)

    def test_a_new_device_replaces_the_old_one(self) -> None:
        first = self.create().data
        second = self.create(device_label="Pixel 9").data
        old = DispatchDevice.objects.get(pk=first["device_id"])
        self.assertEqual(old.revoked_reason, "replaced")
        self.assertIsNotNone(old.revoked_at)
        self.assertIsNone(self.redis.get(f"dispatch:dsid:{old.id}"))
        self.assertEqual(self.redis.get(f"dispatch:dsid:{second['device_id']}"), "active")
        self.assertEqual(DispatchDevice.objects.filter(user=self.user, revoked_at__isnull=True).count(), 1)
        reply = self.client.post("/api/dispatch-app/session/", {"device_token": first["device_token"]}, format="json", **HUB_HEADERS)
        self.assertEqual(reply.status_code, 401)
        self.assertEqual(reply.data, {"error": "This phone is no longer linked.", "code": "device_revoked"})
        # Replacing never calls the hub: the hub asked for the device and applies the rule itself.
        self.assertEqual(self.hub.calls, [])

    def test_unknown_and_inactive_users(self) -> None:
        reply = self.create(btm_user_id=999999)
        self.assertEqual((reply.status_code, reply.data["code"]), (404, "user_not_found"))
        reply = self.create(btm_user_id="x")
        self.assertEqual((reply.status_code, reply.data["code"]), (404, "user_not_found"))
        self.user.is_active = False
        self.user.save()
        reply = self.create()
        self.assertEqual((reply.status_code, reply.data["code"]), (409, "user_inactive"))
        self.assertFalse(DispatchDevice.objects.exists())

    def test_session_exchange(self) -> None:
        created = self.create().data
        self.redis.ttls[f"dispatch:dsid:{created['device_id']}"] = 5
        reply = self.client.post("/api/dispatch-app/session/", {"device_token": created["device_token"]}, format="json", **HUB_HEADERS)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(set(reply.data), {"access", "expires_at", "user", "mailbox"})
        self.assertRegex(reply.data["expires_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(reply.data["mailbox"], "dana@spaceline.test")
        self.assertTrue(reply.data["user"]["is_active"])
        access = AccessToken(reply.data["access"])
        self.assertEqual(access["dsid"], created["device_id"])
        self.assertEqual(access["user_id"], self.user.id)
        self.assertEqual(self.redis.ttls[f"dispatch:dsid:{created['device_id']}"], 1800)
        self.assertIsNotNone(DispatchDevice.objects.get(pk=created["device_id"]).last_seen_at)
        # The token works on Django.
        self.auth(reply.data["access"])
        self.assertEqual(self.client.get("/api/dispatch-app/device/").status_code, 200)

    def test_session_for_an_inactive_user_says_so(self) -> None:
        created = self.create().data
        CustomUser.objects.filter(pk=self.user.pk).update(is_active=False)
        reply = self.client.post("/api/dispatch-app/session/", {"device_token": created["device_token"]}, format="json", **HUB_HEADERS)
        self.assertEqual(reply.status_code, 200)
        self.assertFalse(reply.data["user"]["is_active"])
        self.assertEqual(self.redis.get(f"dispatch:dsid:{created['device_id']}"), "inactive")

    def test_session_with_bad_tokens(self) -> None:
        for body in ({}, {"device_token": ""}, {"device_token": "dsp_nope"}, {"device_token": 5}):
            reply = self.client.post("/api/dispatch-app/session/", body, format="json", **HUB_HEADERS)
            self.assertEqual((reply.status_code, reply.data["code"]), (401, "device_revoked"))

    def test_revoke(self) -> None:
        created = self.create().data
        device = DispatchDevice.objects.get(pk=created["device_id"])
        phone = self.phone_token(device)
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.client.post("/api/dispatch-app/devices/revoke/", {"device_token": created["device_token"]}, format="json", **HUB_HEADERS)
        self.assertEqual((reply.status_code, reply.data), (200, {"revoked": True}))
        device.refresh_from_db()
        self.assertEqual(device.revoked_reason, "hub")
        self.assertIsNone(self.redis.get(f"dispatch:dsid:{device.id}"))
        again = self.client.post("/api/dispatch-app/devices/revoke/", {"device_token": created["device_token"]}, format="json", **HUB_HEADERS)
        self.assertEqual((again.status_code, again.data), (200, {"revoked": False}))
        unknown = self.client.post("/api/dispatch-app/devices/revoke/", {}, format="json", **HUB_HEADERS)
        self.assertEqual(unknown.status_code, 200)
        self.auth(phone)
        self.assertEqual(self.client.get("/api/dispatch-app/device/").status_code, 401)
        self.assertEqual(self.hub.calls, [])

    def test_redis_down_never_breaks_linking(self) -> None:
        with mock.patch.object(state, "_client", BrokenRedis()):
            created = self.create()
            self.assertEqual(created.status_code, 201)
            reply = self.client.post("/api/dispatch-app/session/", {"device_token": created.data["device_token"]}, format="json", **HUB_HEADERS)
            self.assertEqual(reply.status_code, 200)


class WebEndpointTests(DispatchBase):
    def setUp(self) -> None:
        super().setUp()
        self.auth(self.web_token(self.user))

    def test_link_code_needs_the_hub(self) -> None:
        with override_settings(DRIVER_HUB_TOKEN=""):
            reply = self.client.post("/api/dispatch-app/link-code/")
        self.assertEqual((reply.status_code, reply.data["code"]), (503, "hub_not_configured"))

    @override_settings(**HUB)
    def test_link_code(self) -> None:
        self.hub.replies["/t/v1/dispatch/link-codes"] = FakeResponse(
            201, {"code": "K7M2QX", "qr_text": "https://drivers.boxtruckmanage.com/link/K7M2QX", "expires_at": "2026-11-21T10:02:00Z"}
        )
        reply = self.client.post("/api/dispatch-app/link-code/")
        self.assertEqual(reply.status_code, 201)
        self.assertEqual(
            reply.data,
            {"code": "K7M2QX", "qr_text": "https://drivers.boxtruckmanage.com/link/K7M2QX", "expires_at": "2026-11-21T10:02:00Z"},
        )
        self.assertEqual(
            self.hub.calls,
            [("POST", "/t/v1/dispatch/link-codes", {"btm_user_id": self.user.id, "first_name": "Dana", "last_name": "Moss", "email": "dana@example.com", "active": True})],
        )

    @override_settings(**HUB)
    def test_link_code_hub_errors(self) -> None:
        self.hub.replies["/t/v1/dispatch/link-codes"] = FakeResponse(409, {"error": "inactive", "code": "user_inactive"})
        reply = self.client.post("/api/dispatch-app/link-code/")
        self.assertEqual((reply.status_code, reply.data["code"]), (409, "user_inactive"))
        self.hub.replies["/t/v1/dispatch/link-codes"] = FakeResponse(500, {"error": "boom"})
        reply = self.client.post("/api/dispatch-app/link-code/")
        self.assertEqual((reply.status_code, reply.data["code"]), (502, "hub_unreachable"))
        self.hub.replies["/t/v1/dispatch/link-codes"] = requests.ConnectionError("down")
        reply = self.client.post("/api/dispatch-app/link-code/")
        self.assertEqual((reply.status_code, reply.data["code"]), (502, "hub_unreachable"))

    def test_device_none_then_linked(self) -> None:
        self.assertEqual(self.client.get("/api/dispatch-app/device/").data, {"device": None})
        device, _ = self.link()
        data = self.client.get("/api/dispatch-app/device/").data["device"]
        self.assertEqual(set(data), {"platform", "device_label", "linked_at", "last_seen_at"})
        self.assertEqual((data["platform"], data["device_label"]), ("ios", "iPhone 15"))

    @override_settings(**HUB)
    def test_unlink(self) -> None:
        device, _ = self.link()
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.client.delete("/api/dispatch-app/device/")
        self.assertEqual((reply.status_code, reply.data), (200, {"ok": True}))
        device.refresh_from_db()
        self.assertEqual(device.revoked_reason, "unlinked")
        self.assertIsNone(self.redis.get(f"dispatch:dsid:{device.id}"))
        self.assertEqual(self.hub.calls, [("POST", "/t/v1/dispatch/unlink", {"btm_user_id": self.user.id})])
        self.assertEqual(self.client.get("/api/dispatch-app/device/").data, {"device": None})

    @override_settings(**HUB)
    def test_unlink_when_the_hub_is_down(self) -> None:
        device, _ = self.link()
        self.hub.replies["/t/v1/dispatch/unlink"] = requests.ConnectionError("down")
        reply = self.client.delete("/api/dispatch-app/device/")
        self.assertEqual((reply.status_code, reply.data), (200, {"ok": True}))
        device.refresh_from_db()
        self.assertEqual(device.revoked_reason, "unlinked")


@override_settings(**HUB)
class UserChangeTests(DispatchBase):
    def test_deactivate_and_reactivate(self) -> None:
        device, _ = self.link()
        self.user.is_active = False
        with self.captureOnCommitCallbacks(execute=True):
            self.user.save()
        self.assertEqual(self.redis.get(f"dispatch:dsid:{device.id}"), "inactive")
        self.user.is_active = True
        with self.captureOnCommitCallbacks(execute=True):
            self.user.save()
        self.assertEqual(self.redis.get(f"dispatch:dsid:{device.id}"), "active")
        self.assertEqual(
            self.hub.calls,
            [
                ("POST", "/t/v1/dispatch/users/status", {"btm_user_id": self.user.id, "active": False}),
                ("POST", "/t/v1/dispatch/users/status", {"btm_user_id": self.user.id, "active": True}),
            ],
        )

    def test_other_saves_and_unlinked_users_call_nothing(self) -> None:
        self.link()
        with self.captureOnCommitCallbacks(execute=True):
            self.user.first_name = "Dina"
            self.user.save()
            self.user.save(update_fields=["first_name"])
            self.other.is_active = False
            self.other.save()
        self.assertEqual(self.hub.calls, [])

    def test_delete_user(self) -> None:
        device, _ = self.link()
        user_id = self.user.id
        with self.captureOnCommitCallbacks(execute=True):
            self.user.delete()
        device.refresh_from_db()
        self.assertEqual(device.revoked_reason, "user_deleted")
        self.assertIsNone(device.user_id)
        self.assertIsNone(self.redis.get(f"dispatch:dsid:{device.id}"))
        self.assertEqual(self.hub.calls, [("POST", "/t/v1/dispatch/unlink", {"btm_user_id": user_id})])


@override_settings(**HUB)
class NotifyTests(DispatchBase):
    # users.permissions.IsInternalService reads config.settings directly, so no override.
    def post(self, secret: str | None = None, **body: Any) -> Any:
        secret = settings.INTERNAL_SERVICE_SECRET if secret is None else secret
        payload = {
            "btm_user_id": self.user.id,
            "kind": "broker_reply",
            "title": "TQL replied",
            "body": "Can you do 1,150?",
            "data": {"thread_id": "abc", "load_id": "L1"},
        }
        payload.update(body)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post("/api/dispatch-app/internal/notify/", payload, format="json", HTTP_X_INTERNAL_SECRET=secret)

    def test_needs_the_internal_secret(self) -> None:
        self.assertEqual(self.post(secret="wrong").status_code, 403)

    def test_skipped_when_nobody_is_linked(self) -> None:
        reply = self.post()
        self.assertEqual((reply.status_code, reply.data), (200, {"queued": False}))
        self.assertEqual(self.hub.calls, [])

    def test_sent_to_the_hub(self) -> None:
        self.link()
        reply = self.post()
        self.assertEqual(reply.data, {"queued": True})
        self.assertEqual(
            self.hub.calls,
            [("POST", "/t/v1/dispatch/notify", {
                "btm_user_id": self.user.id, "kind": "broker_reply", "title": "TQL replied",
                "body": "Can you do 1,150?", "data": {"thread_id": "abc", "load_id": "L1"},
            })],
        )

    def test_bad_kind_and_inactive_user(self) -> None:
        self.link()
        self.assertEqual(self.post(kind="driver_bid").status_code, 400)
        CustomUser.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.post().data, {"queued": False})
        self.assertEqual(self.hub.calls, [])

    def test_hub_failure_never_reaches_the_caller(self) -> None:
        self.link()
        self.hub.replies["/t/v1/dispatch/notify"] = requests.ConnectionError("down")
        self.assertEqual(self.post(kind="bid_won").data, {"queued": True})
        self.hub.replies["/t/v1/dispatch/notify"] = FakeResponse(500, {"error": "boom"})
        self.assertTrue(services.notify(self.user.id, "bid_booked", "Load booked", "Chicago, IL to Indianapolis, IN", {"load_id": "L1"}))

    def test_notify_uses_a_short_timeout(self) -> None:
        self.link()
        seen: list = []

        def record(method: str, url: str, json: Any = None, headers: Any = None, timeout: Any = None) -> FakeResponse:
            seen.append(timeout)
            return FakeResponse(200, {"delivered": True})

        with mock.patch("hub.services.driver_hub.requests.request", record):
            self.post()
        self.assertEqual(seen, [3])


@override_settings(EMAIL_AGENT_INTERNAL_URL="http://agent.test", INTERNAL_SERVICE_SECRET="internal-secret")
class MailboxTests(TestCase):
    def test_reads_the_email_agent(self) -> None:
        with mock.patch("dispatch_app.mailbox.requests.get", return_value=FakeResponse(200, {"connected": True, "email_address": "dana@spaceline.test"})) as get:
            self.assertEqual(mailbox.mailbox_for(7), "dana@spaceline.test")
        self.assertEqual(get.call_args.args[0], "http://agent.test/internal/v1/accounts/users/7")
        self.assertEqual(get.call_args.kwargs["headers"], {"X-Internal-Secret": "internal-secret"})

    def test_no_mailbox_or_errors_are_null(self) -> None:
        for reply in (FakeResponse(200, {"connected": False}), FakeResponse(500), requests.Timeout("slow")):
            with mock.patch("dispatch_app.mailbox.requests.get", side_effect=[reply]):
                self.assertIsNone(mailbox.mailbox_for(7))
