"""Monthly pay: fixed salary and commission by pick-up month, the daily bonus by booked day in
Central time, dated bonus rules, saving, and who may see it."""
from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounting.models import Payout
from billing.models import Broker, Load, LoadStatus, LoadStop
from hiring.models import Driver, DriverStatus
from users.models import Company, CustomUser, Department

from .models import BonusOverride, BonusRule, EmployeeSalary
from .services import month_report, parse_month

D = Decimal
NOV = date(2026, 11, 1)
OCT = date(2026, 10, 1)


def central(y, m, d, h=12, mi=0):
    return timezone.make_aware(datetime(y, m, d, h, mi), timezone.get_current_timezone())


class SalariesBase(TestCase):
    def setUp(self):
        Company.objects.create(name='Space Line LLC', shipment_number='10001')
        mgmt = Department.objects.create(name='Management')
        dispatch = Department.objects.create(name='Dispatch')
        billing = Department.objects.create(name='Billing')
        self.boss = CustomUser.objects.create_user(username='boss', password='x', department=mgmt, first_name='Big', last_name='Boss', is_active=True)
        self.dana = CustomUser.objects.create_user(username='dana', password='x', department=dispatch, first_name='Dana', last_name='Ortiz', is_active=True, started_working_date=date(2026, 3, 10))
        self.sam = CustomUser.objects.create_user(username='sam', password='x', department=dispatch, first_name='Sam', last_name='Reyes', is_active=True, started_working_date=date(2026, 11, 3))
        self.elena = CustomUser.objects.create_user(username='elena', password='x', department=billing, first_name='Elena', last_name='Varga', is_active=True)
        self.driver = Driver.objects.create(full_name='Kwame Osei', unit_number='19', status=DriverStatus.objects.create(name='Active'))
        self.broker = Broker.objects.create(name='Northline Logistics', mc='784512')
        self.delivered = LoadStatus.objects.create(name='Delivered')
        self.cancelled = LoadStatus.objects.create(name='Cancelled')
        self.n = 0

    def load(self, by, booked, pickup=None, carrier='1300', driver='874', status=None):
        self.n += 1
        l = Load.objects.create(driver=self.driver, broker=self.broker, load_number=str(100000 + self.n), booked_by=by, created_by=by,
                                driver_pay=D(driver), carrier_pay=D(carrier), payment_type='Regular Pay', status=status or self.delivered,
                                pickup_date=pickup or booked)
        Load.objects.filter(pk=l.pk).update(created_at=booked)
        return Load.objects.get(pk=l.pk)

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def row(self, report, user):
        return next(r for r in report['rows'] if r['user']['id'] == user.id)


class CommissionAndSalary(SalariesBase):
    def test_salary_carries_forward_and_commission_follows_pickup_month(self):
        EmployeeSalary.objects.create(user=self.dana, month=date(2026, 9, 1), fixed_salary=D('1600'), profit_percentage=D('10'))
        self.load(self.dana, central(2026, 11, 3), carrier='1300', driver='874')          # 426 profit in November
        self.load(self.dana, central(2026, 10, 30), pickup=central(2026, 11, 2), carrier='2000', driver='1500')  # booked Oct, picked up Nov: 500
        self.load(self.dana, central(2026, 11, 29), pickup=central(2026, 12, 1), carrier='900', driver='600')    # picked up Dec: not here
        self.load(self.dana, central(2026, 11, 5), carrier='5000', driver='100', status=self.cancelled)        # left out
        r = self.row(month_report(NOV, today=date(2026, 11, 20)), self.dana)
        self.assertEqual((r['set_for'], r['fixed_salary'], r['profit_percentage']), ('2026-09', D('1600.00'), D('10.00')))
        self.assertEqual((r['loads_count'], r['profit'], r['commission']), (2, D('926.00'), D('92.60')))

    def test_new_employee_has_no_salary_and_billing_books_nothing(self):
        report = month_report(NOV, today=date(2026, 11, 20))
        sam = self.row(report, self.sam)
        self.assertIsNone(sam['fixed_salary'])
        self.assertIsNone(sam['set_for'])
        self.assertEqual(sam['user']['started_working_date'], date(2026, 11, 3))
        elena = self.row(report, self.elena)
        self.assertEqual((elena['loads_count'], elena['total']), (0, D('0.00')))
        self.assertEqual(report['through'], date(2026, 11, 20))
        self.assertTrue(report['is_current'])

    def test_quick_pay_fee_does_not_move_the_profit(self):
        EmployeeSalary.objects.create(user=self.dana, month=NOV, fixed_salary=D('0'), profit_percentage=D('10'))
        load = self.load(self.dana, central(2026, 11, 3), carrier='1300', driver='874')
        Payout.objects.update_or_create(load=load, defaults={'gross': D('830.30'), 'quick_pay_fee': D('43.70'), 'payment_type': 'Quick Pay'})
        Load.objects.filter(pk=load.pk).update(driver_pay=D('830.30'))
        r = self.row(month_report(NOV, today=date(2026, 11, 20)), self.dana)
        self.assertEqual((r['profit'], r['commission']), (D('426.00'), D('42.60')))


class DailyBonus(SalariesBase):
    def setUp(self):
        super().setUp()
        BonusRule.objects.create(effective_from=date(2026, 7, 1), profit_step=D('500'), bonus_amount=D('50'))

    def test_fifty_for_every_full_five_hundred_booked_in_a_day(self):
        self.load(self.dana, central(2026, 11, 4, 9), carrier='1000', driver='700')    # 300
        self.load(self.dana, central(2026, 11, 4, 16), carrier='1000', driver='700')   # 300: day is 600 -> 50
        self.load(self.dana, central(2026, 11, 6), carrier='2000', driver='750')       # 1250 -> 100
        self.load(self.dana, central(2026, 11, 9), carrier='1000', driver='501')       # 499 -> nothing
        self.load(self.dana, central(2026, 11, 9), carrier='3000', driver='100', status=self.cancelled)
        r = self.row(month_report(NOV, today=date(2026, 11, 20)), self.dana)
        self.assertEqual((r['bonus'], r['bonus_days'], r['total']), (D('150.00'), 2, D('150.00')))

    def test_bonus_follows_the_booked_day_in_central_time_not_the_pickup(self):
        # 11:30 pm Central on Nov 30 is already Dec 1 in UTC; it is still a November booking.
        self.load(self.dana, central(2026, 11, 30, 23, 30), pickup=central(2026, 12, 2), carrier='1800', driver='1000')
        nov = self.row(month_report(NOV, today=date(2026, 12, 5)), self.dana)
        dec = self.row(month_report(date(2026, 12, 1), today=date(2026, 12, 5)), self.dana)
        self.assertEqual((nov['bonus'], nov['loads_count']), (D('50.00'), 0))
        self.assertEqual((dec['bonus'], dec['loads_count'], dec['profit']), (D('0.00'), 1, D('800.00')))

    def test_a_rule_change_applies_from_its_date_only(self):
        BonusRule.objects.create(effective_from=date(2026, 11, 10), profit_step=D('400'), bonus_amount=D('100'))
        self.load(self.dana, central(2026, 11, 5), carrier='1000', driver='550')   # 450: old rule, nothing
        self.load(self.dana, central(2026, 11, 12), carrier='1000', driver='550')  # 450: new rule, 100
        report = month_report(NOV, today=date(2026, 11, 20))
        r = self.row(report, self.dana)
        self.assertEqual(r['bonus'], D('100.00'))
        self.assertEqual(report['rule']['bonus_amount'], D('100.00'))
        self.assertEqual(report['rule_at_start']['bonus_amount'], D('50.00'))
        self.assertEqual([c['effective_from'] for c in report['rule_changes']], ['2026-11-10'])
        october = self.row(month_report(OCT, today=date(2026, 11, 20)), self.dana)
        self.assertEqual(october['bonus'], D('0.00'))

    def test_a_zero_amount_turns_the_bonus_off_from_its_date(self):
        self.load(self.dana, central(2026, 11, 5), carrier='2000', driver='750')   # 1250 under the old rule: 100
        self.load(self.dana, central(2026, 11, 12), carrier='2000', driver='750')  # after the switch-off: nothing
        r = self.api(self.boss).post(reverse('salaries-bonus-rules'), {'effective_from': '2026-11-10', 'profit_step': '500', 'bonus_amount': '0'}, format='json')
        self.assertEqual(r.status_code, 201)
        report = month_report(NOV, today=date(2026, 11, 20))
        self.assertEqual((self.row(report, self.dana)['bonus'], self.row(report, self.dana)['bonus_days']), (D('100.00'), 1))
        self.assertEqual(report['rule']['bonus_amount'], D('0.00'))
        december = month_report(date(2026, 12, 1), today=date(2026, 12, 5))
        self.assertEqual(december['rule']['bonus_amount'], D('0.00'))

    def test_no_rule_means_no_bonus(self):
        BonusRule.objects.all().delete()
        self.load(self.dana, central(2026, 11, 6), carrier='2000', driver='750')
        report = month_report(NOV, today=date(2026, 11, 20))
        self.assertEqual(self.row(report, self.dana)['bonus'], D('0.00'))
        self.assertIsNone(report['rule'])


class TheApi(SalariesBase):
    def test_only_management_may_open_it(self):
        self.assertEqual(self.api(self.dana).get(reverse('salaries-report'), {'month': '2026-11'}).status_code, 403)
        self.assertEqual(self.api(self.boss).get(reverse('salaries-report'), {'month': '2026-11'}).status_code, 200)
        self.assertEqual(self.api(self.boss).get(reverse('salaries-report'), {'month': 'november'}).status_code, 400)

    def test_saving_writes_the_month_and_nulls_remove_it(self):
        EmployeeSalary.objects.create(user=self.dana, month=OCT, fixed_salary=D('1500'), profit_percentage=D('8'))
        c = self.api(self.boss)
        r = c.put(reverse('salaries-report') + '?month=2026-11', {'items': [
            {'user': self.dana.id, 'fixed_salary': '1600', 'profit_percentage': '12'},
            {'user': self.sam.id, 'fixed_salary': '1200', 'profit_percentage': None},
        ]}, format='json')
        self.assertEqual(r.status_code, 200)
        dana = self.row(r.data, self.dana)
        self.assertEqual((dana['set_for'], dana['fixed_salary'], dana['profit_percentage']), ('2026-11', D('1600.00'), D('12.00')))
        sam = self.row(r.data, self.sam)
        self.assertEqual((sam['fixed_salary'], sam['profit_percentage']), (D('1200.00'), D('0.00')))
        self.assertEqual(EmployeeSalary.objects.get(user=self.dana, month=NOV).updated_by, self.boss)

        r = c.put(reverse('salaries-report') + '?month=2026-11', {'items': [{'user': self.dana.id, 'fixed_salary': None, 'profit_percentage': None}]}, format='json')
        dana = self.row(r.data, self.dana)
        self.assertEqual((dana['set_for'], dana['fixed_salary']), ('2026-10', D('1500.00')))

    def test_bad_numbers_are_refused(self):
        c = self.api(self.boss)
        r = c.put(reverse('salaries-report') + '?month=2026-11', {'items': [{'user': self.dana.id, 'fixed_salary': '-5', 'profit_percentage': '10'}]}, format='json')
        self.assertEqual(r.status_code, 400)
        r = c.put(reverse('salaries-report') + '?month=2026-11', {'items': [{'user': self.dana.id, 'fixed_salary': '5', 'profit_percentage': '101'}]}, format='json')
        self.assertEqual(r.status_code, 400)
        r = c.post(reverse('salaries-bonus-rules'), {'effective_from': '2026-12-01', 'profit_step': '0', 'bonus_amount': '50'}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_bonus_rules_are_listed_and_replaced_by_date(self):
        c = self.api(self.boss)
        self.assertEqual(c.post(reverse('salaries-bonus-rules'), {'effective_from': '2026-07-01', 'profit_step': '500', 'bonus_amount': '50'}, format='json').status_code, 201)
        self.assertEqual(c.post(reverse('salaries-bonus-rules'), {'effective_from': '2026-07-01', 'profit_step': '500', 'bonus_amount': '60'}, format='json').status_code, 201)
        rules = c.get(reverse('salaries-bonus-rules')).data
        self.assertEqual(len(rules), 1)
        self.assertEqual((rules[0]['bonus_amount'], rules[0]['created_by_name']), ('60.00', 'Big Boss'))

    def test_employee_detail_lists_loads_days_and_history(self):
        BonusRule.objects.create(effective_from=date(2026, 7, 1), profit_step=D('500'), bonus_amount=D('50'))
        EmployeeSalary.objects.create(user=self.dana, month=date(2026, 9, 1), fixed_salary=D('1600'), profit_percentage=D('10'))
        load = self.load(self.dana, central(2026, 11, 4), carrier='2000', driver='750')
        LoadStop.objects.create(load=load, city='Dallas', state='TX', order=1, load_pickup=True)
        LoadStop.objects.create(load=load, city='Memphis', state='TN', order=2, load_drop=True)
        self.load(self.dana, central(2026, 10, 7), carrier='1300', driver='874')
        r = self.api(self.boss).get(reverse('salaries-employee', args=[self.dana.id]), {'month': '2026-11'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data['loads']), 1)
        self.assertEqual((r.data['loads'][0]['origin'], r.data['loads'][0]['destination'], r.data['loads'][0]['profit']), ('Dallas, TX', 'Memphis, TN', D('1250.00')))
        self.assertEqual([(d['date'], d['bonus']) for d in r.data['bonus_days']], [(date(2026, 11, 4), D('100.00'))])
        months = {h['month']: h for h in r.data['history']}
        self.assertEqual(r.data['history'][0]['month'], '2026-11')
        self.assertEqual(months['2026-11']['total'], D('1600.00') + D('125.00') + D('100.00'))
        self.assertEqual((months['2026-10']['profit'], months['2026-10']['commission']), (D('426.00'), D('42.60')))
        self.assertIsNone(months['2026-08']['fixed_salary'])
        self.assertEqual(r.data['history'][-1]['month'], '2026-03')

    def test_a_bonus_set_by_hand_replaces_the_calculation_for_that_month_only(self):
        BonusRule.objects.create(effective_from=date(2026, 7, 1), profit_step=D('500'), bonus_amount=D('50'))
        EmployeeSalary.objects.create(user=self.dana, month=OCT, fixed_salary=D('1000'), profit_percentage=D('0'))
        self.load(self.dana, central(2026, 11, 6), carrier='2000', driver='750')   # 1250 booked that day: 100
        self.load(self.dana, central(2026, 10, 6), carrier='2000', driver='750')   # October: 100 as well
        c = self.api(self.boss)
        r = c.put(reverse('salaries-report') + '?month=2026-11', {'items': [{'user': self.dana.id, 'bonus_override': '240'}]}, format='json')
        self.assertEqual(r.status_code, 200)
        dana = self.row(r.data, self.dana)
        self.assertEqual((dana['bonus'], dana['bonus_calculated'], dana['bonus_override'], dana['total']), (D('240.00'), D('100.00'), D('240.00'), D('1240.00')))
        # the salary row was not touched by an item that only carried the bonus
        self.assertEqual((dana['set_for'], dana['fixed_salary']), ('2026-10', D('1000.00')))
        self.assertEqual(BonusOverride.objects.get(user=self.dana, month=NOV).updated_by, self.boss)
        # October keeps its own calculation
        self.assertEqual(self.row(month_report(OCT, today=date(2026, 11, 20)), self.dana)['bonus'], D('100.00'))
        hist = {h['month']: h for h in c.get(reverse('salaries-employee', args=[self.dana.id]), {'month': '2026-11'}).data['history']}
        self.assertEqual((hist['2026-11']['bonus'], hist['2026-11']['bonus_override'], hist['2026-10']['bonus'], hist['2026-10']['bonus_override']), (D('240.00'), D('240.00'), D('100.00'), None))
        # null puts the calculation back
        r = c.put(reverse('salaries-report') + '?month=2026-11', {'items': [{'user': self.dana.id, 'bonus_override': None}]}, format='json')
        dana = self.row(r.data, self.dana)
        self.assertEqual((dana['bonus'], dana['bonus_override']), (D('100.00'), None))
        self.assertFalse(BonusOverride.objects.filter(user=self.dana).exists())
        r = c.put(reverse('salaries-report') + '?month=2026-11', {'items': [{'user': self.dana.id, 'bonus_override': '-1'}]}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_parse_month(self):
        self.assertEqual(parse_month('2026-02'), date(2026, 2, 1))
        with self.assertRaises(ValueError):
            parse_month('2026-2-1')
