"""Lead rotation, the lead team API, Telegram wiring and the lead_assigned event."""

import sys
import threading
import types
from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from hiring import leads
from hiring.models import (
    Driver,
    DriverFile,
    DriverInviteLink,
    DriverStatus,
    LeadAssignment,
    LeadPost,
    LeadSettings,
    LeadTeamMember,
    Vehicle,
    VehicleFile,
)
from users.models import Company, CustomUser, Department, Page, UserPageAccess


def _telegram_module() -> types.ModuleType:
    """hiring.telegram, or a stand-in with the same names while it is not merged yet.
    Every test patches the functions it relies on."""
    try:
        from hiring import telegram

        return telegram
    except ImportError:
        import hiring

        mod = types.ModuleType("hiring.telegram")

        class TelegramError(Exception):
            pass

        mod.TelegramError = TelegramError
        for name in ("check_token", "get_chat", "to_photo", "build_caption", "send_lead", "send_text"):
            setattr(mod, name, lambda *a, **k: None)
        sys.modules["hiring.telegram"] = mod
        hiring.telegram = mod
        return mod


TG = _telegram_module()
TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


class LeadBase(TestCase):
    def setUp(self) -> None:
        Company.objects.create(
            name="Space Line LLC", contract_template_text="Agreement with {{contractor_name}}."
        )
        self.pending = DriverStatus.objects.create(name="Pending")
        self.active = DriverStatus.objects.create(name="Active")
        DriverStatus.objects.create(name="Out of service")
        depts = {n: Department.objects.create(name=n) for n in ("Management", "Developer", "Dispatch Manager", "Dispatch", "Billing")}
        self.manager = self._user("boss", depts["Management"], "Nodira", "Karimova")
        self.dev = self._user("dev", depts["Developer"], "Dev", "Eloper")
        self.dm = self._user("dm", depts["Dispatch Manager"], "Diyor", "Manager")
        self.a = self._user("sardor", depts["Dispatch"], "Sardor", "Aliyev")
        self.b = self._user("bekzod", depts["Dispatch"], "Bekzod", "Tursunov")
        self.c = self._user("kamola", depts["Dispatch"], "Kamola", "Rashidova")
        self.billing = self._user("bill", depts["Billing"], "Bill", "Ing")

    def _user(self, username: str, dept: Department, first: str, last: str) -> CustomUser:
        return CustomUser.objects.create_user(
            username=username, password="x", first_name=first, last_name=last, department=dept, is_active=True
        )

    def _api(self, user: CustomUser) -> APIClient:
        api = APIClient()
        api.force_authenticate(user)
        return api

    def _team(self, *users: CustomUser) -> None:
        for u in users:
            leads.add_member(u, "", self.manager)

    def _driver(self, name: str = "Lead", **kw) -> Driver:
        kw.setdefault("status", self.pending)
        return Driver.objects.create(full_name=name, **kw)

    def _deal(self, n: int) -> list[str]:
        out = []
        for i in range(n):
            d = self._driver(f"Lead {i}")
            user = leads.assign_by_rotation(d.pk)
            out.append(user.username if user else None)
        return out


class RotationTests(LeadBase):
    def test_strict_turn_order_then_back_to_the_top(self) -> None:
        self._team(self.a, self.b, self.c)
        self.assertEqual(self._deal(5), ["sardor", "bekzod", "kamola", "sardor", "bekzod"])
        rows = list(LeadAssignment.objects.values_list("reason", "by_id"))
        self.assertEqual(rows, [("rotation", None)] * 5)
        self.assertEqual(LeadSettings.get().last_position, 2)

    def test_member_added_mid_rotation_joins_the_end(self) -> None:
        self._team(self.a, self.b)
        self.assertEqual(self._deal(1), ["sardor"])
        self._team(self.c)
        self.assertEqual(self._deal(4), ["bekzod", "kamola", "sardor", "bekzod"])

    def test_member_removed_mid_rotation_is_skipped(self) -> None:
        self._team(self.a, self.b, self.c)
        self.assertEqual(self._deal(1), ["sardor"])
        LeadTeamMember.objects.filter(user=self.b).delete()
        self.assertEqual(self._deal(3), ["kamola", "sardor", "kamola"])

    def test_last_assigned_member_removed_then_new_member_is_next(self) -> None:
        self._team(self.a, self.b)
        self.assertEqual(self._deal(2), ["sardor", "bekzod"])
        LeadTeamMember.objects.filter(user=self.b).delete()
        self._team(self.c)
        self.assertEqual(LeadTeamMember.objects.get(user=self.c).position, 3)
        self.assertEqual(self._deal(2), ["kamola", "sardor"])

    def test_empty_team_leaves_the_lead_unassigned(self) -> None:
        self.assertEqual(self._deal(1), [None])
        self.assertFalse(LeadAssignment.objects.exists())
        self.assertIsNone(Driver.objects.get().assigned_to_id)

    def test_inactive_account_is_skipped(self) -> None:
        self._team(self.a, self.b)
        CustomUser.objects.filter(pk=self.a.pk).update(is_active=False)
        self.assertEqual(self._deal(2), ["bekzod", "bekzod"])


class InviteSubmitTests(LeadBase):
    def setUp(self) -> None:
        super().setUp()
        self.invite = DriverInviteLink.objects.create(
            created_by=self.manager, expires_at=timezone.now() + timedelta(days=7)
        )
        on = patch("hiring.live.enabled", return_value=True)
        on.start()
        self.addCleanup(on.stop)
        for target in ("hiring.live.notify",):
            p = patch(target)
            p.start()
            self.addCleanup(p.stop)
        post = patch("hiring.live.post_event")
        self.post_event = post.start()
        self.addCleanup(post.stop)

    def _submit(self, invite: DriverInviteLink | None = None, phone: str = "8175550198", name: str = "Tyler Jenkins"):
        import io

        invite = invite or self.invite
        payload = {
            "company_name": "TJ Hauling",
            "company_city": "Fort Worth",
            "company_state": "TX",
            "driver": {"driver_full_name": name, "phone": phone},
        }
        with patch("hiring.views.fill_w9", return_value=io.BytesIO(b"%PDF-1.4")), patch(
            "hiring.views.generate_contract", return_value=io.BytesIO(b"%PDF-1.4")
        ), self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(
                f"{reverse('driver-invite-submit')}?token={invite.token}", payload, content_type="application/json"
            )
        self.assertEqual(res.status_code, 201, res.content)
        return Driver.objects.get(pk=res.json()["driver_id"])

    def test_new_driver_is_assigned_announced_and_posted(self) -> None:
        self._team(self.a, self.b)
        d = self._submit()
        self.assertEqual(d.assigned_to_id, self.a.pk)
        self.assertIsNotNone(d.assigned_at)
        # No bot token: the post is recorded as skipped.
        post = LeadPost.objects.get(driver=d)
        self.assertEqual((post.kind, post.status), ("lead", "skipped"))
        self.assertIn("bot token", post.error)
        event_type, payload = self.post_event.call_args.args
        self.assertEqual(event_type, "lead_assigned")
        self.assertEqual(payload["reason"], "new")
        self.assertEqual(payload["user_ids"][0], self.a.pk)
        self.assertEqual(set(payload["user_ids"]), {self.a.pk, self.manager.pk, self.dev.pk, self.dm.pk})
        lead = payload["lead"]
        self.assertEqual(lead["id"], d.pk)
        self.assertEqual(lead["full_name"], "Tyler Jenkins")
        self.assertEqual((lead["city"], lead["state"]), ("Fort Worth", "TX"))
        self.assertEqual(lead["assigned_to"], {"id": self.a.pk, "name": "Sardor Aliyev"})
        self.assertIsNone(lead["cdl_url"])
        self.assertFalse(lead["cdl_is_pdf"])

    def test_resubmit_does_not_reassign(self) -> None:
        self._team(self.a, self.b)
        first = self._submit()
        again = self._submit(name="Tyler J Jenkins")
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(LeadAssignment.objects.count(), 1)
        self.assertEqual(LeadPost.objects.count(), 1)
        other = DriverInviteLink.objects.create(created_by=self.manager)
        self.assertEqual(self._submit(other, phone="8175550111", name="Second").assigned_to_id, self.b.pk)

    def test_empty_team_still_posts_and_tells_admins(self) -> None:
        d = self._submit()
        self.assertIsNone(d.assigned_to_id)
        payload = self.post_event.call_args.args[1]
        self.assertEqual(set(payload["user_ids"]), {self.manager.pk, self.dev.pk, self.dm.pk})
        self.assertIsNone(payload["lead"]["assigned_to"])
        self.assertEqual(LeadPost.objects.filter(driver=d).count(), 1)

    def test_a_failing_rotation_never_breaks_the_submit(self) -> None:
        self._team(self.a)
        with patch("hiring.leads.assign_by_rotation", side_effect=RuntimeError("db down")):
            d = self._submit()
        self.assertIsNone(d.assigned_to_id)

    def test_other_create_paths_do_not_assign(self) -> None:
        self._team(self.a)
        api = self._api(self.manager)
        with self.captureOnCommitCallbacks(execute=True):
            res = api.post("/api/hiring/drivers/", {"full_name": "Quick", "status": self.pending.pk}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertFalse(LeadAssignment.objects.exists())
        self.assertFalse(LeadPost.objects.exists())


class TelegramWiringTests(LeadBase):
    def setUp(self) -> None:
        super().setUp()
        self._team(self.a)
        LeadTeamMember.objects.filter(user=self.a).update(telegram_username="sardor_aliyev")
        row = LeadSettings.get()
        row.bot_token_enc = leads.encrypt_token(TOKEN)
        row.chat_id = "-1002214558731"
        row.save()
        self.driver = self._driver("Tyler Jenkins", phone_number="8175550198")
        leads.assign_by_rotation(self.driver.pk)
        to_photo = patch("hiring.telegram.to_photo", side_effect=lambda data, name, ct: b"jpg:" + name.encode())
        self.to_photo = to_photo.start()
        self.addCleanup(to_photo.stop)
        caption = patch("hiring.telegram.build_caption", return_value="CAPTION")
        self.build_caption = caption.start()
        self.addCleanup(caption.stop)
        send = patch("hiring.telegram.send_lead", return_value={"message_id": "77", "attempts": 2})
        self.send_lead = send.start()
        self.addCleanup(send.stop)

    def _vehicle_file(self, name: str, filename: str) -> None:
        vehicle = self.driver.vehicles.first() or Vehicle.objects.create(
            driver=self.driver, make="Ford", model="Transit", payload=0, gvw=0
        )
        VehicleFile.objects.create(vehicle=vehicle, name=name, document=SimpleUploadedFile(filename, b"\xff\xd8 image"))

    def test_album_with_pdf_cdl_and_right_side(self) -> None:
        DriverFile.objects.create(
            driver=self.driver, name="Driver license", document=SimpleUploadedFile("cdl.pdf", b"%PDF-1.4 cdl")
        )
        self._vehicle_file("cargo space photo", "cargo.jpg")
        self._vehicle_file("Photo Right Side", "right.jpg")
        post = leads.post_lead(self.driver.pk)
        token, chat, photos, caption = self.send_lead.call_args.args
        self.assertEqual((token, chat, caption), (TOKEN, "-1002214558731", "CAPTION"))
        self.assertEqual(len(photos), 3)
        self.assertTrue(photos[0].startswith(b"jpg:cdl"))
        self.assertTrue(photos[1].startswith(b"jpg:cargo"))
        self.assertTrue(photos[2].startswith(b"jpg:right"))
        kw = self.build_caption.call_args.kwargs
        self.assertEqual(kw["full_name"], "Tyler Jenkins")
        self.assertEqual(kw["phone"], "8175550198")
        self.assertEqual(kw["dispatcher_name"], "Sardor Aliyev")
        self.assertEqual(kw["dispatcher_username"], "sardor_aliyev")
        self.assertEqual(kw["link"], f"https://spaceline.boxtruckmanage.com/hiring?driver={self.driver.pk}")
        self.assertEqual(kw["notes"], ["CDL came as a PDF, first page shown", "Right side photo (no left side)"])
        self.assertEqual((post.status, post.photos, post.attempts, post.telegram_message_id), ("sent", 3, 2, "77"))

    def test_left_side_wins_over_right(self) -> None:
        DriverFile.objects.create(driver=self.driver, name="Driver license", document=SimpleUploadedFile("cdl.jpg", b"\xff\xd8"))
        self._vehicle_file("Cargo Space Photo", "cargo.jpg")
        self._vehicle_file("Photo Left Side", "left.jpg")
        self._vehicle_file("Photo Right Side", "right.jpg")
        leads.post_lead(self.driver.pk)
        photos = self.send_lead.call_args.args[2]
        self.assertTrue(photos[2].startswith(b"jpg:left"))
        self.assertEqual(self.build_caption.call_args.kwargs["notes"], [])

    def test_missing_files_are_named_and_text_is_sent(self) -> None:
        post = leads.post_lead(self.driver.pk)
        self.assertEqual(self.send_lead.call_args.args[2], [])
        self.assertEqual(
            self.build_caption.call_args.kwargs["notes"], ["No CDL uploaded", "No cargo space photo", "No side photo"]
        )
        self.assertEqual((post.status, post.photos), ("sent", 0))

    def test_a_file_that_is_not_a_picture_is_skipped(self) -> None:
        self.to_photo.side_effect = lambda data, name, ct: None
        self._vehicle_file("Cargo Space Photo", "cargo.txt")
        leads.post_lead(self.driver.pk)
        self.assertIn("No cargo space photo", self.build_caption.call_args.kwargs["notes"])

    def test_telegram_error_is_recorded(self) -> None:
        self.send_lead.side_effect = TG.TelegramError("Bad Request: chat not found")
        post = leads.post_lead(self.driver.pk)
        self.assertEqual((post.status, post.error), ("failed", "Bad Request: chat not found"))

    def test_unassigned_lead_has_no_dispatcher(self) -> None:
        d = self._driver("Nobody")
        leads.post_lead(d.pk)
        kw = self.build_caption.call_args.kwargs
        self.assertEqual((kw["dispatcher_name"], kw["dispatcher_username"]), ("", ""))

    def test_skipped_without_token_or_chat(self) -> None:
        LeadSettings.objects.filter(pk=1).update(chat_id="")
        post = leads.post_lead(self.driver.pk)
        self.assertEqual((post.status, post.error), ("skipped", "No group chat ID is saved."))
        LeadSettings.objects.filter(pk=1).update(chat_id="-100123456", bot_token_enc="")
        post = leads.post_lead(self.driver.pk)
        self.assertEqual((post.status, post.error), ("skipped", "No bot token is saved."))
        self.send_lead.assert_not_called()

    def test_link_base_from_env(self) -> None:
        with patch("hiring.leads.config", return_value="https://staging.example.com/"):
            self.assertEqual(leads.card_link(5), "https://staging.example.com/hiring?driver=5")


class BoardScopeTests(LeadBase):
    def setUp(self) -> None:
        super().setUp()
        self._team(self.a, self.b)
        self.mine = self._driver("Mine")
        self.theirs = self._driver("Theirs")
        self.nobody = self._driver("Nobody")
        leads.assign_by_rotation(self.mine.pk)
        leads.assign_by_rotation(self.theirs.pk)

    def _lead_names(self, user: CustomUser) -> list[str]:
        res = self._api(user).get("/api/hiring/board/")
        self.assertEqual(res.status_code, 200, res.content)
        return sorted(c["full_name"] for c in res.json()["lead"])

    def test_team_member_sees_only_their_own(self) -> None:
        self.assertEqual(self._lead_names(self.a), ["Mine"])
        body = self._api(self.a).get("/api/hiring/board/").json()
        self.assertEqual(body["viewer"], {"user_id": self.a.pk, "is_admin": False, "on_team": True})

    def test_admin_and_other_users_see_everything(self) -> None:
        self.assertEqual(self._lead_names(self.manager), ["Mine", "Nobody", "Theirs"])
        self.assertEqual(self._lead_names(self.billing), ["Mine", "Nobody", "Theirs"])
        body = self._api(self.dm).get("/api/hiring/board/").json()
        self.assertEqual(body["viewer"], {"user_id": self.dm.pk, "is_admin": True, "on_team": False})

    def test_admin_on_the_team_still_sees_everything(self) -> None:
        self._team(self.manager)
        self.assertEqual(self._lead_names(self.manager), ["Mine", "Nobody", "Theirs"])

    def test_card_fields(self) -> None:
        LeadPost.objects.create(driver=self.mine, kind="test", status="sent")
        LeadPost.objects.create(driver=self.mine, kind="lead", status="failed", error="chat not found")
        card = next(c for c in self._api(self.manager).get("/api/hiring/board/").json()["lead"] if c["id"] == self.mine.pk)
        self.assertEqual(card["assigned_to"], {"id": self.a.pk, "name": "Sardor Aliyev"})
        self.assertIsNotNone(card["assigned_at"])
        self.assertEqual(len(card["assignments"]), 1)
        self.assertEqual(card["assignments"][0]["to"], {"id": self.a.pk, "name": "Sardor Aliyev"})
        self.assertIsNone(card["assignments"][0]["by"])
        self.assertEqual(card["assignments"][0]["reason"], "rotation")
        self.assertEqual(card["telegram"]["status"], "failed")
        self.assertEqual(card["telegram"]["error"], "chat not found")
        other = next(c for c in self._api(self.manager).get("/api/hiring/board/").json()["lead"] if c["id"] == self.nobody.pk)
        self.assertIsNone(other["assigned_to"])
        self.assertIsNone(other["telegram"])
        self.assertEqual(other["assignments"], [])

    def test_team_member_moves_and_deletes_only_their_own(self) -> None:
        api = self._api(self.a)
        res = api.post(f"/api/hiring/drivers/{self.mine.pk}/hiring-stage/", {"stage": "called"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["assigned_to"]["id"], self.a.pk)
        res = api.post(f"/api/hiring/drivers/{self.theirs.pk}/hiring-stage/", {"stage": "called"}, format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(api.delete(f"/api/hiring/drivers/{self.theirs.pk}/").status_code, 403)
        self.assertEqual(api.delete(f"/api/hiring/drivers/{self.nobody.pk}/").status_code, 403)
        self.assertEqual(api.delete(f"/api/hiring/drivers/{self.mine.pk}/").status_code, 204)
        self.assertTrue(Driver.objects.filter(pk=self.theirs.pk).exists())

    def test_admins_and_non_members_keep_their_rights(self) -> None:
        res = self._api(self.billing).post(f"/api/hiring/drivers/{self.theirs.pk}/hiring-stage/", {"stage": "ready"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._api(self.dm).delete(f"/api/hiring/drivers/{self.theirs.pk}/").status_code, 204)


class LeadCountTests(LeadBase):
    def test_scopes(self) -> None:
        self._team(self.a, self.b)
        d1, d2, d3 = self._driver("One"), self._driver("Two"), self._driver("Three")
        for d in (d1, d2, d3):
            leads.assign_by_rotation(d.pk)  # a, b, a
        self._driver("Unassigned")
        self._driver("Called", hiring_stage="called")
        self._driver("Lead stage", hiring_stage="lead")
        Driver.objects.filter(pk=d3.pk).update(hiring_stage="ready")
        self._driver("Active one", status=self.active)
        get = lambda u: self._api(u).get("/api/hiring/lead-count/").json()  # noqa: E731
        self.assertEqual(get(self.manager), {"count": 4, "scope": "all"})
        self.assertEqual(get(self.dev), {"count": 4, "scope": "all"})
        self.assertEqual(get(self.a), {"count": 1, "scope": "mine"})
        self.assertEqual(get(self.b), {"count": 1, "scope": "mine"})
        self.assertEqual(get(self.c), {"count": 0, "scope": "none"})
        self.assertEqual(get(self.billing), {"count": 0, "scope": "none"})


class ReassignTests(LeadBase):
    def setUp(self) -> None:
        super().setUp()
        self._team(self.a, self.b)
        self.driver = self._driver("Tyler")
        leads.assign_by_rotation(self.driver.pk)
        on = patch("hiring.live.enabled", return_value=True)
        on.start()
        self.addCleanup(on.stop)
        notify = patch("hiring.live.notify")
        notify.start()
        self.addCleanup(notify.stop)
        post = patch("hiring.live.post_event")
        self.post_event = post.start()
        self.addCleanup(post.stop)

    def _assign(self, user: CustomUser, target: CustomUser, driver: Driver | None = None):
        driver = driver or self.driver
        with self.captureOnCommitCallbacks(execute=True):
            return self._api(user).post(f"/api/hiring/drivers/{driver.pk}/assign/", {"user_id": target.pk}, format="json")

    def test_admin_reassigns(self) -> None:
        res = self._assign(self.dm, self.b)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["assigned_to"], {"id": self.b.pk, "name": "Bekzod Tursunov"})
        self.assertEqual([a["reason"] for a in res.json()["assignments"]], ["rotation", "manual"])
        self.assertEqual(res.json()["assignments"][1]["by"]["id"], self.dm.pk)
        event_type, payload = self.post_event.call_args.args
        self.assertEqual((event_type, payload["reason"], payload["user_ids"]), ("lead_assigned", "reassigned", [self.b.pk]))
        self.assertEqual(payload["lead"]["assigned_to"]["id"], self.b.pk)
        self.assertFalse(LeadPost.objects.exists())
        # The rotation pointer did not move: the next new lead goes to b, after a.
        self.assertEqual(self._deal(1), ["bekzod"])

    def test_assigning_an_old_unassigned_lead(self) -> None:
        old = self._driver("Old")
        res = self._assign(self.manager, self.a, old)
        self.assertEqual(res.status_code, 200, res.content)
        old.refresh_from_db()
        self.assertEqual(old.assigned_to_id, self.a.pk)

    def test_only_admins_and_only_to_team_members(self) -> None:
        self.assertEqual(self._assign(self.a, self.b).status_code, 403)
        self.assertEqual(self._assign(self.billing, self.b).status_code, 403)
        res = self._assign(self.manager, self.c)
        self.assertEqual(res.status_code, 400)
        self.assertIn("not on the lead team", res.json()["detail"])
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.assigned_to_id, self.a.pk)
        self.post_event.assert_not_called()


class TeamApiTests(LeadBase):
    def test_get_lists_members_in_order_with_next_and_month_count(self) -> None:
        self._team(self.a, self.b, self.c)
        d1, d2 = self._driver("One"), self._driver("Two")
        leads.assign_by_rotation(d1.pk)
        leads.assign_by_rotation(d2.pk)
        Driver.objects.filter(pk=d2.pk).update(assigned_at=leads.month_start() - timedelta(minutes=1))
        res = self._api(self.dm).get("/api/hiring/lead-team/")
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual([m["user"]["id"] for m in body["members"]], [self.a.pk, self.b.pk, self.c.pk])
        self.assertEqual(body["members"][0]["user"], {"id": self.a.pk, "name": "Sardor Aliyev", "department": "Dispatch"})
        self.assertEqual([m["month_count"] for m in body["members"]], [1, 0, 0])
        self.assertEqual([m["next"] for m in body["members"]], [False, False, True])
        self.assertEqual(body["next_user_id"], self.c.pk)
        self.assertEqual(body["chat"], {"chat_id": "", "chat_title": "", "connected": False})
        self.assertEqual(body["bot"], {"configured": False, "username": ""})

    def test_only_admins(self) -> None:
        self.assertEqual(self._api(self.a).get("/api/hiring/lead-team/").status_code, 403)
        self.assertEqual(self._api(self.billing).post("/api/hiring/lead-team/", {"user_id": self.a.pk}).status_code, 403)
        self.assertEqual(self._api(self.dev).get("/api/hiring/lead-team/").status_code, 200)

    def test_add_validates(self) -> None:
        api = self._api(self.manager)
        res = api.post("/api/hiring/lead-team/", {"user_id": self.a.pk, "telegram_username": "@sardor_aliyev"}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["telegram_username"], "sardor_aliyev")
        self.assertEqual(res.json()["position"], 1)
        self.assertEqual(LeadTeamMember.objects.get(user=self.a).added_by_id, self.manager.pk)
        res = api.post("/api/hiring/lead-team/", {"user_id": self.a.pk}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("already", res.json()["detail"])
        CustomUser.objects.filter(pk=self.b.pk).update(is_active=False)
        self.assertEqual(api.post("/api/hiring/lead-team/", {"user_id": self.b.pk}, format="json").status_code, 400)
        res = api.post("/api/hiring/lead-team/", {"user_id": self.c.pk, "telegram_username": "ab"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(LeadTeamMember.objects.filter(user=self.c).exists())
        self.assertEqual(api.post("/api/hiring/lead-team/", {"user_id": 99999}, format="json").status_code, 400)
        self.assertEqual(api.post("/api/hiring/lead-team/", {}, format="json").status_code, 400)
        res = api.post("/api/hiring/lead-team/", {"user_id": self.c.pk}, format="json")
        self.assertEqual((res.status_code, res.json()["telegram_username"], res.json()["position"]), (201, "", 2))

    def test_patch_username(self) -> None:
        self._team(self.a)
        api = self._api(self.manager)
        url = f"/api/hiring/lead-team/{self.a.pk}/"
        res = api.patch(url, {"telegram_username": " @Sardor_99 "}, format="json")
        self.assertEqual((res.status_code, res.json()["telegram_username"]), (200, "Sardor_99"))
        for bad in ("9sardor", "sar", "sardor-aliyev", "a" * 33):
            res = api.patch(url, {"telegram_username": bad}, format="json")
            self.assertEqual(res.status_code, 400, bad)
            self.assertTrue(res.json()["detail"])
        self.assertEqual(api.patch(url, {"telegram_username": ""}, format="json").json()["telegram_username"], "")
        self.assertEqual(api.patch(f"/api/hiring/lead-team/{self.b.pk}/", {"telegram_username": ""}, format="json").status_code, 404)

    def test_delete_keeps_their_leads(self) -> None:
        self._team(self.a, self.b)
        d = self._driver("Kept")
        leads.assign_by_rotation(d.pk)
        res = self._api(self.manager).delete(f"/api/hiring/lead-team/{self.a.pk}/")
        self.assertEqual(res.status_code, 204)
        d.refresh_from_db()
        self.assertEqual(d.assigned_to_id, self.a.pk)
        self.assertFalse(LeadTeamMember.objects.filter(user=self.a).exists())
        self.assertEqual(self._api(self.manager).delete(f"/api/hiring/lead-team/{self.a.pk}/").status_code, 404)


class ChatAndTestMessageTests(LeadBase):
    def _set_token(self) -> None:
        row = LeadSettings.get()
        row.bot_token_enc = leads.encrypt_token(TOKEN)
        row.bot_username = "btm_leads_bot"
        row.save()

    def test_chat_id_validation_and_title(self) -> None:
        api = self._api(self.dm)
        for bad in ("abc", "123", "-100-123", ""):
            self.assertEqual(api.put("/api/hiring/lead-settings/chat/", {"chat_id": bad}, format="json").status_code, 400, bad)
        res = api.put("/api/hiring/lead-settings/chat/", {"chat_id": "-1002214558731"}, format="json")
        self.assertEqual(res.json(), {"chat_id": "-1002214558731", "chat_title": "", "connected": False})
        self._set_token()
        with patch("hiring.telegram.get_chat", return_value={"title": "BTM Leads", "type": "supergroup", "members": 14}) as gc:
            res = api.put("/api/hiring/lead-settings/chat/", {"chat_id": "-1002214558731"}, format="json")
        gc.assert_called_once_with(TOKEN, "-1002214558731")
        self.assertEqual(res.json(), {"chat_id": "-1002214558731", "chat_title": "BTM Leads", "connected": True})
        with patch("hiring.telegram.get_chat", side_effect=TG.TelegramError("Bad Request: chat not found")):
            res = api.put("/api/hiring/lead-settings/chat/", {"chat_id": "-1009999999999"}, format="json")
        self.assertEqual((res.status_code, res.json()["detail"]), (400, "Bad Request: chat not found"))
        self.assertEqual(LeadSettings.get().chat_id, "-1002214558731")
        self.assertEqual(api.delete("/api/hiring/lead-settings/chat/").status_code, 204)
        self.assertEqual((LeadSettings.get().chat_id, LeadSettings.get().chat_title), ("", ""))
        self.assertEqual(self._api(self.a).put("/api/hiring/lead-settings/chat/", {"chat_id": "-1002214558731"}, format="json").status_code, 403)

    def test_send_test_message(self) -> None:
        api = self._api(self.manager)
        res = api.post("/api/hiring/lead-settings/test/")
        self.assertEqual(res.status_code, 400)
        self._set_token()
        LeadSettings.objects.filter(pk=1).update(chat_id="-1002214558731")
        with patch("hiring.telegram.send_text", return_value="501") as st:
            res = api.post("/api/hiring/lead-settings/test/")
        self.assertEqual((res.status_code, res.json()), (200, {"ok": True}))
        self.assertEqual(st.call_args.args, (TOKEN, "-1002214558731", leads.TEST_MESSAGE))
        post = LeadPost.objects.filter(kind="test").first()
        self.assertEqual((post.status, post.telegram_message_id), ("sent", "501"))
        with patch("hiring.telegram.send_text", side_effect=TG.TelegramError("Forbidden: bot was kicked from the supergroup chat")):
            res = api.post("/api/hiring/lead-settings/test/")
        self.assertEqual((res.status_code, res.json()["detail"]), (400, "Forbidden: bot was kicked from the supergroup chat"))
        self.assertEqual(LeadPost.objects.filter(kind="test").first().status, "failed")
        self.assertEqual(self._api(self.a).post("/api/hiring/lead-settings/test/").status_code, 403)


class BotTokenTests(LeadBase):
    def test_developers_only(self) -> None:
        for user in (self.manager, self.dm, self.a):
            self.assertEqual(self._api(user).get("/api/hiring/lead-settings/bot/").status_code, 403)
            self.assertEqual(self._api(user).get("/api/hiring/lead-posts/").status_code, 403)

    def test_token_saved_encrypted_and_never_returned(self) -> None:
        api = self._api(self.dev)
        self.assertEqual(api.get("/api/hiring/lead-settings/bot/").json()["configured"], False)
        with patch("hiring.telegram.check_token", return_value="btm_leads_bot"):
            res = api.put("/api/hiring/lead-settings/bot/", {"token": TOKEN}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body["masked"], "123456789:AAHd" + "•" * 22 + "saw")
        self.assertEqual((body["configured"], body["username"]), (True, "btm_leads_bot"))
        self.assertEqual(body["saved_by"], {"id": self.dev.pk, "name": "Dev Eloper"})
        self.assertIsNotNone(body["saved_at"])
        row = LeadSettings.get()
        self.assertNotIn(TOKEN, row.bot_token_enc)
        self.assertEqual(leads.bot_token(row), TOKEN)
        secret = TOKEN.split(":")[1]
        for res in (
            api.get("/api/hiring/lead-settings/bot/"),
            self._api(self.manager).get("/api/hiring/lead-team/"),
        ):
            self.assertNotIn(secret, res.content.decode())
        self.assertEqual(self._api(self.manager).get("/api/hiring/lead-team/").json()["bot"], {"configured": True, "username": "btm_leads_bot"})
        self.assertEqual(api.delete("/api/hiring/lead-settings/bot/").status_code, 204)
        self.assertEqual(api.get("/api/hiring/lead-settings/bot/").json(), {"configured": False, "username": "", "masked": "", "saved_at": None, "saved_by": None})

    def test_bad_token_is_refused(self) -> None:
        api = self._api(self.dev)
        with patch("hiring.telegram.check_token", side_effect=TG.TelegramError("Unauthorized")):
            res = api.put("/api/hiring/lead-settings/bot/", {"token": TOKEN}, format="json")
        self.assertEqual((res.status_code, res.json()["detail"]), (400, "Unauthorized"))
        self.assertEqual(api.put("/api/hiring/lead-settings/bot/", {"token": "nonsense"}, format="json").status_code, 400)
        self.assertEqual(LeadSettings.get().bot_token_enc, "")

    def test_saving_token_fills_an_existing_chat_title(self) -> None:
        LeadSettings.objects.update_or_create(pk=1, defaults={"chat_id": "-1002214558731"})
        with patch("hiring.telegram.check_token", return_value="btm_leads_bot"), patch(
            "hiring.telegram.get_chat", return_value={"title": "BTM Leads", "type": "supergroup", "members": 3}
        ):
            self._api(self.dev).put("/api/hiring/lead-settings/bot/", {"token": TOKEN}, format="json")
        self.assertEqual(LeadSettings.get().chat_title, "BTM Leads")

    def test_lead_posts_newest_first(self) -> None:
        self._team(self.a)
        d = self._driver("Tyler")
        leads.assign_by_rotation(d.pk)
        LeadPost.objects.create(driver=d, kind="lead", status="sent", photos=2, attempts=1)
        LeadPost.objects.create(kind="test", status="failed", error="nope")
        res = self._api(self.dev).get("/api/hiring/lead-posts/?limit=1")
        self.assertEqual(len(res.json()), 1)
        rows = self._api(self.dev).get("/api/hiring/lead-posts/").json()
        self.assertEqual([r["kind"] for r in rows], ["test", "lead"])
        self.assertIsNone(rows[0]["driver"])
        self.assertEqual(rows[1]["driver"], {"id": d.pk, "name": "Tyler"})
        self.assertEqual(rows[1]["assigned_to"], {"id": self.a.pk, "name": "Sardor Aliyev"})
        self.assertEqual(set(rows[1]), {"id", "at", "kind", "status", "error", "photos", "attempts", "driver", "assigned_to"})


class PageAccessTests(LeadBase):
    def _pages(self, user: CustomUser) -> list[dict]:
        return self._api(user).get("/api/users/my-pages/").json()

    def _profile_pages(self, user: CustomUser) -> list[dict]:
        res = self._api(user).get("/api/users/user-profile/").json()
        return res[0]["page_access"]

    def test_team_member_gets_hiring_without_rows(self) -> None:
        loads = Page.objects.create(key="loads", name="Loads", path="/loads")
        UserPageAccess.objects.create(user=self.a, page=loads, can_view=True)
        self.assertEqual([p["key"] for p in self._pages(self.a)], ["loads"])
        self._team(self.a)
        pages = self._pages(self.a)
        hiring = next(p for p in pages if p["key"] == "hiring")
        self.assertEqual(hiring, {"key": "hiring", "name": "Hiring", "path": "/hiring", "can_view": True, "can_create": False, "can_edit": True, "can_delete": True})
        prof = next(p for p in self._profile_pages(self.a) if p["page_key"] == "hiring")
        self.assertTrue(prof["can_view"] and prof["can_edit"] and prof["can_delete"])
        self.assertEqual(UserPageAccess.objects.filter(user=self.a).count(), 1)
        LeadTeamMember.objects.filter(user=self.a).delete()
        self.assertNotIn("hiring", [p["key"] for p in self._pages(self.a)])
        self.assertNotIn("hiring", [p["page_key"] for p in self._profile_pages(self.a)])

    def test_existing_hiring_row_is_widened(self) -> None:
        page = Page.objects.create(key="hiring", name="Hiring", path="/hiring")
        UserPageAccess.objects.create(user=self.a, page=page, can_view=False)
        self._team(self.a)
        rows = [p for p in self._profile_pages(self.a) if p["page_key"] == "hiring"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["can_view"] and rows[0]["can_edit"] and rows[0]["can_delete"])
        self.assertEqual([p["key"] for p in self._pages(self.a)], ["hiring"])


class RotationLockTests(TransactionTestCase):
    """Two submits at the same moment get two different people."""

    def test_two_simultaneous_assignments(self) -> None:
        pending = DriverStatus.objects.create(name="Pending")
        users = [
            CustomUser.objects.create_user(username=f"u{i}", password="x", first_name="U", last_name=str(i), is_active=True)
            for i in range(2)
        ]
        for u in users:
            leads.add_member(u, "", None)
        drivers = [Driver.objects.create(full_name=f"D{i}", status=pending) for i in range(2)]
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []
        real_next = leads.next_member

        def slow_next(*args, **kwargs):
            member = real_next(*args, **kwargs)
            import time

            time.sleep(0.3)  # widen the window two unlocked readers would both hit
            return member

        def run(driver_id: int) -> None:
            try:
                barrier.wait()
                leads.assign_by_rotation(driver_id)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        with patch("hiring.leads.next_member", side_effect=slow_next):
            threads = [threading.Thread(target=run, args=(d.pk,)) for d in drivers]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(errors, [])
        got = sorted(Driver.objects.values_list("assigned_to_id", flat=True))
        self.assertEqual(got, sorted(u.pk for u in users))
