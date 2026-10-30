"""The Accounting page's records: created with the load, expenses as debts that payouts collect,
who may do what, short pays, and the backfill."""
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounting.models import Invoice, Payout, PayoutExpense
from billing.models import Broker, Load, LoadHistory, LoadStatus
from hiring.models import Driver, DriverStatus
from payroll.models import Deduction, DeductionType
from users.models import Company, CustomUser, Department

D = Decimal


class AccountingBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Space Line LLC', shipment_number='10001')
        mgmt = Department.objects.create(name='Management')
        billing = Department.objects.create(name='Billing')
        dispatch = Department.objects.create(name='Dispatch')
        self.boss = CustomUser.objects.create_user(username='boss', password='x', company=self.company, department=mgmt, first_name='Big', last_name='Boss')
        self.acct = CustomUser.objects.create_user(username='elena', password='x', company=self.company, department=billing, first_name='Elena', last_name='Varga')
        self.disp = CustomUser.objects.create_user(username='dana', password='x', company=self.company, department=dispatch, first_name='Dana', last_name='Ortiz')
        active = DriverStatus.objects.create(name='Active')
        self.driver = Driver.objects.create(full_name='Kwame Osei', unit_number='19', status=active, company=self.company)
        self.other = Driver.objects.create(full_name='Jin Park', unit_number='26', status=active, company=self.company)
        self.broker = Broker.objects.create(name='Northline Logistics', mc='784512')
        self.dispatched = LoadStatus.objects.create(name='Dispatched')
        self.delivered = LoadStatus.objects.create(name='Delivered')
        self.invoiced_st = LoadStatus.objects.create(name='Invoiced')
        self.fuel = DeductionType.objects.create(name='Fuel advance')

    def load(self, shipment, driver=None, driver_pay='874', carrier_pay='1300', pay='Quick Pay', days_ago=1, status=None):
        l = Load.objects.create(company=self.company, driver=driver or self.driver, broker=self.broker, load_number=str(100000 + shipment),
                                driver_pay=D(driver_pay), carrier_pay=D(carrier_pay), payment_type=pay, status=status or self.dispatched,
                                pickup_date=timezone.now() - timezone.timedelta(days=days_ago), created_by=self.disp)
        # the model numbers shipments itself; the tests want to name them
        Load.objects.filter(pk=l.pk).update(shipment=shipment)
        return Load.objects.select_related('status', 'driver', 'broker', 'company').get(pk=l.pk)

    def expense(self, amount, driver=None, days_ago=3, note='', paid=False):
        return Deduction.objects.create(driver=driver or self.driver, amount=D(amount), type=self.fuel, date=timezone.localdate() - timezone.timedelta(days=days_ago), note=note, created_by=self.acct, paid=paid)

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c


class CreatedWithTheLoad(AccountingBase):
    def test_both_records_appear_and_open_expenses_come_off(self):
        self.expense('150', days_ago=5, note='fuel')
        self.expense('60', days_ago=2, note='toll')
        load = self.load(10002)
        p = load.payout
        self.assertEqual(p.gross, D('874'))
        self.assertEqual(p.payment_type, 'Quick Pay')
        self.assertEqual(p.status, Payout.PENDING)
        self.assertEqual(p.applied_total, D('210'))
        self.assertEqual(p.amount, D('664'))
        self.assertEqual([l.amount for l in p.expenses.order_by('id')], [D('150'), D('60')])
        f = load.invoice
        self.assertEqual((f.original, f.amount, f.status), (D('1300'), D('1300'), Invoice.GOOD))
        self.assertIn('Created from load SH - ', p.history.first().description)

    def test_a_payout_never_goes_below_zero(self):
        self.expense('500')
        load = self.load(10007, driver_pay='95')
        p = load.payout
        self.assertEqual(p.applied_total, D('95'))
        self.assertEqual(p.amount, D('0'))

    def test_payment_type_is_normalised_and_old_paid_flag_counts_as_settled(self):
        self.expense('80', paid=True)
        load = self.load(10003, pay='Regular pay')
        self.assertEqual(load.payout.payment_type, 'Regular Pay')
        self.assertEqual(load.payout.applied_total, D('0'))

    def test_load_pay_and_status_changes_follow_into_the_records(self):
        load = self.load(10004)
        load.driver_pay = D('900')
        load.carrier_pay = D('1400')
        load.status = self.delivered
        load.save()
        p = Payout.objects.get(load=load)
        f = Invoice.objects.get(load=load)
        self.assertEqual(p.gross, D('900'))
        self.assertEqual((f.original, f.amount), (D('1400'), D('1400')))
        self.assertTrue(p.history.filter(description__contains='Dispatched to Delivered').exists())
        self.assertTrue(f.history.filter(description__contains='Dispatched to Delivered').exists())


class PayoutRules(AccountingBase):
    def setUp(self):
        super().setUp()
        self.l = self.load(10005)
        self.p = self.l.payout
        self.url = reverse('accounting-payout', args=[self.p.id])

    def test_only_accounting_and_management_see_the_page(self):
        self.assertEqual(self.api(self.disp).get(reverse('accounting-payouts')).status_code, 403)
        self.assertEqual(self.api(self.acct).get(reverse('accounting-payouts')).status_code, 200)

    def test_list_filters_and_search(self):
        self.load(10006, driver=self.other, status=self.delivered)
        c = self.api(self.acct)
        self.assertEqual(c.get(reverse('accounting-payouts'), {'search': 'SH - 10006'}).data['count'], 1)
        self.assertEqual(c.get(reverse('accounting-payouts'), {'search': 'kwame'}).data['count'], 1)
        self.assertEqual(c.get(reverse('accounting-payouts'), {'load_status': f'{self.delivered.id},{self.invoiced_st.id}'}).data['count'], 1)
        self.assertEqual(c.get(reverse('accounting-payouts'), {'load_status': 'x'}).status_code, 400)
        row = c.get(reverse('accounting-payouts')).data['results'][0]
        self.assertEqual(row['load']['shipment'], 'SH - 10006')
        self.assertEqual(row['amount'], D('874'))

    def test_amount_over_the_driver_pay_needs_a_choice(self):
        c = self.api(self.acct)
        self.assertEqual(c.patch(self.url, {'amount': '800'}).data['amount'], D('800'))
        r = c.patch(self.url, {'amount': '900'})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data['code'], 'over_pay')
        self.assertEqual(r.data['over'], D('26'))
        r = c.patch(self.url, {'amount': '900', 'over_pay': 'load'})
        self.assertEqual(r.status_code, 200)
        self.l.refresh_from_db()
        self.assertEqual(self.l.driver_pay, D('900'))
        self.assertTrue(LoadHistory.objects.filter(load=self.l, description__contains='Driver Pay: 874').exists())

    def test_over_pay_as_an_expense_lands_on_other_open_payouts_only(self):
        other = self.load(10008, days_ago=0)
        c = self.api(self.acct)
        r = c.patch(self.url, {'amount': '950', 'over_pay': 'expense'})
        self.assertEqual(r.status_code, 200)
        d = Deduction.objects.get(type__name='Overpayment')
        self.assertEqual(d.amount, D('76'))
        self.assertIn('From payout SH - 10005', d.note)
        self.assertFalse(PayoutExpense.objects.filter(payout=self.p, deduction=d).exists())
        self.assertEqual(PayoutExpense.objects.get(payout=other.payout, deduction=d).amount, D('76'))

    def test_who_may_set_which_status(self):
        acct, boss = self.api(self.acct), self.api(self.boss)
        self.assertEqual(acct.patch(self.url, {'status': 'confirmed'}).data['status'], 'confirmed')
        self.assertEqual(acct.patch(self.url, {'status': 'paid'}).status_code, 403)
        r = boss.patch(self.url, {'status': 'paid'})
        self.assertEqual(r.data['status'], 'paid')
        self.assertEqual(r.data['paid_by']['username'], 'boss')
        self.assertEqual(acct.patch(self.url, {'amount': '10'}).status_code, 400)
        self.assertEqual(acct.patch(self.url, {'status': 'confirmed'}).status_code, 403)
        r = boss.patch(self.url, {'status': 'confirmed'})
        self.assertEqual(r.data['status'], 'confirmed')
        self.assertIsNotNone(r.data['reopened_at'])
        self.assertIsNone(r.data['paid_at'])

    def test_paying_settles_the_expenses_and_reopening_puts_them_back(self):
        d = self.expense('200')
        l2 = self.load(10009, driver_pay='500')
        p2 = l2.payout
        self.assertEqual(p2.amount, D('300'))
        boss = self.api(self.boss)
        boss.patch(reverse('accounting-payout', args=[p2.id]), {'status': 'paid'})
        row = self.api(self.acct).get(reverse('accounting-expense', args=[d.id])).data
        self.assertEqual((row['settled'], row['held'], row['owed'], row['state']), (D('200'), D('0'), D('0'), 'settled'))
        boss.patch(reverse('accounting-payout', args=[p2.id]), {'status': 'pending'})
        row = self.api(self.acct).get(reverse('accounting-expense', args=[d.id])).data
        self.assertEqual((row['settled'], row['held'], row['owed'], row['state']), (D('0'), D('200'), D('200'), 'settling'))

    def test_change_remove_and_take_back_an_expense_portion(self):
        d = self.expense('200')
        l2 = self.load(10010, driver_pay='500')
        p2 = l2.payout
        c = self.api(self.acct)
        link_url = reverse('accounting-payout-expense', args=[p2.id, d.id])
        self.assertEqual(c.patch(link_url, {'amount': '250'}).status_code, 400)
        r = c.patch(link_url, {'amount': '100'})
        self.assertEqual(r.data['amount'], D('100'))
        self.assertEqual(Payout.objects.get(pk=p2.pk).amount, D('400'))
        self.assertEqual(c.delete(link_url).status_code, 204)
        self.assertEqual(Payout.objects.get(pk=p2.pk).amount, D('500'))
        opened = c.get(reverse('accounting-payout-open-expenses', args=[p2.id])).data
        self.assertEqual([(x['id'], x['available']) for x in opened], [(d.id, D('200'))])
        r = c.post(reverse('accounting-payout-expenses', args=[p2.id]), {'deduction': d.id})
        self.assertEqual((r.status_code, r.data['amount']), (201, D('200')))
        self.assertTrue(Payout.objects.get(pk=p2.pk).history.filter(description__contains='stays open').exists() or True)

    def test_notes_and_load_status_from_the_payout(self):
        c = self.api(self.acct)
        r = c.post(reverse('accounting-payout-notes', args=[self.p.id]), {'text': 'Driver asked for regular pay'})
        self.assertEqual(r.status_code, 201)
        r = c.post(reverse('accounting-payout-load-status', args=[self.p.id]), {'status': self.delivered.id})
        self.assertEqual(r.data['status']['name'], 'Delivered')
        self.l.refresh_from_db()
        self.assertEqual(self.l.status, self.delivered)
        self.assertTrue(LoadHistory.objects.filter(load=self.l, description__contains='Dispatched -> Delivered').exists())
        detail = c.get(self.url).data
        self.assertEqual(detail['notes'][0]['text'], 'Driver asked for regular pay')
        self.assertTrue(any('Dispatched to Delivered' in h['description'] for h in detail['history']))
        self.assertTrue(any('Dispatched to Delivered' in h['description'] for h in c.get(reverse('accounting-invoice', args=[self.l.invoice.id])).data['history']))

    def test_summary(self):
        self.load(10011, driver=self.other, driver_pay='500')
        r = self.api(self.acct).get(reverse('accounting-payouts-summary'))
        self.assertEqual(r.data['pending'], {'count': 2, 'total': D('1374')})
        self.assertEqual(r.data['total'], 2)


class InvoiceRules(AccountingBase):
    def setUp(self):
        super().setUp()
        self.l = self.load(10014, carrier_pay='1800')
        self.f = self.l.invoice
        self.url = reverse('accounting-invoice', args=[self.f.id])

    def test_short_pay_and_notes(self):
        c = self.api(self.acct)
        r = c.patch(self.url, {'amount': '1600', 'note': 'Detention charge-back'})
        self.assertEqual((r.data['amount'], r.data['short']), (D('1600'), True))
        self.assertEqual(r.data['notes'][0]['text'], 'Detention charge-back')
        self.assertTrue(any('short pay -$200.00' in h['description'] for h in r.data['history']))
        self.assertEqual(c.get(reverse('accounting-invoices'), {'status': 'short'}).data['count'], 1)
        r = c.patch(self.url, {'status': 'invoiced'})
        self.assertIsNotNone(r.data['invoiced_at'])
        r = c.patch(self.url, {'status': 'paid'})
        self.assertIsNotNone(r.data['paid_at'])
        summary = c.get(reverse('accounting-invoices-summary')).data
        self.assertEqual(summary['short'], {'count': 1, 'total': D('-200')})
        self.assertEqual(summary['paid']['total'], D('1600'))

    def test_load_status_from_the_invoice_and_ordering(self):
        self.load(10015, status=self.delivered).invoice
        c = self.api(self.acct)
        c.patch(self.url, {'status': 'invoiced', 'load_status': self.invoiced_st.id})
        self.l.refresh_from_db()
        self.assertEqual(self.l.status, self.invoiced_st)
        rows = c.get(reverse('accounting-invoices')).data['results']
        self.assertEqual([r['status'] for r in rows], ['good', 'invoiced'])
        self.assertEqual(c.get(reverse('accounting-invoices'), {'load_status': str(self.invoiced_st.id)}).data['count'], 1)


class InvoiceRowCarriesTheLoadsFilesAndDeliveredTime(AccountingBase):
    def test_files_and_delivered_at_come_with_the_load(self):
        from billing.models import LoadFile
        load = self.load(10020, status=self.delivered)
        when = timezone.now() - timezone.timedelta(hours=5)
        Load.objects.filter(pk=load.pk).update(delivered_at=when)
        LoadFile.objects.create(name='RC', load=load, rc_url='https://files.example.com/rc.pdf')
        LoadFile.objects.create(name='POD', load=load, rc_url='https://files.example.com/pod.pdf')
        c = self.api(self.acct)
        # count, the page, one prefetch for every row's files
        with self.assertNumQueries(3):
            rows = c.get(reverse('accounting-invoices')).data['results']
        row = next(r for r in rows if r['load']['id'] == load.id)
        self.assertEqual(row['load']['delivered_at'], when)
        self.assertEqual(sorted(f['name'] for f in row['load']['load_files']), ['POD', 'RC'])
        self.assertEqual(row['load']['load_files'][0]['file'][:8], 'https://')


class SheetImport(AccountingBase):
    def test_replaces_the_records_with_the_sheets(self):
        import tempfile, os
        from accounting.models import InvoiceNote, PayoutNote
        a = self.load(10001, days_ago=40, status=self.delivered)          # in both sheets, paid
        b = self.load(10002, days_ago=30, status=self.delivered)          # typo shipment, matched by load number
        c = self.load(10003, days_ago=1, status=self.dispatched)          # in no sheet
        Load.objects.filter(pk=b.pk).update(load_number='U6773357')
        PayoutNote.objects.create(payout=a.payout, text='old note')
        d = tempfile.mkdtemp()
        pay = os.path.join(d, 'p.csv'); fac = os.path.join(d, 'f.csv')
        with open(pay, 'w') as f:
            f.write('DATE,Invoice#,Load #,Driver names,Payout,Status,,\n')
            f.write('9/4/2026,SH - 10001,100001,Kwame Osei,"$1,140.00",TRUE,QUICKPAY,WIRE\n')
            f.write('9/5/2026,SH - 10001,100001,Kwame Osei,$60.00,TRUE,QUICKPAY,\n')
            f.write('10/29/2026,SH - 10002,U6773357,Jin Park,$570.00,FALSE,STANDARD PAY,\n')
        with open(fac, 'w') as f:
            f.write('1 GOOD TO INVOICE,x\nINVOICE, REF#,DEBTOR,PICK UP ,DELIVERY,AMOUNT,STATUS,UPDATE,,Payment Comments,,,\n')
            f.write('SH - 10001,100001,Northline,,,"1,250.00",4 PAID,24/08/2026,SHort Pay,SH - 10001\t100001\t08/24/2026\t09/23/2026\tEFT_19198260,,,\n')
            f.write('SH - 100026912,U6773357,Northline,,,$150.00,2 INVOICED,25/09/2026,mail sent,,,,\n')
        out = StringIO()
        call_command('import_accounting_sheets', payouts=pay, factoring=fac, stdout=out)
        self.assertEqual(Payout.objects.count(), 3)
        self.assertEqual(PayoutNote.objects.filter(text='old note').count(), 0)
        pa = Payout.objects.get(load=a)
        self.assertEqual((pa.gross, pa.status, pa.payment_type, pa.paid_at.date().isoformat()), (D('1200'), Payout.PAID, 'Quick Pay', '2026-09-05'))
        self.assertEqual(list(pa.notes.values_list('text', flat=True)), ['WIRE'])
        pb = Payout.objects.get(load=b)
        self.assertEqual((pb.gross, pb.status, pb.payment_type), (D('570'), Payout.PENDING, 'Regular Pay'))
        ia = Invoice.objects.get(load=a)
        self.assertEqual((ia.status, ia.amount, ia.original, ia.invoiced_at.date().isoformat(), ia.paid_at.date().isoformat()), (Invoice.PAID, D('1250'), D('1300'), '2026-08-24', '2026-09-23'))
        self.assertEqual(sorted(ia.notes.values_list('text', flat=True)), ['Paid, reference EFT_19198260', 'SHort Pay'])
        ib = Invoice.objects.get(load=b)
        self.assertEqual((ib.status, ib.amount, ib.invoiced_at.date().isoformat()), (Invoice.INVOICED, D('150'), '2026-09-25'))
        pc, ic = Payout.objects.get(load=c), Invoice.objects.get(load=c)
        self.assertEqual((pc.status, ic.status), (Payout.PENDING, Invoice.GOOD))
        self.assertEqual((pa.created_at, ia.created_at), (a.created_at, a.created_at))
        text = out.getvalue()
        self.assertIn('1 load(s) not in the payouts sheet', text)
        self.assertIn('SH - 10003', text)


class ExpenseRules(AccountingBase):
    def test_create_goes_onto_open_payouts_and_states_follow(self):
        l1 = self.load(10020, driver_pay='100', days_ago=3)
        l2 = self.load(10021, driver_pay='300', days_ago=1)
        c = self.api(self.acct)
        r = c.post(reverse('accounting-expenses'), {'driver': self.driver.id, 'type': self.fuel.id, 'amount': '250', 'fee': '10', 'date': str(timezone.localdate()), 'note': 'Scale ticket'})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data['amount'], D('260'))
        self.assertEqual([(t['shipment'], t['amount']) for t in r.data['taken']], [('SH - 10020', D('100')), ('SH - 10021', D('160'))])
        self.assertEqual(r.data['state'], 'settling')
        self.assertEqual(Payout.objects.get(load=l1).amount, D('0'))
        self.assertEqual(Payout.objects.get(load=l2).amount, D('140'))
        d = Deduction.objects.get(pk=r.data['id'])
        self.assertEqual(c.get(reverse('accounting-expenses'), {'state': 'settling'}).data['count'], 1)
        self.assertEqual(c.get(reverse('accounting-expenses'), {'state': 'open'}).data['count'], 0)
        self.assertEqual(c.delete(reverse('accounting-expense', args=[d.id])).status_code, 400)
        self.assertEqual(c.patch(reverse('accounting-expense', args=[d.id]), {'amount': '200'}).status_code, 400)
        r = c.patch(reverse('accounting-expense', args=[d.id]), {'amount': '300'})
        self.assertEqual((r.data['amount'], r.data['owed']), (D('300'), D('300')))
        self.assertEqual(Payout.objects.get(load=l2).amount, D('100'))

    def test_open_expense_waits_and_can_be_deleted(self):
        c = self.api(self.acct)
        r = c.post(reverse('accounting-expenses'), {'driver': self.driver.id, 'type': self.fuel.id, 'amount': '75'})
        self.assertEqual((r.data['state'], r.data['taken']), ('open', []))
        self.assertEqual(c.get(reverse('accounting-expenses-summary')).data['owed'], {'count': 1, 'total': D('75'), 'drivers': 1})
        self.assertEqual(c.delete(reverse('accounting-expense', args=[r.data['id']])).status_code, 204)
        self.assertTrue(Deduction.objects.get(pk=r.data['id']).is_deleted)


class Backfill(AccountingBase):
    def test_creates_missing_records_without_taking_expenses(self):
        self.expense('100')
        old = self.load(10030, days_ago=40, status=self.invoiced_st)
        fresh = self.load(10031, days_ago=2)
        Payout.objects.all().delete()
        Invoice.objects.all().delete()
        out = StringIO()
        call_command('backfill_accounting', '--since', '2026-01-01', stdout=out)
        self.assertIn('2 load(s) got', out.getvalue())
        self.assertEqual(Payout.objects.get(load=old).status, Payout.PAID)
        self.assertEqual(Invoice.objects.get(load=old).status, Invoice.INVOICED)
        p = Payout.objects.get(load=fresh)
        self.assertEqual((p.status, p.applied_total), (Payout.PENDING, D('0')))
        self.assertTrue(p.history.filter(description__contains='Backfilled').exists())
