from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from users.models import Company, CustomUser, Department
from users.serializers import CustomTokenObtainPairSerializer


class OneCompanyTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Space Line LLC', mc='123')
        self.boss = CustomUser.objects.create_user(
            username='boss', password='x', is_active=True,
            department=Department.objects.create(name='Management'),
        )
        self.dispatcher = CustomUser.objects.create_user(
            username='dana', password='x', is_active=True,
            department=Department.objects.create(name='Dispatch'),
        )
        self.client = APIClient()

    def test_a_second_company_is_refused(self):
        with self.assertRaises(ValidationError):
            Company.objects.create(name='Nahl Group')
        self.assertEqual(Company.objects.count(), 1)

    def test_the_company_can_still_be_saved(self):
        self.company.mc = '456'
        self.company.save()
        self.assertEqual(Company.current().mc, '456')

    def test_current_creates_the_row_on_an_empty_database(self):
        Company.objects.all().delete()
        self.assertEqual(Company.current().name, 'Company')
        self.assertEqual(Company.objects.count(), 1)

    def test_token_carries_the_company_for_atrek_and_the_email_agent(self):
        token = AccessToken(str(CustomTokenObtainPairSerializer.get_token(self.dispatcher).access_token))
        self.assertEqual(token['company_id'], self.company.id)
        self.assertEqual(token['company'], 'Space Line LLC')

    def test_anyone_signed_in_can_read_the_profile(self):
        self.client.force_authenticate(self.dispatcher)
        response = self.client.get('/api/users/company/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['name'], 'Space Line LLC')

    def test_signed_out_cannot_read_the_profile(self):
        self.assertEqual(self.client.get('/api/users/company/').status_code, 401)

    def test_management_can_edit_the_profile(self):
        self.client.force_authenticate(self.boss)
        response = self.client.patch('/api/users/company/', {'mc': '789'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Company.current().mc, '789')

    def test_a_dispatcher_cannot_edit_the_profile(self):
        self.client.force_authenticate(self.dispatcher)
        response = self.client.patch('/api/users/company/', {'mc': '789'}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_the_list_route_is_gone(self):
        self.client.force_authenticate(self.boss)
        self.assertEqual(self.client.get('/api/users/companies/').status_code, 404)

    def test_every_user_is_listed_whatever_the_department(self):
        self.client.force_authenticate(self.dispatcher)
        response = self.client.get('/api/users/list/')
        self.assertEqual(response.status_code, 200)
        rows = response.data['results'] if isinstance(response.data, dict) else response.data
        self.assertEqual({row['username'] for row in rows}, {'boss', 'dana'})
        self.assertNotIn('company', rows[0])


class ConsolidateMigrationTests(TransactionTestCase):
    """Spaceline stays. Nahl's users stay with it, and the rest of Nahl's data goes."""

    BEFORE = [
        ('users', '0012_seed_wasatch_contract_template'),
        ('hiring', '0025_driver_availability'),
        ('billing', '0011_brokerblacklist'),
        ('payroll', '0003_statementdeduction_deductionhistory'),
        ('dispatchers', '0001_initial'),
        ('ai', '0001_initial'),
        ('accounting', '0002_payout_quick_pay_fee'),
        ('mobile', '0001_initial'),
    ]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.BEFORE)
        self.old = executor.loader.project_state(self.BEFORE).apps
        self.addCleanup(self._to_latest)

        apps = self.old
        Company = apps.get_model('users', 'Company')
        self.space = Company.objects.create(name='Space Line LLC')
        self.nahl = Company.objects.create(name='Nahl Group LLC')

        Team = apps.get_model('users', 'Team')
        self.space_team = Team.objects.create(company=self.space, name='Alpha')
        nahl_team = Team.objects.create(company=self.nahl, name='Alpha')

        department = apps.get_model('users', 'Department').objects.create(name='Dispatch')
        User = apps.get_model('users', 'CustomUser')
        self.space_user = User.objects.create(
            username='space', company=self.space, department=department, team=self.space_team)
        self.nahl_user = User.objects.create(
            username='nahl', company=self.nahl, department=department, team=nahl_team)
        apps.get_model('users', 'UserInvite').objects.create(
            username='invited', company=self.nahl, department=department)

        status = apps.get_model('hiring', 'DriverStatus').objects.create(name='Active')
        Driver = apps.get_model('hiring', 'Driver')
        self.space_driver = Driver.objects.create(company=self.space, full_name='S', status=status)
        self.nahl_driver = Driver.objects.create(company=self.nahl, full_name='N', status=status, team=nahl_team)
        apps.get_model('hiring', 'DriverInviteLink').objects.create(created_by=self.nahl_user, company=self.nahl)

        Load = apps.get_model('billing', 'Load')
        self.space_load = Load.objects.create(company=self.space, driver=self.space_driver, load_number='S1')
        nahl_load = Load.objects.create(company=self.nahl, driver=self.nahl_driver, load_number='N1')

        Payout = apps.get_model('accounting', 'Payout')
        Payout.objects.create(load=self.space_load)
        Payout.objects.create(load=nahl_load)
        Invoice = apps.get_model('accounting', 'Invoice')
        Invoice.objects.create(load=self.space_load)
        Invoice.objects.create(load=nahl_load)

        Statement = apps.get_model('payroll', 'Statement')
        Statement.objects.create(company=self.space, start_date='2026-01-01', end_date='2026-01-07')
        Statement.objects.create(company=self.nahl, start_date='2026-01-01', end_date='2026-01-07')
        Deduction = apps.get_model('payroll', 'Deduction')
        Deduction.objects.create(driver=self.space_driver, amount=1)
        Deduction.objects.create(driver=self.nahl_driver, amount=1)

        Salary = apps.get_model('dispatchers', 'DispatchSalary')
        Salary.objects.create(company=self.space, percentage=10)
        Salary.objects.create(company=self.nahl, percentage=20)
        Salary.objects.create(company=None, percentage=30)
        Bonus = apps.get_model('dispatchers', 'DispatchBonus')
        Bonus.objects.create(company=self.space, bonus_amount=10)
        Bonus.objects.create(company=self.nahl, bonus_amount=20)
        Bonus.objects.create(company=None, bonus_amount=30)

        Blacklist = apps.get_model('billing', 'BrokerBlacklist')
        Blacklist.objects.create(company=self.space, name='B', mc='111')
        Blacklist.objects.create(company=self.nahl, name='B', mc='111')

    def _to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_one_company_remains_and_keeps_its_id(self):
        self._to_latest()
        self.assertEqual([(c.id, c.name) for c in Company.objects.all()], [(self.space.id, 'Space Line LLC')])

    def test_nahl_users_survive(self):
        self._to_latest()
        self.assertEqual(set(CustomUser.objects.values_list('username', flat=True)), {'space', 'nahl'})
        self.assertFalse(hasattr(CustomUser.objects.get(username='nahl'), 'company'))

    def test_nahl_accounting_data_goes(self):
        self._to_latest()
        from accounting.models import Invoice, Payout
        from billing.models import Load
        from payroll.models import Deduction, Statement
        self.assertEqual(list(Load.objects.values_list('load_number', flat=True)), ['S1'])
        self.assertEqual(Payout.objects.get().load_id, self.space_load.id)
        self.assertEqual(Invoice.objects.get().load_id, self.space_load.id)
        self.assertEqual(Statement.objects.count(), 1)
        self.assertEqual(Deduction.objects.get().driver_id, self.space_driver.id)

    def test_nahl_settings_cannot_leak_into_spaceline(self):
        self._to_latest()
        from billing.models import BrokerBlacklist
        from dispatchers.models import DispatchBonus, DispatchSalary
        self.assertEqual(sorted(DispatchSalary.objects.values_list('percentage', flat=True)), [10, 30])
        self.assertEqual(list(DispatchBonus.objects.values_list('bonus_amount', flat=True)), [10])
        self.assertEqual(BrokerBlacklist.objects.count(), 1)

    def test_teams_with_the_same_name_do_not_collide(self):
        self._to_latest()
        from users.models import Team, UserInvite
        self.assertEqual(Team.objects.get().id, self.space_team.id)
        self.assertIsNone(CustomUser.objects.get(username='nahl').team_id)
        self.assertEqual(CustomUser.objects.get(username='space').team_id, self.space_team.id)
        self.assertEqual(UserInvite.objects.count(), 0)

    def test_nahl_drivers_stay_unless_asked_otherwise(self):
        self._to_latest()
        from hiring.models import Driver, DriverInviteLink
        driver = Driver.objects.get(id=self.nahl_driver.id)
        self.assertIsNone(driver.team_id)
        self.assertEqual(Driver.objects.count(), 2)
        self.assertEqual(DriverInviteLink.objects.count(), 0)

    def test_an_unknown_company_stops_everything(self):
        Company_old = self.old.get_model('users', 'Company')
        third = Company_old.objects.create(name='Priority Freight LLC')
        with self.assertRaisesMessage(RuntimeError, 'Priority Freight LLC'):
            self._to_latest()
        self.assertEqual(Company_old.objects.count(), 3)
        self.assertEqual(self.old.get_model('billing', 'Load').objects.count(), 2)
        self.assertEqual(self.old.get_model('users', 'CustomUser').objects.count(), 2)
        third.delete()
