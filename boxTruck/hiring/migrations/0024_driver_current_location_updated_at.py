from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hiring', '0023_remove_drivercompany_driver'),
    ]

    operations = [
        migrations.AddField(
            model_name='driver',
            name='current_location_updated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
