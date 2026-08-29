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
from rest_framework.test import APIClient
from django.utils import timezone

from hiring import document_ai
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


class DocumentNormalizationTests(TestCase):
    """The model returns what is printed on the page; the form needs values it
    can actually put in an input. Everything below is a shape a real document
    prints and a real form would choke on."""

    def _normalize(self, **fields):
        fields.setdefault('document_type', 'cdl')
        return document_ai.normalize(fields)

    def test_dates_are_converted_to_iso_whatever_the_document_printed(self):
        fields, _ = self._normalize(cdl_issue_date='03/14/2022', dob='July 2, 1988')
        self.assertEqual(fields['cdl_issue_date'], '2022-03-14')
        self.assertEqual(fields['dob'], '1988-07-02')

    def test_unreadable_date_is_dropped_rather_than_passed_through(self):
        fields, warnings = self._normalize(cdl_expiration='EXP 00/00/0000')
        self.assertNotIn('cdl_expiration', fields)
        self.assertTrue(any('as a date' in w for w in warnings))

    def test_expired_document_is_flagged(self):
        fields, warnings = self._normalize(cdl_expiration='2020-01-31')
        self.assertEqual(fields['cdl_expiration'], '2020-01-31')
        self.assertTrue(any('already passed' in w for w in warnings))

    def test_dob_in_the_past_is_not_flagged_as_expired(self):
        _, warnings = self._normalize(dob='1988-07-02')
        self.assertEqual(warnings, [])

    def test_state_names_are_abbreviated(self):
        fields, _ = self._normalize(state='Illinois', company_state='oh', cdl_state='IL')
        self.assertEqual(fields['state'], 'IL')
        self.assertEqual(fields['company_state'], 'OH')
        self.assertEqual(fields['cdl_state'], 'IL')

    def test_ein_is_reformatted_and_routing_stripped_to_digits(self):
        fields, _ = self._normalize(company_employer_id='876543210', routing_number='071-000-013')
        self.assertEqual(fields['company_employer_id'], '87-6543210')
        self.assertEqual(fields['routing_number'], '071000013')

    def test_short_routing_number_is_flagged(self):
        _, warnings = self._normalize(routing_number='07100')
        self.assertTrue(any('expected 9' in w for w in warnings))

    def test_blank_and_null_fields_are_omitted_entirely(self):
        fields, _ = self._normalize(full_name='  Bob Driver ', vin=None, make='   ')
        self.assertEqual(fields, {'full_name': 'Bob Driver'})


class DocumentPrefillTests(TestCase):
    def test_fields_are_grouped_into_the_form_sections_they_belong_to(self):
        prefill, _, _ = document_ai.build_prefill([{
            'file_name': 'license.jpg',
            'fields': {
                'full_name': 'Bob Driver', 'cdl_number': 'C123',
                'company_name': 'Bob Hauling LLC', 'vin': '1FT', 'bank_name': 'Chase',
            },
        }])
        self.assertEqual(prefill['driver'], {'full_name': 'Bob Driver', 'cdl_number': 'C123'})
        self.assertEqual(prefill['company'], {'name': 'Bob Hauling LLC'})
        self.assertEqual(prefill['vehicle'], {'vin': '1FT'})
        self.assertEqual(prefill['deposit'], {'bank_name': 'Chase'})

    def test_documents_disagreeing_is_reported_not_silently_resolved(self):
        prefill, conflicts, _ = document_ai.build_prefill([
            {'file_name': 'registration.pdf', 'fields': {'vin': '1FTBW2CM1JKA00001'}},
            {'file_name': 'coi.pdf', 'fields': {'vin': '1FTBW2CM1JKA99999'}},
        ])
        self.assertEqual(prefill['vehicle']['vin'], '1FTBW2CM1JKA00001')
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['from_file'], 'registration.pdf')
        self.assertEqual(conflicts[0]['other_file'], 'coi.pdf')

    def test_the_same_value_from_two_documents_is_not_a_conflict(self):
        _, conflicts, _ = document_ai.build_prefill([
            {'file_name': 'a.pdf', 'fields': {'company_state': 'IL'}},
            {'file_name': 'b.pdf', 'fields': {'company_state': 'il'}},
        ])
        self.assertEqual(conflicts, [])

    def test_sensitive_fields_are_called_out_for_confirmation(self):
        _, _, sensitive = document_ai.build_prefill([{
            'file_name': 'check.jpg',
            'fields': {'account_number': '123456', 'bank_name': 'Chase'},
        }])
        self.assertEqual(sensitive, [{'section': 'deposit', 'field': 'account_number'}])


class DocumentParseEndpointTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Space Line LLC')
        self.staff = CustomUser.objects.create_user(
            username='recruiter', password='x', company=self.company,
        )
        # JWT is the only configured authentication class, so a session
        # login is not enough to reach the view.
        self.client = APIClient()
        self.client.force_authenticate(self.staff)
        self.url = reverse('driver-documents-parse')

    def _file(self, name='license.jpg'):
        return SimpleUploadedFile(name, b'\xff\xd8\xff fake jpeg', content_type='image/jpeg')

    def _parsed(self, file_name, **fields):
        return {
            'file_name': file_name, 'document_type': 'cdl',
            'document_type_label': "Driver's License / CDL",
            'fields': fields, 'warnings': [], 'error': None,
        }

    def test_a_batch_comes_back_per_file_and_merged(self):
        with patch('hiring.views.parse_document') as parse:
            parse.side_effect = [
                self._parsed('license.jpg', full_name='Bob Driver', cdl_number='C123'),
                self._parsed('registration.pdf', vin='1FTBW2CM1JKA00001'),
            ]
            response = self.client.post(self.url, {
                'files': [self._file(), self._file('registration.pdf')],
            })

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body['results']), 2)
        self.assertEqual(body['prefill']['driver']['full_name'], 'Bob Driver')
        self.assertEqual(body['prefill']['vehicle']['vin'], '1FTBW2CM1JKA00001')

    def test_a_single_file_can_be_sent_as_file_instead_of_files(self):
        with patch('hiring.views.parse_document', return_value=self._parsed('license.jpg', full_name='Bob')):
            response = self.client.post(self.url, {'file': self._file()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['prefill']['driver']['full_name'], 'Bob')

    def test_one_unreadable_file_does_not_lose_the_rest_of_the_batch(self):
        broken = {
            'file_name': 'blurry.jpg', 'document_type': None, 'document_type_label': None,
            'fields': {}, 'warnings': [], 'error': 'The document could not be read.',
        }
        with patch('hiring.views.parse_document') as parse:
            parse.side_effect = [broken, self._parsed('license.jpg', full_name='Bob Driver')]
            response = self.client.post(self.url, {
                'files': [self._file('blurry.jpg'), self._file()],
            })

        body = response.json()
        self.assertEqual(body['results'][0]['error'], 'The document could not be read.')
        self.assertEqual(body['prefill']['driver']['full_name'], 'Bob Driver')

    def test_document_type_hints_are_passed_through_positionally(self):
        with patch('hiring.views.parse_document') as parse:
            parse.side_effect = [
                self._parsed('license.jpg'), self._parsed('registration.pdf'),
            ]
            self.client.post(self.url, {
                'files': [self._file(), self._file('registration.pdf')],
                'document_types': ['cdl', 'vehicle_registration'],
            })
        self.assertEqual(
            [call.args[1] for call in parse.call_args_list],
            ['cdl', 'vehicle_registration'],
        )

    def test_no_files_is_refused(self):
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, 400)

    def test_oversized_batch_is_refused_before_any_ai_call(self):
        with patch('hiring.views.parse_document') as parse:
            response = self.client.post(self.url, {
                'files': [self._file(f'{i}.jpg') for i in range(11)],
            })
        self.assertEqual(response.status_code, 400)
        parse.assert_not_called()

    def test_endpoint_requires_login(self):
        self.client.force_authenticate(None)
        response = self.client.post(self.url, {'files': [self._file()]})
        self.assertIn(response.status_code, (401, 403))


class DocumentFileTypeTests(TestCase):
    """Guards that run before any AI call, so a bad upload costs nothing."""

    def test_unsupported_file_type_is_rejected_without_calling_the_model(self):
        upload = SimpleUploadedFile('notes.docx', b'PK\x03\x04', content_type='application/msword')
        with patch('hiring.document_ai._client') as client:
            result = document_ai.parse_document(upload)
        self.assertIn('Unsupported file type', result['error'])
        client.assert_not_called()

    def test_content_type_falls_back_to_the_file_extension(self):
        # Phone uploads routinely arrive as application/octet-stream.
        upload = SimpleUploadedFile('license.pdf', b'%PDF-1.4', content_type='application/octet-stream')
        self.assertEqual(document_ai.detect_mime_type(upload), 'application/pdf')

    def test_empty_file_is_rejected_without_calling_the_model(self):
        upload = SimpleUploadedFile('license.jpg', b'', content_type='image/jpeg')
        with patch('hiring.document_ai._client') as client:
            result = document_ai.parse_document(upload)
        self.assertEqual(result['error'], 'File is empty.')
        client.assert_not_called()

    def test_oversized_file_is_rejected_without_calling_the_model(self):
        upload = SimpleUploadedFile(
            'huge.jpg', b'x' * (document_ai.MAX_FILE_BYTES + 1), content_type='image/jpeg',
        )
        with patch('hiring.document_ai._client') as client:
            result = document_ai.parse_document(upload)
        self.assertIn('larger than', result['error'])
        client.assert_not_called()


class HeicConversionTests(TestCase):
    """iPhones hand over HEIC, which the model will not take. It is converted
    to JPEG on the way out rather than bounced back at the driver."""

    def test_heic_is_accepted_by_the_file_type_check(self):
        upload = SimpleUploadedFile('license.heic', b'\x00\x00\x00 ftypheic', content_type='image/heic')
        self.assertEqual(document_ai.detect_mime_type(upload), 'image/heic')

    def test_heic_is_converted_to_jpeg_before_it_is_sent(self):
        with patch('hiring.document_ai._to_jpeg', return_value=b'jpeg bytes') as convert:
            data, mime_type = document_ai._prepare_upload(b'heic bytes', 'image/heic')
        convert.assert_called_once_with(b'heic bytes')
        self.assertEqual((data, mime_type), (b'jpeg bytes', 'image/jpeg'))

    def test_supported_types_are_passed_through_untouched(self):
        with patch('hiring.document_ai._to_jpeg') as convert:
            data, mime_type = document_ai._prepare_upload(b'%PDF', 'application/pdf')
        convert.assert_not_called()
        self.assertEqual((data, mime_type), (b'%PDF', 'application/pdf'))

    def test_missing_pillow_heif_becomes_an_actionable_message(self):
        upload = SimpleUploadedFile('license.heic', b'\x00\x00\x00 ftypheic', content_type='image/heic')
        with patch('hiring.document_ai._to_jpeg', side_effect=ValueError(
                'HEIC images are not supported on this server. Re-save the photo '
                'as JPEG or PNG and upload it again.')), \
             patch('hiring.document_ai._client') as client:
            result = document_ai.parse_document(upload)
        self.assertIn('Re-save the photo', result['error'])
        client.assert_not_called()


class DocumentRequestShapeTests(TestCase):
    """What actually goes over the wire to OpenAI."""

    def _parse(self, upload):
        response = type('R', (), {'output_parsed': document_ai.ExtractedDocument(
            document_type=document_ai.DocumentType.CDL, full_name='Bob Driver',
        )})()
        with patch('hiring.document_ai._client') as client:
            client.return_value.responses.parse.return_value = response
            result = document_ai.parse_document(upload)
            return result, client.return_value.responses.parse.call_args.kwargs

    def test_a_pdf_is_sent_as_an_input_file(self):
        upload = SimpleUploadedFile('w9.pdf', b'%PDF-1.4', content_type='application/pdf')
        result, kwargs = self._parse(upload)
        part = kwargs['input'][0]['content'][1]
        self.assertEqual(part['type'], 'input_file')
        self.assertEqual(part['filename'], 'w9.pdf')
        self.assertTrue(part['file_data'].startswith('data:application/pdf;base64,'))
        self.assertEqual(result['fields'], {'full_name': 'Bob Driver'})

    def test_an_image_is_sent_as_an_input_image(self):
        upload = SimpleUploadedFile('license.jpg', b'\xff\xd8\xff', content_type='image/jpeg')
        _, kwargs = self._parse(upload)
        part = kwargs['input'][0]['content'][1]
        self.assertEqual(part['type'], 'input_image')
        self.assertTrue(part['image_url'].startswith('data:image/jpeg;base64,'))

    def test_the_schema_is_sent_so_the_response_is_structured(self):
        upload = SimpleUploadedFile('license.jpg', b'\xff\xd8\xff', content_type='image/jpeg')
        _, kwargs = self._parse(upload)
        self.assertIs(kwargs['text_format'], document_ai.ExtractedDocument)

    def test_a_model_that_rejects_temperature_is_retried_without_it(self):
        upload = SimpleUploadedFile('license.jpg', b'\xff\xd8\xff', content_type='image/jpeg')
        response = type('R', (), {'output_parsed': document_ai.ExtractedDocument(
            document_type=document_ai.DocumentType.CDL, full_name='Bob Driver',
        )})()
        with patch('hiring.document_ai._client') as client:
            client.return_value.responses.parse.side_effect = [
                Exception("Unsupported parameter: 'temperature' is not supported"),
                response,
            ]
            result = document_ai.parse_document(upload)

        calls = client.return_value.responses.parse.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertNotIn('temperature', calls[1].kwargs)
        self.assertIsNone(result['error'])

    def test_an_api_failure_does_not_leak_internals_to_the_caller(self):
        upload = SimpleUploadedFile('license.jpg', b'\xff\xd8\xff', content_type='image/jpeg')
        with patch('hiring.document_ai._client') as client:
            client.return_value.responses.parse.side_effect = Exception(
                'org-secret-123 quota exceeded'
            )
            result = document_ai.parse_document(upload)
        self.assertEqual(result['error'], 'Could not analyze this file.')
        self.assertNotIn('org-secret-123', result['error'])
