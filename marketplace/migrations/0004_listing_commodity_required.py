import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("marketplace", "0003_load_standard_commodities")]
    operations = [
        migrations.AlterField(
            model_name="listing", name="commodity",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="listings",
                                    to="marketplace.commodity"),
        ),
    ]
