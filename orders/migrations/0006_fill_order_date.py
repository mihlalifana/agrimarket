from django.db import migrations
from django.utils import timezone


def fill(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    for o in Order.objects.all().only("pk", "created_at"):
        Order.objects.filter(pk=o.pk).update(order_date=timezone.localtime(o.created_at).date())


class Migration(migrations.Migration):
    dependencies = [("orders", "0005_order_date_and_window")]
    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
