from django.db import migrations


def backfill_driver_company(apps, schema_editor):
    Driver = apps.get_model('hiring', 'Driver')
    DriverCompany = apps.get_model('hiring', 'DriverCompany')
    for company in DriverCompany.objects.exclude(driver__isnull=True):
        Driver.objects.filter(pk=company.driver_id).update(driver_company_id=company.pk)


def reverse_noop(apps, schema_editor):
    # driver_company is dropped by 0023 anyway; nothing to undo here.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('hiring', '0021_driver_driver_company'),
    ]

    operations = [
        migrations.RunPython(backfill_driver_company, reverse_noop),
    ]
