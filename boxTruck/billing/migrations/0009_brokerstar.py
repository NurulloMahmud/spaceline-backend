from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('users', '0002_company_address'),
        ('billing', '0008_tag_color'),
    ]

    operations = [
        migrations.CreateModel(
            name='BrokerStar',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('stars', models.PositiveSmallIntegerField()),
                ('comment', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('broker', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='stars', to='billing.broker')),
                ('company', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='broker_stars', to='users.company')),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='broker_stars', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'broker_stars',
                'verbose_name': 'Broker Star',
                'verbose_name_plural': 'Broker Stars',
            },
        ),
    ]
