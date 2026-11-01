from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('apitokens', '0008_zidconnection'),
    ]

    operations = [
        migrations.CreateModel(
            name='ZidSubscription',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('store_id', models.CharField(max_length=32, unique=True)),
                ('store_uuid', models.CharField(blank=True, db_index=True, default='', max_length=64)),
                ('plan_id', models.CharField(blank=True, default='', max_length=32)),
                ('plan_name', models.CharField(blank=True, default='', max_length=120, verbose_name='الباقة')),
                ('plan_type', models.CharField(blank=True, default='', max_length=32)),
                ('is_active', models.BooleanField(default=False, verbose_name='الاشتراك فعّال')),
                ('end_date', models.DateTimeField(blank=True, null=True)),
                ('last_event', models.CharField(blank=True, default='', max_length=80)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'اشتراك زد',
                'verbose_name_plural': 'اشتراكات زد',
            },
        ),
    ]
