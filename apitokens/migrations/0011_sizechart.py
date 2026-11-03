import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('apitokens', '0010_tryon_limits'),
    ]

    operations = [
        migrations.CreateModel(
            name='SizeChart',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('category', models.CharField(blank=True, default='', max_length=120, verbose_name='الفئة')),
                ('chart', models.JSONField(default=list, verbose_name='المقاسات')),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('merchant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='size_charts', to='apitokens.apitoken')),
            ],
            options={
                'verbose_name': 'جدول مقاسات',
                'verbose_name_plural': 'جداول المقاسات',
                'constraints': [models.UniqueConstraint(fields=('merchant', 'category'), name='unique_size_chart_per_category')],
            },
        ),
    ]
