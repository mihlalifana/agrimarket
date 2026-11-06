"""The farmer approval step was removed: orders are confirmed once payment is secured.

Existing data: flag sales that were entered after the day they happened, and
confirm sub-orders that were only waiting for the farmer's approval.
"""
from django.db import migrations
from django.utils import timezone


def forwards(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    SubOrder = apps.get_model("orders", "SubOrder")
    StatusChange = apps.get_model("orders", "StatusChange")
    for o in Order.objects.all().only("pk", "order_date", "created_at"):
        if o.order_date < timezone.localtime(o.created_at).date():
            Order.objects.filter(pk=o.pk).update(recorded_late=True)
    for sub in SubOrder.objects.filter(status="pending").select_related("order__payment"):
        pay = getattr(sub.order, "payment", None)
        if sub.order.payment_method == "cod" or (pay and pay.status == "paid"):
            when = (pay.confirmed_at if pay and pay.confirmed_at else sub.created_at)
            SubOrder.objects.filter(pk=sub.pk).update(status="approved", approved_at=when)
            if not sub.invoice_number:
                SubOrder.objects.filter(pk=sub.pk).update(invoice_number=f"INV-{when:%Y}-{sub.pk:06d}")
            StatusChange.objects.create(suborder=sub, from_status="pending", to_status="approved",
                                        note="Payment received", created_at=when)


class Migration(migrations.Migration):
    dependencies = [("orders", "0007_order_recorded_late_alter_dispute_created_at_and_more")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
