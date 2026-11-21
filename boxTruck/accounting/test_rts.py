"""Factoring Submission: loads go to RTS Financial as one purchase, all or nothing."""
import csv
from datetime import timedelta
from io import BytesIO, StringIO
from unittest import mock

from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from pypdf import PdfReader, PdfWriter

from accounting import rts
from accounting.models import Invoice, RtsPurchase
from accounting.tests import D, AccountingBase
from billing.models import Broker, LoadFile, LoadHistory, LoadStatus

ACCOUNTS = {'standard': {'login': '123456REC', 'password': 'pw'}, 'express': {'login': '', 'password': ''}}


def pdf_bytes(pages=1):
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(612, 792)
    out = BytesIO()
    w.write(out)
    return out.getvalue()


def png_bytes():
    out = BytesIO()
    Image.new('RGB', (2400, 1800), (200, 30, 30)).save(out, format='PNG')
    return out.getvalue()


class FakeFTP:
    """Keeps what was stored, in order; fail decides per file name and try whether to break."""

    def __init__(self, server):
        self.server = server

    def storbinary(self, cmd, fh):
        name = cmd.split(' ', 1)[1]
        self.server.tries[name] = self.server.tries.get(name, 0) + 1
        if self.server.fail(name, self.server.tries[name]):
            raise OSError('connection reset')
        self.server.files.append((name, fh.read()))

    def quit(self):
        pass


class FakeServer:
    def __init__(self, fail=lambda name, attempt: False, down=False):
        self.files = []
        self.tries = {}
        self.fail = fail
        self.down = down
        self.logins = []

    def connect(self, acct):
        self.logins.append(acct['login'])
        if self.down:
            raise rts.Unreachable('timed out')
        return FakeFTP(self)

    def names(self):
        return [n for n, _ in self.files]


@override_settings(RTS_ACCOUNTS=ACCOUNTS)
class RtsBase(AccountingBase):
    def setUp(self):
        super().setUp()
        self.broker2 = Broker.objects.create(name='Bluegate Freight', mc='381907', address='12 Dock St', city='Joliet', state='IL', zipcode='60431')
        self.rejected = LoadStatus.objects.create(name='Rejected')

    def ready_load(self, shipment, broker=None, carrier_pay='1300', pod=None, rc=None, delivered_days=1):
        l = self.load(shipment, carrier_pay=carrier_pay, status=self.delivered)
        if broker:
            type(l).objects.filter(pk=l.pk).update(broker=broker)
        type(l).objects.filter(pk=l.pk).update(delivered_at=timezone.now() - timedelta(days=delivered_days))
        LoadFile.objects.create(load=l, name='ratecon', file=ContentFile(rc if rc is not None else pdf_bytes(2), name=f'rc{shipment}.pdf'))
        if pod is not False:
            LoadFile.objects.create(load=l, name='bol', file=ContentFile(pod if pod is not None else png_bytes(), name=f'bol{shipment}.png'))
        return type(l).objects.get(pk=l.pk)

    def submit(self, invoices, kind='standard', user=None, server=None):
        server = server or FakeServer()
        with mock.patch.object(rts, 'connect', server.connect), mock.patch('accounting.views.run_purchase', rts.safe_run):
            with self.captureOnCommitCallbacks(execute=True):
                r = self.api(user or self.acct).post(reverse('accounting-rts-purchases'), {'invoices': invoices, 'account_type': kind}, format='json')
        return r, server


class Candidates(RtsBase):
    def test_lists_good_invoices_with_what_stops_each(self):
        ok = self.ready_load(20001)
        no_pod = self.ready_load(20002, pod=False)
        dead = self.ready_load(20003)
        type(dead).objects.filter(pk=dead.pk).update(status=self.rejected)
        no_ref = self.ready_load(20004)
        type(no_ref).objects.filter(pk=no_ref.pk).update(load_number='')
        sent = self.ready_load(20005)
        Invoice.objects.filter(load=sent).update(status=Invoice.INVOICED)
        r = self.api(self.acct).get(reverse('accounting-rts-candidates'))
        self.assertEqual(r.status_code, 200)
        why = {x['load']['shipment']: x['blockers'] for x in r.json()['results']}
        self.assertEqual(why['SH - 20001'], [])
        self.assertEqual(why['SH - 20002'], ['No POD/BOL'])
        self.assertEqual(why['SH - 20003'], ['Rejected load'])
        self.assertEqual(why['SH - 20004'], ['No ref #'])
        self.assertNotIn('SH - 20005', why)

    def test_account_types_and_who_may_see(self):
        r = self.api(self.acct).get(reverse('accounting-rts'))
        types = {t['key']: t for t in r.json()['account_types']}
        self.assertTrue(types['standard']['enabled'])
        self.assertFalse(types['express']['enabled'])
        self.assertIn('Waiting on RTS', types['express']['note'])
        self.assertEqual(r.json()['sent_today'], 0)
        self.assertEqual(self.api(self.disp).get(reverse('accounting-rts')).status_code, 403)
        self.assertEqual(self.api(self.disp).post(reverse('accounting-rts-purchases'), {}, format='json').status_code, 403)


class Submitting(RtsBase):
    def test_one_purchase_for_loads_of_two_brokers(self):
        a = self.ready_load(20011, carrier_pay='2450', delivered_days=3)
        b = self.ready_load(20012, broker=self.broker2, carrier_pay='780.5', delivered_days=1)
        r, server = self.submit([b.invoice.id, a.invoice.id])
        self.assertEqual(r.status_code, 201, r.content)
        p = RtsPurchase.objects.get(pk=r.json()['id'])
        self.assertEqual((p.status, p.account_type, p.total), (RtsPurchase.SUBMITTED, 'standard', D('3230.50')))
        # every PDF first, oldest delivery first, the purchase list last; one login for all of it
        self.assertEqual(server.names(), ['20011.pdf', '20012.pdf', 'rts_invoices.csv'])
        self.assertEqual(server.logins, ['123456REC'])
        rows = list(csv.reader(StringIO(server.files[-1][1].decode())))
        today = timezone.localdate().strftime('%m/%d/%Y')
        self.assertEqual(rows[0], ['Client', 'Invoice#', 'DebtorNo', 'Debtor Name', 'Load #', 'InvDate', 'InvAmt'])
        self.assertEqual(rows[1:], [
            ['123456REC', '20011', 'Northline Logistics', 'Northline Logistics', '120011', today, '2450.00'],
            ['123456REC', '20012', 'Bluegate Freight', 'Bluegate Freight', '120012', today, '780.50'],
        ])
        # our invoice page, the BOL photo, then the two rate con pages
        packet = PdfReader(BytesIO(server.files[0][1]))
        self.assertEqual(len(packet.pages), 4)
        self.assertIn('RTS FINANCIAL', packet.pages[0].extract_text())
        self.assertIn('2,450.00', packet.pages[0].extract_text())
        for load in (a, b):
            f = Invoice.objects.select_related('load__status').get(load=load)
            self.assertEqual(f.status, Invoice.INVOICED)
            self.assertIsNotNone(f.invoiced_at)
            self.assertEqual(f.load.status, self.invoiced_st)
            texts = list(f.history.values_list('description', flat=True))
            self.assertIn(f'Sent to RTS in purchase #{p.id} (Standard)', texts)
            self.assertIn('Good to invoice to Invoiced', texts)
            self.assertTrue(LoadHistory.objects.filter(load=load, description='Load Updated | Status: Delivered -> Invoiced').exists())
        line = p.lines.get(invoice=a.invoice)
        self.assertEqual(line.pages, 4)
        self.assertRegex(line.packet.name, rf'rts_purchases/{p.id}/20011(_\w+)?\.pdf$')
        detail = self.api(self.acct).get(reverse('accounting-rts-purchase', args=[p.id])).json()
        self.assertEqual(detail['status'], 'submitted')
        self.assertEqual([l['shipment'] for l in detail['lines']], ['SH - 20011', 'SH - 20012'])
        self.assertEqual(self.api(self.acct).get(reverse('accounting-rts')).json()['sent_today'], 1)
        listed = self.api(self.acct).get(reverse('accounting-invoices'), {'status': 'invoiced'}).json()['results']
        self.assertEqual({x['rts']['id'] for x in listed}, {p.id})
        self.assertEqual(listed[0]['rts']['account_type'], 'standard')

    def test_a_short_upload_error_is_tried_again(self):
        a = self.ready_load(20021)
        server = FakeServer(fail=lambda name, attempt: name == '20021.pdf' and attempt == 1)
        r, server = self.submit([a.invoice.id], server=server)
        self.assertEqual(RtsPurchase.objects.get(pk=r.json()['id']).status, RtsPurchase.SUBMITTED)
        self.assertEqual(server.tries['20021.pdf'], 2)

    def test_refused_up_front(self):
        a = self.ready_load(20031)
        no_pod = self.ready_load(20032, pod=False)
        api = self.api(self.acct)
        url = reverse('accounting-rts-purchases')
        self.assertEqual(api.post(url, {'invoices': [a.invoice.id], 'account_type': 'fast'}, format='json').status_code, 400)
        r = api.post(url, {'invoices': [a.invoice.id], 'account_type': 'express'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('Express is not set up', str(r.json()))
        self.assertEqual(api.post(url, {'invoices': [], 'account_type': 'standard'}, format='json').status_code, 400)
        r = api.post(url, {'invoices': [a.invoice.id, no_pod.invoice.id], 'account_type': 'standard'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('SH - 20032: No POD/BOL', str(r.json()))
        self.assertFalse(RtsPurchase.objects.exists())

    def test_a_load_already_on_its_way_cannot_go_twice(self):
        a = self.ready_load(20041)
        api = self.api(self.acct)
        with mock.patch('accounting.views.run_purchase'):
            first = api.post(reverse('accounting-rts-purchases'), {'invoices': [a.invoice.id], 'account_type': 'standard'}, format='json')
        self.assertEqual(first.status_code, 201)
        second = api.post(reverse('accounting-rts-purchases'), {'invoices': [a.invoice.id], 'account_type': 'standard'}, format='json')
        self.assertEqual(second.status_code, 400)
        self.assertIn('Already being sent', str(second.json()))
        self.assertEqual(api.get(reverse('accounting-rts')).json()['active']['id'], first.json()['id'])


class Failing(RtsBase):
    def assert_nothing_changed(self, *loads):
        for load in loads:
            f = Invoice.objects.select_related('load').get(load=load)
            self.assertEqual(f.status, Invoice.GOOD)
            self.assertEqual(f.load.status, self.delivered)

    def test_an_unreadable_file_stops_it_before_anything_is_sent(self):
        a = self.ready_load(20051)
        b = self.ready_load(20052, broker=self.broker2, pod=b'not a picture at all')
        r, server = self.submit([a.invoice.id, b.invoice.id])
        p = RtsPurchase.objects.get(pk=r.json()['id'])
        self.assertEqual((p.status, p.error_kind, p.bad_invoice_id, p.step), (RtsPurchase.FAILED, 'bad_file', b.invoice.id, RtsPurchase.PREPARING))
        self.assertEqual(p.error, "POD/BOL can't be read")
        self.assertEqual(server.files, [])
        self.assert_nothing_changed(a, b)
        # sent again without that load, the rest goes through as a new purchase
        r2, _ = self.submit([a.invoice.id])
        self.assertEqual(RtsPurchase.objects.get(pk=r2.json()['id']).status, RtsPurchase.SUBMITTED)

    def test_rts_down(self):
        a = self.ready_load(20061)
        r, server = self.submit([a.invoice.id], server=FakeServer(down=True))
        p = RtsPurchase.objects.get(pk=r.json()['id'])
        self.assertEqual((p.status, p.error_kind, p.step), (RtsPurchase.FAILED, 'down', RtsPurchase.UPLOADING))
        self.assertIn("Couldn't reach RTS", p.error)
        self.assert_nothing_changed(a)

    def test_cut_off_partway_sends_no_purchase_list(self):
        a = self.ready_load(20071)
        b = self.ready_load(20072)
        r, server = self.submit([a.invoice.id, b.invoice.id], server=FakeServer(fail=lambda name, attempt: name == '20072.pdf'))
        p = RtsPurchase.objects.get(pk=r.json()['id'])
        self.assertEqual((p.status, p.error_kind), (RtsPurchase.FAILED, 'partial'))
        self.assertEqual(server.tries['20072.pdf'], 3)
        self.assertNotIn('rts_invoices.csv', server.names())
        self.assert_nothing_changed(a, b)

    def test_purchase_list_failing_says_rts_may_have_it(self):
        a = self.ready_load(20081)
        r, server = self.submit([a.invoice.id], server=FakeServer(fail=lambda name, attempt: name.endswith('.csv')))
        p = RtsPurchase.objects.get(pk=r.json()['id'])
        self.assertEqual((p.status, p.error_kind, p.step), (RtsPurchase.FAILED, 'unsure', RtsPurchase.SUBMITTING))
        self.assertIn('Check RTS Pro', p.error)
        self.assert_nothing_changed(a)

    def test_a_job_that_died_is_not_left_sending(self):
        a = self.ready_load(20091)
        with mock.patch('accounting.views.run_purchase'):
            r = self.api(self.acct).post(reverse('accounting-rts-purchases'), {'invoices': [a.invoice.id], 'account_type': 'standard'}, format='json')
        RtsPurchase.objects.filter(pk=r.json()['id']).update(updated_at=timezone.now() - timedelta(minutes=20))
        body = self.api(self.acct).get(reverse('accounting-rts')).json()
        self.assertIsNone(body['active'])
        p = RtsPurchase.objects.get(pk=r.json()['id'])
        self.assertEqual((p.status, p.error_kind), (RtsPurchase.FAILED, 'stale'))
        self.assert_nothing_changed(a)
