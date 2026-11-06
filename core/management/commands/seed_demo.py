"""Load a small, realistic demo season:  python manage.py seed_demo

Five farms, five buyers, one agronomist, drivers and staff (password for all:
Farm2026!). Everything is dated between 1 March and today and was created
through the same services the screens use, so every timestamp, invoice,
history entry and notification carries the day it happened:

* vegetable plantings with successive picks, each pick listed for sale;
  stock that is not sold before it expires is written off (wastage report);
* last season's maize sold from the silo, with monthly price changes;
* goats and weaners sold per head, with one herd inside a withdrawal period;
* buyers ordering on their usual weekdays, paying by card or cash on
  delivery; packing, driver scheduling, delivery and completion on the
  following days;
* production costs per crop and herd scaled to the area planted;
* an agronomist with soil tests, field visits, spray records (one crop still
  inside its pre-harvest interval), recommendations and pest alerts;
* buyer searches and quote requests that nobody could fill (unmet demand).
"""
import random
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal as D

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import FarmerProfile, User
from advisory.models import (AgronomistProfile, FarmLink, FieldVisit, PestAlert, Recommendation, RegisteredRemedy,
                             RemedyUse, SoilTest, SprayRecord)
from advisory.services import send_alert
from core import clock
from core.models import SiteSettings
from farm.crop_guide import CROP_GUIDE
from farm.models import CostEntry, Field, Harvest, Herd, Planting, Treatment
from marketplace.models import (Category, Commodity, FavouriteFarm, Listing, LivestockDetail, PriceTier,
                                SearchLog, StockAdjustment, StockAlert)
from marketplace.services import adjust_stock
from marketplace.standards import sync_standards
from orders import services as osvc
from orders.paystack import ZA_BANKS
from orders.models import DeliveryRate, Dispute, Order, Payment, QuoteRequest, Refund, Review, SubOrder

PASSWORD = "Farm2026!"
S = SubOrder.Status
CARD = "4111111111111111"
RATES = {"FS": (350, 120), "GP": (400, 140), "NW": (420, 150), "MP": (450, 160), "LP": (450, 160),
         "KZN": (520, 180), "EC": (550, 190), "NC": (600, 200), "WC": (650, 210)}

# Price pattern through the year (Jan..Dec): vegetables are dear in winter, grain after harvest is cheap.
SEASON = {
    "veg": [0.90, 0.92, 0.95, 1.00, 1.08, 1.18, 1.22, 1.15, 1.02, 0.96, 0.92, 0.90],
    "grain": [1.10, 1.12, 1.08, 1.00, 0.94, 0.92, 0.94, 0.97, 1.00, 1.03, 1.06, 1.08],
    "eggs": [1.00, 0.99, 1.00, 1.02, 1.03, 1.04, 1.04, 1.03, 1.01, 1.00, 1.02, 1.05],
    "live": [0.95, 0.93, 0.93, 0.95, 0.98, 1.00, 1.01, 1.03, 1.05, 1.07, 1.10, 1.12],
}

FARMS = {
    "rietvlei": ("farmer@agrimarket.test", "Johan", "van Wyk", "FS", "0831000001", "Rietvlei Farm",
                 "Bloemfontein", "4123456789", "RVW",
                 [("Land 2", 32, "Avalon", False), ("Land 3", 45, "Hutton", False), ("Land 5", 28, "Clovelly", False)]),
    "umgeni": ("farmer2@agrimarket.test", "Nomsa", "Khumalo", "KZN", "0831000002", "Umgeni Valley Produce",
               "Howick", "", "UVP",
               [("Tunnel A", 0.25, "Sandy loam", True), ("River block", 0.6, "Alluvial loam", True),
                ("Hill block", 1.2, "Hutton", True), ("Spinach beds", 0.25, "Sandy loam", True)]),
    "bokamoso": ("farmer4@agrimarket.test", "Lebogang", "Mashaba", "LP", "0831000005", "Bokamoso Farm",
                 "Polokwane", "", "BKM",
                 [("Garden 1", 0.3, "Sandy loam", True), ("Garden 2", 0.6, "Sandy loam", True),
                  ("Garden 3", 0.5, "Hutton", True), ("Garden 4", 0.3, "Clay loam", True),
                  ("Lower field", 6, "Hutton", False)]),
    "highveld": ("farmer5@agrimarket.test", "Pieter", "Joubert", "MP", "0831000006", "Highveld Fields",
                 "Ermelo", "4555123456", "HVF",
                 [("Tunnel 2", 0.25, "Sandy loam", True), ("Pivot 1", 2.5, "Hutton", True),
                  ("Pivot 2", 1.2, "Hutton", True), ("Noord-land", 50, "Avalon", False)]),
    "kgalagadi": ("farmer3@agrimarket.test", "Kagiso", "Molefe", "NW", "0831000003", "Kgalagadi Livestock",
                  "Vryburg", "4987654321", "KGL", [("Lusern camp", 8, "Kalahari sand", True)]),
    "karoo": ("farmer6@agrimarket.test", "Elsabe", "Botha", "NC", "0831000007", "Karoo Skaap Boerdery",
              "Colesberg", "4777987654", "KSB", [("Rivier land", 4, "Sandy loam", True)]),
    "amatola": ("farmer7@agrimarket.test", "Nomvula", "Gqola", "EC", "0831000008", "Amatola Greens",
                "Stutterheim", "", "AMG",
                [("Block 1", 0.6, "Clay loam", True), ("Block 2", 0.8, "Clay loam", True),
                 ("Veld", 12, "Shale", False)]),
    "lowveld": ("farmer8@agrimarket.test", "Sipho", "Nkuna", "MP", "0831000009", "Lowveld Fresh",
                "Mbombela", "4333221100", "LVF",
                [("Shade house", 0.3, "Sandy loam", True), ("River plot", 1.0, "Alluvial loam", True)]),
    "swartland": ("farmer9@agrimarket.test", "Willem", "Louw", "WC", "0831000010", "Swartland Graan",
                  "Malmesbury", "4222334455", "SWG",
                  [("Koppie land", 60, "Shale", False), ("Huis land", 25, "Shale", False)]),
    "vhembe": ("farmer10@agrimarket.test", "Mpho", "Netshitenzhe", "LP", "0831000011", "Vhembe Organics",
               "Thohoyandou", "", "VHO",
               [("Bed A", 0.3, "Red clay", True), ("Bed B", 0.4, "Red clay", True), ("Bed C", 0.5, "Red clay", True)]),
}

# farm, crop, field, ha, cultivar, grades, planting (month, day) list, base price, yield factor per planting
PLANTINGS = [
    ("umgeni", "TOMATO", "Tunnel A", "0.20", "Roma VF", ["Class 1"], [(3, 3), (7, 28)], 118, [1.05, 1.0]),
    ("umgeni", "CABBAGE", "River block", "0.50", "Grandslam", ["Class 1"], [(3, 2), (6, 15), (9, 20)], 64, [1.0, 0.95, 1.0]),
    ("umgeni", "POTATO", "Hill block", "0.60", "Mondial", ["Class 1", "Class 2"], [(3, 4), (7, 25)], 6.4, [1.0, 1.0]),
    ("umgeni", "SPINACH", "Spinach beds", "0.20", "Fordhook Giant", ["Class 1"], [(3, 10), (5, 20), (8, 1)], 9.5, [1, 0.4, 1]),
    ("bokamoso", "TOMATO", "Garden 1", "0.25", "Star 9037", ["Class 1"], [(3, 2), (7, 1)], 112, [0.95, 1.0]),
    ("bokamoso", "CABBAGE", "Garden 2", "0.50", "Conquistador", ["Class 1"], [(3, 5), (6, 10), (7, 10)], 60, [0.9, 0.35, 1.0]),
    ("bokamoso", "ONION", "Garden 3", "0.40", "Texas Grano", ["Class 1", "Class 2"], [(3, 16), (4, 20)], 6.2, [0.95, 1.0]),
    ("bokamoso", "SPINACH", "Garden 4", "0.25", "Fordhook Giant", ["Class 1"], [(3, 12), (6, 10), (8, 15)], 9.0, [1, 0.9, 1]),
    ("highveld", "TOMATO", "Tunnel 2", "0.20", "Roma", ["Class 1"], [(3, 6), (7, 5)], 120, [1.1, 1.0]),
    ("highveld", "POTATO", "Pivot 1", "0.80", "Sifra", ["Class 1", "Class 2"], [(3, 3), (8, 20)], 6.0, [1.05, 1.0]),
    ("highveld", "CABBAGE", "Pivot 2", "0.60", "Grandslam", ["Class 1"], [(3, 4), (7, 1), (9, 10)], 63, [1.0, 1.0, 1.0]),
    ("rietvlei", "WHEAT-B", "Land 2", "30", "SST 0127", ["Grade 1"], [(5, 28)], 5800, [1.0]),
    ("rietvlei", "MAIZE-W", "Land 3", "40", "PAN 5R-591", ["WM1"], [(10, 12)], 4100, [1.0]),
    ("highveld", "MAIZE-W", "Noord-land", "50", "PAN 5A-291", ["WM1"], [(10, 8)], 4100, [1.0]),
    ("amatola", "CABBAGE", "Block 1", "0.50", "Conquistador", ["Class 1"], [(3, 8), (7, 5)], 61, [1.0, 0.9]),
    ("amatola", "POTATO", "Block 2", "0.60", "BP1", ["Class 1", "Class 2"], [(3, 10), (8, 25)], 6.1, [0.95, 1.0]),
    ("amatola", "SPINACH", "Block 1", "0.10", "Fordhook Giant", ["Class 1"], [(5, 5), (8, 10)], 9.2, [1, 1]),
    ("lowveld", "TOMATO", "Shade house", "0.25", "Roma VF", ["Class 1"], [(3, 9), (7, 8)], 116, [1.0, 1.05]),
    ("lowveld", "BUTTERNUT", "River plot", "0.80", "Waltham", ["Class 1"], [(3, 2), (8, 20)], 5.2, [1.0, 1.0]),
    ("swartland", "WHEAT-B", "Koppie land", "55", "SST 0166", ["Grade 1"], [(5, 15)], 5800, [1.0]),
    ("vhembe", "SPINACH", "Bed A", "0.25", "Fordhook Giant", ["Class 1"], [(3, 6), (6, 1), (8, 20)], 9.4, [1, 1, 0.9]),
    ("vhembe", "CABBAGE", "Bed B", "0.35", "Grandslam", ["Class 1"], [(3, 9), (7, 12)], 62, [1.0, 1.0]),
    ("vhembe", "BUTTERNUT", "Bed C", "0.50", "Waltham", ["Class 1"], [(3, 4), (8, 28)], 5.4, [0.9, 1.0]),
]
# days to first harvest, share of the crop per pick, days between picks
PICKS = {"TOMATO": (100, [0.06, 0.12, 0.18, 0.2, 0.18, 0.14, 0.08, 0.04], 7),
         "CABBAGE": (95, [0.4, 0.35, 0.25], 7), "POTATO": (110, [1.0], 0), "ONION": (160, [1.0], 0),
         "SPINACH": (55, [0.2, 0.25, 0.25, 0.2, 0.1], 7), "WHEAT-B": (160, [1.0], 0), "MAIZE-W": (150, [1.0], 0),
         "BUTTERNUT": (115, [0.5, 0.3, 0.2], 7)}
GROUP = {"TOMATO": "veg", "CABBAGE": "veg", "POTATO": "veg", "ONION": "veg", "SPINACH": "veg",
         "MAIZE-W": "grain", "MAIZE-Y": "grain", "WHEAT-B": "grain", "EGG-L": "eggs", "GOAT": "live",
         "CATTLE-WEANER": "live", "BUTTERNUT": "veg", "LAMB": "live", "SHEEP": "live"}
MIN_ORDER = {"crate": 5, "kg": 50, "ton": 5, "head": 1}

# last season's maize sold from the silo: farm, crop, grade, tons, harvested (m, d), price/t
SILO = [("rietvlei", "MAIZE-W", "WM1", 220, (5, 15), 4150, "PAN 5R-591"),
        ("rietvlei", "MAIZE-Y", "YM1", 140, (5, 20), 3950, "DKC 78-87"),
        ("highveld", "MAIZE-W", "WM1", 60, (5, 25), 4120, "PAN 5A-291"),
        ("bokamoso", "MAIZE-W", "WM2", 12, (6, 10), 4000, ""),
        ("swartland", "WHEAT-B", "Grade 1", 120, None, 5850, "SST 0166"),
        ("amatola", "MAIZE-W", "WM2", 18, (6, 5), 3980, "")]

# farm, herd, animal, breed, head, started (m, d), tags, price/head, avg kg, age months, feed per month
HERDS = [("kgalagadi", "Bonsmara weaners", "CATTLE-WEANER", "Bonsmara", 40, (3, 3), "KGL W 001–040", 8400, 225, 7, 6500),
         ("kgalagadi", "Boer goats", "GOAT", "Boer goat", 45, (3, 3), "KGL G 001–045", 2500, 38, 10, 1800),
         ("bokamoso", "Village goats", "GOAT", "Indigenous veld goat", 25, (3, 6), "BKM G 001–025", 2300, 34, 12, 900),
         ("karoo", "Dorper lambs", "LAMB", "Dorper", 70, (3, 2), "KSB L 001–070", 2350, 40, 5, 4200),
         ("karoo", "Merino ewes", "SHEEP", "Merino", 30, (3, 2), "KSB M 001–030", 2900, 55, 30, 2100),
         ("amatola", "Nguni goats", "GOAT", "Nguni goat", 30, (3, 12), "AMG G 001–030", 2250, 33, 11, 1000)]

BUYERS = [  # email, first, last, province, phone, type, address, weekday(s), every n weeks, groups
    ("buyer@agrimarket.test", "Ayesha", "Patel", "GP", "0841000001", "retail",
     "FreshMart Supermarket\n12 Market Street\nJohannesburg\n2001", [0, 3], 1, ["veg", "eggs"]),
    ("market@agrimarket.test", "Lindiwe", "Dube", "LP", "0841000005", "wholesale",
     "Stand 14, Polokwane Fresh Produce Market\nPolokwane\n0700", [1, 4], 1, ["veg"]),
    ("restaurant@agrimarket.test", "Chantal", "Adams", "GP", "0841000004", "hospitality",
     "The Jacaranda Table\n21 Church Street\nPretoria\n0002", [2], 1, ["veg", "eggs", "live"]),
    ("mill@agrimarket.test", "Ruan", "Steyn", "FS", "0841000002", "processor",
     "Vrystaat Mills\nPlot 4, Industrial Road\nBloemfontein\n9301", [1], 2, ["grain"]),
    ("butchery@agrimarket.test", "Themba", "Zulu", "KZN", "0841000003", "butchery",
     "Zulu Butchery and Abattoir\n8 Church Street\nPietermaritzburg\n3201", [4], 2, ["live"]),
    ("durban@agrimarket.test", "Priya", "Naidoo", "KZN", "0841000006", "wholesale",
     "Stand 3, Durban Fresh Produce Market\nClairwood\n4052", [2], 1, ["veg"]),
    ("feedlot@agrimarket.test", "Gert", "Swanepoel", "NW", "0841000007", "butchery",
     "Bosveld Feedlot\nR52 Lichtenburg Road\nKoster\n0348", [3], 3, ["live"]),
    ("catering@agrimarket.test", "Zodwa", "Mthethwa", "GP", "0841000008", "hospitality",
     "Soweto School Meals Catering\n45 Vilakazi Street\nSoweto\n1804", [1], 1, ["veg", "eggs"]),
    ("coop@agrimarket.test", "Hennie", "du Plessis", "FS", "0841000009", "farmer",
     "Vrystaat Agri Co-op\n2 Kerk Street\nBethlehem\n9701", [3], 3, ["grain"]),
    ("spar@agrimarket.test", "Fatima", "Jacobs", "WC", "0841000010", "retail",
     "Bellville Fresh Spar\n18 Voortrekker Road\nBellville\n7530", [4], 2, ["veg", "eggs"]),
]
RECEIVERS = {"retail": ["T. Nkosi (receiving clerk)", "M. Naidoo (stores)"], "wholesale": ["L. Dube"],
             "hospitality": ["Chef C. Adams", "Kitchen manager"], "processor": ["Weighbridge – J. Pretorius"],
             "butchery": ["T. Zulu", "Stock clerk"], "farmer": ["Co-op silo – D. Mokoena"]}

SPRAYS = {  # crop: list of (days after planting, product, reg no, active ingredient, target, rate, PHI)
    "TOMATO": [(35, "Mancozeb 800 WP", "L5021", "mancozeb", "Early blight", "2 kg/ha", 7),
               (62, "Chlorantraniliprole 200 SC", "L5034", "chlorantraniliprole", "Tomato leafminer (Tuta absoluta)", "150 ml/ha", 3)],
    "CABBAGE": [(30, "Lambda-cyhalothrin 50 EC", "L5047", "lambda-cyhalothrin", "Aphids", "200 ml/ha", 7),
                (55, "Chlorantraniliprole 200 SC", "L5034", "chlorantraniliprole", "Diamondback moth", "100 ml/ha", 3)],
    "POTATO": [(45, "Copper oxychloride 850 WP", "L5052", "copper oxychloride", "Late blight (preventive)", "2.5 kg/ha", 7)],
}


def q(value, unit):
    """Round a quantity the way it is sold."""
    if unit in ("crate", "head"):
        return D(int(value))
    if unit == "kg":
        return (D(value) / 10).quantize(D("1"), ROUND_HALF_UP) * 10
    return D(value).quantize(D("1"), ROUND_HALF_UP)


def price_round(value, unit):
    v = D(str(value))
    step = {"kg": D("0.10"), "crate": D("1"), "ton": D("10"), "head": D("10")}[unit]
    return ((v / step).quantize(D("1"), ROUND_HALF_UP) * step).quantize(D("0.01"))


class Command(BaseCommand):
    help = "Load a small, realistic demo season (1 March to today)."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Run even if users already exist.")

    @transaction.atomic
    def handle(self, *args, **opts):
        others = User.objects.exclude(is_superuser=True).exclude(role="admin")
        if others.exists() and not opts["force"]:
            raise CommandError("Database already has farmers, buyers or staff. Use a fresh database "
                               "(admin accounts may stay) or --force.")
        random.seed(2026)
        self.today = timezone.localdate()
        self.start, _ = SiteSettings.date_window()
        rules = SiteSettings.load()
        # Sample data is paid with the simulated card (no internet). Switch to Paystack under Settings.
        rules.card_gateway = "simulated"
        rules.save(update_fields=["card_gateway"])
        sync_standards(Category, Commodity)
        self.C = {c.code: c for c in Commodity.objects.all()}
        for prov, (base, per_ton) in RATES.items():
            DeliveryRate.objects.update_or_create(province=prov, defaults={"base_fee": base, "per_ton_fee": per_ton})

        self._people()
        self._farms()
        self._advisory_setup()
        self._trade()
        self._advisory_season()
        self._after_trade()
        self._buyer_signals()
        osvc.run_timeouts()
        self._open_orders()
        # more sample spray products (made-up labels) so every demo crop has something to choose from
        from django.core.management import call_command
        call_command("load_sample_remedies", verify_as="admin@agrimarket.test", stdout=self.stdout)
        # People read their notifications as they go; only the last few days are still unread.
        from core.models import Notification
        Notification.objects.filter(created_at__lt=timezone.now() - timedelta(days=4)).update(is_read=True)

        n = Order.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f"Demo season loaded ({self.start:%d %b} – {self.today:%d %b %Y}): {User.objects.count()} users, "
            f"{Planting.objects.count()} plantings, {Harvest.objects.count()} harvests, {Listing.objects.count()} "
            f"listings, {n} orders.\n\nLog in with (password {PASSWORD}):\n"
            "  Admins        admin@, admin2@, admin3@agrimarket.test\n"
            "  Farmers       farmer@ (grain FS), farmer2@ (veg KZN), farmer3@ (cattle NW), farmer4@ (Bokamoso LP),\n"
            "                farmer5@ (MP), farmer6@ (sheep NC), farmer7@ (EC), farmer8@ (Lowveld MP), farmer9@ (wheat WC),\n"
            "                farmer10@ (Vhembe LP), newfarmer@ (waiting for verification)\n"
            "  Agronomists   agronomist@ (three farms), agronomist3@ … agronomist9@, agronomist2@ and agronomist10@ waiting\n"
            "  Buyers        buyer@, market@, restaurant@, mill@, butchery@, durban@, feedlot@, catering@, coop@, spar@\n"
            "  Distributors  driver.lp@, driver2.lp@, driver.kzn@, driver.mp@, driver.fs@, driver.nw@, driver.nc@,\n"
            "                driver.ec@, driver.wc@, driver.gp@  (all @agrimarket.test)"))

    # ------------------------------------------------------------------ helpers
    def d(self, md):
        return date(self.start.year, md[0], md[1])

    def when(self, day, after=None, lo=7, hi=17):
        t = timezone.make_aware(datetime.combine(day, time(random.randint(lo, hi), random.randint(0, 59))))
        if after is not None and t <= after:
            t = after + timedelta(minutes=random.randint(20, 120))
        return min(t, timezone.now())

    def user(self, email, first, last, role, province, phone, joined, **extra):
        with clock.as_of(self.when(joined)):
            u = User.objects.create_user(email=email, password=PASSWORD, first_name=first, last_name=last,
                                         role=role, province=province, phone=phone,
                                         popia_consent_at=clock.now(), **extra)
        User.objects.filter(pk=u.pk).update(date_joined=self.when(joined))
        return u

    def price(self, base, code, day, unit):
        m = SEASON[GROUP[code]][day.month - 1]
        return price_round(base * m * random.uniform(0.96, 1.04), unit)

    def cost(self, farmer, day, category, amount, description, planting=None, herd=None):
        if day > self.today or amount <= 0:
            return
        with clock.as_of(self.when(day)):
            CostEntry.objects.create(farmer=farmer, planting=planting, herd=herd, category=category,
                                     amount=D(amount).quantize(D("0.01")), incurred_on=day,
                                     description=description)

    # ------------------------------------------------------------------ people
    def _people(self):
        s = self.start
        self.admin = User.objects.filter(is_superuser=True).order_by("pk").first()
        if not User.objects.filter(email="admin@agrimarket.test").exists():
            a = User.objects.create_superuser(
                email="admin@agrimarket.test", password=PASSWORD, first_name="Zanele", last_name="Mokoena",
                province="GP", phone="0821000001", popia_consent_at=timezone.now())
            User.objects.filter(pk=a.pk).update(date_joined=self.when(s))
            self.admin = self.admin or a
        for i, (first, last, prov) in enumerate([("Kabelo", "Morake", "GP"), ("Annelie", "Venter", "FS")], 2):
            self.user(f"admin{i}@agrimarket.test", first, last, "admin", prov, f"082100000{i + 1}", s)
        self.drivers = {}
        for email, first, last, prov, phone in [
                ("driver.lp@agrimarket.test", "Tshepo", "Maluleke", "LP", "0821000010"),
                ("driver.kzn@agrimarket.test", "Sipho", "Dlamini", "KZN", "0821000011"),
                ("driver.mp@agrimarket.test", "Johannes", "Mahlangu", "MP", "0821000012"),
                ("driver.fs@agrimarket.test", "Lerato", "Mofokeng", "FS", "0821000013"),
                ("driver.nw@agrimarket.test", "Kabelo", "Seleka", "NW", "0821000014"),
                ("driver.nc@agrimarket.test", "Jan", "Cloete", "NC", "0821000015"),
                ("driver.ec@agrimarket.test", "Luvuyo", "Mbeki", "EC", "0821000016"),
                ("driver.wc@agrimarket.test", "Ashwin", "Petersen", "WC", "0821000017"),
                ("driver.gp@agrimarket.test", "Neo", "Molefe", "GP", "0821000018"),
                ("driver2.lp@agrimarket.test", "Rudzani", "Mudau", "LP", "0821000019")]:
            self.drivers.setdefault(prov, []).append(
                self.user(email, first, last, "distributor", prov, phone, s))
        self.buyers = []
        for email, first, last, prov, phone, btype, address, days, every, groups in BUYERS:
            u = self.user(email, first, last, "buyer", prov, phone, s + timedelta(days=random.randint(0, 5)),
                          buyer_type=btype, address=address)
            u.days, u.every, u.wants = days, every, groups
            self.buyers.append(u)

    def _farms(self):
        self.farms, self.fields = {}, {}
        for key, (email, first, last, prov, phone, name, town, vat, brand, fields) in FARMS.items():
            u = self.user(email, first, last, "farmer", prov, phone, self.start)
            FarmerProfile.objects.create(
                user=u, farm_name=name, town=town, province=prov, brand_mark=brand,
                is_vat_registered=bool(vat), vat_number=vat, status="approved",
                reviewed_at=self.when(self.start + timedelta(days=1)), reviewed_by=self.admin,
                **dict(zip(("bank_code", "bank_name"), random.choice(ZA_BANKS))),
                bank_account=f"62{random.randint(10 ** 7, 10 ** 8 - 1)}")
            self.farms[key] = u
            for fname, ha, soil, irr in fields:
                self.fields[(key, fname)] = Field.objects.create(farmer=u, name=fname, size_ha=D(str(ha)),
                                                                 soil_type=soil, irrigated=irr)
        pending = self.user("newfarmer@agrimarket.test", "Anele", "Ndlovu", "farmer", "EC", "0831000004",
                            self.today - timedelta(days=2))
        FarmerProfile.objects.create(user=pending, farm_name="Amatola Hills", town="Stutterheim", province="EC",
                                     brand_mark="AMH")

    # ------------------------------------------------------------------ advisory (people, soil, links)
    def _advisory_setup(self):
        s = self.start
        self.agro = self.user("agronomist@agrimarket.test", "Refilwe", "Sebola", "agronomist", "LP",
                              "0721000001", s + timedelta(days=2))
        AgronomistProfile.objects.create(user=self.agro, body="sacnasp", registration_no="400318/15",
                                         qualification="MSc Agric (Agronomy), University of Limpopo",
                                         company="Sebola Crop Advisory", crops="Vegetables, maize, soya",
                                         status="approved", reviewed_by=self.admin,
                                         reviewed_at=self.when(s + timedelta(days=3)))
        self.agronomists = [self.agro]
        self.agro.advises = ["bokamoso", "umgeni", "highveld"]   # the README's "three farms"
        for key in self.agro.advises:
            with clock.as_of(self.when(s + timedelta(days=random.randint(4, 10)))):
                FarmLink.objects.create(farmer=self.farms[key], agronomist=self.agro)
        more = [  # email, first, last, province, phone, body, reg, qualification, company, farms advised
            ("agronomist3@agrimarket.test", "Johan", "Kruger", "FS", "0721000003", "sacnasp", "400221/12",
             "BSc Agric (Agronomy), UFS", "Vrystaat Grain Advisers", ["rietvlei"]),
            ("agronomist4@agrimarket.test", "Thabo", "Mokoena", "MP", "0721000004", "sascp", "SASCP-1874",
             "BSc Agric (Plant Production)", "Lowveld Agri Services", ["lowveld"]),
            ("agronomist5@agrimarket.test", "Anita", "Pillay", "KZN", "0721000005", "sacnasp", "400455/18",
             "MSc Agric (Crop Science), UKZN", "Midlands Crop Care", ["umgeni"]),
            ("agronomist6@agrimarket.test", "Lindile", "Sokhela", "EC", "0721000006", "sacnasp", "400512/19",
             "BSc Agric, Fort Hare", "Amathole Agri Advice", ["amatola"]),
            ("agronomist7@agrimarket.test", "Gerrit", "Visser", "WC", "0721000007", "sacnasp", "400118/09",
             "PhD Agronomy, Stellenbosch", "Swartland Graanadvies", ["swartland"]),
            ("agronomist8@agrimarket.test", "Mashudu", "Ramabulana", "LP", "0721000008", "sascp", "SASCP-2033",
             "BSc Agric (Soil Science), Univen", "Vhembe Soil & Crop", ["vhembe"]),
            ("agronomist9@agrimarket.test", "Karabo", "Lekgetho", "NW", "0721000009", "sacnasp", "400690/21",
             "BSc Agric (Pasture Science), NWU", "Bushveld Pasture Advice", ["kgalagadi"]),
        ]
        for email, first, last, prov, phone, body, reg, qual, company, farms in more:
            u = self.user(email, first, last, "agronomist", prov, phone, s + timedelta(days=random.randint(1, 10)))
            AgronomistProfile.objects.create(user=u, body=body, registration_no=reg, qualification=qual,
                                             company=company, status="approved", reviewed_by=self.admin,
                                             reviewed_at=self.when(s + timedelta(days=12)))
            u.advises = farms
            self.agronomists.append(u)
            for key in farms:
                with clock.as_of(self.when(s + timedelta(days=random.randint(12, 25)))):
                    FarmLink.objects.create(farmer=self.farms[key], agronomist=u)
        for email, first, last, prov, phone, body, reg, qual, company in [
                ("agronomist2@agrimarket.test", "Sipho", "Mthembu", "KZN", "0721000002", "sascp", "SASCP-2291",
                 "BSc Agric (Plant Production)", "Midlands Agri"),
                ("agronomist10@agrimarket.test", "Retha", "Nel", "NC", "0721000010", "sacnasp", "400733/22",
                 "BSc Agric (Agronomy), UFS", "Gariep Crop Consulting")]:
            u = self.user(email, first, last, "agronomist", prov, phone, self.today - timedelta(days=1))
            AgronomistProfile.objects.create(user=u, body=body, registration_no=reg, qualification=qual,
                                             company=company)
        # Soil tests on every field of every advised farm
        labs = {"bokamoso": "Polokwane Soil Lab", "umgeni": "Midlands Soil Lab", "highveld": "Ermelo Agri Lab",
                "rietvlei": "Bloemfontein Soil Lab", "lowveld": "Mbombela Agri Lab", "amatola": "East London Soil Lab",
                "swartland": "Malmesbury Grondlab", "vhembe": "Thohoyandou Soil Lab", "kgalagadi": "Vryburg Soil Lab"}
        tester = {key: a for a in self.agronomists[1:] for key in a.advises}
        tester.update({k: self.agro for k in ("bokamoso", "umgeni", "highveld")})
        ph = {("bokamoso", "Garden 2"): "4.6", ("bokamoso", "Lower field"): "5.1", ("highveld", "Noord-land"): "5.4"}
        for (key, fname), field in self.fields.items():
            if key not in labs:
                continue
            day = s + timedelta(days=random.randint(8, 20) if tester[key] == self.agro else random.randint(26, 40))
            with clock.as_of(self.when(day)):
                SoilTest.objects.create(field=field, sampled_on=day, lab=labs[key],
                                        ph_kcl=D(ph.get((key, fname), random.choice(["5.6", "5.9", "6.1", "6.3"]))),
                                        p_mg_kg=D(random.randint(14, 42)), k_mg_kg=D(random.randint(110, 260)),
                                        organic_carbon=D(random.choice(["0.6", "0.8", "1.1", "1.4"])),
                                        added_by=tester[key])

    # ------------------------------------------------------------------ the season, day by day
    def _trade(self):
        self.events = {}           # day -> [callables]
        self.live = []             # listings currently on sale in the simulation
        self.plantings = []
        self._plan_crops()
        self._plan_silo()
        self._plan_eggs()
        self._plan_herds()
        week = 0
        day = self.start
        while day <= self.today:
            if day.weekday() == 0 and day != self.start:
                week += 1
            for fn in self.events.get(day, []):
                fn()
            if day.day == 1:
                self._monthly_prices(day)
            self._expire(day)
            for b in self.buyers:
                if day <= self.today - timedelta(days=3) and day.weekday() in b.days and week % b.every == (self.buyers.index(b) % b.every):
                    self._buy(b, day)
            day += timedelta(days=1)
        # sold-out listings older than two weeks are archived by the farmer
        Listing.objects.filter(quantity_available=0, harvest_date__lt=self.today - timedelta(days=14)).update(
            status=Listing.Status.ARCHIVED)

    def on(self, day, fn):
        if self.start <= day <= self.today:
            self.events.setdefault(day, []).append(fn)

    # crops ---------------------------------------------------------------
    def _plan_crops(self):
        for farm, code, fname, ha, cultivar, grades, dates, base, factors in PLANTINGS:
            for md, factor in zip(dates, factors):
                planted = self.d(md)
                if planted > self.today:
                    continue
                self._planting(farm, code, fname, D(ha), cultivar, grades, planted, base, factor)

    def _planting(self, farm, code, fname, ha, cultivar, grades, planted, base, factor):
        g = CROP_GUIDE[code]
        farmer, field = self.farms[farm], self.fields[(farm, fname)]
        unit = g["unit"]
        per_ha = g["yield_"][1 if field.irrigated else 0]
        first, shares, gap = PICKS[code]
        expected = q(per_ha * ha, unit)
        with clock.as_of(self.when(planted)):
            p = Planting.objects.create(farmer=farmer, field=field, commodity=self.C[code], cultivar=cultivar,
                                        planted_on=planted, area_ha=ha, unit=unit, expected_yield=expected,
                                        expected_harvest=planted + timedelta(days=first))
        self.plantings.append(p)
        total = per_ha * ha * D(str(factor)) * D(str(random.uniform(0.92, 1.06)))
        picks = [(planted + timedelta(days=first + i * gap), total * D(str(sh))) for i, sh in enumerate(shares)]
        last = picks[-1][0]
        status = (Planting.Status.COMPLETED if last <= self.today else
                  Planting.Status.HARVESTING if picks[0][0] <= self.today else Planting.Status.GROWING)
        Planting.objects.filter(pk=p.pk).update(status=status)
        for day, qty in picks:
            self.on(day, lambda p=p, day=day, qty=qty, grades=grades, base=base: self._harvest(p, day, qty, grades, base))
        # costs, scaled to the area planted
        cost_ha = g["cost"][1 if field.irrigated else 0] or g["cost"][1]
        total_cost = float(cost_ha * ha) * random.uniform(0.92, 1.08)
        span = (last - planted).days or 1
        name = self.C[code].name.lower()
        self.cost(farmer, planted, "seed", total_cost * 0.16, f"Seed / seedlings – {name}", planting=p)
        self.cost(farmer, planted, "fertiliser", total_cost * 0.12, "Planting fertiliser", planting=p)
        self.cost(farmer, planted + timedelta(days=min(35, span)), "fertiliser", total_cost * 0.08,
                  "Top-dressing (LAN)", planting=p)
        self.cost(farmer, planted, "machinery", total_cost * 0.04, "Land preparation", planting=p)
        for i, (sd, *_rest) in enumerate(SPRAYS.get(code, [])):
            self.cost(farmer, planted + timedelta(days=sd), "chemicals", total_cost * 0.14 / len(SPRAYS[code]),
                      f"Spray: {_rest[0]}", planting=p)
        if code not in SPRAYS:
            self.cost(farmer, planted + timedelta(days=40), "chemicals", total_cost * 0.10, "Herbicide and insecticide",
                      planting=p)
        months = max(1, span // 30)
        for m in range(months):
            day = planted + timedelta(days=30 * (m + 1) - 2)
            self.cost(farmer, min(day, last), "labour", total_cost * 0.24 / months, "Wages", planting=p)
            if field.irrigated:
                self.cost(farmer, min(day, last), "irrigation", total_cost * 0.10 / months,
                          "Electricity for pumps", planting=p)
            self.cost(farmer, min(day, last), "fuel", total_cost * 0.06 / months, "Diesel", planting=p)
        if GROUP[code] == "veg":
            self.cost(farmer, picks[0][0], "packaging", total_cost * 0.08, "Crates and bags", planting=p)
        # sprays (before the first pick, so the pre-harvest interval is always respected)
        for sd, product, reg, ai, target, rate, phi in SPRAYS.get(code, []):
            day = planted + timedelta(days=sd)
            if day <= self.today and day + timedelta(days=phi) < picks[0][0]:
                with clock.as_of(self.when(day)):
                    SprayRecord.objects.create(planting=p, remedy=self.remedy(product, reg, ai, p.commodity, phi, rate,
                                                                              target),
                                               target=target, rate=rate, applied_on=day,
                                               applied_by=f"{farmer.first_name} and team",
                                               recommended_by=self.agro if FarmLink.objects.filter(
                                                   farmer=farmer, agronomist=self.agro).exists() else None)

    def remedy(self, product, reg, ai, commodity, phi, rate, target):
        """A verified catalogue entry for the sample sprays (fictitious registration numbers)."""
        made = self.when(self.start)
        r, _ = RegisteredRemedy.objects.get_or_create(registration_no=reg, defaults={
            "trade_name": product, "active_ingredient": ai,
            "source": "Sample data for the demo - not a real label",
            "status": RegisteredRemedy.Status.VERIFIED, "verified_by": self.admin,
            "created_at": made, "verified_at": self.when(self.start, after=made)})
        RemedyUse.objects.get_or_create(remedy=r, commodity=commodity,
                                        defaults={"phi_days": phi, "max_rate": rate, "targets": target})
        return r

    def _harvest(self, p, day, qty, grades, base):
        code, unit = p.commodity.code, p.unit
        splits = [(grades[0], D("0.82")), (grades[1], D("0.18"))] if len(grades) > 1 else [(grades[0], D("1"))]
        for grade, share in splits:
            amount = q(qty * share, unit)
            if amount <= 0:
                continue
            moment = self.when(day, lo=6, hi=11)
            with clock.as_of(moment):
                h = Harvest.objects.create(planting=p, harvested_on=day, quantity=amount, grade=grade)
            if GROUP[code] == "grain":
                continue  # grain goes to the silo; not part of this demo's sales
            price = self.price(base * (0.8 if grade != grades[0] else 1), code, day, unit)
            shelf = p.commodity.shelf_life_days
            with clock.as_of(self.when(day, after=moment, lo=11, hi=16)):
                l = Listing.objects.create(
                    farmer=p.farmer, commodity=p.commodity, variety=p.cultivar, harvest=h, unit=unit, price=price,
                    quantity_available=amount, min_order_qty=D(MIN_ORDER[unit]),
                    low_stock_threshold=q(amount * D("0.1"), unit), grade=grade, harvest_date=day,
                    expiry_date=day + timedelta(days=shelf) if shelf else None, near_expiry_discount_pct=10,
                    description=f"{p.commodity.name} from {p.field.name}, picked {day:%d %b}.")
            self.live.append(l)

    # silo maize ------------------------------------------------------------
    def _plan_silo(self):
        for farm, code, grade, tons, md, base, variety in SILO:
            day = self.d(md) if md else self.start + timedelta(days=2)

            def make(farm=farm, code=code, grade=grade, tons=tons, day=day, base=base, variety=variety, md=md):
                with clock.as_of(self.when(day)):
                    l = Listing.objects.create(
                        farmer=self.farms[farm], commodity=self.C[code], variety=variety, unit="ton",
                        price=self.price(base, code, day, "ton"), quantity_available=D(tons), min_order_qty=D(5),
                        low_stock_threshold=D(10), grade=grade, harvest_date=day if md else None,
                        description="Last season's crop, stored in our silo. Moisture below 14%.")
                    if tons >= 60:
                        PriceTier.objects.create(listing=l, min_quantity=D(30), unit_price=l.price - D(100))
                l.base = base
                self.live.append(l)
            self.on(day, make)

    def _monthly_prices(self, day):
        """Farmers update grain and livestock prices at the start of each month."""
        for l in self.live:
            if l.unit in ("ton", "head") and hasattr(l, "base") and l.quantity_available > 0:
                new = self.price(l.base, l.commodity.code, day, l.unit)
                Listing.objects.filter(pk=l.pk).update(price=new)
                l.price = new
                tiers = list(l.price_tiers.all())
                for t in tiers:
                    t.unit_price = new - D(100)
                    t.save(update_fields=["unit_price"])

    # eggs -----------------------------------------------------------------
    def _plan_eggs(self):
        """A new batch of eggs every Monday; last week's leftovers are sold first, then expire."""
        day = self.start + timedelta(days=(7 - self.start.weekday()) % 7)
        while day <= self.today:
            for farm in ("umgeni", "vhembe"):
                self.on(day, lambda day=day, farm=farm: self._egg_batch(day, farm))
            day += timedelta(days=7)

    def _egg_batch(self, day, farm="umgeni"):
        with clock.as_of(self.when(day, lo=7, hi=9)):
            l = Listing.objects.create(
                farmer=self.farms[farm], commodity=self.C["EGG-L"], unit="crate", grade="Grade 1",
                price=self.price(78, "EGG-L", day, "crate"), quantity_available=D(random.randint(170, 200) if farm == "umgeni" else random.randint(80, 110)),
                min_order_qty=D(10), low_stock_threshold=D(30), harvest_date=day,
                expiry_date=day + timedelta(days=28), near_expiry_discount_pct=10,
                description="Large brown eggs, trays of 30, collected this week from our free-range flock.")
        self.live.append(l)

    # livestock --------------------------------------------------------------
    def _plan_herds(self):
        self.herds = {}
        for farm, name, code, breed, head, md, tags, base, kg, age, feed in HERDS:
            day = self.d(md)
            farmer = self.farms[farm]
            with clock.as_of(self.when(day)):
                h = Herd.objects.create(farmer=farmer, name=name, commodity=self.C[code], breed=breed,
                                        head_count=head, started_on=day, tag_range=tags)
            self.herds[name] = h
            vacc = day + timedelta(days=5)
            with clock.as_of(self.when(vacc)):
                Treatment.objects.create(herd=h, kind="vaccine", remedy="Pulpy kidney / tetanus vaccine",
                                         registration_no="G2931", reason="Annual vaccination", animals="All",
                                         dose="2 ml SC", treated_on=vacc, withdrawal_days=21,
                                         administered_by=farmer.first_name)
            dip = self.d((8, 18))
            with clock.as_of(self.when(dip)):
                Treatment.objects.create(herd=h, kind="dip", remedy="Pour-on tick control", registration_no="G3421",
                                         reason="Tick control before summer", animals="All", dose="1 ml / 10 kg",
                                         treated_on=dip, withdrawal_days=14, administered_by=farmer.first_name)
            m = day
            while m <= self.today:
                self.cost(farmer, m + timedelta(days=27), "feed", feed * random.uniform(0.9, 1.1), "Feed and licks", herd=h)
                self.cost(farmer, m + timedelta(days=27), "labour", feed * 0.5, "Herdsman wages", herd=h)
                m += timedelta(days=30)
            self.cost(farmer, vacc, "vet", head * 18, "Vaccines", herd=h)
            self.cost(farmer, dip, "vet", head * 25, "Tick control", herd=h)
            list_day = day + timedelta(days=24)

            def make(h=h, farmer=farmer, code=code, breed=breed, head=head, base=base, kg=kg, age=age,
                     tags=tags, list_day=list_day):
                with clock.as_of(self.when(list_day)):
                    l = Listing.objects.create(
                        farmer=farmer, commodity=self.C[code], variety=breed, herd=h, unit="head",
                        price=self.price(base, code, list_day, "head"), quantity_available=D(int(head * 0.85)),
                        min_order_qty=D(1), low_stock_threshold=D(3),
                        description=f"{breed}, vaccinated, branded {farmer.farm.brand_mark}. Loading ramp on farm.")
                    LivestockDetail.objects.create(listing=l, species=self.C[code].category.name, breed=breed,
                                                   sex="mixed", age_months=age, avg_weight_kg=D(kg), tag_or_brand=tags,
                                                   vaccination_status="up_to_date",
                                                   vaccination_notes="Pulpy kidney, tetanus")
                l.base = base
                self.live.append(l)
            self.on(list_day, make)
        # The weaners were injected against internal parasites 5 days ago: 35-day withdrawal, not for sale yet.
        w = self.herds["Bonsmara weaners"]
        day = self.today - timedelta(days=5)

        def deworm():
            with clock.as_of(self.when(day)):
                Treatment.objects.create(
                    herd=w, kind="deworm", remedy="Doramectin injection", registration_no="G3790", reason="Roundworm",
                    animals="All weaners still on the farm", dose="1 ml / 50 kg", treated_on=day, withdrawal_days=35,
                    administered_by="Kagiso")
        self.on(day, deworm)
        self.cost(self.farms["kgalagadi"], day, "vet", 1450, "Deworming", herd=w)

    # buying -----------------------------------------------------------------
    def _for_sale(self, day, groups):
        out = []
        for l in self.live:
            l.refresh_from_db()
            if GROUP[l.commodity.code] not in groups or l.quantity_available < l.min_order_qty:
                continue
            if l.status != Listing.Status.ACTIVE or (l.expiry_date and l.expiry_date < day):
                continue
            if Listing.objects.sellable(on=day).filter(pk=l.pk).exists():
                out.append(l)
        return out

    def _qty(self, l, buyer):
        stock = l.quantity_available
        code = l.commodity.code
        if l.unit == "head":
            want = D(random.randint(3, 8))
        elif l.unit == "ton":
            want = D(random.choice([15, 20, 25, 30, 32]))
        elif code == "EGG-L":
            want = D(random.choice([30, 40, 60, 80]))
        elif code in ("POTATO", "ONION"):
            want = q(stock * D(str(random.uniform(0.18, 0.35))), "kg")
            want = max(want, D(500))
        else:
            want = q(stock * D(str(random.uniform(0.45, 0.8))), l.unit)
        want = min(want, stock)
        return want if want >= l.min_order_qty else None

    def _buy(self, buyer, day):
        options = self._for_sale(day, buyer.wants)
        if not options:
            return
        random.shuffle(options)
        n = 1 if buyer.wants in (["grain"], ["live"]) else random.randint(2, 4)
        lines = []
        for l in options[:n]:
            qty = self._qty(l, buyer)
            if qty:
                lines.append({"listing": l, "quantity": qty})
        if not lines:
            return
        delivery = random.random() < 0.75
        pay = "card"
        moment = self.when(day, lo=7, hi=12)
        try:
            with clock.as_of(moment):
                order = osvc._create_order(buyer, lines, "delivery" if delivery else "collection", pay,
                                           buyer.address if delivery else "", buyer.province if delivery else "",
                                           CARD, order_date=day)
        except osvc.CheckoutError:
            return
        self._progress(order, day)

    def _progress(self, order, day):
        """Payment, packing, driver, delivery and confirmation on the days after the order."""
        for sub in order.suborders.select_related("farmer__farm"):
            sub.refresh_from_db()
            if sub.status != S.APPROVED:
                continue
            start = timezone.localtime(osvc.last_event(sub)).date()
            # A farm occasionally cannot supply (frost, hail) and cancels before packing.
            if sub.farmer == self.farms["bokamoso"] and start.month == 7 and not getattr(self, "frost_cancel", False):
                self.frost_cancel = True
                with clock.as_of(self.when(start, after=osvc.last_event(sub))):
                    osvc.change_status(sub, S.CANCELLED, sub.farmer,
                                       note="Frost damaged the crop overnight; we cannot supply this order.")
                self._refund(sub, start + timedelta(days=2))
                continue
            pack = start + timedelta(days=random.choice([0, 1, 1]))
            if pack > self.today:
                continue
            with clock.as_of(self.when(pack, after=osvc.last_event(sub), lo=8, hi=15)):
                osvc.change_status(sub, S.PACKED, sub.farmer)
            if sub.is_delivery:
                go = pack + timedelta(days=1)
                if go > self.today:
                    continue
                scheduled = False
                for driver in self.drivers[sub.farmer.farm.province]:
                    for slot in ("08:00-12:00", "12:00-16:00", "16:00-18:00"):
                        try:
                            with clock.as_of(self.when(pack, after=osvc.last_event(sub), lo=12, hi=17)):
                                osvc.schedule_delivery(sub, go, slot, sub.farmer, distributor=driver)
                            scheduled = True
                            break
                        except osvc.TransitionError:
                            continue
                    if scheduled:
                        break
                if not scheduled:
                    continue
                sub.refresh_from_db()
                with clock.as_of(self.when(go, after=osvc.last_event(sub), lo=6, hi=9)):
                    osvc.change_status(sub, S.IN_TRANSIT, sub.distributor)
                if go == self.today and random.random() < 0.5:
                    continue  # still on the road
                with clock.as_of(self.when(go, after=osvc.last_event(sub), lo=11, hi=16)):
                    osvc.change_status(sub, S.DELIVERED, sub.distributor,
                                       note="Received by " + random.choice(RECEIVERS[order.buyer.buyer_type]))
                done = go
            else:
                done = pack + timedelta(days=random.choice([1, 2]))
                if done > self.today:
                    continue
                with clock.as_of(self.when(done, after=osvc.last_event(sub), lo=8, hi=16)):
                    osvc.change_status(sub, S.DELIVERED, sub.farmer,
                                       note="Collected by " + random.choice(RECEIVERS[order.buyer.buyer_type]))
            confirm = done + timedelta(days=1)
            sub.refresh_from_db()
            if confirm <= self.today and random.random() < 0.55:
                with clock.as_of(self.when(confirm, after=osvc.last_event(sub))):
                    osvc.change_status(sub, S.COMPLETED, order.buyer, note="Goods received in good order")
                self._review(sub, confirm)

    def _refund(self, sub, day):
        """Refunds go back to the buyer's card straight away; date them on the day they happened."""
        refund = Refund.objects.filter(suborder=sub, status=Refund.Status.PENDING).first()
        if refund:
            with clock.as_of(self.when(min(day, self.today), after=osvc.last_event(sub))):
                osvc.send_refund(refund.pk)

    def _review(self, sub, day):
        if random.random() > 0.4:
            return
        comments = {5: ["Excellent quality, will order again.", "Fresh and well packed.", "Exactly as described."],
                    4: ["Good quality, delivery a bit late.", "Good stock, a few damaged crates.", "Happy with this."],
                    3: ["Average — some uneven sizes.", "Okay, but packed loosely."]}
        rating = random.choices([5, 4, 3], weights=[55, 35, 10])[0]
        with clock.as_of(self.when(day, after=sub.completed_at)):
            for it in sub.items.all():
                Review.objects.get_or_create(suborder=sub, listing=it.listing, defaults={
                    "buyer": sub.order.buyer, "rating": rating, "comment": random.choice(comments[rating])})

    def _expire(self, day):
        """Perishable stock not sold by its expiry date is written off the next day."""
        for l in self.live:
            if l.expiry_date and l.expiry_date == day - timedelta(days=1):
                l.refresh_from_db()
                if l.quantity_available > 0:
                    with clock.as_of(self.when(day, lo=7, hi=9)):
                        adjust_stock(l, -l.quantity_available, StockAdjustment.Reason.EXPIRED, l.farmer,
                                     "Not sold before the expiry date")
                Listing.objects.filter(pk=l.pk).update(status=Listing.Status.ARCHIVED)
                l.status = Listing.Status.ARCHIVED

    # ------------------------------------------------------------------ advisory through the season
    def _advisory_season(self):
        a = self.agro
        visits = [
            ("bokamoso", "Garden 2", 20, "nutrient", "Low soil pH (4.6) — poor early growth", "medium",
             "Leaves pale and uneven on the low side of the garden. Soil test shows pH 4.6.",
             ("fertiliser", "Apply 2 t/ha dolomitic lime on Garden 2 and work it in before the next planting.", 30)),
            ("bokamoso", "Garden 1", 48, "pest", "Tomato leafminer (Tuta absoluta)", "medium",
             "Mines on 10% of leaves in the lower canopy. Pheromone traps catching 15 moths a week.",
             ("spray", "Spray chlorantraniliprole 150 ml/ha now and again in 14 days. Keep the 3-day pre-harvest "
                       "interval.", 3)),
            ("umgeni", "Tunnel A", 40, "disease", "Early blight", "low",
             "Target spots on older leaves. Humidity high in the tunnel in the mornings.",
             ("spray", "Mancozeb 2 kg/ha every 10 days while it stays humid; open the tunnel sides earlier.", 5)),
            ("highveld", "Pivot 2", 33, "pest", "Diamondback moth", "low",
             "Small caterpillars on outer leaves, below the threshold.",
             ("spray", "Scout weekly. Spray only if more than 1 caterpillar per plant.", 10)),
            ("umgeni", "Hill block", 70, "none", "", "", "Good stand, tubers bulking well. No blight seen.", None),
            ("bokamoso", "Garden 2", 122, "weather", "Frost damage", "high",
             "Frost on the night of 30 June burnt the outer leaves of 45% of the cabbages.",
             ("other", "Harvest the undamaged heads first; sell damaged heads as Class 2 within a week.", 4)),
            ("highveld", "Pivot 1", 160, "none", "", "", "Second potato planting emerged evenly.", None),
            ("umgeni", "River block", 200, "none", "", "",
             "Cabbage clean. River block will be free in December.",
             ("planting", "Plant carrots on River block in December: buyers are searching for them and nobody is "
                          "selling. Rotate away from cabbage.", 60)),
            ("highveld", "Noord-land", 225, "none", "", "", "Maize emerging well; planter set at 60 000 seeds/ha.",
             ("fertiliser", "Top-dress 150 kg/ha LAN at V6 (about 4 weeks from now).", 28)),
            ("bokamoso", "Garden 1", 228, "pest", "Tomato leafminer (Tuta absoluta)", "high",
             "Leafminer pressure high again on the second tomato planting; trap counts 60 per week.",
             ("spray", "Spray chlorantraniliprole 150 ml/ha today. Do not harvest for 7 days.", 1)),
        ]
        for farm, fname, offset, problem, detail, sev, obs, rec in visits:
            day = self.start + timedelta(days=offset)
            if day > self.today:
                continue
            farmer, field = self.farms[farm], self.fields[(farm, fname)]
            planting = (Planting.objects.filter(field=field, planted_on__lte=day).order_by("-planted_on").first())
            moment = self.when(day, lo=8, hi=13)
            with clock.as_of(moment):
                v = FieldVisit.objects.create(farmer=farmer, agronomist=a, field=field, planting=planting,
                                              visited_on=day, problem=problem, problem_detail=detail,
                                              severity=sev, observations=obs,
                                              growth_stage=self._stage(planting, day))
                from core.services import notify
                notify(farmer, f"Field visit by {a.get_full_name()}", f"{field.name}: {v.get_problem_display()}",
                       link="/advice/")
            if rec:
                kind, advice, days = rec
                with clock.as_of(self.when(day, after=moment)):
                    r = Recommendation.objects.create(farmer=farmer, agronomist=a, field=field, planting=planting,
                                                      visit=v, kind=kind, advice=advice,
                                                      do_by=day + timedelta(days=days))
                done = day + timedelta(days=max(1, days - 1))
                if done <= self.today - timedelta(days=3) and not (farm == "bokamoso" and kind == "fertiliser"):
                    Recommendation.objects.filter(pk=r.pk).update(status="done", done_on=done,
                                                                  farmer_note=random.choice(["Done", "Done as advised",
                                                                                             ""]))
        # The other agronomists visit the farms they advise every five to six weeks.
        findings = [("none", "", "", "Crop looks even and healthy.", None),
                    ("pest", "Aphids", "low", "A few aphid colonies on outer leaves, natural enemies present.",
                     ("spray", "Scout again in a week; spray only if colonies spread.", 7)),
                    ("nutrient", "Nitrogen shortage", "medium", "Older leaves yellowing from the tips.",
                     ("fertiliser", "Top-dress 100 kg/ha LAN and irrigate it in.", 5)),
                    ("weed", "Nutgrass", "medium", "Nutgrass coming through between the rows.",
                     ("spray", "Hand-weed the rows now; plan a pre-emergence herbicide next season.", 10)),
                    ("disease", "Downy mildew", "low", "Spots on a few plants after the cold nights.",
                     ("spray", "Remove affected leaves and apply copper oxychloride 2.5 kg/ha.", 4))]
        for agro in self.agronomists[1:]:
            for key in agro.advises:
                fields = [f for (k, _n), f in self.fields.items() if k == key]
                day = self.start + timedelta(days=random.randint(30, 45))
                while day <= self.today - timedelta(days=2) and fields:
                    field = random.choice(fields)
                    planting = Planting.objects.filter(field=field, planted_on__lte=day).order_by("-planted_on").first()
                    problem, detail, sev, obs, rec = random.choice(findings)
                    with clock.as_of(self.when(day, lo=8, hi=14)):
                        v = FieldVisit.objects.create(farmer=self.farms[key], agronomist=agro, field=field,
                                                      planting=planting, visited_on=day, problem=problem,
                                                      problem_detail=detail, severity=sev, observations=obs,
                                                      growth_stage=self._stage(planting, day))
                        if rec:
                            r = Recommendation.objects.create(farmer=self.farms[key], agronomist=agro, field=field,
                                                              planting=planting, visit=v, kind=rec[0], advice=rec[1],
                                                              do_by=day + timedelta(days=rec[2]))
                            if day + timedelta(days=rec[2]) < self.today - timedelta(days=2) and random.random() < 0.8:
                                Recommendation.objects.filter(pk=r.pk).update(
                                    status="done", done_on=day + timedelta(days=rec[2] - 1), farmer_note="Done")
                    day += timedelta(days=random.randint(35, 45))
        # The latest leafminer spray on Bokamoso's tomatoes was applied right after the last pick and is
        # still inside its 7-day pre-harvest interval: the next pick has to wait.
        tom = (Planting.objects.filter(farmer=self.farms["bokamoso"], commodity=self.C["TOMATO"])
               .order_by("-planted_on").first())
        last = tom.harvests.order_by("-harvested_on").first() if tom else None
        if last and last.harvested_on > self.today - timedelta(days=7):
            day = last.harvested_on
            with clock.as_of(self.when(day, after=last.created_at)):
                # label PHI on tomatoes is 7 days, so this crop is blocked from harvest for a week
                SprayRecord.objects.create(planting=tom, remedy=self.remedy(
                                               "Mancozeb 800 WP", "L5021", "mancozeb",
                                               tom.commodity, 7, "2 kg/ha", "Early blight"),
                                           target="Early blight", rate="2 kg/ha", applied_on=day,
                                           applied_by="Lebogang and team", recommended_by=a)
        for title, code, prov, sev, msg, offset, valid in [
            ("Tomato leafminer pressure is high", "TOMATO", "LP", "warning",
             "Trap counts above 50 moths a week around Polokwane. Check the lower leaves twice a week and rotate "
             "insecticide groups.", 9, 30),
            ("Fall armyworm in young maize", "MAIZE-W", "MP", "warning",
             "Moths caught near Ermelo. Scout newly emerged maize for window-pane damage on the leaves.", 4, 40),
            ("Brown locust hoppers reported", "WHEAT-B", "WC", "watch",
             "Small hopper bands seen near Malmesbury. Report sightings; check field edges.", 6, 21),
            ("Late blight weather", "POTATO", "KZN", "watch",
             "Cool, wet nights forecast for the Midlands. Keep preventive copper sprays up to date.", 120, 10),
        ]:
            day = self.today - timedelta(days=offset)
            with clock.as_of(self.when(day)):
                alert = PestAlert.objects.create(agronomist=a, commodity=self.C[code], province=prov, severity=sev,
                                                 title=title, message=msg, valid_until=day + timedelta(days=valid))
                send_alert(alert)

    @staticmethod
    def _stage(p, day):
        if not p:
            return ""
        days = (day - p.planted_on).days
        if days < 21:
            return "Seedling"
        if p.harvests.filter(harvested_on__lte=day).exists():
            return "Harvesting"
        return "Vegetative" if days < 50 else "Flowering / heading"

    # ------------------------------------------------------------------ open items for today
    def _after_trade(self):
        t = self.today
        # A harvest recorded but not listed yet.
        p = (Planting.objects.filter(farmer=self.farms["highveld"], commodity=self.C["CABBAGE"],
                                     status__in=["harvesting", "growing"]).order_by("-planted_on").first())
        if p and p.planted_on < t - timedelta(days=30):
            with clock.as_of(self.when(t - timedelta(days=1))):
                Harvest.objects.create(planting=p, harvested_on=t - timedelta(days=1), quantity=D(260),
                                       grade="Class 1", notes="Early heads from the edge rows")
        # A dispute: bruised tomatoes, resolved with a partial refund.
        sub = (SubOrder.objects.filter(status=S.DELIVERED, items__product_code="TOMATO",
                                       delivered_at__lt=timezone.now() - timedelta(days=3))
               .order_by("order__order_date").first())
        if sub is not None:
            day = timezone.localtime(sub.delivered_at).date() + timedelta(days=1)
            with clock.as_of(self.when(day, after=sub.delivered_at)):
                dispute = osvc.raise_dispute(sub, sub.order.buyer, "6 crates arrived bruised and soft at the bottom "
                                                                   "of the stack. Photos taken on arrival.")
            with clock.as_of(self.when(day + timedelta(days=1), after=dispute.created_at)):
                osvc.resolve_dispute(dispute, sub.farmer, Dispute.Status.PARTIAL_REFUND,
                                     "You are right, the crates were stacked too high. We refund 6 crates.",
                                     refund_amount=min(sub.goods_subtotal + sub.vat_amount - D("1"),
                                                       D("6") * sub.items.first().unit_price))
            self._refund(sub, day + timedelta(days=1))
        # Favourite farms
        names = {b.email: b for b in self.buyers}
        for email, farm in [("buyer@agrimarket.test", "umgeni"), ("buyer@agrimarket.test", "bokamoso"),
                            ("market@agrimarket.test", "bokamoso"), ("mill@agrimarket.test", "rietvlei"),
                            ("butchery@agrimarket.test", "kgalagadi"), ("restaurant@agrimarket.test", "highveld")]:
            with clock.as_of(self.when(self.start + timedelta(days=random.randint(10, 60)))):
                FavouriteFarm.objects.create(buyer=names[email], farmer=self.farms[farm])

    def _open_orders(self):
        """Recent orders at every stage, so each role has something to do today."""
        t = self.today
        b = {x.email.split("@")[0]: x for x in self.buyers}

        def pick(code, farm=None, day=t):
            qs = (Listing.objects.sellable(on=day).filter(commodity=self.C[code])
                  .filter(Q(harvest_date__isnull=True) | Q(harvest_date__lte=day)))
            if farm:
                qs = qs.filter(farmer=self.farms[farm])
            return qs.order_by("-quantity_available").first()

        def order(buyer, items, method, pay, day, at=None):
            lines = [{"listing": l, "quantity": min(D(qty), l.quantity_available)} for l, qty in items
                     if l is not None and l.quantity_available >= l.min_order_qty]
            if not lines:
                return None
            with clock.as_of(at or self.when(day, lo=7, hi=10)):
                return osvc._create_order(buyer, lines, method, pay, buyer.address if method == "delivery" else "",
                                          buyer.province if method == "delivery" else "", CARD, order_date=day)

        # Mill: cash-on-delivery order two days ago, confirmed and waiting to be packed.
        order(b["mill"], [(pick("MAIZE-W", "rietvlei", t - timedelta(days=2)), 20)], "delivery", "card",
              t - timedelta(days=2))
        # Restaurant: cash-on-delivery order yesterday, waiting to be packed.
        order(b["restaurant"], [(pick("EGG-L", day=t - timedelta(days=1)), 40),
                                (pick("SPINACH", day=t - timedelta(days=1)), 60)], "delivery", "card",
              t - timedelta(days=1))
        # Market agent: card order yesterday, packed, driver on the road today.
        o = order(b["market"], [(pick("CABBAGE", "bokamoso", t - timedelta(days=1))
                                 or pick("CABBAGE", day=t - timedelta(days=1)), 40)], "delivery", "card",
                  t - timedelta(days=1))
        if o:
            sub = o.suborders.first()
            with clock.as_of(self.when(t - timedelta(days=1), after=osvc.last_event(sub), lo=12, hi=15)):
                osvc.change_status(sub, S.PACKED, sub.farmer)
                for slot in ("08:00-12:00", "12:00-16:00", "16:00-18:00"):
                    try:
                        osvc.schedule_delivery(sub, t, slot, sub.farmer,
                                               distributor=self.drivers[sub.farmer.farm.province][0])
                        break
                    except osvc.TransitionError:
                        continue
            sub.refresh_from_db()
            if sub.distributor:
                with clock.as_of(self.when(t, after=osvc.last_event(sub), lo=6, hi=7)):
                    osvc.change_status(sub, S.IN_TRANSIT, sub.distributor)
        # Supermarket: card order this morning from two farms — both farms must pack it.
        now = timezone.localtime()
        at = now if now.hour >= 7 else None
        order(b["buyer"], [(pick("TOMATO"), 30), (pick("ONION"), 500), (pick("EGG-L"), 60)], "delivery", "card",
              t, at=at)
        # Butchery: paid by card, goats to collect tomorrow.
        o = order(b["butchery"], [(pick("GOAT", "bokamoso") or pick("GOAT"), 4)], "collection", "card", t)

    def _buyer_signals(self):
        """Things buyers looked for and could not find — unmet demand for insights and the planner."""
        b = {x.email.split("@")[0]: x for x in self.buyers}
        for code, n, who in [("ORANGE", 5, ["buyer", "restaurant", "spar"]), ("CARROT", 6, ["buyer", "market", "durban"]),
                             ("SOYA", 2, ["mill"])]:
            for _ in range(n):
                with clock.as_of(self.when(self.today - timedelta(days=random.randint(1, 25)))):
                    SearchLog.objects.create(query=self.C[code].name.lower(), commodity=self.C[code], results=0,
                                             user=b[random.choice(who)])
        for code, who in [("ORANGE", "buyer"), ("CARROT", "restaurant"), ("CARROT", "catering")]:
            with clock.as_of(self.when(self.today - timedelta(days=random.randint(5, 15)))):
                StockAlert.objects.create(buyer=b[who], commodity=self.C[code])
        with clock.as_of(self.when(self.today - timedelta(days=4))):
            rfq = QuoteRequest.objects.create(buyer=b["market"], commodity=self.C["BUTTERNUT"], grade="Class 1",
                                              quantity=2000, unit="kg", needed_by=self.today + timedelta(days=30),
                                              delivery_province="LP", notes="Weekly supply for the market floor.")
            osvc.open_rfq(rfq)
        with clock.as_of(self.when(self.today - timedelta(days=2))):
            rfq = QuoteRequest.objects.create(buyer=b["mill"], commodity=self.C["MAIZE-Y"], grade="YM1", quantity=30,
                                              unit="ton", needed_by=self.today + timedelta(days=21),
                                              delivery_province="FS",
                                              notes="Bulk delivery to our silo in Bloemfontein; weighbridge on site.")
            osvc.open_rfq(rfq)
            ym = Listing.objects.filter(farmer=self.farms["rietvlei"], commodity=self.C["MAIZE-Y"],
                                        quantity_available__gte=30).first()
            if ym:
                osvc.submit_quote(rfq, self.farms["rietvlei"], ym, ym.price - D(60), "Can deliver within 7 days.")
