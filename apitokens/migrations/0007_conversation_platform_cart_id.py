from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('apitokens', '0006_sallaconnection'),
    ]

    operations = [
        migrations.AddField(
            model_name='conversation',
            name='platform_cart_id',
            field=models.CharField(blank=True, db_index=True, default='', max_length=64),
        ),
    ]
