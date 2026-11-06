"""Reference figures for South African field and vegetable crops.

Used by the planting planner only when the farm has no history of its own and
too few other farms have grown the crop. They are typical guide values in the
style of the provincial agriculture department enterprise budgets: real farms
vary widely, which is why the planner always prefers the farm's own records,
then the average of nearby farms, and says which source it used.

Crates are 15 kg field crates (tomatoes) or about 6 heads (cabbage).
Months are the usual planting months for the summer-rainfall interior
(Highveld / Bushveld); coastal and winter-rainfall areas differ.
"""
from decimal import Decimal as D

GRASS = "Grasses (maize, wheat)"
NIGHTSHADE = "Nightshades (tomato, potato)"
BRASSICA = "Cabbage family"
ALLIUM = "Onion family"
LEGUME = "Legumes (soya)"

# code: harvest span (days from first to last pick), unit, days to first harvest, yield/ha (dryland, irrigated), input cost/ha (dryland, irrigated),
#       guide price per unit, planting months, crop family, preferred pH (KCl) range, needs irrigation
CROP_GUIDE = {
    "MAIZE-W": dict(span=14, unit="ton", days=150, yield_=(D("5"), D("11")), cost=(D("14000"), D("29000")), price=D("4000"),
                    months=[10, 11, 12], family=GRASS, ph=(D("4.8"), D("7.0")), irrigation=False),
    "MAIZE-Y": dict(span=14, unit="ton", days=150, yield_=(D("5"), D("11")), cost=(D("14000"), D("29000")), price=D("3900"),
                    months=[10, 11, 12], family=GRASS, ph=(D("4.8"), D("7.0")), irrigation=False),
    "WHEAT-B": dict(span=14, unit="ton", days=160, yield_=(D("2.5"), D("7")), cost=(D("11000"), D("24000")), price=D("5800"),
                    months=[5, 6, 7], family=GRASS, ph=(D("5.0"), D("7.5")), irrigation=False),
    "SUNFL": dict(span=14, unit="ton", days=125, yield_=(D("1.6"), D("2.8")), cost=(D("8000"), D("13000")), price=D("8200"),
                  months=[11, 12, 1], family="Daisy family (sunflower)", ph=(D("4.8"), D("7.5")), irrigation=False),
    "SOYA": dict(span=14, unit="ton", days=135, yield_=(D("2.5"), D("4")), cost=(D("10000"), D("16000")), price=D("7800"),
                 months=[11, 12], family=LEGUME, ph=(D("5.0"), D("7.0")), irrigation=False),
    "TOMATO": dict(span=56, unit="crate", days=110, yield_=(None, D("4000")), cost=(None, D("220000")), price=D("110"),
                   months=[1, 2, 7, 8, 9], family=NIGHTSHADE, ph=(D("5.5"), D("7.0")), irrigation=True),
    "POTATO": dict(span=14, unit="kg", days=110, yield_=(None, D("35000")), cost=(None, D("160000")), price=D("6.00"),
                   months=[1, 2, 7, 8], family=NIGHTSHADE, ph=(D("4.8"), D("6.5")), irrigation=True),
    "ONION": dict(span=14, unit="kg", days=160, yield_=(None, D("45000")), cost=(None, D("120000")), price=D("5.50"),
                  months=[3, 4, 5], family=ALLIUM, ph=(D("5.5"), D("7.5")), irrigation=True),
    "CABBAGE": dict(span=21, unit="crate", days=100, yield_=(None, D("3600")), cost=(None, D("80000")), price=D("62"),
                    months=[2, 3, 4, 7, 8, 9], family=BRASSICA, ph=(D("5.5"), D("7.5")), irrigation=True),
    "CARROT": dict(span=21, unit="kg", days=110, yield_=(None, D("40000")), cost=(None, D("90000")), price=D("5.00"),
                   months=[2, 3, 4, 7, 8, 9], family="Carrot family", ph=(D("5.5"), D("7.0")), irrigation=True),
    "SPINACH": dict(span=35, unit="kg", days=60, yield_=(None, D("20000")), cost=(None, D("55000")), price=D("9.00"),
                    months=[2, 3, 4, 5, 6, 7, 8, 9], family="Beet family (spinach)", ph=(D("6.0"), D("7.5")),
                    irrigation=True),
    "BUTTERNUT": dict(span=21, unit="kg", days=115, yield_=(None, D("25000")), cost=(None, D("65000")), price=D("5.00"),
                      months=[8, 9, 10, 11, 12], family="Pumpkin family", ph=(D("5.5"), D("7.0")), irrigation=True),
}

MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def month_names(months):
    return ", ".join(MONTHS[m] for m in months)
