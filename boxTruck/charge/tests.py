from unittest import mock

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from charge.client import BillingError
from users.models import Company, CustomUser, Department


class ChargeProxyTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Bob Hauling LLC", email="ap@bobhauling.com")
        self.mgmt = Department.objects.create(name="Management")
        self.user = CustomUser.objects.create_user(
            username="owner", password="x", company=self.company,
            first_name="Bo", last_name="Boss", department=self.mgmt, is_active=True,
        )
        self.plain = CustomUser.objects.create_user(
            username="dispatcher", password="x", company=self.company,
            first_name="Di", last_name="Spatch", is_active=True,
        )
        self.nocompany = CustomUser.objects.create_user(
            username="orphan", password="x", first_name="Or", last_name="Phan", is_active=True,
        )
        self.client = APIClient()

    def _as(self, user):
        self.client.force_authenticate(user)

    # -- identity ------------------------------------------------------

    @mock.patch("charge.views.client")
    def test_summary_calls_billing_with_company_id(self, m):
        m.summary.return_value = {"has_card": False, "cards_count": 0}
        self._as(self.user)
        r = self.client.get(reverse("charge-summary"))
        self.assertEqual(r.status_code, 200)
        m.summary.assert_called_once_with(str(self.company.id))
        self.assertEqual(r.json()["cards_count"], 0)

    @mock.patch("charge.views.client")
    def test_frontend_cannot_choose_the_payer(self, m):
        m.list_cards.return_value = []
        self._as(self.plain)
        # even if the caller tries to pass an external_user_id, the view ignores the body
        self.client.get(reverse("charge-cards"), {"external_user_id": "999"})
        m.list_cards.assert_called_once_with(str(self.company.id))

    def test_user_without_company_is_400(self):
        self._as(self.nocompany)
        r = self.client.get(reverse("charge-summary"))
        self.assertEqual(r.status_code, 400)

    def test_unauthenticated_is_401(self):
        r = self.client.get(reverse("charge-summary"))
        self.assertEqual(r.status_code, 401)

    # -- cards -------------------------------------------------------

    @mock.patch("charge.views.client")
    def test_card_setup_defaults_return_url_and_forwards_company_contact(self, m):
        m.create_card_setup_session.return_value = {"id": "x", "checkout_url": "https://c/", "status": "pending"}
        self._as(self.user)
        r = self.client.post(reverse("charge-card-setup"), {}, format="json")
        self.assertEqual(r.status_code, 200)
        _, kwargs = m.create_card_setup_session.call_args
        self.assertTrue(kwargs["return_url"].endswith("/billing/cards"))
        self.assertEqual(kwargs["email"], "ap@bobhauling.com")
        self.assertEqual(kwargs["name"], "Bob Hauling LLC")

    @mock.patch("charge.views.client")
    def test_card_mutations_need_management_or_billing(self, m):
        self._as(self.plain)  # no department
        self.assertEqual(self.client.post(reverse("charge-card-setup"), {}, format="json").status_code, 403)
        m.create_card_setup_session.assert_not_called()

    # -- error passthrough ---------------------------------------------

    @mock.patch("charge.views.client")
    def test_billing_error_is_propagated_with_status_and_body(self, m):
        m.summary.side_effect = BillingError(403, "project is disabled")
        self._as(self.user)
        r = self.client.get(reverse("charge-summary"))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json(), {"detail": "project is disabled"})

    @mock.patch("charge.views.client")
    def test_unknown_payer_reads_as_empty_not_404(self, m):
        m.summary.side_effect = BillingError(404, "unknown external_user_id")
        m.list_cards.side_effect = BillingError(404, "unknown external_user_id")
        self._as(self.user)
        s = self.client.get(reverse("charge-summary"))
        self.assertEqual(s.status_code, 200)
        self.assertEqual(s.json()["has_card"], False)
        c = self.client.get(reverse("charge-cards"))
        self.assertEqual(c.status_code, 200)
        self.assertEqual(c.json(), [])

    @mock.patch("charge.views.client")
    def test_billing_unreachable_is_502(self, m):
        m.upcoming.side_effect = BillingError(502, "billing service unreachable")
        self._as(self.user)
        r = self.client.get(reverse("charge-upcoming"))
        self.assertEqual(r.status_code, 502)
