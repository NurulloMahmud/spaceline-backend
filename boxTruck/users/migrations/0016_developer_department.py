"""
The Developer department: everything Management can do, plus the accounts of
other developers and the posted-loads listener mailbox (see users.permissions).
"""
from django.db import migrations


def add_developer(apps, schema_editor):
    Department = apps.get_model('users', 'Department')
    if not Department.objects.filter(name__iexact='Developer').exists():
        Department.objects.create(name='Developer')


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('users', '0015_delete_other_companies'),
    ]

    operations = [
        migrations.RunPython(add_developer, noop),
    ]
