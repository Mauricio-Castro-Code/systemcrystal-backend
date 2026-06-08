from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0004_quotation_delivery_instructions"),
    ]

    operations = [
        migrations.AddField(
            model_name="quotation",
            name="advance_payment",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name="quotation",
            name="apply_tax",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="quotation",
            name="discount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name="quotation",
            name="tax_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
    ]
