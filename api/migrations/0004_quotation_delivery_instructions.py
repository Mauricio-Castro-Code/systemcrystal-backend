from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0003_orderworkflowevent_order_statuses"),
    ]

    operations = [
        migrations.AddField(
            model_name="quotation",
            name="delivery_instructions",
            field=models.CharField(blank=True, max_length=220),
        ),
    ]
