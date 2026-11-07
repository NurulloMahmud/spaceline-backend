"""
Driver Hub integration: the hub pushing drivers here, the client we call it
with, the invite from the Drivers page, and the relays back (bid status,
payouts, blacklist). Every HTTP call is mocked; nothing here reaches a network.
"""

import importlib
import re
import tempfile
import threading
from io import StringIO
import zoneinfo
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import requests

from django.apps import apps as django_apps
from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.response import Response
from rest_framework.test import APIClient

from billing.models import Broker, BrokerBlacklist, Load
from hiring.google_maps import geocode_zip
from hiring.models import (
    Driver,
    DriverCompany,
    DriverFile,
    DriverHistory,
    DriverStatus,
    Vehicle,
    VehicleEquipment,
    VehicleFile,
    ZipLocation,
)
from hub import bids, blacklist, payouts, registry, sync
from hub.models import DriverBidDecision
from hub.services import driver_hub
from mobile.models import DriverLocation
from accounting.models import Payout, PayoutExpense
from billing.models import LoadStop
from payroll.models import Deduction, DeductionType
from users.models import Company, CustomUser, Department

HUB = dict(DRIVER_HUB_URL="https://hub.test", DRIVER_HUB_TOKEN="tenant-token")
LOCAL_FILES = dict(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    },
    MEDIA_ROOT=tempfile.mkdtemp(),
)

PROFILE = {
    "hub_driver_id": 7,
    "driver": {
        "id": 7,
        "email": "bob@example.com",
        "first_name": "Bob",
        "last_name": "Smith",
        "phone": "5551234",
        "company_name": "Bob Hauling LLC",
        "mc": "123456",
        "employer_id": "99-1234567",
        "business_as": "Bob Hauling",
        "business_type": "LLC",
        "address": "1 Main St",
        "city": "Cleveland",
        "state": "OH",
        "zip": "44101",
        "lat": 41.5,
        "lng": -81.7,
    },
    "vehicle": {
        "year": 2022,
        "make": "Ford",
        "model": "Transit",
        "vehicle_type": "cargo van",
        "unit_number": "214",
        "payload_lbs": 3200,
        "gvw_lbs": 9000,
        "cargo_length_in": 144,
        "cargo_width_in": 70,
        "cargo_height_in": 76,
        "door_width_in": 60,
        "door_height_in": 70,
        "dock_height_in": 40,
        "dock_type": "ramp",
        "equipment": "Lift gate, Pallet jack",
    },
    "documents": [
        {
            "kind": "license",
            "title": "CDL",
            "file_url": "https://files.test/cdl.pdf",
            "expires_at": "2027-01-15T00:00:00Z",
        },
        {
            "kind": "medical_card",
            "title": "",
            "file_url": "https://files.test/med.pdf",
            "expires_at": "2026-06-01T00:00:00Z",
        },
        {
            "kind": "insurance",
            "title": "Progressive",
            "file_url": "https://files.test/ins.pdf",
            "expires_at": "2026-09-30T00:00:00Z",
        },
    ],
}


def _response(status_code=200, body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b"x" if body is not None else b""
    resp.text = text
    resp.json.return_value = body if body is not None else {}
    return resp


@override_settings(**HUB, **LOCAL_FILES)
class HubDriverEndpointTests(TestCase):
    def setUp(self):
        Company.objects.create(name="Space Line LLC")
        DriverStatus.objects.create(name="Pending")
        self.client = APIClient()
        self.url = reverse("hub-drivers")
        self.remove_url = reverse("hub-drivers-remove")

    def push(self, payload=PROFILE, token="tenant-token"):
        with patch("hub.sync.fetch_file", return_value=b"%PDF-1.4 fake") as fetch:
            response = self.client.post(
                self.url, payload, format="json", HTTP_X_HUB_TOKEN=token
            )
        return response, fetch

    def test_rejects_a_wrong_token(self):
        response, _ = self.push(token="nope")
        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(Driver.objects.count(), 0)

    def test_rejects_when_no_token_is_configured(self):
        with override_settings(DRIVER_HUB_TOKEN=""):
            response = self.client.post(
                self.url, PROFILE, format="json", HTTP_X_HUB_TOKEN=""
            )
        self.assertIn(response.status_code, (401, 403))

    def test_creates_the_driver_company_vehicle_and_documents(self):
        response, fetch = self.push()
        self.assertEqual(response.status_code, 200, response.content)

        driver = Driver.objects.get(id=response.json()["driver_id"])
        self.assertEqual(driver.hub_driver_id, 7)
        self.assertEqual(driver.hub_status, "connected")
        self.assertEqual(driver.full_name, "Bob Smith")
        self.assertEqual(driver.email, "bob@example.com")
        self.assertEqual(driver.phone_number, "5551234")
        self.assertEqual(driver.unit_number, "214")
        self.assertEqual(driver.zip_code, "44101")
        self.assertEqual(driver.current_latitude, 41.5)
        self.assertIsNotNone(driver.current_location_updated_at)
        self.assertEqual(driver.cdl_expiration, date(2027, 1, 15))
        self.assertEqual(driver.medical_exp_date, date(2026, 6, 1))

        company = driver.driver_company
        self.assertEqual(company.name, "Bob Hauling LLC")
        self.assertEqual(company.mc, "123456")
        self.assertEqual(company.employer_id, "99-1234567")
        self.assertEqual(company.applicant_first_name, "Bob")

        vehicle = Vehicle.objects.get(driver=driver)
        self.assertEqual(
            (vehicle.make, vehicle.model, vehicle.year), ("Ford", "Transit", 2022)
        )
        self.assertEqual(vehicle.vehicle_type, "cargo van")
        self.assertEqual((vehicle.payload, vehicle.gvw), (3200, 9000))
        self.assertEqual((vehicle.length, vehicle.width, vehicle.height), (144, 70, 76))
        self.assertEqual(vehicle.dock_height, "40 in ramp")
        self.assertEqual(vehicle.insurance_expiry_date, date(2026, 9, 30))
        self.assertEqual(
            sorted(
                VehicleEquipment.objects.filter(vehicle=vehicle).values_list(
                    "name", flat=True
                )
            ),
            ["Lift gate", "Pallet jack"],
        )

        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(
            sorted(
                DriverFile.objects.filter(driver=driver).values_list("name", flat=True)
            ),
            ["CDL (driver app)", "Medical Card (driver app)"],
        )
        self.assertEqual(VehicleFile.objects.filter(vehicle=vehicle).count(), 1)
        self.assertEqual(
            VehicleFile.objects.get(vehicle=vehicle).name, "Progressive (driver app)"
        )

    def test_links_the_invited_driver_by_email(self):
        invited = Driver.objects.create(
            full_name="Robert Smith",
            email="Bob@Example.com",
            status=DriverStatus.objects.first(),
            hub_invite_id=5,
            hub_invite_status="pending",
        )
        response, _ = self.push()
        self.assertEqual(response.json()["driver_id"], invited.id)
        self.assertEqual(Driver.objects.count(), 1)
        invited.refresh_from_db()
        self.assertEqual(invited.hub_driver_id, 7)
        self.assertEqual(invited.full_name, "Bob Smith")
        self.assertEqual(invited.hub_status, "connected")

    def test_a_second_push_updates_the_same_driver(self):
        first, _ = self.push()
        changed = {
            **PROFILE,
            "driver": {
                **PROFILE["driver"],
                "phone": "5559999",
                "email": "new@example.com",
            },
        }
        second, fetch = self.push(changed)
        self.assertEqual(first.json()["driver_id"], second.json()["driver_id"])
        self.assertEqual(Driver.objects.count(), 1)
        self.assertEqual(Vehicle.objects.count(), 1)
        driver = Driver.objects.get()
        self.assertEqual(
            (driver.phone_number, driver.email), ("5559999", "new@example.com")
        )
        # Files already stored under the same name are not fetched again.
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(DriverFile.objects.filter(driver=driver).count(), 2)

    def test_a_missing_file_does_not_block_the_sync(self):
        with patch("hub.sync.fetch_file", return_value=None):
            response = self.client.post(
                self.url, PROFILE, format="json", HTTP_X_HUB_TOKEN="tenant-token"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DriverFile.objects.count(), 0)
        self.assertEqual(Driver.objects.get().cdl_expiration, date(2027, 1, 15))

    def test_requires_hub_driver_id(self):
        response, _ = self.push({"driver": {"email": "x@y.z"}})
        self.assertEqual(response.status_code, 400)

    def test_remove_keeps_the_driver_and_drops_the_link(self):
        response, _ = self.push()
        driver_id = response.json()["driver_id"]
        response = self.client.post(
            self.remove_url,
            {"hub_driver_id": 7},
            format="json",
            HTTP_X_HUB_TOKEN="tenant-token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"driver_id": driver_id, "removed": True})
        driver = Driver.objects.get(id=driver_id)
        self.assertIsNone(driver.hub_driver_id)
        self.assertEqual(driver.hub_status, "left")

        response = self.client.post(
            self.remove_url,
            {"hub_driver_id": 7},
            format="json",
            HTTP_X_HUB_TOKEN="tenant-token",
        )
        self.assertEqual(response.json()["removed"], False)

    def remove(self, hub_driver_id=7):
        return self.client.post(
            self.remove_url,
            {"hub_driver_id": hub_driver_id},
            format="json",
            HTTP_X_HUB_TOKEN="tenant-token",
        )

    def test_remove_sets_the_out_of_service_status(self):
        DriverStatus.objects.create(name="Active")
        oos = DriverStatus.objects.create(name="Out-of-Service")
        driver_id = self.push()[0].json()["driver_id"]
        self.assertEqual(self.remove().status_code, 200)
        driver = Driver.objects.get(id=driver_id)
        self.assertEqual(driver.status_id, oos.id)
        self.assertEqual(driver.hub_status, "left")

    def test_remove_matches_the_status_name_loosely(self):
        for name in ("out of service", "OUT-OF-SERVICE", "Out Of Service"):
            DriverStatus.objects.all().exclude(name="Pending").delete()
            oos = DriverStatus.objects.create(name=name)
            driver = Driver.objects.filter(hub_driver_id__isnull=False).first()
            if driver is None:
                driver_id = self.push()[0].json()["driver_id"]
            else:
                driver_id = driver.id
            self.remove()
            self.assertEqual(Driver.objects.get(id=driver_id).status_id, oos.id, name)
            Driver.objects.filter(id=driver_id).update(hub_driver_id=7)

    def test_remove_without_such_a_status_warns_and_keeps_the_status(self):
        driver_id = self.push()[0].json()["driver_id"]
        before = Driver.objects.get(id=driver_id).status_id
        count = DriverStatus.objects.count()
        with self.assertLogs("hub.sync", level="WARNING") as logs:
            self.assertEqual(self.remove().status_code, 200)
        self.assertIn("out-of-service", logs.output[0])
        driver = Driver.objects.get(id=driver_id)
        self.assertEqual(driver.status_id, before)
        self.assertEqual(driver.hub_status, "left")
        self.assertEqual(DriverStatus.objects.count(), count)

    def test_a_deleted_app_account_also_sets_out_of_service(self):
        # The hub reports a deleted account through the same remove call.
        oos = DriverStatus.objects.create(name="Out of Service")
        driver_id = self.push()[0].json()["driver_id"]
        self.assertEqual(self.remove().json()["removed"], True)
        driver = Driver.objects.get(id=driver_id)
        self.assertEqual(driver.status_id, oos.id)
        self.assertIsNone(driver.hub_driver_id)
        self.assertEqual(driver.hub_status, "left")


@override_settings(**HUB)
class DriverHubClientTests(TestCase):
    def test_invite_posts_the_email_with_the_bearer_token(self):
        body = {
            "invite_id": 5,
            "status": "pending",
            "delivery": "emailed",
            "hub_driver_id": None,
        }
        with patch(
            "hub.services.driver_hub.requests.request",
            return_value=_response(201, body),
        ) as req:
            result = driver_hub.invite("bob@example.com", "Dylan Brooks")
        self.assertEqual(result, body)
        req.assert_called_once_with(
            "POST",
            "https://hub.test/t/v1/invites",
            json={"email": "bob@example.com", "invited_by": "Dylan Brooks"},
            headers={"Authorization": "Bearer tenant-token"},
            timeout=driver_hub.TIMEOUT,
        )

    def test_invite_status_reads_the_invite(self):
        with patch(
            "hub.services.driver_hub.requests.request",
            return_value=_response(200, {"status": "accepted"}),
        ) as req:
            self.assertEqual(driver_hub.invite_status(5), {"status": "accepted"})
        self.assertEqual(req.call_args.args, ("GET", "https://hub.test/t/v1/invites/5"))

    def test_update_bid_target_sends_the_status_and_amounts(self):
        with patch(
            "hub.services.driver_hub.requests.request",
            return_value=_response(200, {"ok": True}),
        ) as req:
            driver_hub.update_bid_target(
                12,
                "placed",
                carrier_amount=Decimal("1400"),
                driver_amount=1250,
                placed_by="Dylan Brooks",
                tenant_bid_id="abc",
                note="Bid emailed",
            )
        self.assertEqual(
            req.call_args.args, ("POST", "https://hub.test/t/v1/bid-targets/12/status")
        )
        self.assertEqual(
            req.call_args.kwargs["json"],
            {
                "status": "placed",
                "carrier_amount": 1400.0,
                "driver_amount": 1250.0,
                "placed_by": "Dylan Brooks",
                "tenant_bid_id": "abc",
                "note": "Bid emailed",
            },
        )

    def test_update_bid_target_refuses_an_unknown_status(self):
        with patch("hub.services.driver_hub.requests.request") as req:
            with self.assertRaises(ValueError):
                driver_hub.update_bid_target(12, "maybe")
        req.assert_not_called()

    def test_replace_blacklist_puts_the_full_list(self):
        with patch(
            "hub.services.driver_hub.requests.request",
            return_value=_response(200, {"count": 1}),
        ) as req:
            driver_hub.replace_blacklist([{"name": "TQL", "mc": "312916"}])
        self.assertEqual(req.call_args.args, ("PUT", "https://hub.test/t/v1/blacklist"))
        self.assertEqual(
            req.call_args.kwargs["json"],
            {"brokers": [{"name": "TQL", "email": "", "mc": "312916"}]},
        )

    def test_an_error_reply_raises_with_the_hub_message(self):
        with patch(
            "hub.services.driver_hub.requests.request",
            return_value=_response(409, {"error": "already invited"}),
        ):
            with self.assertRaises(driver_hub.DriverHubError) as ctx:
                driver_hub.invite("bob@example.com", "Dylan")
        self.assertEqual(
            (ctx.exception.status_code, ctx.exception.detail), (409, "already invited")
        )

    def test_unconfigured_hub_raises_without_a_request(self):
        with override_settings(DRIVER_HUB_TOKEN=""):
            with patch("hub.services.driver_hub.requests.request") as req:
                with self.assertRaises(driver_hub.DriverHubError) as ctx:
                    driver_hub.invite("bob@example.com", "Dylan")
        self.assertEqual(ctx.exception.status_code, 503)
        req.assert_not_called()


@override_settings(**HUB)
class InviteToAppTests(TestCase):
    def setUp(self):
        Company.objects.create(name="Space Line LLC")
        self.user = CustomUser.objects.create_user(
            username="dylan", password="x", first_name="Dylan", last_name="Brooks"
        )
        self.driver = Driver.objects.create(
            full_name="Alex Kim",
            email="alex@example.com",
            status=DriverStatus.objects.create(name="Active"),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse("drivers-invite-app", args=[self.driver.id])

    def test_sends_the_invite_and_stores_the_result(self):
        reply = {
            "invite_id": 5,
            "status": "pending",
            "delivery": "emailed",
            "hub_driver_id": None,
        }
        with patch("hub.services.driver_hub.invite", return_value=reply) as invite:
            response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        invite.assert_called_once_with("alex@example.com", "Dylan Brooks")
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.hub_invite_id, 5)
        self.assertEqual(self.driver.hub_invite_status, "pending")
        self.assertIsNotNone(self.driver.hub_invited_at)
        self.assertEqual(self.driver.hub_status, "invited")
        self.assertEqual(response.json()["hub_status"], "invited")
        self.assertEqual(response.json()["delivery"], "emailed")

    def test_a_corrected_email_is_kept_on_the_driver(self):
        reply = {
            "invite_id": 6,
            "status": "pending",
            "delivery": "none",
            "hub_driver_id": None,
        }
        with patch("hub.services.driver_hub.invite", return_value=reply) as invite:
            response = self.client.post(
                self.url, {"email": "alex.kim@example.com"}, format="json"
            )
        self.assertEqual(response.status_code, 200)
        invite.assert_called_once_with("alex.kim@example.com", "Dylan Brooks")
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.email, "alex.kim@example.com")

    def test_an_already_active_driver_is_linked_at_once(self):
        reply = {
            "invite_id": 7,
            "status": "active",
            "delivery": "pushed",
            "hub_driver_id": 42,
        }
        with patch("hub.services.driver_hub.invite", return_value=reply), patch(
            "hub.services.driver_hub.link_driver"
        ) as link:
            response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 200)
        link.assert_called_once_with(42, self.driver.id)
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.hub_driver_id, 42)
        self.assertEqual(self.driver.hub_status, "connected")

    def test_no_email_is_a_400(self):
        self.driver.email = ""
        self.driver.save()
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_hub_failure_is_reported(self):
        with patch(
            "hub.services.driver_hub.invite",
            side_effect=driver_hub.DriverHubError(502, "driver hub unreachable"),
        ):
            response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 502)
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.hub_status, "not_invited")

    def test_hub_status_is_on_the_driver_list(self):
        response = self.client.get(reverse("driver-list"))
        self.assertEqual(response.status_code, 200)
        row = (
            response.json()["results"][0]
            if "results" in response.json()
            else response.json()[0]
        )
        self.assertEqual(row["hub_status"], "not_invited")


@override_settings(**HUB)
class BidStatusRelayTests(TestCase):
    LOAD = "8d1e4f2a-0000-4000-8000-000000000001"
    BIDS = [
        {
            "id": "b1",
            "action": "driver_bid",
            "bid_amount": 1250.0,
            "note": "hub target #12",
            "driver": {"id": 214},
        },
        {
            "id": "b2",
            "action": "driver_bid",
            "bid_amount": 900.0,
            "note": "hub target #13",
            "driver": {"id": 188},
        },
        {
            "id": "b3",
            "action": "dispatcher_bid",
            "bid_amount": 1400.0,
            "note": "Bid emailed to broker",
            "driver": {"id": 214},
        },
        {
            "id": "b4",
            "action": "driver_bid",
            "bid_amount": 700.0,
            "note": "from telegram",
            "driver": {"id": 300},
        },
    ]

    def setUp(self):
        Company.objects.create(name="Space Line LLC")

    def test_parses_the_hub_target_out_of_the_note(self):
        self.assertEqual(bids.hub_target_id("hub target #12"), 12)
        self.assertEqual(
            bids.hub_target_id("Bid from the app, hub target #7, sprinter"), 7
        )
        self.assertIsNone(bids.hub_target_id("Bid emailed to broker"))
        self.assertIsNone(bids.hub_target_id(None))

    def test_relays_only_the_matching_drivers_target(self):
        with patch("hub.bids.load_bids", return_value=self.BIDS), patch(
            "hub.services.driver_hub.update_bid_target"
        ) as update:
            reached = bids.relay_bid_status(
                self.LOAD,
                "placed",
                carrier_amount=1400,
                driver_amount=1250,
                placed_by="Dylan Brooks",
                tenant_bid_id="b3",
                driver_id=214,
            )
        self.assertEqual(reached, [12])
        update.assert_called_once_with(
            12,
            "placed",
            carrier_amount=1400,
            driver_amount=1250,
            placed_by="Dylan Brooks",
            tenant_bid_id="b3",
            note=None,
        )

    def test_without_a_driver_every_app_bid_is_told(self):
        not_app = driver_hub.DriverHubError(404, "no such bid", "not_found")
        with patch("hub.bids.load_bids", return_value=self.BIDS), patch(
            "hub.services.driver_hub.update_bid_target"
        ) as update, patch(
            "hub.services.driver_hub.update_bid_by_tenant_bid", side_effect=not_app
        ) as by_bid:
            reached = bids.relay_bid_status(self.LOAD, "lost")
        self.assertEqual(reached, [12, 13])
        self.assertEqual(update.call_count, 2)
        # The Telegram bid is asked about by its id; the hub has no such app bid.
        self.assertEqual(by_bid.call_args.args, ("b4", "lost"))
        self.assertEqual(
            (
                by_bid.call_args.kwargs["load_id"],
                by_bid.call_args.kwargs["tenant_driver_id"],
            ),
            (self.LOAD, 300),
        )

    def test_a_hub_failure_on_one_target_does_not_stop_the_rest(self):
        with patch("hub.bids.load_bids", return_value=self.BIDS), patch(
            "hub.services.driver_hub.update_bid_target",
            side_effect=[driver_hub.DriverHubError(500, "boom"), {"ok": True}],
        ), patch(
            "hub.services.driver_hub.update_bid_by_tenant_bid",
            return_value={"id": 40},
        ):
            reached = bids.relay_bid_status(self.LOAD, "lost")
        self.assertEqual(reached, [13, 40])

    def test_bids_are_read_from_the_loads_service_with_the_internal_secret(self):
        body = {"success": True, "data": {"color": "red", "bids": self.BIDS}}
        with patch("hub.bids.requests.get", return_value=_response(200, body)) as get:
            rows = bids.load_bids(self.LOAD)
        self.assertEqual(len(rows), 4)
        self.assertTrue(get.call_args.args[0].endswith(f"/loads/{self.LOAD}/bids"))
        self.assertEqual(
            get.call_args.kwargs["headers"],
            {"X-Internal-Secret": settings.INTERNAL_SERVICE_SECRET},
        )
        self.assertEqual(
            get.call_args.kwargs["params"], {"company_id": Company.current().id}
        )

    def test_endpoint_accepts_the_internal_secret(self):
        client = APIClient()
        with patch("hub.bids.load_bids", return_value=self.BIDS), patch(
            "hub.services.driver_hub.update_bid_target"
        ) as update:
            response = client.post(
                reverse("hub-bid-status"),
                {
                    "load_id": self.LOAD,
                    "status": "won",
                    "driver_id": 214,
                    "carrier_amount": 1400,
                    "driver_amount": 1250,
                    "placed_by": "Dylan Brooks",
                },
                format="json",
                HTTP_X_INTERNAL_SECRET=settings.INTERNAL_SERVICE_SECRET,
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"targets": [12], "declined_bid_ids": []})
        self.assertEqual(update.call_args.args, (12, "won"))

    def test_endpoint_names_the_dispatcher_and_takes_target_ids(self):
        user = CustomUser.objects.create_user(
            username="maria", password="x", first_name="Maria", last_name="Lopez"
        )
        client = APIClient()
        client.force_authenticate(user)
        with patch("hub.bids.load_bids") as load_bids, patch(
            "hub.services.driver_hub.update_bid_target"
        ) as update:
            response = client.post(
                reverse("hub-bid-status"),
                {
                    "load_id": self.LOAD,
                    "status": "declined",
                    "hub_target_ids": [12],
                    "note": "Broker is blacklisted",
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        # Read once, only to keep the decline under the bid's own id.
        load_bids.assert_called_once_with(self.LOAD)
        update.assert_called_once_with(
            12,
            "declined",
            carrier_amount=None,
            driver_amount=None,
            placed_by="Maria Lopez",
            tenant_bid_id=None,
            note="Broker is blacklisted",
        )

    def test_endpoint_resolves_the_dispatcher_by_id(self):
        user = CustomUser.objects.create_user(
            username="dylan", password="x", first_name="Dylan", last_name="Brooks"
        )
        client = APIClient()
        with patch("hub.bids.load_bids", return_value=self.BIDS), patch(
            "hub.services.driver_hub.update_bid_target"
        ) as update:
            response = client.post(
                reverse("hub-bid-status"),
                {
                    "load_id": self.LOAD,
                    "status": "placed",
                    "driver_id": 214,
                    "placed_by_user_id": user.id,
                    "carrier_amount": 1400,
                    "driver_amount": 1250,
                    "tenant_bid_id": "neg-1",
                },
                format="json",
                HTTP_X_INTERNAL_SECRET=settings.INTERNAL_SERVICE_SECRET,
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(update.call_args.kwargs["placed_by"], "Dylan Brooks")
        self.assertEqual(update.call_args.kwargs["tenant_bid_id"], "neg-1")

    def test_endpoint_rejects_strangers_and_bad_statuses(self):
        client = APIClient()
        response = client.post(
            reverse("hub-bid-status"),
            {"load_id": self.LOAD, "status": "won"},
            format="json",
        )
        self.assertEqual(response.status_code, 401)
        response = client.post(
            reverse("hub-bid-status"),
            {"load_id": self.LOAD, "status": "meh"},
            format="json",
            HTTP_X_INTERNAL_SECRET=settings.INTERNAL_SERVICE_SECRET,
        )
        self.assertEqual(response.status_code, 400)


@override_settings(**HUB)
class BlacklistPushTests(TestCase):
    def test_a_change_pushes_the_full_list_after_commit(self):
        Broker.objects.create(name="TQL", mc="312916", email="bids@tql.com")
        with patch("hub.services.driver_hub.replace_blacklist") as replace:
            with self.captureOnCommitCallbacks(execute=True):
                BrokerBlacklist.objects.create(name="Echo", mc="")
            with self.captureOnCommitCallbacks(execute=True):
                entry = BrokerBlacklist.objects.create(name="TQL", mc="312916")
        self.assertEqual(replace.call_count, 2)
        self.assertEqual(
            replace.call_args.args[0],
            [
                {"name": "Echo", "mc": "", "email": ""},
                {"name": "TQL", "mc": "312916", "email": "bids@tql.com"},
            ],
        )

        with patch("hub.services.driver_hub.replace_blacklist") as replace:
            with self.captureOnCommitCallbacks(execute=True):
                entry.delete()
        replace.assert_called_once_with([{"name": "Echo", "mc": "", "email": ""}])

    def test_a_hub_outage_never_breaks_the_save(self):
        with patch(
            "hub.services.driver_hub.replace_blacklist",
            side_effect=driver_hub.DriverHubError(502, "down"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                BrokerBlacklist.objects.create(name="Echo")
        self.assertEqual(BrokerBlacklist.objects.count(), 1)

    def test_nothing_is_sent_when_the_hub_is_not_configured(self):
        with override_settings(DRIVER_HUB_TOKEN=""), patch(
            "hub.services.driver_hub.replace_blacklist"
        ) as replace:
            with self.captureOnCommitCallbacks(execute=True):
                BrokerBlacklist.objects.create(name="Echo")
        replace.assert_not_called()

    def test_the_command_resends_everything(self):
        BrokerBlacklist.objects.create(name="Echo")
        with patch("hub.services.driver_hub.replace_blacklist") as replace:
            call_command("sync_hub_blacklist")
        replace.assert_called_once_with([{"name": "Echo", "mc": "", "email": ""}])


def _profile(**driver_changes: object) -> dict:
    """PROFILE with some driver fields changed; deep enough that tests can edit the copy."""
    return {
        **PROFILE,
        "driver": {**PROFILE["driver"], **driver_changes},
        "vehicle": dict(PROFILE["vehicle"]),
        "documents": [dict(d) for d in PROFILE["documents"]],
    }


@override_settings(**HUB, **LOCAL_FILES)
class HubPushTestCase(TestCase):
    def setUp(self) -> None:
        Company.objects.create(name="Space Line LLC")
        self.pending = DriverStatus.objects.create(name="Pending")
        self.client = APIClient()

    def push(
        self, payload: dict, fetched: bytes | None = b"%PDF-1.4 fake"
    ) -> tuple[Driver, MagicMock]:
        with patch("hub.sync.fetch_file", return_value=fetched) as fetch:
            response = self.client.post(
                reverse("hub-drivers"),
                payload,
                format="json",
                HTTP_X_HUB_TOKEN="tenant-token",
            )
        self.assertEqual(response.status_code, 200, response.content)
        return Driver.objects.get(id=response.json()["driver_id"]), fetch


class SharedCompanyTests(HubPushTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.shared = DriverCompany.objects.create(
            name="Spaceline LLC",
            mc="111111",
            employer_id="88-0000001",
            phone_number="3125550100",
            business_as="Spaceline",
            business_type="LLC",
            zipcode="60601",
            state="IL",
            city="Chicago",
            address="1 Wacker Dr",
            email="office@spaceline.test",
            applicant_first_name="Office",
            applicant_last_name="Manager",
        )
        self.snapshot = DriverCompany.objects.filter(pk=self.shared.pk).values().get()
        self.bob = Driver.objects.create(
            full_name="Bob Smith",
            email="bob@example.com",
            status=self.pending,
            driver_company=self.shared,
            hub_invite_id=5,
            hub_invite_status="pending",
        )
        self.other = Driver.objects.create(
            full_name="Ann Lee", status=self.pending, driver_company=self.shared
        )

    def assert_shared_untouched(self) -> None:
        self.assertEqual(
            DriverCompany.objects.filter(pk=self.shared.pk).values().get(),
            self.snapshot,
        )
        self.other.refresh_from_db()
        self.assertEqual(self.other.driver_company_id, self.shared.pk)

    def test_a_shared_company_is_never_edited_and_the_driver_gets_its_own(self) -> None:
        driver, _ = self.push(_profile())
        self.assertEqual(driver.pk, self.bob.pk)
        self.assertNotEqual(driver.driver_company_id, self.shared.pk)
        own = driver.driver_company
        self.assertEqual(
            (own.name, own.mc, own.employer_id, own.business_as),
            ("Bob Hauling LLC", "123456", "99-1234567", "Bob Hauling"),
        )
        self.assertEqual(
            (own.phone_number, own.email, own.applicant_first_name),
            ("5551234", "bob@example.com", "Bob"),
        )
        self.assert_shared_untouched()

        # Later pushes edit the driver's own company, still never the shared one.
        driver, _ = self.push(_profile(company_name="Bob Hauling Inc"))
        self.assertEqual(driver.driver_company_id, own.pk)
        self.assertEqual(DriverCompany.objects.get(pk=own.pk).name, "Bob Hauling Inc")
        self.assertEqual(DriverCompany.objects.count(), 2)
        self.assert_shared_untouched()

    def test_the_same_shared_company_keeps_the_driver_on_it(self) -> None:
        driver, _ = self.push(
            _profile(
                company_name="Spaceline, LLC",
                mc="MC-111111",
                employer_id="",
                business_as="",
            )
        )
        self.assertEqual(driver.driver_company_id, self.shared.pk)
        self.assertEqual(DriverCompany.objects.count(), 1)
        self.assert_shared_untouched()

    def test_a_different_mc_is_a_different_company_even_with_the_same_name(
        self,
    ) -> None:
        driver, _ = self.push(_profile(company_name="Spaceline LLC", mc="222222"))
        self.assertNotEqual(driver.driver_company_id, self.shared.pk)
        self.assertEqual(driver.driver_company.mc, "222222")
        self.assert_shared_untouched()

    def test_an_own_company_is_updated_in_place_and_never_blanked(self) -> None:
        own = DriverCompany.objects.create(
            name="Old Name",
            mc="999999",
            employer_id="11-1111111",
            phone_number="5550000",
            business_as="Old",
            business_type="Corp",
            zipcode="44101",
            state="OH",
            city="Cleveland",
            address="9 Elm St",
            email="old@example.com",
            applicant_first_name="Robert",
            applicant_last_name="Smith",
        )
        self.bob.driver_company = own
        self.bob.hub_driver_id = (
            7  # linked already, so the empty email below still finds Bob
        )
        self.bob.save()
        driver, _ = self.push(
            _profile(
                company_name="New Name LLC",
                mc="",
                employer_id="",
                business_as="",
                business_type="",
                address="",
                city="",
                state="",
                zip="",
                phone="",
                email="",
                first_name="",
                last_name="",
            )
        )
        self.assertEqual(driver.driver_company_id, own.pk)
        own.refresh_from_db()
        self.assertEqual(own.name, "New Name LLC")
        self.assertEqual(
            (
                own.mc,
                own.employer_id,
                own.business_as,
                own.business_type,
                own.zipcode,
                own.state,
                own.city,
                own.address,
            ),
            (
                "999999",
                "11-1111111",
                "Old",
                "Corp",
                "44101",
                "OH",
                "Cleveland",
                "9 Elm St",
            ),
        )
        self.assertEqual(
            (
                own.phone_number,
                own.email,
                own.applicant_first_name,
                own.applicant_last_name,
            ),
            ("5550000", "old@example.com", "Robert", "Smith"),
        )
        self.assert_shared_untouched()

    def test_no_company_fields_leave_the_company_alone(self) -> None:
        driver, _ = self.push(
            _profile(company_name="", mc="", employer_id="", business_as="")
        )
        self.assertEqual(driver.driver_company_id, self.shared.pk)
        self.assert_shared_untouched()


@override_settings(**HUB)
class InviteStatusTests(TestCase):
    def setUp(self) -> None:
        Company.objects.create(name="Space Line LLC")
        self.active = DriverStatus.objects.create(name="Active")
        self.driver = Driver.objects.create(
            full_name="Alex Kim",
            email="alex@example.com",
            status=self.active,
            hub_invite_id=5,
            hub_invite_status="pending",
        )
        self.client = APIClient()
        self.url = reverse("hub-invites-status")

    def answer(self, body: dict, token: str = "tenant-token") -> Response:
        return self.client.post(self.url, body, format="json", HTTP_X_HUB_TOKEN=token)

    def test_the_path_and_token(self) -> None:
        self.assertEqual(self.url, "/api/hub/invites/status/")
        response = self.answer(
            {"invite_id": 5, "email": "alex@example.com", "status": "declined"},
            token="nope",
        )
        self.assertIn(response.status_code, (401, 403))
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.hub_invite_status, "pending")

    def test_a_decline_shows_on_the_tracking_page(self) -> None:
        response = self.answer(
            {"invite_id": 5, "email": "alex@example.com", "status": "declined"}
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["ok"], True)
        self.driver.refresh_from_db()
        self.assertEqual(
            (self.driver.hub_invite_status, self.driver.hub_status),
            ("declined", "declined"),
        )

        user = CustomUser.objects.create_user(username="dylan", password="x")
        web = APIClient()
        web.force_authenticate(user)
        rows = web.get(reverse("tracking-drivers")).json()["results"]
        self.assertEqual(
            [(r["id"], r["hub_status"]) for r in rows], [(self.driver.id, "declined")]
        )
        # And the drivers list the invite dialog reads.
        row = web.get(reverse("driver-list")).json()
        row = row["results"][0] if "results" in row else row[0]
        self.assertEqual(row["hub_status"], "declined")

    def test_an_accept_is_recorded_until_the_profile_arrives(self) -> None:
        response = self.answer(
            {"invite_id": 5, "email": "alex@example.com", "status": "accepted"}
        )
        self.assertEqual(response.json()["ok"], True)
        self.driver.refresh_from_db()
        self.assertEqual(
            (self.driver.hub_invite_status, self.driver.hub_status),
            ("accepted", "invited"),
        )

    def test_falls_back_to_the_email_of_a_driver_without_an_invite_id(self) -> None:
        other = Driver.objects.create(
            full_name="Sam Roe",
            email="Sam@Example.com",
            status=self.active,
            hub_invite_status="pending",
        )
        response = self.answer(
            {"invite_id": 9, "email": "sam@example.com", "status": "declined"}
        )
        self.assertEqual(
            response.json(),
            {"ok": True, "driver_id": other.id, "hub_status": "declined"},
        )
        other.refresh_from_db()
        self.assertEqual(
            (other.hub_invite_status, other.hub_invite_id), ("declined", 9)
        )

    def test_an_unknown_invite_is_ok_false_and_touches_nothing(self) -> None:
        # Same email, but this driver has another (newer) invite: not a match.
        response = self.answer(
            {"invite_id": 4, "email": "alex@example.com", "status": "declined"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": False})
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.hub_invite_status, "pending")

    def test_a_connected_driver_stays_connected(self) -> None:
        self.driver.hub_driver_id = 42
        self.driver.hub_invite_status = "active"
        self.driver.save()
        response = self.answer(
            {"invite_id": 5, "email": "alex@example.com", "status": "declined"}
        )
        self.assertEqual(response.json()["ok"], True)
        self.driver.refresh_from_db()
        self.assertEqual(
            (self.driver.hub_invite_status, self.driver.hub_status),
            ("active", "connected"),
        )

    def test_a_bad_request_is_a_400(self) -> None:
        self.assertEqual(
            self.answer({"invite_id": 5, "status": "maybe"}).status_code, 400
        )
        self.assertEqual(self.answer({"status": "declined"}).status_code, 400)


class DocumentRemovalTests(HubPushTestCase):
    def names(self, driver: Driver) -> list[str]:
        return sorted(
            DriverFile.objects.filter(driver=driver).values_list("name", flat=True)
        )

    def vehicle_names(self, driver: Driver) -> list[str]:
        return sorted(
            VehicleFile.objects.filter(vehicle__driver=driver).values_list(
                "name", flat=True
            )
        )

    def test_a_document_removed_in_the_app_is_removed_here(self) -> None:
        driver, _ = self.push(_profile())
        self.assertEqual(
            sorted(
                DriverFile.objects.filter(driver=driver).values_list("name", "hub_kind")
            ),
            [
                ("CDL (driver app)", "license"),
                ("Medical Card (driver app)", "medical_card"),
            ],
        )
        vehicle = Vehicle.objects.get(driver=driver)
        DriverFile.objects.create(
            driver=driver, name="CDL scan", document=ContentFile(b"x", name="scan.pdf")
        )
        VehicleFile.objects.create(
            vehicle=vehicle,
            name="Insurance card",
            document=ContentFile(b"x", name="card.pdf"),
        )

        payload = _profile()
        payload["documents"] = [
            d for d in payload["documents"] if d["kind"] == "medical_card"
        ]
        driver, fetch = self.push(payload)
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(self.names(driver), ["CDL scan", "Medical Card (driver app)"])
        self.assertEqual(self.vehicle_names(driver), ["Insurance card"])

    def test_null_documents_remove_every_app_file_and_a_missing_key_none(self) -> None:
        driver, _ = self.push(_profile())
        payload = _profile()
        del payload["documents"]
        driver, _ = self.push(payload)
        self.assertEqual(len(self.names(driver)), 2)

        payload["documents"] = None
        driver, _ = self.push(payload)
        self.assertEqual(self.names(driver), [])
        self.assertEqual(self.vehicle_names(driver), [])

    def test_files_from_before_the_marker_are_known_by_their_name(self) -> None:
        driver, _ = self.push(_profile())
        # What the sync stored before hub_kind existed.
        DriverFile.objects.filter(driver=driver).update(hub_kind="")
        DriverFile.objects.create(
            driver=driver,
            name="Driver's license (driver app)",
            document=ContentFile(b"x", name="old.pdf"),
        )
        driver, fetch = self.push(_profile())
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(
            sorted(
                DriverFile.objects.filter(driver=driver).values_list("name", "hub_kind")
            ),
            [
                ("CDL (driver app)", "license"),
                ("Medical Card (driver app)", "medical_card"),
            ],
        )

    def test_a_failed_download_keeps_the_old_file(self) -> None:
        driver, _ = self.push(_profile())
        payload = _profile()
        payload["documents"][0] = {
            **payload["documents"][0],
            "title": "New CDL",
            "file_url": "https://files.test/cdl-2.pdf",
        }
        driver, fetch = self.push(payload, fetched=None)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(
            self.names(driver), ["CDL (driver app)", "Medical Card (driver app)"]
        )

        driver, _ = self.push(payload)
        self.assertEqual(
            self.names(driver), ["Medical Card (driver app)", "New CDL (driver app)"]
        )

    def test_two_documents_with_one_title_are_both_kept(self) -> None:
        payload = _profile()
        payload["documents"] = [
            {
                "kind": "other",
                "title": "Permit",
                "file_url": "https://files.test/p1.pdf",
            },
            {
                "kind": "other",
                "title": "Permit",
                "file_url": "https://files.test/p2.pdf",
            },
        ]
        driver, fetch = self.push(payload)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(
            self.names(driver), ["Permit (driver app)", "Permit 2 (driver app)"]
        )
        driver, fetch = self.push(payload)
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(
            self.names(driver), ["Permit (driver app)", "Permit 2 (driver app)"]
        )


class VehicleNumberTests(HubPushTestCase):
    EMPTY = {
        "payload_lbs": 0,
        "gvw_lbs": None,
        "cargo_length_in": 0,
        "cargo_width_in": 0,
        "cargo_height_in": None,
        "door_width_in": 0,
        "door_height_in": 0,
        "dock_height_in": 0,
        "year": 0,
    }

    def test_zero_and_null_never_overwrite_real_numbers(self) -> None:
        driver, _ = self.push(_profile())
        payload = _profile()
        payload["vehicle"] = {**payload["vehicle"], **self.EMPTY}
        driver, _ = self.push(payload)
        vehicle = Vehicle.objects.get(driver=driver)
        self.assertEqual(
            (vehicle.payload, vehicle.gvw, vehicle.year), (3200, 9000, 2022)
        )
        self.assertEqual((vehicle.length, vehicle.width, vehicle.height), (144, 70, 76))
        self.assertEqual((vehicle.door_open_width, vehicle.door_open_height), (60, 70))
        self.assertEqual(vehicle.dock_height, "ramp")

    def test_a_new_vehicle_without_numbers_stays_empty(self) -> None:
        payload = _profile()
        payload["vehicle"] = {**payload["vehicle"], **self.EMPTY}
        driver, _ = self.push(payload)
        vehicle = Vehicle.objects.get(driver=driver)
        self.assertEqual((vehicle.payload, vehicle.gvw), (0, 0))
        self.assertEqual(
            (
                vehicle.length,
                vehicle.width,
                vehicle.height,
                vehicle.door_open_width,
                vehicle.year,
            ),
            (None,) * 5,
        )


class AppLocationTests(HubPushTestCase):
    AKRON = (41.0814, -81.5190)

    def setUp(self) -> None:
        super().setUp()
        ZipLocation.objects.create(
            zip_code="44301",
            city="Akron",
            state="OH",
            latitude=41.0447,
            longitude=-81.5205,
        )
        ZipLocation.objects.create(
            zip_code="44101",
            city="Cleveland",
            state="OH",
            latitude=41.4995,
            longitude=-81.6954,
        )

    def test_the_app_position_is_named_after_the_nearest_zip(self) -> None:
        driver, _ = self.push(_profile(lat=self.AKRON[0], lng=self.AKRON[1]))
        self.assertEqual(
            (driver.current_latitude, driver.current_longitude), self.AKRON
        )
        self.assertEqual(
            (
                driver.current_zip,
                driver.current_city,
                driver.current_state,
                driver.current_address,
            ),
            ("44301", "Akron", "OH", "Akron, OH 44301"),
        )
        self.assertIsNotNone(driver.current_location_updated_at)
        # The mailing address is what the driver typed, not the position.
        self.assertEqual(
            (driver.address, driver.city, driver.state, driver.zip_code),
            ("1 Main St", "Cleveland", "OH", "44101"),
        )

        user = CustomUser.objects.create_user(username="dylan", password="x")
        web = APIClient()
        web.force_authenticate(user)
        row = web.get(reverse("tracking-drivers")).json()["results"][0]
        self.assertEqual(
            (row["current_city"], row["current_state"], row["current_zip"]),
            ("Akron", "OH", "44301"),
        )

    def test_a_far_position_is_a_distance_and_an_unknown_one_coordinates(self) -> None:
        # About 50 miles south of Akron.
        driver, _ = self.push(_profile(lat=40.36, lng=-81.52))
        self.assertEqual(
            (driver.current_zip, driver.current_city, driver.current_state),
            ("", "", ""),
        )
        self.assertRegex(driver.current_address, r"^\d+ mi from Akron, OH 44301$")

        driver, _ = self.push(_profile(lat=33.4484, lng=-112.074))
        self.assertEqual((driver.current_zip, driver.current_city), ("", ""))
        self.assertEqual(driver.current_address, "33.4484, -112.0740")

    def test_no_position_or_zero_leaves_the_location_alone(self) -> None:
        driver, _ = self.push(_profile(lat=None, lng=None))
        self.assertIsNone(driver.current_latitude)
        self.assertIsNone(driver.current_location_updated_at)
        driver, _ = self.push(_profile(lat=0, lng=0))
        self.assertIsNone(driver.current_latitude)

    def test_a_resent_position_does_not_undo_a_typed_zip(self) -> None:
        driver, _ = self.push(_profile(lat=self.AKRON[0], lng=self.AKRON[1]))
        stamp = timezone.now() - timedelta(hours=1)
        Driver.objects.filter(pk=driver.pk).update(
            current_zip="44101",
            current_city="Cleveland",
            current_latitude=41.4995,
            current_longitude=-81.6954,
            current_location_updated_at=stamp,
        )
        # The driver changes the phone; the hub sends the old position again.
        driver, _ = self.push(
            _profile(lat=self.AKRON[0], lng=self.AKRON[1], phone="5550002")
        )
        self.assertEqual(
            (driver.current_zip, driver.current_city, driver.current_latitude),
            ("44101", "Cleveland", 41.4995),
        )
        self.assertEqual(driver.current_location_updated_at, stamp)

        driver, _ = self.push(_profile(lat=41.05, lng=-81.52))
        self.assertEqual(
            (driver.current_zip, driver.current_latitude), ("44301", 41.05)
        )
        self.assertGreater(driver.current_location_updated_at, stamp)
        self.assertEqual(
            list(
                DriverLocation.objects.filter(driver=driver).values_list(
                    "latitude", "device"
                )
            ),
            [(41.05, "driver app"), (self.AKRON[0], "driver app")],
        )

    def test_geocoded_zips_are_kept_for_later(self) -> None:
        def google(postal_code: str) -> MagicMock:
            resp = MagicMock()
            resp.json.return_value = {
                "status": "OK",
                "results": [
                    {
                        "geometry": {"location": {"lat": 41.48, "lng": -81.70}},
                        "formatted_address": f"Cleveland, OH {postal_code}, USA",
                        "address_components": [
                            {"long_name": postal_code, "types": ["postal_code"]},
                            {
                                "long_name": "Cleveland",
                                "types": ["locality", "political"],
                            },
                            {
                                "long_name": "Ohio",
                                "short_name": "OH",
                                "types": ["administrative_area_level_1", "political"],
                            },
                        ],
                    }
                ],
            }
            return resp

        with patch("hiring.google_maps.requests.get", return_value=google("44113")):
            geo = geocode_zip("44113")
        self.assertEqual(
            (geo["city"], geo["state"], geo["zip"]), ("Cleveland", "OH", "44113")
        )
        self.assertEqual(
            ZipLocation.objects.filter(
                zip_code="44113", city="Cleveland", state="OH", latitude=41.48
            ).count(),
            1,
        )

        # Not a plain ZIP, or Google answered another ZIP: not kept.
        with patch("hiring.google_maps.requests.get", return_value=google("44114")):
            geocode_zip("Cleveland, OH")
            geocode_zip("44199")
        self.assertFalse(
            ZipLocation.objects.filter(zip_code__in=["44114", "44199"]).exists()
        )

    def test_the_migration_seeds_zips_from_typed_driver_locations(self) -> None:
        ZipLocation.objects.all().delete()
        common = {
            "status": self.pending,
            "current_latitude": 41.0,
            "current_longitude": -81.5,
        }
        Driver.objects.create(
            full_name="A",
            current_zip="44301",
            current_city="Akron",
            current_state="OH",
            current_address="Akron, OH 44301, USA",
            **common,
        )
        # The ZIP was saved but its geocode failed: the rest is another place's.
        Driver.objects.create(
            full_name="B",
            current_zip="60601",
            current_city="Akron",
            current_address="Akron, OH 44301, USA",
            **common,
        )
        # A driver-app driver: the hub moved the coordinates under the ZIP.
        Driver.objects.create(
            full_name="C",
            current_zip="44101",
            current_address="Cleveland, OH 44101, USA",
            hub_driver_id=3,
            **common,
        )
        Driver.objects.create(full_name="D", current_zip="", **common)

        migration = importlib.import_module(
            "hiring.migrations.0028_hub_file_kind_zip_locations"
        )
        migration.backfill_zip_locations(django_apps, None)
        self.assertEqual(
            list(ZipLocation.objects.values_list("zip_code", "city", "state")),
            [("44301", "Akron", "OH")],
        )


class RFC3339Tests(TestCase):
    def test_dates_are_utc_midnight_and_datetimes_keep_their_offset(self) -> None:
        chicago = zoneinfo.ZoneInfo("America/Chicago")
        cases = [
            (None, None),
            ("", None),
            (date(2026, 10, 26), "2026-10-26T00:00:00Z"),
            ("2026-10-26", "2026-10-26T00:00:00Z"),
            (
                datetime(2026, 11, 2, 15, 4, 5, 123456, tzinfo=dt_timezone.utc),
                "2026-11-02T15:04:05+00:00",
            ),
            (datetime(2026, 11, 2, 9, 0, tzinfo=chicago), "2026-11-02T09:00:00-06:00"),
            (datetime(2026, 10, 26, 9, 0), "2026-10-26T09:00:00-05:00"),
            ("2026-11-02T15:04:05Z", "2026-11-02T15:04:05+00:00"),
            ("next week", "next week"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(driver_hub.rfc3339(value), expected)


class UploadNameTests(TestCase):
    def test_a_renamed_upload_is_still_the_same_file(self) -> None:
        self.assertEqual(
            sync._upload_name(
                "https://hub.test/files/12/ab34-my license.pdf?exp=1&sig=x", "license"
            ),
            "ab34-my_license.pdf",
        )
        self.assertEqual(
            sync._upload_name("https://hub.test/files/", "license"), "license.bin"
        )
        self.assertTrue(sync._same_upload("driver_files/ab34-cdl.pdf", "ab34-cdl.pdf"))
        self.assertTrue(
            sync._same_upload("driver_files/ab34-cdl_Xy3kP9q.pdf", "ab34-cdl.pdf")
        )
        self.assertFalse(
            sync._same_upload("driver_files/ab34-cdl-2.pdf", "ab34-cdl.pdf")
        )
        self.assertFalse(
            sync._same_upload("driver_files/xab34-cdl.pdf", "ab34-cdl.pdf")
        )


@override_settings(**HUB)
class HubClientTests(TestCase):
    def test_upsert_payout_posts_the_body_and_refuses_an_unknown_status(self) -> None:
        body = {"external_id": "payout-1", "hub_driver_id": 7, "status": "hold"}
        with patch(
            "hub.services.driver_hub.requests.request",
            return_value=_response(200, {"id": 1}),
        ) as req:
            driver_hub.upsert_payout(body)
        self.assertEqual(req.call_args.args, ("POST", "https://hub.test/t/v1/payouts"))
        self.assertEqual(req.call_args.kwargs["json"], body)
        with patch("hub.services.driver_hub.requests.request") as req:
            with self.assertRaises(ValueError):
                driver_hub.upsert_payout({**body, "status": "scheduled"})
        req.assert_not_called()

    def test_delete_payout_and_a_404_is_fine(self) -> None:
        with patch(
            "hub.services.driver_hub.requests.request", return_value=_response(204)
        ) as req:
            driver_hub.delete_payout("payout-12")
        self.assertEqual(
            req.call_args.args, ("DELETE", "https://hub.test/t/v1/payouts/payout-12")
        )
        with patch(
            "hub.services.driver_hub.requests.request",
            return_value=_response(404, {"error": "no such payout"}),
        ):
            driver_hub.delete_payout("payout-12")
        with patch(
            "hub.services.driver_hub.requests.request",
            return_value=_response(500, {"error": "boom"}),
        ):
            with self.assertRaises(driver_hub.DriverHubError):
                driver_hub.delete_payout("payout-12")

    def test_lookup_sends_emails_and_hub_ids_together_in_batches(self) -> None:
        replies = [
            _response(
                200,
                {
                    "drivers": [
                        {"email": "a@x.test", "registered": True},
                        {"email": "b@x.test", "registered": False},
                    ],
                    "members": [
                        {"hub_driver_id": 7, "exists": True, "status": "active"}
                    ],
                },
            ),
            _response(
                200,
                {"drivers": [{"email": "c@x.test", "registered": True}], "members": []},
            ),
        ]
        with patch.object(driver_hub, "LOOKUP_LIMIT", 2), patch(
            "hub.services.driver_hub.requests.request", side_effect=replies
        ) as req:
            answer = driver_hub.lookup_drivers(
                ["a@x.test", "b@x.test", "c@x.test"], [7]
            )
        self.assertEqual(
            [r["email"] for r in answer["drivers"]],
            ["a@x.test", "b@x.test", "c@x.test"],
        )
        self.assertEqual(
            answer["members"],
            [{"hub_driver_id": 7, "exists": True, "status": "active"}],
        )
        self.assertEqual(req.call_count, 2)
        self.assertEqual(
            req.call_args_list[0].args, ("POST", "https://hub.test/t/v1/drivers/lookup")
        )
        self.assertEqual(
            req.call_args_list[0].kwargs["json"],
            {"emails": ["a@x.test", "b@x.test"], "hub_driver_ids": [7]},
        )
        self.assertEqual(
            req.call_args_list[1].kwargs["json"],
            {"emails": ["c@x.test"], "hub_driver_ids": []},
        )
        self.assertEqual(req.call_args.kwargs["timeout"], driver_hub.LOOKUP_TIMEOUT)

    def test_lookup_with_nothing_to_ask_sends_nothing(self) -> None:
        with patch("hub.services.driver_hub.requests.request") as req:
            self.assertEqual(
                driver_hub.lookup_drivers([], [0]), {"drivers": [], "members": []}
            )
        req.assert_not_called()

    def test_tenant_name(self) -> None:
        with patch(
            "hub.services.driver_hub.requests.request",
            return_value=_response(200, {"name": "Space Line LLC"}),
        ) as req:
            driver_hub.update_tenant("Space Line LLC")
            self.assertEqual(driver_hub.get_tenant(), {"name": "Space Line LLC"})
        self.assertEqual(
            req.call_args_list[0].args, ("PUT", "https://hub.test/t/v1/tenant")
        )
        self.assertEqual(
            req.call_args_list[0].kwargs["json"], {"name": "Space Line LLC"}
        )
        self.assertEqual(
            req.call_args_list[1].args, ("GET", "https://hub.test/t/v1/tenant")
        )

    def test_the_error_code_is_kept(self) -> None:
        reply = _response(
            409,
            {
                "error": "This driver has not registered in the driver app yet.",
                "code": "not_registered",
            },
        )
        with patch("hub.services.driver_hub.requests.request", return_value=reply):
            with self.assertRaises(driver_hub.DriverHubError) as ctx:
                driver_hub.invite("bob@example.com", "Dylan")
        self.assertEqual(
            (ctx.exception.status_code, ctx.exception.code),
            (409, driver_hub.NOT_REGISTERED),
        )


@override_settings(**HUB)
class InviteNotRegisteredTests(TestCase):
    MESSAGE = "This driver has not registered in the driver app yet."

    def setUp(self) -> None:
        Company.objects.create(name="Space Line LLC")
        user = CustomUser.objects.create_user(
            username="dylan", password="x", first_name="Dylan", last_name="Brooks"
        )
        self.driver = Driver.objects.create(
            full_name="Alex Kim",
            email="alex@example.com",
            status=DriverStatus.objects.create(name="Active"),
        )
        self.client = APIClient()
        self.client.force_authenticate(user)
        self.url = reverse("drivers-invite-app", args=[self.driver.id])

    def test_an_unregistered_email_is_a_409_and_nothing_changes(self) -> None:
        reply = _response(409, {"error": self.MESSAGE, "code": "not_registered"})
        with patch("hub.services.driver_hub.requests.request", return_value=reply):
            response = self.client.post(
                self.url, {"email": "alex.kim@example.com"}, format="json"
            )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(
            response.json(), {"error": self.MESSAGE, "code": "not_registered"}
        )
        self.driver.refresh_from_db()
        self.assertEqual(
            (
                self.driver.email,
                self.driver.hub_invite_id,
                self.driver.hub_invite_status,
                self.driver.hub_invited_at,
            ),
            ("alex@example.com", None, None, None),
        )
        self.assertFalse(DriverHistory.objects.filter(driver=self.driver).exists())

    def test_another_conflict_stays_a_400(self) -> None:
        with patch(
            "hub.services.driver_hub.requests.request",
            return_value=_response(409, {"error": "already invited"}),
        ):
            response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 400)


@override_settings(**HUB)
class RegistrationLookupTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        Company.objects.create(name="Space Line LLC")
        active = DriverStatus.objects.create(name="Active")

        def make(name: str, **fields: object) -> Driver:
            return Driver.objects.create(full_name=name, status=active, **fields)

        self.ann = make("A Ann", email="ann@x.test")
        self.bea = make(
            "B Bea", email="bea@x.test", hub_invite_id=3, hub_invite_status="pending"
        )
        self.cal = make(
            "C Cal", email="cal@x.test", hub_driver_id=9, hub_invite_status="active"
        )
        self.dee = make("D Dee", email="")
        self.eve = make("E Eve", email="Eve@X.test", hub_invite_status="declined")
        self.fay = make("F Fay", email="fay@x.test")
        self.gus = make(
            "G Gus", email="gus@x.test", hub_driver_id=55, hub_invite_status="active"
        )
        # Rows by email, for drivers without a hub id.
        self.rows = {
            "ann@x.test": {"registered": True, "hub_driver_id": 21, "status": "none"},
            "bea@x.test": {
                "registered": False,
                "hub_driver_id": None,
                "status": "invited",
            },
            "eve@x.test": {
                "registered": True,
                "hub_driver_id": 23,
                "status": "invited",
            },
            "fay@x.test": {"registered": True, "hub_driver_id": 44, "status": "active"},
            "typo@x.test": {
                "registered": False,
                "hub_driver_id": None,
                "status": "none",
            },
            "gus@x.test": {"registered": True, "hub_driver_id": 55, "status": "left"},
        }
        # Rows by hub driver id, for connected drivers.
        self.members = {
            9: {"exists": True, "status": "active"},
            55: {"exists": True, "status": "active"},
            44: {"exists": True, "status": "active"},  # Fay, once linked
        }
        user = CustomUser.objects.create_user(username="dylan", password="x")
        self.client = APIClient()
        self.client.force_authenticate(user)

    def hub(self, method: str, url: str, json: dict, **kwargs: object) -> MagicMock:
        return _response(
            200,
            {
                "drivers": [{"email": e, **self.rows[e]} for e in json["emails"]],
                "members": [
                    {"hub_driver_id": i, **self.members[i]}
                    for i in json["hub_driver_ids"]
                ],
            },
        )

    def get(self) -> dict:
        response = self.client.get(reverse("tracking-drivers"))
        self.assertEqual(response.status_code, 200, response.content)
        return {
            r["full_name"]: (r["hub_registered"], r["hub_status"])
            for r in response.json()["results"]
        }

    def test_the_safety_net_also_sets_out_of_service(self) -> None:
        oos = DriverStatus.objects.create(name="Out of service")
        for row in ({"exists": True, "status": "left"}, {"exists": False}):
            driver = Driver.objects.create(
                full_name="Net Case",
                email="net@x.test",
                hub_driver_id=77,
                hub_invite_status="active",
                status=DriverStatus.objects.get_or_create(name="Active")[0],
            )
            registry.reconcile_member(driver, row)
            driver.refresh_from_db()
            self.assertEqual(driver.status_id, oos.id, row)
            self.assertEqual(driver.hub_status, "left", row)
            self.assertIsNone(driver.hub_driver_id)
            driver.delete()

    def test_the_safety_net_without_the_status_keeps_the_status(self) -> None:
        active = DriverStatus.objects.get_or_create(name="Active")[0]
        driver = Driver.objects.create(
            full_name="Net Case", hub_driver_id=78, status=active
        )
        with self.assertLogs("hub.registry", level="WARNING"):
            registry.reconcile_member(driver, {"exists": False})
        driver.refresh_from_db()
        self.assertEqual(driver.status_id, active.id)
        self.assertEqual(driver.hub_status, "left")

    def test_one_lookup_per_page_shows_who_is_registered_and_corrects_the_rest(
        self,
    ) -> None:
        self.members[55] = {"exists": True, "status": "left"}
        with patch(
            "hub.services.driver_hub.requests.request", side_effect=self.hub
        ) as req:
            rows = self.get()
        self.assertEqual(req.call_count, 1)
        self.assertEqual(
            req.call_args.args, ("POST", "https://hub.test/t/v1/drivers/lookup")
        )
        # Connected drivers by hub id, the rest by email, in one request.
        self.assertEqual(
            req.call_args.kwargs["json"],
            {
                "emails": ["ann@x.test", "bea@x.test", "eve@x.test", "fay@x.test"],
                "hub_driver_ids": [9, 55],
            },
        )
        self.assertEqual(
            rows,
            {
                "A Ann": (True, "not_invited"),
                "B Bea": (False, "not_registered"),
                "C Cal": (True, "connected"),
                "D Dee": (False, "not_registered"),
                "E Eve": (True, "invited"),  # declined, then invited again in the app
                "F Fay": (True, "connected"),  # linked in the hub, now linked here
                "G Gus": (True, "left"),  # left in the app; the push never arrived
            },
        )
        self.fay.refresh_from_db()
        self.assertEqual(
            (self.fay.hub_driver_id, self.fay.hub_invite_status), (44, "active")
        )
        self.gus.refresh_from_db()
        self.assertEqual(
            (self.gus.hub_driver_id, self.gus.hub_invite_status), (None, "left")
        )
        self.eve.refresh_from_db()
        self.assertEqual(self.eve.hub_invite_status, "pending")
        self.bea.refresh_from_db()
        self.assertEqual(self.bea.hub_invite_status, "pending")  # stored as is

        # Cached for a minute: the next page load asks only about the two
        # whose link changed (Gus now by email, Fay now by hub id); the one
        # after, nothing.
        with patch(
            "hub.services.driver_hub.requests.request", side_effect=self.hub
        ) as req:
            self.assertEqual(self.get()["G Gus"], (True, "left"))
        self.assertEqual(
            req.call_args.kwargs["json"],
            {"emails": ["gus@x.test"], "hub_driver_ids": [44]},
        )
        with patch("hub.services.driver_hub.requests.request") as req:
            self.assertEqual(self.get()["B Bea"], (False, "not_registered"))
        req.assert_not_called()

    def test_a_connected_driver_whose_email_was_changed_here_stays_connected(
        self,
    ) -> None:
        # A dispatcher typed an address that has no app account.
        Driver.objects.filter(pk=self.cal.pk).update(email="typo@x.test")
        with patch(
            "hub.services.driver_hub.requests.request", side_effect=self.hub
        ) as req:
            rows = self.get()
        self.assertNotIn("typo@x.test", req.call_args.kwargs["json"]["emails"])
        self.assertEqual(rows["C Cal"], (True, "connected"))
        self.cal.refresh_from_db()
        self.assertEqual(
            (self.cal.hub_driver_id, self.cal.hub_invite_status), (9, "active")
        )

    def test_a_deleted_app_account_unlinks_the_driver(self) -> None:
        # The hub forgets deleted accounts: the member row says it no longer exists.
        self.members[9] = {"exists": False, "status": "none"}
        with patch("hub.services.driver_hub.requests.request", side_effect=self.hub):
            rows = self.get()
        self.assertEqual(rows["C Cal"], (False, "not_registered"))
        self.cal.refresh_from_db()
        self.assertEqual(
            (
                self.cal.hub_driver_id,
                self.cal.hub_invite_id,
                self.cal.hub_invite_status,
            ),
            (None, None, "left"),
        )

    def test_only_left_or_deleted_unlinks_a_connected_driver(self) -> None:
        for status in ("none", "invited", "declined"):
            cache.clear()
            self.members[9] = {"exists": True, "status": status}
            with patch(
                "hub.services.driver_hub.requests.request", side_effect=self.hub
            ):
                self.assertEqual(self.get()["C Cal"], (True, "connected"))
        self.cal.refresh_from_db()
        self.assertEqual(self.cal.hub_driver_id, 9)

    def test_a_push_or_invite_forgets_the_cached_answer(self) -> None:
        self.rows["fay@x.test"]["status"] = "none"  # no link to change the second ask
        with patch("hub.services.driver_hub.requests.request", side_effect=self.hub):
            self.get()
        registry.forget("ann@x.test", hub_driver_ids=[9])
        with patch(
            "hub.services.driver_hub.requests.request", side_effect=self.hub
        ) as req:
            self.get()
        self.assertEqual(
            req.call_args.kwargs["json"],
            {"emails": ["ann@x.test"], "hub_driver_ids": [9]},
        )

    def test_a_profile_push_forgets_the_drivers_member_answer(self) -> None:
        self.rows["fay@x.test"]["status"] = "none"  # no link to change the second ask
        with patch("hub.services.driver_hub.requests.request", side_effect=self.hub):
            self.get()
        with patch("hub.sync.fetch_file", return_value=b"%PDF"), override_settings(
            **LOCAL_FILES
        ):
            sync.upsert_driver(
                {
                    **PROFILE,
                    "hub_driver_id": 9,
                    "driver": {**PROFILE["driver"], "email": "cal@x.test"},
                }
            )
        with patch(
            "hub.services.driver_hub.requests.request", side_effect=self.hub
        ) as req:
            self.get()
        self.assertEqual(
            req.call_args.kwargs["json"], {"emails": [], "hub_driver_ids": [9]}
        )

    def test_a_down_hub_keeps_the_stored_status(self) -> None:
        with patch(
            "hub.services.driver_hub.requests.request",
            side_effect=requests.ConnectionError("refused"),
        ) as req:
            rows = self.get()
            self.get()
        self.assertEqual(req.call_count, 1)  # not asked again for DOWN_SECONDS
        self.assertEqual(rows["B Bea"], (None, "invited"))
        self.assertEqual(rows["G Gus"], (None, "connected"))
        self.assertEqual(rows["D Dee"], (None, "not_invited"))

    def test_a_page_with_nothing_to_look_up_asks_nothing(self) -> None:
        Driver.objects.exclude(pk=self.dee.pk).delete()
        with patch("hub.services.driver_hub.requests.request") as req:
            rows = self.get()
        req.assert_not_called()
        self.assertEqual(rows, {"D Dee": (False, "not_registered")})

    def test_without_the_hub_nothing_is_asked(self) -> None:
        with override_settings(DRIVER_HUB_TOKEN=""), patch(
            "hub.services.driver_hub.requests.request"
        ) as req:
            rows = self.get()
        req.assert_not_called()
        self.assertEqual(rows["A Ann"], (None, "not_invited"))

    def test_a_hub_driver_id_already_taken_is_not_linked_twice(self) -> None:
        self.rows["fay@x.test"]["hub_driver_id"] = 9  # Cal's
        with patch("hub.services.driver_hub.requests.request", side_effect=self.hub):
            rows = self.get()
        self.assertEqual(rows["F Fay"], (True, "not_invited"))
        self.fay.refresh_from_db()
        self.assertIsNone(self.fay.hub_driver_id)


@override_settings(**HUB)
class AccountingPayoutSyncTests(TestCase):
    def setUp(self) -> None:
        Company.objects.create(name="Space Line LLC", shipment_number="10001")
        self.boss = CustomUser.objects.create_user(
            username="boss",
            password="x",
            department=Department.objects.create(name="Management"),
        )
        self.acct = CustomUser.objects.create_user(
            username="elena",
            password="x",
            department=Department.objects.create(name="Billing"),
        )
        active = DriverStatus.objects.create(name="Active")
        self.driver = Driver.objects.create(
            full_name="Kwame Osei", status=active, hub_driver_id=7
        )
        self.other_app = Driver.objects.create(
            full_name="Jin Park", status=active, hub_driver_id=8
        )
        self.not_app = Driver.objects.create(full_name="Ola Ade", status=active)
        self.fuel = DeductionType.objects.create(name="Fuel advance")
        self.calls: list[tuple[str, str, dict | None]] = []

    def hub(
        self, method: str, url: str, json: dict | None = None, **kwargs: object
    ) -> MagicMock:
        self.calls.append((method, url.replace("https://hub.test", ""), json))
        return _response(200, {"ok": True})

    def run_hub(
        self, action: Callable[[], Any], reply: Callable[..., MagicMock] | None = None
    ) -> Any:
        with patch(
            "hub.services.driver_hub.requests.request", side_effect=reply or self.hub
        ):
            with self.captureOnCommitCallbacks(execute=True):
                return action()

    def make_load(self, driver: Driver, pay: str = "Regular Pay") -> Load:
        load = Load.objects.create(
            driver=driver,
            load_number="L-77",
            driver_pay=Decimal("874"),
            carrier_pay=Decimal("1300"),
            payment_type=pay,
            pickup_date=datetime(2026, 11, 2, 14, 0, tzinfo=dt_timezone.utc),
            drop_date=datetime(2026, 11, 3, 18, 30, tzinfo=dt_timezone.utc),
        )
        LoadStop.objects.create(
            load=load, city="Chicago", state="IL", order=1, load_pickup=True
        )
        LoadStop.objects.create(
            load=load, city="Columbus", state="OH", order=2, load_drop=True
        )
        return load

    def api(self, user: CustomUser) -> APIClient:
        client = APIClient()
        client.force_authenticate(user)
        return client

    def posts(self) -> list[dict]:
        return [
            body
            for method, path, body in self.calls
            if method == "POST" and path == "/t/v1/payouts"
        ]

    def test_a_quick_pay_load_is_one_push_whose_lines_add_up_to_what_accounting_pays(
        self,
    ) -> None:
        Deduction.objects.create(
            driver=self.driver,
            amount=Decimal("150"),
            type=self.fuel,
            date=date(2026, 11, 1),
        )
        load = self.run_hub(lambda: self.make_load(self.driver, pay="Quick Pay"))
        payout = Payout.objects.get(load=load)
        self.assertEqual(
            (payout.gross, payout.quick_pay_fee, payout.amount),
            (Decimal("830.30"), Decimal("43.70"), Decimal("680.30")),
        )
        # Payout created, fee taken, expense taken, stops added: still one call.
        self.assertEqual(len(self.calls), 1)
        body = self.posts()[0]
        self.assertEqual(
            body,
            {
                "external_id": f"payout-{payout.id}",
                "hub_driver_id": 7,
                "status": "pending",
                "gross": 874.0,
                "quick_pay_fee": 43.7,
                "deductions": 150.0,
                "net": 680.3,
                "paid_at": None,
                "load": {
                    "reference": "L-77",
                    "pick_up_at": "Chicago, IL",
                    "deliver_to": "Columbus, OH",
                    "pick_up_date": "2026-11-02T14:00:00+00:00",
                    "delivery_date": "2026-11-03T18:30:00+00:00",
                },
                "lines": [
                    {"kind": "fee", "label": "Quick Pay fee (5%)", "amount": -43.7},
                    {"kind": "expense", "label": "Fuel advance", "amount": -150.0},
                ],
            },
        )
        self.assertAlmostEqual(
            body["gross"] + sum(line["amount"] for line in body["lines"]), body["net"]
        )

    def test_status_changes_and_new_expenses_are_pushed(self) -> None:
        load = self.run_hub(lambda: self.make_load(self.driver))
        payout = Payout.objects.get(load=load)
        url = reverse("accounting-payout", args=[payout.id])
        self.calls.clear()

        response = self.run_hub(
            lambda: self.api(self.acct).patch(url, {"status": "hold"}, format="json")
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([b["status"] for b in self.posts()], ["hold"])

        response = self.run_hub(
            lambda: self.api(self.boss).patch(url, {"status": "paid"}, format="json")
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.posts()[-1]["status"], "paid")
        self.assertRegex(
            self.posts()[-1]["paid_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$"
        )

        response = self.run_hub(
            lambda: self.api(self.boss).patch(url, {"status": "pending"}, format="json")
        )
        response = self.run_hub(
            lambda: self.api(self.acct).post(
                reverse("accounting-expenses"),
                {
                    "driver": self.driver.id,
                    "type": self.fuel.id,
                    "amount": "100",
                    "fee": "10",
                },
                format="json",
            )
        )
        self.assertEqual(response.status_code, 201, response.content)
        last = self.posts()[-1]
        self.assertEqual(
            (last["status"], last["deductions"], last["net"]), ("pending", 110.0, 764.0)
        )
        self.assertEqual(
            last["lines"],
            [{"kind": "expense", "label": "Fuel advance", "amount": -110.0}],
        )

    def test_stops_saved_after_the_load_reach_the_hub(self) -> None:
        """The Add load form's order: the load (and its payout) first, then each stop."""
        dispatcher = CustomUser.objects.create_user(
            username="dana",
            password="x",
            department=Department.objects.create(name="Dispatch"),
        )
        client = self.api(dispatcher)
        load = self.run_hub(
            lambda: Load.objects.create(
                driver=self.driver, load_number="TEST-QA-1", driver_pay=Decimal("500")
            )
        )
        stops_url = reverse("load-stops-list")
        with patch("billing.views.debounce_recalculate_miles"):
            first = self.run_hub(
                lambda: client.post(
                    stops_url,
                    {
                        "load": load.id,
                        "order": 1,
                        "city": "Chicago",
                        "state": "IL",
                        "load_pickup": True,
                    },
                    format="json",
                )
            )
            second = self.run_hub(
                lambda: client.post(
                    stops_url,
                    {
                        "load": load.id,
                        "order": 2,
                        "city": "Indianapolis",
                        "state": "IN",
                        "load_drop": True,
                    },
                    format="json",
                )
            )
        self.assertEqual(
            (first.status_code, second.status_code),
            (201, 201),
            (first.content, second.content),
        )
        routes = [
            (b["load"]["pick_up_at"], b["load"]["deliver_to"]) for b in self.posts()
        ]
        self.assertEqual(
            routes, [("", ""), ("Chicago, IL", ""), ("Chicago, IL", "Indianapolis, IN")]
        )

        with patch("billing.tasks.calculate_loaded_miles_background"), patch(
            "billing.tasks.calculate_empty_miles_multi_background"
        ):
            gone = self.run_hub(
                lambda: client.delete(
                    reverse("load-stops-detail", args=[second.json()["id"]])
                )
            )
        self.assertEqual(gone.status_code, 204)
        self.assertEqual(
            (
                self.posts()[-1]["load"]["pick_up_at"],
                self.posts()[-1]["load"]["deliver_to"],
            ),
            ("Chicago, IL", ""),
        )

        # New dates on the load are pushed too.
        load = Load.objects.get(pk=load.pk)
        load.pickup_date = datetime(2026, 11, 7, 13, 0, tzinfo=dt_timezone.utc)
        self.run_hub(load.save)
        self.assertEqual(
            self.posts()[-1]["load"]["pick_up_date"], "2026-11-07T13:00:00+00:00"
        )
        self.assertEqual(len(self.posts()), 5)

    def test_a_driver_pay_change_on_the_load_is_pushed(self) -> None:
        load = self.run_hub(lambda: self.make_load(self.driver))
        self.calls.clear()
        load = Load.objects.get(pk=load.pk)
        load.driver_pay = Decimal("900")
        self.run_hub(load.save)
        self.assertEqual(
            [(b["gross"], b["net"]) for b in self.posts()], [(900.0, 900.0)]
        )

    def test_a_new_driver_takes_the_payout_off_the_old_one(self) -> None:
        load = self.run_hub(lambda: self.make_load(self.driver))
        payout_id = Payout.objects.get(load=load).id
        self.calls.clear()
        load = Load.objects.get(pk=load.pk)
        load.driver = self.other_app
        self.run_hub(load.save)
        self.assertEqual(
            [(m, p) for m, p, _ in self.calls],
            [
                ("DELETE", f"/t/v1/payouts/payout-{payout_id}"),
                ("POST", "/t/v1/payouts"),
            ],
        )
        self.assertEqual(self.posts()[0]["hub_driver_id"], 8)

        self.calls.clear()
        load.driver = self.not_app
        self.run_hub(load.save)
        self.assertEqual(
            [(m, p) for m, p, _ in self.calls],
            [("DELETE", f"/t/v1/payouts/payout-{payout_id}")],
        )

    def test_a_deleted_payout_is_deleted_on_the_hub(self) -> None:
        load = self.run_hub(lambda: self.make_load(self.driver))
        payout_id = Payout.objects.get(load=load).id
        self.calls.clear()
        self.run_hub(load.delete)
        self.assertEqual(
            [(m, p) for m, p, _ in self.calls],
            [("DELETE", f"/t/v1/payouts/payout-{payout_id}")],
        )

    def test_drivers_outside_the_app_and_an_unconfigured_hub_send_nothing(self) -> None:
        self.run_hub(lambda: self.make_load(self.not_app))
        with override_settings(DRIVER_HUB_TOKEN=""):
            self.run_hub(lambda: self.make_load(self.driver))
        self.assertEqual(self.calls, [])

    def test_a_hub_error_never_breaks_accounting(self) -> None:
        load = self.run_hub(lambda: self.make_load(self.driver))
        payout = Payout.objects.get(load=load)
        with self.assertLogs("hub.payouts", "WARNING") as logs:
            response = self.run_hub(
                lambda: self.api(self.acct).patch(
                    reverse("accounting-payout", args=[payout.id]),
                    {"status": "confirmed"},
                    format="json",
                ),
                reply=lambda *a, **kw: _response(503, {"error": "down"}),
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn(f"payout {payout.id} not sent to the hub", logs.output[0])
        payout.refresh_from_db()
        self.assertEqual(payout.status, "confirmed")

        bad = _response(200, {})
        bad.json.side_effect = ValueError("not JSON")
        with self.assertLogs("hub.payouts", "ERROR"):
            response = self.run_hub(
                lambda: self.api(self.acct).patch(
                    reverse("accounting-payout", args=[payout.id]),
                    {"status": "pending"},
                    format="json",
                ),
                reply=lambda *a, **kw: bad,
            )
        self.assertEqual(response.status_code, 200)

    def test_the_command_resends_every_app_drivers_payout(self) -> None:
        self.make_load(self.driver)
        self.make_load(self.other_app)
        self.make_load(self.not_app)
        out = StringIO()
        with patch("hub.services.driver_hub.requests.request", side_effect=self.hub):
            call_command("sync_hub_payouts", stdout=out)
        self.assertEqual(sorted(b["hub_driver_id"] for b in self.posts()), [7, 8])
        self.assertIn("Sent 2 payout(s) to the hub.", out.getvalue())

    def test_the_statement_sync_is_gone(self) -> None:
        self.assertFalse(hasattr(payouts, "sync_statement_payout"))


@override_settings(**HUB)
class TenantNameTests(TestCase):
    def setUp(self) -> None:
        self.company = Company.objects.create(name="Space Line LLC")
        self.users = {
            name: CustomUser.objects.create_user(
                username=name.lower(),
                password="x",
                department=Department.objects.create(name=name),
            )
            for name in ("Developer", "Management", "Dispatch")
        }

    def patch_company(self, who: str, body: dict) -> tuple[Response, MagicMock]:
        client = APIClient()
        client.force_authenticate(self.users[who])
        with patch(
            "hub.services.driver_hub.requests.request",
            return_value=_response(200, {"name": "x"}),
        ) as req:
            with self.captureOnCommitCallbacks(execute=True):
                response = client.patch("/api/users/company/", body, format="json")
        return response, req

    def test_developers_and_management_rename_the_carrier_and_the_app_follows(
        self,
    ) -> None:
        for who, name in (
            ("Developer", "Space Line Freight"),
            ("Management", "Space Line Freight LLC"),
        ):
            response, req = self.patch_company(who, {"name": name})
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(
                req.call_args.args, ("PUT", "https://hub.test/t/v1/tenant")
            )
            self.assertEqual(req.call_args.kwargs["json"], {"name": name})
        self.assertEqual(Company.current().name, "Space Line Freight LLC")

    def test_others_cannot_and_other_fields_send_nothing(self) -> None:
        response, req = self.patch_company("Dispatch", {"name": "Mine"})
        self.assertEqual(response.status_code, 403)
        req.assert_not_called()
        response, req = self.patch_company("Developer", {"mc": "999"})
        self.assertEqual(response.status_code, 200)
        req.assert_not_called()

    def test_a_hub_error_does_not_fail_the_rename(self) -> None:
        client = APIClient()
        client.force_authenticate(self.users["Developer"])
        with patch(
            "hub.services.driver_hub.requests.request",
            return_value=_response(503, {"error": "down"}),
        ), self.assertLogs("hub.tenant", "WARNING"):
            with self.captureOnCommitCallbacks(execute=True):
                response = client.patch(
                    "/api/users/company/", {"name": "New Name"}, format="json"
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Company.current().name, "New Name")

    def test_the_command_sends_the_name(self) -> None:
        out = StringIO()
        with patch(
            "hub.services.driver_hub.requests.request",
            return_value=_response(200, {"name": "x"}),
        ) as req:
            call_command("sync_hub_tenant", stdout=out)
        self.assertEqual(req.call_args.kwargs["json"], {"name": "Space Line LLC"})
        self.assertIn("'Space Line LLC'", out.getvalue())
        with override_settings(DRIVER_HUB_TOKEN=""), patch(
            "hub.services.driver_hub.requests.request"
        ) as req:
            call_command("sync_hub_tenant", stdout=StringIO())
        req.assert_not_called()


WRITE_SQL = re.compile(r'^\s*(INSERT INTO|UPDATE|DELETE FROM)\s+"(\w+)"', re.IGNORECASE)


def written_tables(queries: CaptureQueriesContext) -> list[str]:
    return [
        m.group(2) for q in queries.captured_queries if (m := WRITE_SQL.match(q["sql"]))
    ]


class PositionOnlyPushTests(HubPushTestCase):
    """The hub now pushes when the driver moves; such a push must cost a driver row and a location."""

    def setUp(self) -> None:
        super().setUp()
        ZipLocation.objects.create(
            zip_code="44301",
            city="Akron",
            state="OH",
            latitude=41.0447,
            longitude=-81.5205,
        )
        ZipLocation.objects.create(
            zip_code="44101",
            city="Cleveland",
            state="OH",
            latitude=41.4995,
            longitude=-81.6954,
        )

    def test_a_moved_position_updates_the_place_and_writes_nothing_else(self) -> None:
        driver, _ = self.push(_profile(lat=41.05, lng=-81.52))
        self.assertEqual(driver.current_city, "Akron")
        company = (
            DriverCompany.objects.filter(pk=driver.driver_company_id).values().get()
        )
        vehicle = Vehicle.objects.get(driver=driver)
        equipment = list(
            VehicleEquipment.objects.filter(vehicle=vehicle).values_list(
                "id", flat=True
            )
        )
        files = list(
            DriverFile.objects.filter(driver=driver).values_list("id", "updated_at")
        )

        for lat, lng, city in ((41.49, -81.69, "Cleveland"), (41.06, -81.52, "Akron")):
            with CaptureQueriesContext(connection) as queries:
                driver, fetch = self.push(_profile(lat=lat, lng=lng))
            fetch.assert_not_called()
            self.assertEqual(
                sorted(written_tables(queries)), ["driver_locations", "drivers"]
            )
            self.assertEqual(
                (driver.current_city, driver.current_latitude), (city, lat)
            )

        self.assertEqual(
            DriverCompany.objects.filter(pk=company["id"]).values().get(), company
        )
        self.assertEqual(
            Vehicle.objects.get(pk=vehicle.pk).updated_at, vehicle.updated_at
        )
        self.assertEqual(
            list(
                VehicleEquipment.objects.filter(vehicle=vehicle).values_list(
                    "id", flat=True
                )
            ),
            equipment,
        )
        self.assertEqual(
            list(
                DriverFile.objects.filter(driver=driver).values_list("id", "updated_at")
            ),
            files,
        )
        self.assertEqual(DriverLocation.objects.filter(driver=driver).count(), 3)

        # The same profile once more: nothing at all is written.
        with CaptureQueriesContext(connection) as queries:
            self.push(_profile(lat=41.06, lng=-81.52))
        self.assertEqual(written_tables(queries), [])

    def test_a_real_change_still_writes_only_its_row(self) -> None:
        driver, _ = self.push(_profile())
        payload = _profile()
        payload["vehicle"] = {
            **payload["vehicle"],
            "payload_lbs": 3500,
            "equipment": "Lift gate",
        }
        with CaptureQueriesContext(connection) as queries:
            driver, _ = self.push(payload)
        self.assertEqual(
            sorted(set(written_tables(queries))), ["vehicle_equipments", "vehicles"]
        )
        self.assertEqual(Vehicle.objects.get(driver=driver).payload, 3500)
        self.assertEqual(
            list(
                VehicleEquipment.objects.filter(vehicle__driver=driver).values_list(
                    "name", flat=True
                )
            ),
            ["Lift gate"],
        )


@override_settings(**HUB, **LOCAL_FILES)
class LinkSendsOlderPayoutsTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        Company.objects.create(name="Space Line LLC", shipment_number="10001")
        self.pending = DriverStatus.objects.create(name="Pending")
        self.calls: list[tuple[str, str, dict | None]] = []
        inline = patch("hub.payouts._in_background", side_effect=lambda work: work())
        self.background = inline.start()
        self.addCleanup(inline.stop)

    def hub(
        self, method: str, url: str, json: dict | None = None, **kwargs: object
    ) -> MagicMock:
        self.calls.append((method, url.replace("https://hub.test", ""), json))
        if url.endswith("/drivers/lookup"):
            return _response(
                200,
                {
                    "drivers": [
                        {
                            "email": e,
                            "registered": True,
                            "hub_driver_id": 44,
                            "status": "active",
                        }
                        for e in json["emails"]
                    ]
                },
            )
        if url.endswith("/invites"):
            return _response(
                201, {"invite_id": 5, "status": "active", "hub_driver_id": 44}
            )
        return _response(200, {"ok": True})

    def payout_posts(self) -> list[tuple[str, int]]:
        return [
            (b["external_id"], b["hub_driver_id"])
            for m, p, b in self.calls
            if p == "/t/v1/payouts"
        ]

    def driver_with_payouts(
        self, email: str, count: int = 2
    ) -> tuple[Driver, list[int]]:
        driver = Driver.objects.create(
            full_name="Bob Smith",
            email=email,
            status=self.pending,
            hub_invite_id=5,
            hub_invite_status="pending",
        )
        ids = []
        for n in range(count):
            load = Load.objects.create(
                driver=driver, load_number=f"L-{n}", driver_pay=Decimal("500")
            )
            ids.append(Payout.objects.get(load=load).id)
        return driver, ids

    def test_the_first_profile_push_sends_the_drivers_payouts(self) -> None:
        driver, ids = self.driver_with_payouts("bob@example.com")
        client = APIClient()
        with patch("hub.sync.fetch_file", return_value=b"%PDF"), patch(
            "hub.services.driver_hub.requests.request", side_effect=self.hub
        ):
            with self.captureOnCommitCallbacks(execute=True):
                response = client.post(
                    reverse("hub-drivers"),
                    PROFILE,
                    format="json",
                    HTTP_X_HUB_TOKEN="tenant-token",
                )
            self.assertEqual(response.json()["driver_id"], driver.id)
            self.assertEqual(
                sorted(self.payout_posts()), [(f"payout-{pk}", 7) for pk in ids]
            )

            # A later push of the same driver does not resend them.
            self.calls.clear()
            with self.captureOnCommitCallbacks(execute=True):
                client.post(
                    reverse("hub-drivers"),
                    _profile(phone="5550003"),
                    format="json",
                    HTTP_X_HUB_TOKEN="tenant-token",
                )
        self.assertEqual(self.payout_posts(), [])

    def test_a_link_found_by_the_lookup_sends_them_too(self) -> None:
        driver, ids = self.driver_with_payouts("fay@x.test", count=1)
        web = APIClient()
        web.force_authenticate(
            CustomUser.objects.create_user(username="dylan", password="x")
        )
        with patch("hub.services.driver_hub.requests.request", side_effect=self.hub):
            with self.captureOnCommitCallbacks(execute=True):
                web.get(reverse("tracking-drivers"))
        driver.refresh_from_db()
        self.assertEqual(driver.hub_driver_id, 44)
        self.assertEqual(self.payout_posts(), [(f"payout-{ids[0]}", 44)])

    def test_an_invite_to_a_driver_already_in_the_app_sends_them(self) -> None:
        driver, ids = self.driver_with_payouts("bob@example.com", count=1)
        web = APIClient()
        web.force_authenticate(
            CustomUser.objects.create_user(username="dylan", password="x")
        )
        with patch("hub.services.driver_hub.requests.request", side_effect=self.hub):
            with self.captureOnCommitCallbacks(execute=True):
                response = web.post(
                    reverse("drivers-invite-app", args=[driver.id]), {}, format="json"
                )
        self.assertEqual(response.json()["hub_status"], "connected")
        self.assertEqual(self.payout_posts(), [(f"payout-{ids[0]}", 44)])

    def test_a_driver_without_payouts_starts_nothing(self) -> None:
        client = APIClient()
        with patch("hub.sync.fetch_file", return_value=b"%PDF"), patch(
            "hub.services.driver_hub.requests.request", side_effect=self.hub
        ):
            with self.captureOnCommitCallbacks(execute=True):
                client.post(
                    reverse("hub-drivers"),
                    PROFILE,
                    format="json",
                    HTTP_X_HUB_TOKEN="tenant-token",
                )
        self.background.assert_not_called()


class BackgroundRunnerTests(TestCase):
    def test_runs_the_work_on_another_thread_and_closes_its_connections(self) -> None:
        seen: list[str] = []
        done = threading.Event()

        def work() -> None:
            seen.append(threading.current_thread().name)
            done.set()

        with patch("hub.payouts.connections.close_all") as close_all:
            payouts._in_background(work)
            self.assertTrue(done.wait(5))
            for _ in range(50):
                if close_all.called:
                    break
                threading.Event().wait(0.02)
        self.assertNotEqual(seen, [threading.current_thread().name])
        close_all.assert_called_once()


@override_settings(**HUB)
class BidRelayByBidIdTests(TestCase):
    """App bids carry the driver's own note, so the hub is told by the loads-service bid id."""

    LOAD = "8d1e4f2a-0000-4000-8000-000000000002"
    BIDS = [
        {
            "id": "6f1c2a90-aaaa-4bbb-8ccc-000000000001",
            "action": "driver_bid",
            "bid_amount": 1250.0,
            "note": "TEST bid from QA, can load tomorrow 1 PM",
            "driver": {"id": 214},
        },
        {
            "id": "6f1c2a90-aaaa-4bbb-8ccc-000000000002",
            "action": "driver_bid",
            "bid_amount": 1100.0,
            "note": "Driver app bid",
            "driver": {"id": 188},
        },
        {
            "id": "old-1",
            "action": "driver_bid",
            "bid_amount": 1000.0,
            "note": "hub target #12",
            "driver": {"id": 214},
        },
        {
            "id": "b3",
            "action": "dispatcher_bid",
            "bid_amount": 1400.0,
            "note": "Bid emailed to broker",
            "driver": {"id": 214},
        },
    ]

    def setUp(self) -> None:
        Company.objects.create(name="Space Line LLC")
        self.calls: list[tuple[str, str, dict | None]] = []

    def hub(
        self, method: str, url: str, json: dict | None = None, **kwargs: object
    ) -> MagicMock:
        self.calls.append((method, url.replace("https://hub.test", ""), json))
        if url.endswith("/by-tenant-bid/status"):
            return _response(200, {"id": 31, "status": json["status"]})
        return _response(200, {"id": 12, "status": json["status"]})

    def test_a_driver_bid_without_the_tag_is_relayed_by_its_id(self) -> None:
        with patch("hub.bids.load_bids", return_value=self.BIDS[:1]), patch(
            "hub.services.driver_hub.requests.request", side_effect=self.hub
        ):
            reached = bids.relay_bid_status(
                self.LOAD,
                "placed",
                carrier_amount=1400,
                driver_amount=1250,
                placed_by="Dylan Brooks",
                tenant_bid_id="b3",
                note="Bid emailed",
                driver_id=214,
            )
        self.assertEqual(reached, [31])
        self.assertEqual(
            self.calls,
            [
                (
                    "POST",
                    "/t/v1/bid-targets/by-tenant-bid/status",
                    {
                        "tenant_bid_id": "6f1c2a90-aaaa-4bbb-8ccc-000000000001",
                        "status": "placed",
                        "carrier_amount": 1400.0,
                        "driver_amount": 1250.0,
                        "placed_by": "Dylan Brooks",
                        "note": "Bid emailed",
                        "load_id": self.LOAD,
                        "tenant_driver_id": 214,
                    },
                )
            ],
        )

    def test_an_old_tagged_bid_still_uses_its_target(self) -> None:
        with patch("hub.bids.load_bids", return_value=self.BIDS), patch(
            "hub.services.driver_hub.requests.request", side_effect=self.hub
        ):
            reached = bids.relay_bid_status(
                self.LOAD, "won", tenant_bid_id="b3", driver_id=214
            )
        self.assertEqual(reached, [31, 12])
        self.assertEqual(
            [(m, p) for m, p, _ in self.calls],
            [
                ("POST", "/t/v1/bid-targets/by-tenant-bid/status"),
                ("POST", "/t/v1/bid-targets/12/status"),
            ],
        )
        self.assertEqual(self.calls[1][2], {"status": "won", "tenant_bid_id": "b3"})

    def test_the_driver_filter_picks_that_drivers_bids(self) -> None:
        with patch("hub.bids.load_bids", return_value=self.BIDS), patch(
            "hub.services.driver_hub.requests.request", side_effect=self.hub
        ):
            bids.relay_bid_status(self.LOAD, "lost", driver_id=188)
        self.assertEqual(
            [body["tenant_bid_id"] for _, _, body in self.calls],
            ["6f1c2a90-aaaa-4bbb-8ccc-000000000002"],
        )
        with patch("hub.bids.load_bids", return_value=self.BIDS):
            found = bids.driver_bid_targets(self.LOAD, 214)
        self.assertEqual(
            [(b.target_id, b.tenant_bid_id, b.driver_id) for b in found],
            [(None, "6f1c2a90-aaaa-4bbb-8ccc-000000000001", 214), (12, None, 214)],
        )

    def test_the_driver_filter_bodies_carry_the_load_and_driver(self) -> None:
        with patch("hub.bids.load_bids", return_value=self.BIDS), patch(
            "hub.services.driver_hub.requests.request", side_effect=self.hub
        ):
            bids.relay_bid_status(self.LOAD, "lost", driver_id=188)
        self.assertEqual(
            self.calls[0][2],
            {
                "tenant_bid_id": "6f1c2a90-aaaa-4bbb-8ccc-000000000002",
                "status": "lost",
                "load_id": self.LOAD,
                "tenant_driver_id": 188,
            },
        )

    def test_the_client_leaves_out_a_driver_it_does_not_know(self) -> None:
        with patch(
            "hub.services.driver_hub.requests.request",
            return_value=_response(200, {"id": 31}),
        ) as req:
            driver_hub.update_bid_by_tenant_bid(
                "abc", "declined", load_id=self.LOAD, tenant_driver_id=None
            )
        self.assertEqual(
            req.call_args.kwargs["json"],
            {"tenant_bid_id": "abc", "status": "declined", "load_id": self.LOAD},
        )

    def test_a_404_is_quiet_and_the_rest_carry_on(self) -> None:
        replies = [
            _response(404, {"error": "no such driver-app bid", "code": "not_found"}),
            _response(200, {"id": 32, "status": "lost"}),
            _response(200, {"id": 12, "status": "lost"}),
        ]
        with patch("hub.bids.load_bids", return_value=self.BIDS), patch(
            "hub.services.driver_hub.requests.request", side_effect=replies
        ), self.assertLogs("hub", "INFO") as logs:
            reached = bids.relay_bid_status(self.LOAD, "lost")
        self.assertEqual(reached, [32, 12])
        self.assertEqual({r.levelname for r in logs.records}, {"INFO"})
        self.assertIn(
            "no driver-app bid 6f1c2a90-aaaa-4bbb-8ccc-000000000001",
            "\n".join(logs.output),
        )

    def test_the_endpoint_takes_driver_bid_ids(self) -> None:
        user = CustomUser.objects.create_user(
            username="maria", password="x", first_name="Maria", last_name="Lopez"
        )
        client = APIClient()
        client.force_authenticate(user)
        with patch("hub.bids.load_bids") as load_bids, patch(
            "hub.services.driver_hub.requests.request", side_effect=self.hub
        ):
            response = client.post(
                reverse("hub-bid-status"),
                {
                    "load_id": self.LOAD,
                    "status": "declined",
                    "driver_id": 188,
                    "driver_bid_ids": ["6f1c2a90-aaaa-4bbb-8ccc-000000000002"],
                    "note": "Declined by dispatch",
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json(),
            {
                "targets": [31],
                "declined_bid_ids": ["6f1c2a90-aaaa-4bbb-8ccc-000000000002"],
            },
        )
        load_bids.assert_not_called()
        self.assertEqual(
            self.calls[0][2],
            {
                "tenant_bid_id": "6f1c2a90-aaaa-4bbb-8ccc-000000000002",
                "status": "declined",
                "placed_by": "Maria Lopez",
                "note": "Declined by dispatch",
                "load_id": self.LOAD,
                "tenant_driver_id": 188,
            },
        )
        response = client.post(
            reverse("hub-bid-status"),
            {"load_id": self.LOAD, "status": "lost", "driver_bid_ids": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


@override_settings(**HUB)
class DriverBidDecisionTests(TestCase):
    """A dispatcher's decline is kept here too, so Bid history stops asking for a review."""

    LOAD = "8d1e4f2a-0000-4000-8000-000000000003"
    OTHER_LOAD = "8d1e4f2a-0000-4000-8000-000000000004"

    def setUp(self) -> None:
        Company.objects.create(name="Space Line LLC")
        active = DriverStatus.objects.create(name="Active")
        self.kwame = Driver.objects.create(full_name="Kwame Osei", status=active)
        self.jin = Driver.objects.create(full_name="Jin Park", status=active)
        self.maria = CustomUser.objects.create_user(
            username="maria", password="x", first_name="Maria", last_name="Lopez"
        )
        self.dylan = CustomUser.objects.create_user(username="dylan", password="x")
        self.bids = [
            {
                "id": "bid-a",
                "action": "driver_bid",
                "note": "TEST bid from QA",
                "driver": {"id": self.kwame.id},
            },
            {
                "id": "old-1",
                "action": "driver_bid",
                "note": "hub target #12",
                "driver": {"id": self.kwame.id},
            },
            {
                "id": "bid-c",
                "action": "driver_bid",
                "note": "Driver app bid",
                "driver": {"id": self.jin.id},
            },
            {
                "id": "disp-1",
                "action": "dispatcher_bid",
                "note": "Bid emailed",
                "driver": {"id": self.kwame.id},
            },
        ]
        self.calls: list[tuple[str, str, dict | None]] = []

    def hub(
        self, method: str, url: str, json: dict | None = None, **kwargs: object
    ) -> MagicMock:
        self.calls.append((method, url.replace("https://hub.test", ""), json))
        return _response(
            200, {"id": 31 if "by-tenant-bid" in url else 12, "status": json["status"]}
        )

    def post(
        self,
        body: dict,
        user: CustomUser | None = None,
        reply: Callable[..., MagicMock] | None = None,
    ) -> Response:
        client = APIClient()
        client.force_authenticate(user or self.maria)
        with patch("hub.bids.load_bids", return_value=self.bids), patch(
            "hub.services.driver_hub.requests.request", side_effect=reply or self.hub
        ):
            return client.post(
                reverse("hub-bid-status"),
                {"load_id": self.LOAD, "status": "declined", **body},
                format="json",
            )

    def test_a_named_decline_is_kept_and_relayed(self) -> None:
        response = self.post(
            {
                "driver_id": self.kwame.id,
                "driver_bid_ids": ["bid-a"],
                "note": "Declined by dispatch",
            }
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json(), {"targets": [31], "declined_bid_ids": ["bid-a"]}
        )
        row = DriverBidDecision.objects.get()
        self.assertEqual(
            (
                row.bid_id,
                row.load_id,
                row.driver_id,
                row.status,
                row.note,
                row.decided_by_id,
            ),
            (
                "bid-a",
                self.LOAD,
                self.kwame.id,
                "declined",
                "Declined by dispatch",
                self.maria.id,
            ),
        )
        self.assertEqual(
            [p for _, p, _ in self.calls], ["/t/v1/bid-targets/by-tenant-bid/status"]
        )

    def test_a_decline_by_load_and_driver_keeps_each_of_that_drivers_bids(self) -> None:
        response = self.post({"driver_id": self.kwame.id})
        self.assertEqual(
            sorted(response.json()["declined_bid_ids"]), ["bid-a", "old-1"]
        )
        self.assertEqual(
            sorted(DriverBidDecision.objects.values_list("bid_id", flat=True)),
            ["bid-a", "old-1"],
        )
        # The tagged bid still goes to its target; the other by its id.
        self.assertEqual(
            sorted(p for _, p, _ in self.calls),
            ["/t/v1/bid-targets/12/status", "/t/v1/bid-targets/by-tenant-bid/status"],
        )

    def test_old_target_ids_are_kept_under_their_bid_id(self) -> None:
        response = self.post({"driver_id": self.kwame.id, "hub_target_ids": [12]})
        self.assertEqual(
            response.json(), {"targets": [12], "declined_bid_ids": ["old-1"]}
        )

    def test_declining_again_updates_the_same_row(self) -> None:
        self.post(
            {"driver_bid_ids": ["bid-c"], "driver_id": self.jin.id, "note": "Too low"}
        )
        first = DriverBidDecision.objects.get()
        response = self.post(
            {
                "driver_bid_ids": ["bid-c"],
                "driver_id": self.jin.id,
                "note": "Broker is blacklisted",
            },
            user=self.dylan,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DriverBidDecision.objects.count(), 1)
        row = DriverBidDecision.objects.get()
        self.assertEqual(
            (row.pk, row.note, row.decided_by_id),
            (first.pk, "Broker is blacklisted", self.dylan.id),
        )
        self.assertGreaterEqual(row.decided_at, first.decided_at)

    def test_the_decision_is_kept_when_the_hub_is_down_says_404_or_is_off(self) -> None:
        down = self.post(
            {"driver_bid_ids": ["bid-a"]}, reply=requests.ConnectionError("refused")
        )
        missing = self.post(
            {"driver_bid_ids": ["bid-c"]},
            reply=lambda *a, **kw: _response(
                404, {"error": "no such bid", "code": "not_found"}
            ),
        )
        with override_settings(DRIVER_HUB_TOKEN=""):
            off = self.post({"driver_bid_ids": ["old-1"]})
        for response, bid in ((down, "bid-a"), (missing, "bid-c"), (off, "old-1")):
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(
                response.json(), {"targets": [], "declined_bid_ids": [bid]}
            )
        self.assertEqual(
            sorted(DriverBidDecision.objects.values_list("bid_id", flat=True)),
            ["bid-a", "bid-c", "old-1"],
        )

    def test_a_new_bid_after_a_decline_is_not_declined(self) -> None:
        self.bids = self.bids[:1]
        self.post({"driver_id": self.kwame.id})
        # The driver bids again on the same load: a new bid id.
        self.bids.append(
            {
                "id": "bid-a2",
                "action": "driver_bid",
                "note": "Can do 1300",
                "driver": {"id": self.kwame.id},
            }
        )
        client = APIClient()
        client.force_authenticate(self.maria)
        rows = client.get(
            reverse("hub-driver-bid-decisions"), {"load_ids": self.LOAD}
        ).json()
        self.assertEqual([r["bid_id"] for r in rows], ["bid-a"])

    def test_other_statuses_keep_nothing(self) -> None:
        response = self.post({"status": "lost"})
        self.assertEqual(response.json()["declined_bid_ids"], [])
        self.assertFalse(DriverBidDecision.objects.exists())

    def test_a_bad_driver_id_is_a_400(self) -> None:
        self.assertEqual(self.post({"driver_id": "kwame"}).status_code, 400)

    def test_the_list_for_the_web_app(self) -> None:
        self.post(
            {
                "driver_bid_ids": ["bid-a"],
                "driver_id": self.kwame.id,
                "note": "Declined by dispatch",
            }
        )
        DriverBidDecision.objects.create(
            bid_id="bid-z", load_id=self.OTHER_LOAD, note=""
        )
        DriverBidDecision.objects.create(bid_id="bid-y", load_id="elsewhere")
        client = APIClient()
        url = reverse("hub-driver-bid-decisions")
        self.assertEqual(url, "/api/hub/driver-bid-decisions/")
        self.assertEqual(client.get(url, {"load_ids": self.LOAD}).status_code, 401)

        client.force_authenticate(self.dylan)
        rows = client.get(url, {"load_ids": f"{self.LOAD}, {self.OTHER_LOAD},"}).json()
        self.assertEqual(sorted(r["bid_id"] for r in rows), ["bid-a", "bid-z"])
        row = next(r for r in rows if r["bid_id"] == "bid-a")
        saved = DriverBidDecision.objects.get(bid_id="bid-a")
        self.assertEqual(
            {k: v for k, v in row.items() if k != "decided_at"},
            {
                "bid_id": "bid-a",
                "load_id": self.LOAD,
                "driver_id": self.kwame.id,
                "status": "declined",
                "note": "Declined by dispatch",
                "decided_by": {"id": self.maria.id, "name": "Maria Lopez"},
            },
        )
        self.assertEqual(parse_datetime(row["decided_at"]), saved.decided_at)
        self.assertIsNone(next(r for r in rows if r["bid_id"] == "bid-z")["decided_by"])

        self.assertEqual(
            [r["bid_id"] for r in client.get(url, {"bid_ids": "bid-y"}).json()],
            ["bid-y"],
        )
        both = client.get(url, {"load_ids": self.OTHER_LOAD, "bid_ids": "bid-y"}).json()
        self.assertEqual(sorted(r["bid_id"] for r in both), ["bid-y", "bid-z"])
        self.assertEqual(client.get(url).status_code, 400)
        too_many = ",".join(f"load-{n}" for n in range(501))
        self.assertEqual(client.get(url, {"load_ids": too_many}).status_code, 400)
