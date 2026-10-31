"""
Remove the company rows that are not the one being kept. Nothing points at
`companies` any more, so this deletes those rows and nothing else.
"""
from django.db import migrations

KEEP = 'space'


def delete_other_companies(apps, schema_editor):
    Company = apps.get_model('users', 'Company')
    if Company.objects.count() > 1:
        Company.objects.exclude(name__icontains=KEEP).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0014_remove_company'),
        ('hiring', '0026_remove_company'),
        ('billing', '0012_remove_company'),
        ('payroll', '0004_remove_company'),
        ('dispatchers', '0002_remove_company'),
        ('ai', '0002_remove_company'),
    ]

    operations = [
        migrations.RunPython(delete_other_companies, migrations.RunPython.noop),
    ]
