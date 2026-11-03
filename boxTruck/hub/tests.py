"""
Driver Hub integration: the hub pushing drivers here, the client we call it
with, the invite from the Drivers page, and the relays back (bid status,
payouts, blacklist). Every HTTP call is mocked; nothing here reaches a network.
"""
import tempfile
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from billing.models import Broker, BrokerBlacklist, Load
from hiring.models import Driver, DriverFile, DriverStatus, Vehicle, VehicleEquipment, VehicleFile
from hub import bids, blacklist, payouts
from hub.services import driver_hub
from payroll.models import Deduction, DeductionType, Statement, StatementDeduction, StatementLoad, StatementStatus
from users.models import Company, CustomUser

HUB = dict(DRIVER_HUB_URL='https://hub.test', DRIVER_HUB_TOKEN='tenant-token')
LOCAL_FILES = dict(
    STORAGES={
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    },
    MEDIA_ROOT=tempfile.mkdtemp(),
)

PROFILE = {
    'hub_driver_id': 7,
    'driver': {
        'id': 7, 'email': 'bob@example.com', 'first_name': 'Bob', 'last_name': 'Smith', 'phone': '5551234',
        'company_name': 'Bob Hauling LLC', 'mc': '123456', 'employer_id': '99-1234567', 'business_as': 'Bob Hauling',
        'business_type': 'LLC', 'address': '1 Main St', 'city': 'Cleveland', 'state': 'OH', 'zip': '44101',
        'lat': 41.5, 'lng': -81.7,
    },
    'vehicle': {
        'year': 2022, 'make': 'Ford', 'model': 'Transit', 'vehicle_type': 'cargo van', 'unit_number': '214',
        'payload_lbs': 3200, 'gvw_lbs': 9000, 'cargo_length_in': 144, 'cargo_width_in': 70, 'cargo_height_in': 76,
        'door_width_in': 60, 'door_height_in': 70, 'dock_height_in': 40, 'dock_type': 'ramp',
        'equipment': 'Lift gate, Pallet jack',
    },
    'documents': [
        {'kind': 'license', 'title': 'CDL', 'file_url': 'https://files.test/cdl.pdf', 'expires_at': '2027-01-15T00:00:00Z'},
        {'kind': 'medical_card', 'title': '', 'file_url': 'https://files.test/med.pdf', 'expires_at': '2026-06-01T00:00:00Z'},
        {'kind': 'insurance', 'title': 'Progressive', 'file_url': 'https://files.test/ins.pdf', 'expires_at': '2026-09-30T00:00:00Z'},
    ],
}


def _response(status_code=200, body=None, text=''):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b'x' if body is not None else b''
    resp.text = text
    resp.json.return_value = body if body is not None else {}
    return resp


@override_settings(**HUB, **LOCAL_FILES)
class HubDriverEndpointTests(TestCase):
    def setUp(self):
        Company.objects.create(name='Space Line LLC')
        DriverStatus.objects.create(name='Pending')
        self.client = APIClient()
        self.url = reverse('hub-drivers')
        self.remove_url = reverse('hub-drivers-remove')

    def push(self, payload=PROFILE, token='tenant-token'):
        with patch('hub.sync.fetch_file', return_value=b'%PDF-1.4 fake') as fetch:
            response = self.client.post(self.url, payload, format='json', HTTP_X_HUB_TOKEN=token)
        return response, fetch

    def test_rejects_a_wrong_token(self):
        response, _ = self.push(token='nope')
        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(Driver.objects.count(), 0)

    def test_rejects_when_no_token_is_configured(self):
        with override_settings(DRIVER_HUB_TOKEN=''):
            response = self.client.post(self.url, PROFILE, format='json', HTTP_X_HUB_TOKEN='')
        self.assertIn(response.status_code, (401, 403))

    def test_creates_the_driver_company_vehicle_and_documents(self):
        response, fetch = self.push()
        self.assertEqual(response.status_code, 200, response.content)

        driver = Driver.objects.get(id=response.json()['driver_id'])
        self.assertEqual(driver.hub_driver_id, 7)
        self.assertEqual(driver.hub_status, 'connected')
        self.assertEqual(driver.full_name, 'Bob Smith')
        self.assertEqual(driver.email, 'bob@example.com')
        self.assertEqual(driver.phone_number, '5551234')
        self.assertEqual(driver.unit_number, '214')
        self.assertEqual(driver.zip_code, '44101')
        self.assertEqual(driver.current_latitude, 41.5)
        self.assertIsNotNone(driver.current_location_updated_at)
        self.assertEqual(driver.cdl_expiration, date(2027, 1, 15))
        self.assertEqual(driver.medical_exp_date, date(2026, 6, 1))

        company = driver.driver_company
        self.assertEqual(company.name, 'Bob Hauling LLC')
        self.assertEqual(company.mc, '123456')
        self.assertEqual(company.employer_id, '99-1234567')
        self.assertEqual(company.applicant_first_name, 'Bob')

        vehicle = Vehicle.objects.get(driver=driver)
        self.assertEqual((vehicle.make, vehicle.model, vehicle.year), ('Ford', 'Transit', 2022))
        self.assertEqual(vehicle.vehicle_type, 'cargo van')
        self.assertEqual((vehicle.payload, vehicle.gvw), (3200, 9000))
        self.assertEqual((vehicle.length, vehicle.width, vehicle.height), (144, 70, 76))
        self.assertEqual(vehicle.dock_height, '40 in ramp')
        self.assertEqual(vehicle.insurance_expiry_date, date(2026, 9, 30))
        self.assertEqual(
            sorted(VehicleEquipment.objects.filter(vehicle=vehicle).values_list('name', flat=True)),
            ['Lift gate', 'Pallet jack'],
        )

        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(
            sorted(DriverFile.objects.filter(driver=driver).values_list('name', flat=True)),
            ['CDL (driver app)', 'Medical Card (driver app)'],
        )
        self.assertEqual(VehicleFile.objects.filter(vehicle=vehicle).count(), 1)
        self.assertEqual(VehicleFile.objects.get(vehicle=vehicle).name, 'Progressive (driver app)')

    def test_links_the_invited_driver_by_email(self):
        invited = Driver.objects.create(
            full_name='Robert Smith', email='Bob@Example.com', status=DriverStatus.objects.first(),
            hub_invite_id=5, hub_invite_status='pending',
        )
        response, _ = self.push()
        self.assertEqual(response.json()['driver_id'], invited.id)
        self.assertEqual(Driver.objects.count(), 1)
        invited.refresh_from_db()
        self.assertEqual(invited.hub_driver_id, 7)
        self.assertEqual(invited.full_name, 'Bob Smith')
        self.assertEqual(invited.hub_status, 'connected')

    def test_a_second_push_updates_the_same_driver(self):
        first, _ = self.push()
        changed = {**PROFILE, 'driver': {**PROFILE['driver'], 'phone': '5559999', 'email': 'new@example.com'}}
        second, fetch = self.push(changed)
        self.assertEqual(first.json()['driver_id'], second.json()['driver_id'])
        self.assertEqual(Driver.objects.count(), 1)
        self.assertEqual(Vehicle.objects.count(), 1)
        driver = Driver.objects.get()
        self.assertEqual((driver.phone_number, driver.email), ('5559999', 'new@example.com'))
        # Files already stored under the same name are not fetched again.
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(DriverFile.objects.filter(driver=driver).count(), 2)

    def test_a_missing_file_does_not_block_the_sync(self):
        with patch('hub.sync.fetch_file', return_value=None):
            response = self.client.post(self.url, PROFILE, format='json', HTTP_X_HUB_TOKEN='tenant-token')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DriverFile.objects.count(), 0)
        self.assertEqual(Driver.objects.get().cdl_expiration, date(2027, 1, 15))

    def test_requires_hub_driver_id(self):
        response, _ = self.push({'driver': {'email': 'x@y.z'}})
        self.assertEqual(response.status_code, 400)

    def test_remove_keeps_the_driver_and_drops_the_link(self):
        response, _ = self.push()
        driver_id = response.json()['driver_id']
        response = self.client.post(self.remove_url, {'hub_driver_id': 7}, format='json', HTTP_X_HUB_TOKEN='tenant-token')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'driver_id': driver_id, 'removed': True})
        driver = Driver.objects.get(id=driver_id)
        self.assertIsNone(driver.hub_driver_id)
        self.assertEqual(driver.hub_status, 'not_invited')

        response = self.client.post(self.remove_url, {'hub_driver_id': 7}, format='json', HTTP_X_HUB_TOKEN='tenant-token')
        self.assertEqual(response.json()['removed'], False)


@override_settings(**HUB)
class DriverHubClientTests(TestCase):
    def test_invite_posts_the_email_with_the_bearer_token(self):
        body = {'invite_id': 5, 'status': 'pending', 'delivery': 'emailed', 'hub_driver_id': None}
        with patch('hub.services.driver_hub.requests.request', return_value=_response(201, body)) as req:
            result = driver_hub.invite('bob@example.com', 'Dylan Brooks')
        self.assertEqual(result, body)
        req.assert_called_once_with(
            'POST', 'https://hub.test/t/v1/invites',
            json={'email': 'bob@example.com', 'invited_by': 'Dylan Brooks'},
            headers={'Authorization': 'Bearer tenant-token'},
            timeout=driver_hub.TIMEOUT,
        )

    def test_invite_status_reads_the_invite(self):
        with patch('hub.services.driver_hub.requests.request', return_value=_response(200, {'status': 'accepted'})) as req:
            self.assertEqual(driver_hub.invite_status(5), {'status': 'accepted'})
        self.assertEqual(req.call_args.args, ('GET', 'https://hub.test/t/v1/invites/5'))

    def test_update_bid_target_sends_the_status_and_amounts(self):
        with patch('hub.services.driver_hub.requests.request', return_value=_response(200, {'ok': True})) as req:
            driver_hub.update_bid_target(12, 'placed', carrier_amount=Decimal('1400'), driver_amount=1250,
                                         placed_by='Dylan Brooks', tenant_bid_id='abc', note='Bid emailed')
        self.assertEqual(req.call_args.args, ('POST', 'https://hub.test/t/v1/bid-targets/12/status'))
        self.assertEqual(req.call_args.kwargs['json'], {
            'status': 'placed', 'carrier_amount': 1400.0, 'driver_amount': 1250.0,
            'placed_by': 'Dylan Brooks', 'tenant_bid_id': 'abc', 'note': 'Bid emailed',
        })

    def test_update_bid_target_refuses_an_unknown_status(self):
        with patch('hub.services.driver_hub.requests.request') as req:
            with self.assertRaises(ValueError):
                driver_hub.update_bid_target(12, 'maybe')
        req.assert_not_called()

    def test_upsert_payout_serialises_dates_and_decimals(self):
        with patch('hub.services.driver_hub.requests.request', return_value=_response(200, {'id': 1})) as req:
            driver_hub.upsert_payout(
                'statement-9', 7, date(2026, 10, 26), date(2026, 11, 1), 3,
                Decimal('2500.00'), Decimal('105.00'), Decimal('2395.00'), 'paid',
                paid_at=None, lines=[{'kind': 'load', 'amount': 800.0}],
            )
        self.assertEqual(req.call_args.args, ('POST', 'https://hub.test/t/v1/payouts'))
        sent = req.call_args.kwargs['json']
        self.assertEqual(sent['external_id'], 'statement-9')
        self.assertEqual(sent['hub_driver_id'], 7)
        self.assertEqual((sent['period_start'], sent['period_end']), ('2026-10-26', '2026-11-01'))
        self.assertEqual((sent['gross'], sent['deductions'], sent['net']), (2500.0, 105.0, 2395.0))
        self.assertEqual(sent['status'], 'paid')
        self.assertEqual(sent['lines'], [{'kind': 'load', 'amount': 800.0}])

    def test_replace_blacklist_puts_the_full_list(self):
        with patch('hub.services.driver_hub.requests.request', return_value=_response(200, {'count': 1})) as req:
            driver_hub.replace_blacklist([{'name': 'TQL', 'mc': '312916'}])
        self.assertEqual(req.call_args.args, ('PUT', 'https://hub.test/t/v1/blacklist'))
        self.assertEqual(req.call_args.kwargs['json'], {'brokers': [{'name': 'TQL', 'email': '', 'mc': '312916'}]})

    def test_an_error_reply_raises_with_the_hub_message(self):
        with patch('hub.services.driver_hub.requests.request', return_value=_response(409, {'error': 'already invited'})):
            with self.assertRaises(driver_hub.DriverHubError) as ctx:
                driver_hub.invite('bob@example.com', 'Dylan')
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (409, 'already invited'))

    def test_unconfigured_hub_raises_without_a_request(self):
        with override_settings(DRIVER_HUB_TOKEN=''):
            with patch('hub.services.driver_hub.requests.request') as req:
                with self.assertRaises(driver_hub.DriverHubError) as ctx:
                    driver_hub.invite('bob@example.com', 'Dylan')
        self.assertEqual(ctx.exception.status_code, 503)
        req.assert_not_called()


@override_settings(**HUB)
class InviteToAppTests(TestCase):
    def setUp(self):
        Company.objects.create(name='Space Line LLC')
        self.user = CustomUser.objects.create_user(username='dylan', password='x', first_name='Dylan', last_name='Brooks')
        self.driver = Driver.objects.create(full_name='Alex Kim', email='alex@example.com', status=DriverStatus.objects.create(name='Active'))
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse('drivers-invite-app', args=[self.driver.id])

    def test_sends_the_invite_and_stores_the_result(self):
        reply = {'invite_id': 5, 'status': 'pending', 'delivery': 'emailed', 'hub_driver_id': None}
        with patch('hub.services.driver_hub.invite', return_value=reply) as invite:
            response = self.client.post(self.url, {}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        invite.assert_called_once_with('alex@example.com', 'Dylan Brooks')
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.hub_invite_id, 5)
        self.assertEqual(self.driver.hub_invite_status, 'pending')
        self.assertIsNotNone(self.driver.hub_invited_at)
        self.assertEqual(self.driver.hub_status, 'invited')
        self.assertEqual(response.json()['hub_status'], 'invited')
        self.assertEqual(response.json()['delivery'], 'emailed')

    def test_a_corrected_email_is_kept_on_the_driver(self):
        reply = {'invite_id': 6, 'status': 'pending', 'delivery': 'none', 'hub_driver_id': None}
        with patch('hub.services.driver_hub.invite', return_value=reply) as invite:
            response = self.client.post(self.url, {'email': 'alex.kim@example.com'}, format='json')
        self.assertEqual(response.status_code, 200)
        invite.assert_called_once_with('alex.kim@example.com', 'Dylan Brooks')
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.email, 'alex.kim@example.com')

    def test_an_already_active_driver_is_linked_at_once(self):
        reply = {'invite_id': 7, 'status': 'active', 'delivery': 'pushed', 'hub_driver_id': 42}
        with patch('hub.services.driver_hub.invite', return_value=reply), \
             patch('hub.services.driver_hub.link_driver') as link:
            response = self.client.post(self.url, {}, format='json')
        self.assertEqual(response.status_code, 200)
        link.assert_called_once_with(42, self.driver.id)
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.hub_driver_id, 42)
        self.assertEqual(self.driver.hub_status, 'connected')

    def test_no_email_is_a_400(self):
        self.driver.email = ''
        self.driver.save()
        response = self.client.post(self.url, {}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_hub_failure_is_reported(self):
        with patch('hub.services.driver_hub.invite', side_effect=driver_hub.DriverHubError(502, 'driver hub unreachable')):
            response = self.client.post(self.url, {}, format='json')
        self.assertEqual(response.status_code, 502)
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.hub_status, 'not_invited')

    def test_hub_status_is_on_the_driver_list(self):
        response = self.client.get(reverse('driver-list'))
        self.assertEqual(response.status_code, 200)
        row = response.json()['results'][0] if 'results' in response.json() else response.json()[0]
        self.assertEqual(row['hub_status'], 'not_invited')


@override_settings(**HUB)
class BidStatusRelayTests(TestCase):
    LOAD = '8d1e4f2a-0000-4000-8000-000000000001'
    BIDS = [
        {'id': 'b1', 'action': 'driver_bid', 'bid_amount': 1250.0, 'note': 'hub target #12', 'driver': {'id': 214}},
        {'id': 'b2', 'action': 'driver_bid', 'bid_amount': 900.0, 'note': 'hub target #13', 'driver': {'id': 188}},
        {'id': 'b3', 'action': 'dispatcher_bid', 'bid_amount': 1400.0, 'note': 'Bid emailed to broker', 'driver': {'id': 214}},
        {'id': 'b4', 'action': 'driver_bid', 'bid_amount': 700.0, 'note': 'from telegram', 'driver': {'id': 300}},
    ]

    def setUp(self):
        Company.objects.create(name='Space Line LLC')

    def test_parses_the_hub_target_out_of_the_note(self):
        self.assertEqual(bids.hub_target_id('hub target #12'), 12)
        self.assertEqual(bids.hub_target_id('Bid from the app, hub target #7, sprinter'), 7)
        self.assertIsNone(bids.hub_target_id('Bid emailed to broker'))
        self.assertIsNone(bids.hub_target_id(None))

    def test_relays_only_the_matching_drivers_target(self):
        with patch('hub.bids.load_bids', return_value=self.BIDS), \
             patch('hub.services.driver_hub.update_bid_target') as update:
            reached = bids.relay_bid_status(self.LOAD, 'placed', carrier_amount=1400, driver_amount=1250,
                                            placed_by='Dylan Brooks', tenant_bid_id='b3', driver_id=214)
        self.assertEqual(reached, [12])
        update.assert_called_once_with(12, 'placed', carrier_amount=1400, driver_amount=1250,
                                       placed_by='Dylan Brooks', tenant_bid_id='b3', note=None)

    def test_without_a_driver_every_app_bid_is_told(self):
        with patch('hub.bids.load_bids', return_value=self.BIDS), \
             patch('hub.services.driver_hub.update_bid_target') as update:
            reached = bids.relay_bid_status(self.LOAD, 'lost')
        self.assertEqual(reached, [12, 13])
        self.assertEqual(update.call_count, 2)

    def test_a_hub_failure_on_one_target_does_not_stop_the_rest(self):
        with patch('hub.bids.load_bids', return_value=self.BIDS), \
             patch('hub.services.driver_hub.update_bid_target',
                   side_effect=[driver_hub.DriverHubError(500, 'boom'), {'ok': True}]):
            reached = bids.relay_bid_status(self.LOAD, 'lost')
        self.assertEqual(reached, [13])

    def test_bids_are_read_from_the_loads_service_with_the_internal_secret(self):
        body = {'success': True, 'data': {'color': 'red', 'bids': self.BIDS}}
        with patch('hub.bids.requests.get', return_value=_response(200, body)) as get:
            rows = bids.load_bids(self.LOAD)
        self.assertEqual(len(rows), 4)
        self.assertTrue(get.call_args.args[0].endswith(f'/loads/{self.LOAD}/bids'))
        self.assertEqual(get.call_args.kwargs['headers'], {'X-Internal-Secret': settings.INTERNAL_SERVICE_SECRET})
        self.assertEqual(get.call_args.kwargs['params'], {'company_id': Company.current().id})

    def test_endpoint_accepts_the_internal_secret(self):
        client = APIClient()
        with patch('hub.bids.load_bids', return_value=self.BIDS), \
             patch('hub.services.driver_hub.update_bid_target') as update:
            response = client.post(
                reverse('hub-bid-status'),
                {'load_id': self.LOAD, 'status': 'won', 'driver_id': 214, 'carrier_amount': 1400,
                 'driver_amount': 1250, 'placed_by': 'Dylan Brooks'},
                format='json', HTTP_X_INTERNAL_SECRET=settings.INTERNAL_SERVICE_SECRET,
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {'targets': [12]})
        self.assertEqual(update.call_args.args, (12, 'won'))

    def test_endpoint_names_the_dispatcher_and_takes_target_ids(self):
        user = CustomUser.objects.create_user(username='maria', password='x', first_name='Maria', last_name='Lopez')
        client = APIClient()
        client.force_authenticate(user)
        with patch('hub.bids.load_bids') as load_bids, \
             patch('hub.services.driver_hub.update_bid_target') as update:
            response = client.post(
                reverse('hub-bid-status'),
                {'load_id': self.LOAD, 'status': 'declined', 'hub_target_ids': [12], 'note': 'Broker is blacklisted'},
                format='json',
            )
        self.assertEqual(response.status_code, 200, response.content)
        load_bids.assert_not_called()
        update.assert_called_once_with(12, 'declined', carrier_amount=None, driver_amount=None,
                                       placed_by='Maria Lopez', tenant_bid_id=None, note='Broker is blacklisted')

    def test_endpoint_resolves_the_dispatcher_by_id(self):
        user = CustomUser.objects.create_user(username='dylan', password='x', first_name='Dylan', last_name='Brooks')
        client = APIClient()
        with patch('hub.bids.load_bids', return_value=self.BIDS), \
             patch('hub.services.driver_hub.update_bid_target') as update:
            response = client.post(
                reverse('hub-bid-status'),
                {'load_id': self.LOAD, 'status': 'placed', 'driver_id': 214, 'placed_by_user_id': user.id,
                 'carrier_amount': 1400, 'driver_amount': 1250, 'tenant_bid_id': 'neg-1'},
                format='json', HTTP_X_INTERNAL_SECRET=settings.INTERNAL_SERVICE_SECRET,
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(update.call_args.kwargs['placed_by'], 'Dylan Brooks')
        self.assertEqual(update.call_args.kwargs['tenant_bid_id'], 'neg-1')

    def test_endpoint_rejects_strangers_and_bad_statuses(self):
        client = APIClient()
        response = client.post(reverse('hub-bid-status'), {'load_id': self.LOAD, 'status': 'won'}, format='json')
        self.assertEqual(response.status_code, 401)
        response = client.post(reverse('hub-bid-status'), {'load_id': self.LOAD, 'status': 'meh'}, format='json',
                               HTTP_X_INTERNAL_SECRET=settings.INTERNAL_SERVICE_SECRET)
        self.assertEqual(response.status_code, 400)


@override_settings(**HUB)
class BlacklistPushTests(TestCase):
    def test_a_change_pushes_the_full_list_after_commit(self):
        Broker.objects.create(name='TQL', mc='312916', email='bids@tql.com')
        with patch('hub.services.driver_hub.replace_blacklist') as replace:
            with self.captureOnCommitCallbacks(execute=True):
                BrokerBlacklist.objects.create(name='Echo', mc='')
            with self.captureOnCommitCallbacks(execute=True):
                entry = BrokerBlacklist.objects.create(name='TQL', mc='312916')
        self.assertEqual(replace.call_count, 2)
        self.assertEqual(replace.call_args.args[0], [
            {'name': 'Echo', 'mc': '', 'email': ''},
            {'name': 'TQL', 'mc': '312916', 'email': 'bids@tql.com'},
        ])

        with patch('hub.services.driver_hub.replace_blacklist') as replace:
            with self.captureOnCommitCallbacks(execute=True):
                entry.delete()
        replace.assert_called_once_with([{'name': 'Echo', 'mc': '', 'email': ''}])

    def test_a_hub_outage_never_breaks_the_save(self):
        with patch('hub.services.driver_hub.replace_blacklist', side_effect=driver_hub.DriverHubError(502, 'down')):
            with self.captureOnCommitCallbacks(execute=True):
                BrokerBlacklist.objects.create(name='Echo')
        self.assertEqual(BrokerBlacklist.objects.count(), 1)

    def test_nothing_is_sent_when_the_hub_is_not_configured(self):
        with override_settings(DRIVER_HUB_TOKEN=''), patch('hub.services.driver_hub.replace_blacklist') as replace:
            with self.captureOnCommitCallbacks(execute=True):
                BrokerBlacklist.objects.create(name='Echo')
        replace.assert_not_called()

    def test_the_command_resends_everything(self):
        BrokerBlacklist.objects.create(name='Echo')
        with patch('hub.services.driver_hub.replace_blacklist') as replace:
            call_command('sync_hub_blacklist')
        replace.assert_called_once_with([{'name': 'Echo', 'mc': '', 'email': ''}])


@override_settings(**HUB)
class PayoutTests(TestCase):
    def setUp(self):
        Company.objects.create(name='Space Line LLC', shipment_number='1000')
        self.driver = Driver.objects.create(full_name='Bob Smith', status=DriverStatus.objects.create(name='Active'), hub_driver_id=7)
        self.closed = StatementStatus.objects.create(name='Closed')
        self.approved = StatementStatus.objects.create(name='Approved')
        self.statement = Statement.objects.create(
            driver=self.driver, start_date=date(2026, 10, 26), end_date=date(2026, 11, 1),
            gross_amount=Decimal('2500.00'), status=self.approved,
        )
        load = Load.objects.create(driver=self.driver, load_number='L-1', name='Chicago to Columbus', driver_pay=Decimal('2500.00'))
        StatementLoad.objects.create(statement=self.statement, load=load)
        fuel = DeductionType.objects.create(name='Fuel')
        deduction = Deduction.objects.create(driver=self.driver, amount=Decimal('100.00'), fee=Decimal('5.00'), type=fuel, date=date(2026, 10, 28))
        StatementDeduction.objects.create(statement=self.statement, deduction=deduction)

    def test_an_approved_statement_is_a_scheduled_payout(self):
        with patch('hub.services.driver_hub.upsert_payout') as upsert:
            payouts.sync_statement_payout(self.statement)
        sent = upsert.call_args.kwargs
        self.assertEqual(sent['external_id'], f'statement-{self.statement.id}')
        self.assertEqual(sent['hub_driver_id'], 7)
        self.assertEqual(sent['status'], 'scheduled')
        self.assertEqual(sent['load_count'], 1)
        self.assertEqual((sent['gross'], sent['deductions'], sent['net']), (Decimal('2500.00'), Decimal('105.00'), Decimal('2395.00')))
        self.assertIsNone(sent['paid_at'])
        self.assertEqual([l['kind'] for l in sent['lines']], ['load', 'deduction'])
        self.assertEqual(sent['lines'][0]['label'], 'Chicago to Columbus')
        self.assertEqual(sent['lines'][1]['amount'], -105.0)

    def test_a_closed_statement_is_paid(self):
        self.statement.status = self.closed
        self.statement.save()
        with patch('hub.services.driver_hub.upsert_payout') as upsert:
            payouts.sync_statement_payout(self.statement)
        self.assertEqual(upsert.call_args.kwargs['status'], 'paid')
        self.assertIsNotNone(upsert.call_args.kwargs['paid_at'])

    def test_in_process_and_unlinked_drivers_send_nothing(self):
        self.statement.status = StatementStatus.objects.create(name='In Process')
        self.statement.save()
        with patch('hub.services.driver_hub.upsert_payout') as upsert:
            payouts.sync_statement_payout(self.statement)
        upsert.assert_not_called()

        self.statement.status = self.closed
        self.statement.save()
        self.driver.hub_driver_id = None
        self.driver.save()
        with patch('hub.services.driver_hub.upsert_payout') as upsert:
            payouts.sync_statement_payout(self.statement)
        upsert.assert_not_called()

    def test_a_hub_outage_is_swallowed(self):
        with patch('hub.services.driver_hub.upsert_payout', side_effect=driver_hub.DriverHubError(502, 'down')):
            self.assertIsNone(payouts.sync_statement_payout(self.statement))
