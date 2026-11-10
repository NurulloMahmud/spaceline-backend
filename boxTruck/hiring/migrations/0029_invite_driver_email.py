from django.db import migrations
from django.db.models import Count, Q


def copy_company_email(apps, schema_editor):
    """Invite registrations stored the owner's email only on their company.

    Fill the driver's blank email from it, for drivers who are the only
    driver on that company (so the email is unambiguously theirs). Nothing
    that already has a value is touched.
    """
    Driver = apps.get_model("hiring", "Driver")
    drivers = (
        Driver.objects.filter(Q(email__isnull=True) | Q(email=""))
        .exclude(driver_company__isnull=True)
        .exclude(driver_company__email__isnull=True)
        .exclude(driver_company__email="")
        .annotate(company_drivers=Count("driver_company__drivers"))
        .filter(company_drivers=1)
        .select_related("driver_company")
    )
    for driver in drivers:
        Driver.objects.filter(pk=driver.pk).update(email=driver.driver_company.email)


class Migration(migrations.Migration):
    dependencies = [
        ("hiring", "0028_hub_file_kind_zip_locations"),
    ]

    operations = [
        migrations.RunPython(copy_company_email, migrations.RunPython.noop),
    ]
