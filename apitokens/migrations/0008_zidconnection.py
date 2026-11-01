import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('apitokens', '0007_conversation_platform_cart_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='ZidConnection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('store_id', models.CharField(max_length=32, unique=True, verbose_name='رقم المتجر بزد')),
                ('store_uuid', models.CharField(blank=True, db_index=True, default='', max_length=64)),
                ('store_name', models.CharField(blank=True, default='', max_length=255, verbose_name='اسم المتجر')),
                ('store_url', models.CharField(blank=True, default='', max_length=255, verbose_name='رابط المتجر')),
                ('authorization_token', models.TextField(blank=True, default='')),
                ('manager_token', models.TextField(blank=True, default='')),
                ('refresh_token', models.TextField(blank=True, default='')),
                ('token_expires_at', models.DateTimeField(blank=True, null=True)),
                ('tokens_refreshed_at', models.DateTimeField(blank=True, null=True)),
                ('is_active', models.BooleanField(default=True, verbose_name='فعّال')),
                ('last_synced_at', models.DateTimeField(blank=True, null=True, verbose_name='آخر مزامنة')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='تاريخ الربط')),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('merchant', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='zid_connection', to='apitokens.apitoken')),
            ],
            options={
                'verbose_name': 'ربط زد',
                'verbose_name_plural': 'ربط زد',
            },
        ),
    ]
