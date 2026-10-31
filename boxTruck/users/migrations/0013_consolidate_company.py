"""
One company for the whole deployment: Spaceline stays, Nahl goes.

This runs before any company column is dropped, because the columns are how
Nahl's rows are found. Nahl's users are not touched, they simply stop having a
company once `0014` drops the column. The Nahl row itself is removed by `0015`,
after every foreign key to it is gone, so its CASCADEs cannot reach the users.

A database with any other company stops here with nothing changed. Picking
what happens to a third company is not something a migration should guess.
"""
from django.db import migrations

KEEP = 'space'
REMOVE = 'nahl'

# Nahl's drivers, vehicles and driver paperwork stay and become Spaceline's
# unless this is True.
DELETE_REMOVED_DRIVERS = False


def consolidate(apps, schema_editor):
    Company = apps.get_model('users', 'Company')
    companies = list(Company.objects.order_by('id'))
    if len(companies) < 2:
        return

    keep = [c for c in companies if KEEP in c.name.lower()]
    remove = [c for c in companies if REMOVE in c.name.lower()]
    unknown = [c.name for c in companies if c not in keep and c not in remove]
    if len(keep) != 1 or unknown:
        raise RuntimeError(
            f"Cannot pick the one company. Keep matches: {[c.name for c in keep]}, "
            f"not handled: {unknown}. Nothing was changed."
        )
    keep = keep[0]

    Load = apps.get_model('billing', 'Load')
    Statement = apps.get_model('payroll', 'Statement')
    Deduction = apps.get_model('payroll', 'Deduction')
    Driver = apps.get_model('hiring', 'Driver')
    DriverInviteLink = apps.get_model('hiring', 'DriverInviteLink')
    BrokerBlacklist = apps.get_model('billing', 'BrokerBlacklist')
    DispatchSalary = apps.get_model('dispatchers', 'DispatchSalary')
    DispatchBonus = apps.get_model('dispatchers', 'DispatchBonus')
    Team = apps.get_model('users', 'Team')
    UserInvite = apps.get_model('users', 'UserInvite')

    # The accounting data. Payouts, invoices, stops, files and history go with
    # their load, and statement lines go with their statement.
    Statement.objects.filter(company__in=remove).delete()
    Load.objects.filter(company__in=remove).delete()
    Deduction.objects.filter(driver__company__in=remove).delete()

    # Rates and bonuses decide what a dispatcher is paid, so Nahl's cannot be
    # left to apply to Spaceline. Bonuses with no company were never applied by
    # the old code and would start to be, so they go as well. Salary tiers with
    # no company already applied to everyone and stay.
    DispatchSalary.objects.filter(company__in=remove).delete()
    DispatchBonus.objects.exclude(company=keep).delete()

    # A Nahl blacklist entry would start hiding loads from Spaceline.
    BrokerBlacklist.objects.filter(company__in=remove).delete()

    # Members and drivers of a deleted team become unassigned (SET_NULL).
    Team.objects.filter(company__in=remove).delete()

    # Unused links that would sign people up or onboard drivers under Nahl.
    UserInvite.objects.filter(company__in=remove).delete()
    DriverInviteLink.objects.filter(company__in=remove).delete()

    if DELETE_REMOVED_DRIVERS:
        Driver.objects.filter(company__in=remove).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0012_seed_wasatch_contract_template'),
        ('hiring', '0025_driver_availability'),
        ('billing', '0011_brokerblacklist'),
        ('payroll', '0003_statementdeduction_deductionhistory'),
        ('dispatchers', '0001_initial'),
        ('ai', '0001_initial'),
        ('accounting', '0002_payout_quick_pay_fee'),
        ('mobile', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(consolidate, migrations.RunPython.noop),
    ]
