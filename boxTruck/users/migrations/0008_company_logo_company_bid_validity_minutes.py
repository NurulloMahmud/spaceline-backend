from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0007_rename_start_working_date_customuser_started_working_date'),
    ]

    operations = [
        migrations.AddField(
            model_name='company',
            name='logo',
            field=models.ImageField(blank=True, null=True, upload_to='companies/logos/'),
        ),
        migrations.AddField(
            model_name='company',
            name='bid_validity_minutes',
            field=models.PositiveIntegerField(default=15),
        ),
    ]
