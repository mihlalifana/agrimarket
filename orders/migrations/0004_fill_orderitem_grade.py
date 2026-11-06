from django.db import migrations


def fill(apps, schema_editor):
    OrderItem = apps.get_model("orders", "OrderItem")
    for it in OrderItem.objects.select_related("listing__commodity").filter(product_code=""):
        it.product_code = it.listing.commodity.code
        it.grade = it.listing.grade
        it.save(update_fields=["product_code", "grade"])


class Migration(migrations.Migration):
    dependencies = [("orders", "0003_orderitem_grade_snapshot"), ("marketplace", "0004_listing_commodity_required")]
    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
