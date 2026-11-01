import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('apitokens', '0005_shopify_ready_bot'),
    ]

    operations = [
        migrations.CreateModel(
            name='SallaConnection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('salla_merchant_id', models.CharField(max_length=32, unique=True, verbose_name='رقم التاجر بسلة')),
                ('store_name', models.CharField(blank=True, default='', max_length=255, verbose_name='اسم المتجر')),
                ('store_domain', models.CharField(blank=True, default='', max_length=255, verbose_name='رابط المتجر')),
                ('access_token', models.TextField(blank=True, default='')),
                ('refresh_token', models.TextField(blank=True, default='')),
                ('token_expires_at', models.DateTimeField(blank=True, null=True)),
                ('scope', models.CharField(blank=True, default='', max_length=500)),
                ('is_active', models.BooleanField(default=True, verbose_name='فعّال')),
                ('last_synced_at', models.DateTimeField(blank=True, null=True, verbose_name='آخر مزامنة')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='تاريخ الربط')),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('merchant', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='salla_connection', to='apitokens.apitoken')),
            ],
            options={
                'verbose_name': 'ربط سلة',
                'verbose_name_plural': 'ربط سلة',
            },
        ),
    ]
