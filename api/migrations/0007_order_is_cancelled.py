from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0006_add_freight_zone"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="is_cancelled",
            field=models.BooleanField(default=False),
        ),
    ]
