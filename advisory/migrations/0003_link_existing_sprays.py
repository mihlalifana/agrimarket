"""Put products from spray records made before the catalogue into it, as entries WAITING FOR
VERIFICATION, and link the old records to them. Nothing is marked verified automatically: the
administrator must check each entry against the product label before it can be used again."""
from django.db import migrations


def forwards(apps, schema_editor):
    SprayRecord = apps.get_model("advisory", "SprayRecord")
    Remedy = apps.get_model("advisory", "RegisteredRemedy")
    Use = apps.get_model("advisory", "RemedyUse")
    for s in SprayRecord.objects.filter(remedy__isnull=True).select_related("planting"):
        remedy, _ = Remedy.objects.get_or_create(
            registration_no=s.registration_no,
            defaults={"trade_name": s.product, "active_ingredient": s.active_ingredient,
                      "source": "Taken from spray records made before the catalogue - check against the label",
                      "status": "pending"})
        use, created = Use.objects.get_or_create(remedy=remedy, commodity_id=s.planting.commodity_id,
                                                 defaults={"phi_days": s.phi_days})
        if not created and s.phi_days > use.phi_days:   # keep the longest (safest) waiting period seen
            use.phi_days = s.phi_days
            use.save(update_fields=["phi_days"])
        s.remedy = remedy
        s.save(update_fields=["remedy"])


class Migration(migrations.Migration):
    dependencies = [("advisory", "0002_remedy_catalogue")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
