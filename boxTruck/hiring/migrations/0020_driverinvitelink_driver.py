from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('hiring', '0019_driverinvitelink_manager'),
    ]

    operations = [
        migrations.AddField(
            model_name='driverinvitelink',
            name='driver',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='sign_links',
                to='hiring.driver',
            ),
        ),
    ]
