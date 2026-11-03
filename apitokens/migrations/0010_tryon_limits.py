import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('apitokens', '0009_zidsubscription'),
    ]

    operations = [
        migrations.AddField(
            model_name='apitoken',
            name='tryons_per_shopper',
            field=models.PositiveIntegerField(default=2, help_text='0 = بلا حد', verbose_name='عدد التجارب لكل زبون'),
        ),
        migrations.AddField(
            model_name='apitoken',
            name='tryon_limit_days',
            field=models.PositiveIntegerField(default=30, verbose_name='مدة حد التجارب (أيام)'),
        ),
        migrations.CreateModel(
            name='TryOnRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('shopper_id', models.CharField(blank=True, db_index=True, default='', max_length=64)),
                ('ip_hash', models.CharField(blank=True, db_index=True, default='', max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('merchant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='tryon_records', to='apitokens.apitoken')),
            ],
            options={
                'verbose_name': 'تجربة لبس',
                'verbose_name_plural': 'تجارب اللبس',
            },
        ),
    ]
