"""Hiring board: columns, moves, the Active grace hour, and delete vs Out of service."""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from billing.models import Load
from hiring.models import Driver, DriverHistory, DriverStatus, HiringMove
from payroll.models import Deduction
from users.models import Company, CustomUser


class HiringBoardTests(TestCase):
    def setUp(self) -> None:
        Company.objects.create(name="Space Line LLC", shipment_number="10001")
        self.pending = DriverStatus.objects.create(name="Pending")
        self.active = DriverStatus.objects.create(name="Active")
        self.oos = DriverStatus.objects.create(name="Out of service")
        self.user = CustomUser.objects.create_user(username="sarah", password="x", first_name="Sarah", last_name="Mitchell")
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def _driver(self, name: str, status: DriverStatus, **kw) -> Driver:
        return Driver.objects.create(full_name=name, status=status, referral_by=self.user, **kw)

    def _board(self) -> dict:
        res = self.api.get("/api/hiring/board/")
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def _names(self, board: dict, col: str) -> list[str]:
        return [c["full_name"] for c in board[col]]

    def test_columns_and_newest_lead_first(self) -> None:
        old = self._driver("Old Lead", self.pending)
        Driver.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=3))
        self._driver("New Lead", self.pending)
        self._driver("Caller", self.pending, hiring_stage="called")
        self._driver("Readied", self.pending, hiring_stage="ready")
        self._driver("Fresh Active", self.active)
        self._driver("Gone", self.oos)
        b = self._board()
        self.assertEqual(self._names(b, "lead"), ["New Lead", "Old Lead"])
        self.assertEqual(self._names(b, "called"), ["Caller"])
        self.assertEqual(self._names(b, "ready"), ["Readied"])
        self.assertEqual(self._names(b, "active"), ["Fresh Active"])
        self.assertEqual(b["lead"][0]["invited_by"]["name"], "Sarah Mitchell")

    def test_active_driver_leaves_after_first_load(self) -> None:
        d = self._driver("Runner", self.active)
        Load.objects.create(driver=d, load_number="1")
        self.assertEqual(self._names(self._board(), "active"), [])

    def test_moved_to_active_with_a_load_stays_one_hour(self) -> None:
        d = self._driver("Runner", self.pending)
        Load.objects.create(driver=d, load_number="1")
        res = self.api.post(f"/api/hiring/drivers/{d.pk}/hiring-stage/", {"stage": "active"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["stage"], "active")
        self.assertEqual(res.json()["moved_by"]["name"], "Sarah Mitchell")
        self.assertEqual(self._names(self._board(), "active"), ["Runner"])
        Driver.objects.filter(pk=d.pk).update(hiring_moved_at=timezone.now() - timedelta(hours=1, minutes=1))
        self.assertEqual(self._names(self._board(), "active"), [])

    def test_move_sets_status_and_records_history(self) -> None:
        d = self._driver("Mover", self.pending)
        self.api.post(f"/api/hiring/drivers/{d.pk}/hiring-stage/", {"stage": "called"}, format="json")
        d.refresh_from_db()
        self.assertEqual((d.status_id, d.hiring_stage), (self.pending.id, "called"))
        self.api.post(f"/api/hiring/drivers/{d.pk}/hiring-stage/", {"stage": "active"}, format="json")
        d.refresh_from_db()
        self.assertEqual(d.status_id, self.active.id)
        self.api.post(f"/api/hiring/drivers/{d.pk}/hiring-stage/", {"stage": "ready"}, format="json")
        d.refresh_from_db()
        self.assertEqual((d.status_id, d.hiring_stage), (self.pending.id, "ready"))
        self.assertEqual(list(HiringMove.objects.filter(driver=d).values_list("stage", flat=True)), ["called", "active", "ready"])
        self.assertEqual(DriverHistory.objects.filter(driver=d).count(), 3)
        card = next(c for c in self._board()["ready"] if c["id"] == d.pk)
        self.assertEqual([m["stage"] for m in card["moves"]], ["called", "active", "ready"])

    def test_bad_stage_is_refused(self) -> None:
        d = self._driver("Mover", self.pending)
        res = self.api.post(f"/api/hiring/drivers/{d.pk}/hiring-stage/", {"stage": "hired"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_delete_without_records_deletes(self) -> None:
        d = self._driver("Nobody", self.pending)
        self.assertFalse(self.api.get("/api/hiring/driver-list/").json()["results"][0]["has_records"])
        res = self.api.delete(f"/api/hiring/drivers/{d.pk}/")
        self.assertEqual(res.status_code, 204)
        self.assertFalse(Driver.objects.filter(pk=d.pk).exists())

    def test_delete_with_a_load_sets_out_of_service(self) -> None:
        d = self._driver("Hauler", self.active)
        load = Load.objects.create(driver=d, load_number="7")
        self.assertTrue(self.api.get("/api/hiring/driver-list/").json()["results"][0]["has_records"])
        res = self.api.delete(f"/api/hiring/drivers/{d.pk}/")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["outcome"], "out_of_service")
        d.refresh_from_db()
        load.refresh_from_db()
        self.assertEqual(d.status_id, self.oos.id)
        self.assertEqual(load.driver_id, d.pk)

    def test_delete_with_an_expense_sets_out_of_service(self) -> None:
        d = self._driver("Spender", self.pending)
        Deduction.objects.create(driver=d, amount=Decimal("10"))
        res = self.api.delete(f"/api/hiring/drivers/{d.pk}/")
        self.assertEqual(res.json()["outcome"], "out_of_service")
        self.assertTrue(Deduction.objects.filter(driver=d).exists())
