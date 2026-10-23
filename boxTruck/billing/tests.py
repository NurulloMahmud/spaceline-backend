from unittest import mock

from django.db.utils import IntegrityError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from datetime import datetime
from decimal import Decimal

from billing.models import BrokerBlacklist, Load, LoadStatus, normalize_mc
from hiring.models import Driver, DriverStatus
from users.models import Company, CustomUser, Department


class NormalizeMCTests(TestCase):
    def test_variants_collapse(self):
        for raw in ["MC 312916", "312916 ", "312916", "mc#312916", "0312916"]:
            self.assertEqual(normalize_mc(raw), "312916", raw)

    def test_no_digits(self):
        self.assertEqual(normalize_mc("BROKER M.C. NOT ON FILE"), "")
        self.assertEqual(normalize_mc(None), "")
        self.assertEqual(normalize_mc("000"), "")


class ApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mgmt = Department.objects.create(name="Management")
        cls.disp = Department.objects.create(name="Dispatch")
        cls.co_a = Company.objects.create(name="A")
        cls.co_b = Company.objects.create(name="B")
        cls.mgr_a = CustomUser.objects.create_user(
            username="mgr_a", password="x", first_name="M", last_name="A",
            company=cls.co_a, department=cls.mgmt, is_active=True)
        cls.mgr_b = CustomUser.objects.create_user(
            username="mgr_b", password="x", first_name="M", last_name="B",
            company=cls.co_b, department=cls.mgmt, is_active=True)
        cls.disp_a = CustomUser.objects.create_user(
            username="disp_a", password="x", first_name="D", last_name="A",
            company=cls.co_a, department=cls.disp, is_active=True)

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_create_normalizes_and_scopes(self):
        c = self.client_for(self.mgr_a)
        r = c.post("/api/billing/broker-blacklist/",
                   {"name": "Priority 1 Inc", "mc": "MC 0312916", "reason": "no pay",
                    "company": self.co_b.id}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["mc"], "312916")
        row = BrokerBlacklist.objects.get(pk=r.data["id"])
        self.assertEqual(row.company, self.co_a)   # body company_id ignored
        self.assertEqual(row.created_by, self.mgr_a)

    def test_post_is_idempotent(self):
        c = self.client_for(self.mgr_a)
        r1 = c.post("/api/billing/broker-blacklist/", {"name": "X", "mc": "312916"}, format="json")
        r2 = c.post("/api/billing/broker-blacklist/", {"name": "X Again", "mc": "  312916"}, format="json")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 200, r2.data)
        self.assertEqual(r1.data["id"], r2.data["id"])
        self.assertEqual(BrokerBlacklist.objects.count(), 1)

    def test_name_only_idempotent_case_insensitive(self):
        c = self.client_for(self.mgr_a)
        r1 = c.post("/api/billing/broker-blacklist/",
                    {"name": "Circle Logistics", "mc": "BROKER M.C. NOT ON FILE"}, format="json")
        r2 = c.post("/api/billing/broker-blacklist/", {"name": "CIRCLE LOGISTICS"}, format="json")
        self.assertEqual(r1.status_code, 201, r1.data)
        self.assertEqual(r1.data["mc"], "")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(BrokerBlacklist.objects.count(), 1)

    def test_both_empty_is_400(self):
        c = self.client_for(self.mgr_a)
        r = c.post("/api/billing/broker-blacklist/", {"name": "  ", "mc": "N/A"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("name or an MC", str(r.data))

    def test_dispatcher_forbidden(self):
        c = self.client_for(self.disp_a)
        self.assertEqual(c.get("/api/billing/broker-blacklist/").status_code, 403)
        self.assertEqual(c.post("/api/billing/broker-blacklist/",
                                {"name": "X"}, format="json").status_code, 403)

    def test_list_and_delete_are_company_scoped(self):
        mine = BrokerBlacklist.objects.create(company=self.co_a, name="Mine", mc="111")
        theirs = BrokerBlacklist.objects.create(company=self.co_b, name="Theirs", mc="222")
        c = self.client_for(self.mgr_a)
        r = c.get("/api/billing/broker-blacklist/")
        self.assertEqual([e["id"] for e in r.data], [mine.id])
        self.assertEqual(c.delete(f"/api/billing/broker-blacklist/{theirs.id}/").status_code, 404)
        self.assertTrue(BrokerBlacklist.objects.filter(pk=theirs.id).exists())
        self.assertEqual(c.delete(f"/api/billing/broker-blacklist/{mine.id}/").status_code, 204)

    def test_update_methods_are_not_exposed(self):
        row = BrokerBlacklist.objects.create(company=self.co_a, name="Mine", mc="111")
        c = self.client_for(self.mgr_a)
        url = f"/api/billing/broker-blacklist/{row.id}/"
        self.assertEqual(c.put(url, {"name": "Other"}, format="json").status_code, 405)
        self.assertEqual(c.patch(url, {"name": "Other"}, format="json").status_code, 405)
        self.assertEqual(c.get(url).status_code, 200)

    def test_same_mc_allowed_across_companies(self):
        BrokerBlacklist.objects.create(company=self.co_a, name="P1", mc="312916")
        BrokerBlacklist.objects.create(company=self.co_b, name="P1", mc="312916")
        self.assertEqual(BrokerBlacklist.objects.count(), 2)

    def test_db_constraints(self):
        BrokerBlacklist.objects.create(company=self.co_a, name="P1", mc="312916")
        with self.assertRaises(IntegrityError):
            BrokerBlacklist.objects.create(company=self.co_a, name="Other", mc="312916")

    def test_check_constraint(self):
        with self.assertRaises(IntegrityError):
            BrokerBlacklist.objects.create(company=self.co_a, name="", mc="")

    def test_empty_mc_rows_may_repeat_name_when_mc_set(self):
        BrokerBlacklist.objects.create(company=self.co_a, name="Same", mc="1")
        BrokerBlacklist.objects.create(company=self.co_a, name="Same", mc="2")
        self.assertEqual(BrokerBlacklist.objects.count(), 2)

    def test_internal_bulk(self):
        import config.settings as cs
        self.enterContext(mock.patch.object(cs, "INTERNAL_SERVICE_SECRET", "s3cret"))
        BrokerBlacklist.objects.create(company=self.co_a, name="Priority 1 Inc", mc="312916")
        BrokerBlacklist.objects.create(company=self.co_b, name="No File Broker", mc="")
        c = APIClient()
        url = "/api/billing/internal/broker-blacklist-bulk/"
        self.assertIn(c.get(url).status_code, (401, 403))
        r = c.get(url, headers={"X-Internal-Secret": "s3cret"})
        self.assertEqual(r.status_code, 200)
        self.assertCountEqual(r.data, [
            {"company_id": self.co_a.id, "mc": "312916", "name": "Priority 1 Inc"},
            {"company_id": self.co_b.id, "mc": "", "name": "No File Broker"},
        ])


class DriversReportTests(TestCase):
    URL = "/api/billing/drivers-report/"

    @classmethod
    def setUpTestData(cls):
        cls.mgmt = Department.objects.create(name="Management")
        cls.disp = Department.objects.create(name="Dispatch")
        # Load.save() numbers each load from its company's shipment_number.
        cls.co_a = Company.objects.create(name="A", shipment_number="1000")
        cls.co_b = Company.objects.create(name="B", shipment_number="1000")
        cls.mgr = CustomUser.objects.create_user(
            username="mgr", password="x", first_name="M", last_name="G",
            company=cls.co_a, department=cls.mgmt, is_active=True)
        cls.disp_a = CustomUser.objects.create_user(
            username="disp", password="x", first_name="D", last_name="A",
            company=cls.co_a, department=cls.disp, is_active=True)
        status = DriverStatus.objects.create(name="Active")
        cls.d1 = Driver.objects.create(company=cls.co_a, full_name="Ann", unit_number="U1", status=status)
        cls.d2 = Driver.objects.create(company=cls.co_b, full_name="Bob", unit_number="U2", status=status)
        ok = LoadStatus.objects.create(name="Delivered")
        cancelled = LoadStatus.objects.create(name="Cancelled")

        def load(company, driver, carrier, pay, day, status=ok):
            return Load.objects.create(
                company=company, driver=driver, carrier_pay=carrier, driver_pay=pay,
                pickup_date=timezone.make_aware(datetime(2026, 10, day, 9, 0)), status=status)

        load(cls.co_a, cls.d1, 1000, 700, 10)
        load(cls.co_a, cls.d1, 500, 300, 11)
        load(cls.co_a, cls.d1, 9999, 1, 12, status=cancelled)   # excluded
        load(cls.co_a, cls.d1, 800, 500, 25)                     # outside the period
        load(cls.co_b, cls.d2, 2000, 1500, 10)

    def get(self, user, **params):
        c = APIClient()
        c.force_authenticate(user=user)
        return c.get(self.URL, {"start_date": "2026-10-01", "end_date": "2026-10-20", **params})

    def test_totals_per_driver(self):
        r = self.get(self.disp_a)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(len(r.data), 1)                         # company A only
        row = r.data[0]
        self.assertEqual(row["driver__full_name"], "Ann")
        self.assertEqual(row["driver__unit_number"], "U1")
        self.assertEqual(row["total_loads"], 2)
        self.assertEqual(row["total_carrier_pay"], Decimal("1500"))
        self.assertEqual(row["total_driver_pay"], Decimal("1000"))
        self.assertEqual(row["total_profit"], Decimal("500"))

    def test_management_sees_every_company_and_can_filter(self):
        self.assertEqual({r["driver__full_name"] for r in self.get(self.mgr).data}, {"Ann", "Bob"})
        only_b = self.get(self.mgr, company=self.co_b.id)
        self.assertEqual([r["driver__full_name"] for r in only_b.data], ["Bob"])

    def test_company_param_is_ignored_for_dispatchers(self):
        r = self.get(self.disp_a, company=self.co_b.id)
        self.assertEqual([x["driver__full_name"] for x in r.data], ["Ann"])

    def test_bad_input_is_a_400(self):
        self.assertEqual(self.get(self.mgr, start_date="nope").status_code, 400)
        self.assertEqual(self.get(self.mgr, start_date="2026-10-30").status_code, 400)
        self.assertEqual(self.get(self.mgr, date_type="created_at").status_code, 400)
