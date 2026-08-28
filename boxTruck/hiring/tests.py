"""
Driver invite link lifecycle.

The invite flow is three calls sharing one token: register, sign, upload the
signed PDFs. The link has to survive all three, and the middle step happens in
the driver's browser, so nothing on our side observes it. That gap is where
deactivating the link one step early went unnoticed — registration returned
201 with two PDF URLs and looked completely healthy, and the failure only
surfaced when the driver came back to hand the signed copies in.
"""
import io
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hiring.models import (
    Driver, DriverCompany, DriverFile, DriverInviteLink, DriverStatus, Vehicle,
)
from users.models import Company, CustomUser


def _pdf():
    return io.BytesIO(b'%PDF-1.4 fake')


class DriverInviteFlowTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name='Space Line LLC',
            contract_template_text='Agreement between {{contractor_name}} and us.',
        )
        self.staff = CustomUser.objects.create_user(
            username='recruiter', password='x', company=self.company,
            first_name='Rec', last_name='Ruiter',
        )
        DriverStatus.objects.create(name='pending')
        self.invite = DriverInviteLink.objects.create(
            created_by=self.staff, company=self.company,
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )
        self.submit_url = reverse('driver-invite-submit')
        self.documents_url = reverse('driver-invite-documents')

    def _register(self, token=None, **overrides):
        payload = {
            'company_name': 'Bob Hauling LLC',
            'company_address': '1 Main St',
            'company_city': 'Cleveland',
            'company_state': 'OH',
            'company_zip': '44118',
            'company_employer_id': '12-3456789',
            'driver': {'driver_full_name': 'Bob Driver', 'phone': '+15550001111'},
            'vehicle': {'make': 'Ford', 'model': 'Transit'},
        }
        payload.update(overrides)
        token = token or self.invite.token
        with patch('hiring.views.fill_w9', return_value=_pdf()), \
             patch('hiring.views.generate_contract', return_value=_pdf()):
            return self.client.post(
                f'{self.submit_url}?token={token}', payload,
                content_type='application/json',
            )

    def _upload_signed(self, driver_id, token=None):
        token = token or self.invite.token
        return self.client.post(f'{self.documents_url}?token={token}', {
            'driver_id': driver_id,
            'files': [
                SimpleUploadedFile('w9.pdf', b'%PDF signed w9'),
                SimpleUploadedFile('contract.pdf', b'%PDF signed contract'),
            ],
            'names': ['W-9 (Signed)', 'Contractor Agreement (Signed)'],
        })

    # --- the bug this file exists for ------------------------------------

    def test_driver_can_upload_signed_documents_after_registering(self):
        """The whole point of the link: register, sign, hand the PDFs back."""
        registered = self._register()
        self.assertEqual(registered.status_code, 201, registered.data)
        driver_id = registered.data['driver_id']

        uploaded = self._upload_signed(driver_id)

        self.assertEqual(uploaded.status_code, 201, uploaded.data)
        self.assertEqual(
            sorted(f['name'] for f in uploaded.data['files']),
            ['Contractor Agreement (Signed)', 'W-9 (Signed)'],
        )

    def test_link_stays_active_between_registering_and_uploading(self):
        self._register()
        self.invite.refresh_from_db()
        self.assertTrue(self.invite.is_active)
        self.assertTrue(self.invite.is_valid())

    def test_link_dies_once_the_signed_documents_are_in(self):
        """Documented behaviour: it stops working the moment upload succeeds."""
        driver_id = self._register().data['driver_id']
        self._upload_signed(driver_id)

        self.invite.refresh_from_db()
        self.assertFalse(self.invite.is_active)

        again = self._upload_signed(driver_id)
        self.assertEqual(again.status_code, 400)
        self.assertIn('expired', again.data['detail'].lower())

    # --- what the premature deactivation used to protect against ---------

    def test_one_link_cannot_register_two_drivers(self):
        first = self._register()
        self.assertEqual(first.status_code, 201)

        second = self._register(driver={
            'driver_full_name': 'Impostor', 'phone': '+15550002222',
        })

        self.assertEqual(second.status_code, 409)
        self.assertEqual(Driver.objects.count(), 1)

    def test_registration_claims_the_invite_for_the_driver_it_created(self):
        driver_id = self._register().data['driver_id']
        self.invite.refresh_from_db()
        self.assertEqual(self.invite.driver_id, driver_id)

    # --- upload guards ---------------------------------------------------

    def test_upload_before_registering_is_refused(self):
        response = self._upload_signed(driver_id=1)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(DriverFile.objects.count(), 0)

    def test_upload_cannot_target_another_driver_in_the_company(self):
        """The token decides whose files these are, never the request body."""
        self._register()
        someone_else = Driver.objects.create(
            company=self.company, full_name='Other Driver',
            status=DriverStatus.objects.get(name='pending'),
        )

        response = self._upload_signed(driver_id=someone_else.id)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(DriverFile.objects.filter(driver=someone_else).count(), 0)

    def test_upload_without_driver_id_still_works(self):
        """driver_id only ever confirms what the token already decided."""
        self._register()
        response = self.client.post(f'{self.documents_url}?token={self.invite.token}', {
            'files': [SimpleUploadedFile('w9.pdf', b'%PDF signed')],
            'names': ['W-9 (Signed)'],
        })
        self.assertEqual(response.status_code, 201, response.data)

    def test_expired_link_is_refused_at_both_steps(self):
        self.invite.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        self.invite.save(update_fields=['expires_at'])

        self.assertEqual(self._register().status_code, 400)
        self.assertEqual(self._upload_signed(driver_id=1).status_code, 400)

    # --- registration side effects ---------------------------------------

    def test_registration_creates_the_driver_company_and_vehicle(self):
        driver_id = self._register().data['driver_id']

        driver = Driver.objects.get(id=driver_id)
        self.assertEqual(driver.company, self.company)
        self.assertEqual(driver.referral_by, self.staff)
        self.assertEqual(driver.status.name, 'pending')
        self.assertEqual(DriverCompany.objects.get(driver=driver).name, 'Bob Hauling LLC')
        self.assertEqual(Vehicle.objects.get(driver=driver).make, 'Ford')

    def test_registration_returns_both_generated_pdfs(self):
        response = self._register()

        self.assertIn('w9_url', response.data)
        self.assertIn('contract_url', response.data)
        driver_id = response.data['driver_id']
        self.assertEqual(
            sorted(DriverFile.objects.filter(driver_id=driver_id).values_list('name', flat=True)),
            ['Contractor Agreement (Generated)', 'W-9 (Generated)'],
        )


class DriverSignLinkTests(TestCase):
    """A sign-link is minted against a driver that already exists, so it must
    never be usable to register a second one."""

    def setUp(self):
        self.company = Company.objects.create(
            name='Space Line LLC', contract_template_text='Agreement.',
        )
        self.staff = CustomUser.objects.create_user(
            username='recruiter2', password='x', company=self.company,
            first_name='Rec', last_name='Ruiter',
        )
        self.driver = Driver.objects.create(
            company=self.company, full_name='Existing Driver',
            status=DriverStatus.objects.create(name='pending'),
        )
        self.invite = DriverInviteLink.objects.create(
            created_by=self.staff, company=self.company, driver=self.driver,
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )

    def test_sign_link_cannot_be_used_to_register(self):
        with patch('hiring.views.fill_w9', return_value=_pdf()), \
             patch('hiring.views.generate_contract', return_value=_pdf()):
            response = self.client.post(
                f"{reverse('driver-invite-submit')}?token={self.invite.token}",
                {'company_name': 'X', 'driver': {'driver_full_name': 'Y'}},
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(Driver.objects.count(), 1)

    def test_sign_link_accepts_the_signed_documents(self):
        response = self.client.post(
            f"{reverse('driver-invite-documents')}?token={self.invite.token}",
            {
                'driver_id': self.driver.id,
                'files': [SimpleUploadedFile('w9.pdf', b'%PDF signed')],
                'names': ['W-9 (Signed)'],
            },
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.invite.refresh_from_db()
        self.assertFalse(self.invite.is_active)
