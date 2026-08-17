from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('hiring', '0018_driver_telegram_user_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='driverinvitelink',
            name='manager',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='managed_invite_links',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
