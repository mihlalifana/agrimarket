from django.db import migrations


def link_unambiguous_harvest_listings(apps, schema_editor):
    Harvest = apps.get_model("farm", "Harvest")
    Listing = apps.get_model("marketplace", "Listing")
    db = schema_editor.connection.alias

    for harvest in Harvest.objects.using(db).filter(listing__isnull=True).iterator():
        planting = harvest.planting
        matching_harvests = Harvest.objects.using(db).filter(
            planting__farmer_id=planting.farmer_id,
            planting__commodity_id=planting.commodity_id,
            harvested_on=harvest.harvested_on,
            grade=harvest.grade,
            listing__isnull=True,
        )
        if matching_harvests.count() != 1:
            continue

        listings = Listing.objects.using(db).filter(
            farmer_id=planting.farmer_id,
            commodity_id=planting.commodity_id,
            unit=planting.unit,
            harvest_date=harvest.harvested_on,
            grade=harvest.grade,
            harvest__isnull=True,
        )
        if listings.count() == 1:
            listings.update(harvest_id=harvest.pk)


class Migration(migrations.Migration):
    dependencies = [
        ("farm", "0004_no_duplicates"),
        ("marketplace", "0004_listing_commodity_required"),
    ]

    operations = [
        migrations.RunPython(link_unambiguous_harvest_listings, migrations.RunPython.noop),
    ]
