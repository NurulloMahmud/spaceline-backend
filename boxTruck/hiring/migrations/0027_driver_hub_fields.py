from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hiring', '0026_remove_company'),
    ]

    operations = [
        migrations.AddField(
            model_name='driver',
            name='hub_driver_id',
            field=models.PositiveIntegerField(blank=True, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='driver',
            name='hub_invite_id',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='driver',
            name='hub_invite_status',
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name='driver',
            name='hub_invited_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
