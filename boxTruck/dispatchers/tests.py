"""Dispatcher pay comes from the profit a dispatcher booked, so the Quick Pay fee, which moves money
from the driver to the company after booking, must not change it."""
from decimal import Decimal
from datetime import date

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounting.models import Payout
from billing.models import Broker, Load, LoadStatus
from hiring.models import Driver, DriverStatus
from users.models import Company, CustomUser, Department

from .models import DispatchBonus, DispatchSalary

D = Decimal


class QuickPayFeeLeavesDispatcherPayAlone(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Space Line LLC', shipment_number='10001')
        self.boss = CustomUser.objects.create_user(username='boss', password='x', company=self.company, department=Department.objects.create(name='Management'))
        self.acct = CustomUser.objects.create_user(username='elena', password='x', company=self.company, department=Department.objects.create(name='Billing'))
        self.disp = CustomUser.objects.create_user(username='dana', password='x', company=self.company, department=Department.objects.create(name='Dispatch'))
        driver = Driver.objects.create(full_name='Kwame Osei', unit_number='19', status=DriverStatus.objects.create(name='Active'), company=self.company)
        self.load = Load.objects.create(company=self.company, driver=driver, broker=Broker.objects.create(name='Northline Logistics', mc='784512'), load_number='100001',
                                        driver_pay=D('874'), carrier_pay=D('1300'), payment_type='Regular Pay', status=LoadStatus.objects.create(name='Dispatched'),
                                        pickup_date=timezone.now() - timezone.timedelta(minutes=1), booked_by=self.disp, created_by=self.disp)
        # The fee would lift the $426.00 profit over both of these lines.
        DispatchSalary.objects.create(company=self.company, min_amount=D('0'), max_amount=D('449.99'), percentage=D('10'))
        DispatchSalary.objects.create(company=self.company, min_amount=D('450'), percentage=D('20'))
        DispatchBonus.objects.create(company=self.company, min_daily_profit=D('450'), bonus_amount=D('50'))

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def take_fee(self):
        r = self.api(self.acct).patch(reverse('accounting-payout', args=[self.load.payout.id]), {'payment_type': 'Quick Pay'})
        self.assertEqual(r.data['quick_pay_fee'], D('43.70'))

    def own_salary(self):
        return self.api(self.disp).get(reverse('dispatcher-own-salary')).data['salary']

    def test_salary_bonus_and_load_row_stay_as_booked(self):
        before = self.own_salary()
        self.take_fee()
        after = self.own_salary()
        self.assertEqual(after, before)
        self.assertEqual((after['period_profit'], after['flex_salary'], after['total_bonus']), (426.0, 42.6, 0.0))
        row = after['loads'][0]
        self.assertEqual((row['carrier_pay'], row['driver_pay'], row['profit']), (1300.0, 874.0, 426.0))

    def test_the_company_keeps_the_fee(self):
        self.take_fee()
        self.load.refresh_from_db()
        self.assertEqual(self.load.carrier_pay - self.load.driver_pay, D('469.70'))

    def test_monthly_target_stays_as_booked(self):
        self.take_fee()
        self.assertEqual(self.api(self.disp).get(reverse('dispatcher-target')).data['actual_gross'], 426.0)

    def test_management_sees_the_same_numbers(self):
        self.take_fee()
        today = date.today().isoformat()
        r = self.api(self.boss).get(reverse('dispatcher-salary'), {'start_date': today, 'end_date': today})
        row = next(d for d in r.data[0]['dispatchers'] if d['id'] == self.disp.id)
        self.assertEqual((row['period_profit'], row['flex_salary']), (426.0, 42.6))
        self.assertEqual(row['monthly_target']['actual_gross'], 426.0)

    def test_giving_the_fee_back_changes_nothing(self):
        before = self.own_salary()
        self.take_fee()
        self.api(self.acct).patch(reverse('accounting-payout', args=[self.load.payout.id]), {'payment_type': 'Regular Pay'})
        self.assertEqual(self.own_salary(), before)

    def test_a_load_without_a_payout_still_counts(self):
        Payout.objects.filter(load=self.load).delete()
        self.assertEqual(self.own_salary()['period_profit'], 426.0)
        self.assertEqual(self.api(self.disp).get(reverse('dispatcher-target')).data['actual_gross'], 426.0)
