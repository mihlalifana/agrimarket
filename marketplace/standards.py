"""Standard product catalogue with official South African grade tables.

Grades follow the regulations under the Agricultural Product Standards Act,
1990 (Act No. 119 of 1990):
  - Maize: R.4368, regulation 6 (WM1-WM3, YM1-YM3)
  - Bread wheat: R.64, regulation 5 (Grade 1-4, Utility)
  - Sunflower seed: FH1 / FS1 (Grain SA summary of the sunflower regulations)
  - Soya beans: SB1
  - Fresh vegetables: R.364, regulation 3 (Class 1, 2, 3, Lowest Class)
  - Tomatoes: R.3616, regulation 4; potatoes: R.273, regulation 4
  - Fresh fruit: R.631, regulation 3 (Class 1, 2, Lowest Class)
  - Eggs: R.345, regulation 4 (Grade 1-3) and Table 2 (sizes)
Live animals are sold per head and have no grade table here.
Seed is described as certified or uncertified (Plant Improvement Act).

Each entry: (category, kind, code, name, units, grades, reference, perishable, shelf_days)
"""

VEG_CLASSES = ["Class 1", "Class 2", "Class 3", "Lowest Class", "Unclassified"]
FRUIT_CLASSES = ["Class 1", "Class 2", "Lowest Class"]
EGG_GRADES = ["Grade 1", "Grade 2", "Grade 3"]

R_MAIZE = "APS Act R.4368 – maize grading regulations"
R_WHEAT = "APS Act R.64 – bread wheat grading regulations"
R_SUN = "APS Act – sunflower seed grading regulations"
R_SOY = "APS Act – soya bean grading regulations"
R_VEG = "APS Act R.364 – fresh vegetables (local market)"
R_TOM = "APS Act R.3616 – tomatoes (local market)"
R_POT = "APS Act R.273 – potatoes (local market)"
R_FRUIT = "APS Act R.631 – fresh fruit (local market)"
R_EGG = "APS Act R.345 – eggs (grades and sizes)"
R_SEED = "Plant Improvement Act – seed certification"
R_LIVE = "Animal Identification Act – sold per head, identified by brand/tag"

STANDARD_COMMODITIES = [
    # Grains & Oilseeds
    ("Grains & Oilseeds", "produce", "MAIZE-W", "White maize", ["ton", "kg"], ["WM1", "WM2", "WM3"], R_MAIZE, False, 365),
    ("Grains & Oilseeds", "produce", "MAIZE-Y", "Yellow maize", ["ton", "kg"], ["YM1", "YM2", "YM3"], R_MAIZE, False, 365),
    ("Grains & Oilseeds", "produce", "WHEAT-B", "Bread wheat", ["ton", "kg"],
     ["Grade 1", "Grade 2", "Grade 3", "Grade 4", "Utility"], R_WHEAT, False, 365),
    ("Grains & Oilseeds", "produce", "SUNFL", "Sunflower seed", ["ton", "kg"], ["FH1", "FS1"], R_SUN, False, 365),
    ("Grains & Oilseeds", "produce", "SOYA", "Soya beans", ["ton", "kg"], ["SB1"], R_SOY, False, 365),
    # Vegetables
    ("Vegetables", "produce", "TOMATO", "Tomatoes", ["crate", "kg"], VEG_CLASSES, R_TOM, True, 10),
    ("Vegetables", "produce", "POTATO", "Potatoes", ["kg", "ton"], VEG_CLASSES, R_POT, True, 45),
    ("Vegetables", "produce", "CABBAGE", "Cabbage", ["crate", "kg"], VEG_CLASSES, R_VEG, True, 21),
    ("Vegetables", "produce", "ONION", "Onions", ["kg", "ton"], VEG_CLASSES, R_VEG, True, 60),
    ("Vegetables", "produce", "BUTTERNUT", "Butternut", ["kg", "crate"], VEG_CLASSES, R_VEG, True, 60),
    ("Vegetables", "produce", "CARROT", "Carrots", ["kg", "crate"], VEG_CLASSES, R_VEG, True, 30),
    ("Vegetables", "produce", "SPINACH", "Spinach", ["crate", "kg"], VEG_CLASSES, R_VEG, True, 7),
    # Fruit
    ("Fruit", "produce", "AVO", "Avocados", ["crate", "kg"], FRUIT_CLASSES, R_FRUIT, True, 14),
    ("Fruit", "produce", "ORANGE", "Oranges", ["crate", "kg", "ton"], FRUIT_CLASSES, R_FRUIT, True, 30),
    ("Fruit", "produce", "APPLE", "Apples", ["crate", "kg", "ton"], FRUIT_CLASSES, R_FRUIT, True, 60),
    ("Fruit", "produce", "BANANA", "Bananas", ["crate", "kg"], FRUIT_CLASSES, R_FRUIT, True, 10),
    # Animal products
    ("Animal Products", "produce", "EGG-L", "Eggs – large", ["crate"], EGG_GRADES, R_EGG, True, 28),
    ("Animal Products", "produce", "EGG-XL", "Eggs – extra large", ["crate"], EGG_GRADES, R_EGG, True, 28),
    ("Animal Products", "produce", "EGG-M", "Eggs – medium", ["crate"], EGG_GRADES, R_EGG, True, 28),
    ("Animal Products", "produce", "HONEY", "Honey", ["kg"], [], "No grade table applied", False, None),
    ("Animal Products", "produce", "MILK", "Raw milk", ["kg"], [], "Milk and Dairy Products regulations", True, 3),
    # Seeds
    ("Seeds", "produce", "SEED-WHEAT", "Wheat seed", ["kg"], ["Certified", "Uncertified"], R_SEED, False, 365),
    ("Seeds", "produce", "SEED-MAIZE", "Maize seed", ["kg"], ["Certified", "Uncertified"], R_SEED, False, 365),
    # Livestock (per head, no grade)
    ("Cattle", "livestock", "CATTLE-WEANER", "Weaner calves", ["head"], [], R_LIVE, False, None),
    ("Cattle", "livestock", "CATTLE-BULL", "Breeding bulls", ["head"], [], R_LIVE, False, None),
    ("Cattle", "livestock", "CATTLE-COW", "Cows and heifers", ["head"], [], R_LIVE, False, None),
    ("Sheep & Goats", "livestock", "GOAT", "Goats", ["head"], [], R_LIVE, False, None),
    ("Sheep & Goats", "livestock", "LAMB", "Lambs", ["head"], [], R_LIVE, False, None),
    ("Sheep & Goats", "livestock", "SHEEP", "Sheep (ewes and rams)", ["head"], [], R_LIVE, False, None),
    ("Pigs", "livestock", "PIG-WEANER", "Weaner pigs", ["head"], [], R_LIVE, False, None),
    ("Pigs", "livestock", "PIG-PORKER", "Porkers", ["head"], [], R_LIVE, False, None),
    ("Poultry", "livestock", "HEN-LAYER", "Layer hens", ["head"], [], R_LIVE, False, None),
    ("Poultry", "livestock", "BROILER", "Broilers (live)", ["head"], [], R_LIVE, False, None),
]


def sync_standards(Category, Commodity):
    """Create or update the standard categories and commodities. Safe to run repeatedly.

    Takes model classes so it also works inside a data migration.
    """
    created = 0
    for cat_name, kind, code, name, units, grades, ref, perishable, shelf in STANDARD_COMMODITIES:
        cat, _ = Category.objects.get_or_create(name=cat_name, defaults={"kind": kind,
                                                                         "slug": _slug(cat_name)})
        obj, was_created = Commodity.objects.update_or_create(
            code=code,
            defaults={"category": cat, "name": name, "units": ",".join(units),
                      "grades": "\n".join(grades), "standard_reference": ref,
                      "is_perishable": perishable, "shelf_life_days": shelf})
        created += was_created
    return created


def _slug(text):
    from django.utils.text import slugify
    return slugify(text)
