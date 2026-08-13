import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hiring', '0013_alter_driver_current_latitude_and_more'),
        ('users', '0010_team_customuser_team'),
    ]

    operations = [
        migrations.AddField(
            model_name='driver',
            name='team',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='drivers', to='users.team'),
        ),
        migrations.AddField(
            model_name='driver',
            name='dispatcher',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='assigned_drivers', to=settings.AUTH_USER_MODEL),
        ),
    ]
