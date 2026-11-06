"""Load the standard product catalogue and link existing listings to it."""
from django.db import migrations

NAME_HINTS = {
    "white maize": "MAIZE-W", "yellow maize": "MAIZE-Y", "sunflower": "SUNFL", "wheat seed": "SEED-WHEAT",
    "wheat": "WHEAT-B", "soya": "SOYA", "tomato": "TOMATO", "potato": "POTATO", "cabbage": "CABBAGE",
    "onion": "ONION", "butternut": "BUTTERNUT", "carrot": "CARROT", "avocado": "AVO", "orange": "ORANGE",
    "apple": "APPLE", "banana": "BANANA", "egg": "EGG-L", "honey": "HONEY", "weaner calves": "CATTLE-WEANER",
    "bull": "CATTLE-BULL", "goat": "GOAT", "lamb": "LAMB", "dorper": "LAMB", "pig": "PIG-WEANER",
    "hen": "HEN-LAYER", "broiler": "BROILER",
}


def forwards(apps, schema_editor):
    from marketplace.standards import sync_standards
    Category = apps.get_model("marketplace", "Category")
    Commodity = apps.get_model("marketplace", "Commodity")
    Listing = apps.get_model("marketplace", "Listing")
    sync_standards(Category, Commodity)
    for listing in Listing.objects.filter(commodity__isnull=True):
        name = listing.name.lower()
        code = next((c for hint, c in NAME_HINTS.items() if hint in name), None)
        commodity = Commodity.objects.filter(code=code).first() if code else None
        if commodity is None:
            # Unknown product: create a commodity for it in the listing's category.
            commodity, _ = Commodity.objects.get_or_create(
                name=listing.name[:60],
                defaults={"category_id": listing.category_id, "code": f"OTHER-{listing.pk}",
                          "units": listing.unit, "grades": ""})
        listing.commodity = commodity
        listing.category_id = commodity.category_id
        grades = [g.strip() for g in commodity.grades.splitlines() if g.strip()]
        if grades and listing.grade not in grades:
            listing.grade = grades[0]
        if not grades:
            listing.grade = ""
        units = commodity.units.split(",")
        if listing.unit not in units:
            commodity.units = commodity.units + "," + listing.unit
            commodity.save()
        listing.save()


class Migration(migrations.Migration):
    dependencies = [("marketplace", "0002_favouritefarm_searchlog_stockalert_listing_harvest_and_more")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
