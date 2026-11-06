"""Agronomist role, spray pre-harvest intervals, the planting planner and the sales graphs."""
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from advisory.models import AgronomistProfile, FarmLink, PestAlert, SoilTest, SprayRecord
from advisory.services import send_alert
from farm.models import CostEntry, Field, Harvest, Planting
from insights.planner import plan_field
from insights.trends import farmer_trends
from marketplace.models import Category, Commodity
from marketplace.standards import sync_standards
from orders import services

from . import factories as f

TODAY = timezone.localdate()
START = date(TODAY.year, 3, 1) if date(TODAY.year, 3, 1) <= TODAY else date(TODAY.year - 1, 3, 1)
CARD = "4111111111111111"


def agronomist(status="approved", province="FS"):
    u = f.user("agronomist", province=province)
    AgronomistProfile.objects.create(user=u, body="sacnasp", registration_no=f"4000{u.pk}/15",
                                     qualification="BSc Agric", status=status)
    return u


class AgronomistAccessTests(TestCase):
    def setUp(self):
        sync_standards(Category, Commodity)
        self.farm = f.farmer()
        self.other_farm = f.farmer()
        self.agro = agronomist()
        self.field = Field.objects.create(farmer=self.farm, name="Land 1", size_ha=10)

    def test_registration_is_checked_before_advising(self):
        r = self.client.post(reverse("advisory:register"), {
            "first_name": "Refilwe", "last_name": "Sebola", "email": "r@agro.za", "phone": "0721234567",
            "province": "LP", "body": "sacnasp", "registration_no": "400318/15", "qualification": "MSc",
            "password1": "Farm2026!x", "password2": "Farm2026!x", "popia_consent": "on"})
        self.assertEqual(r.status_code, 302)
        u = User.objects.get(email="r@agro.za")
        self.assertEqual(u.role, "agronomist")
        self.assertFalse(u.is_verified_agronomist)
        # not verified: cannot open a farm even if linked
        FarmLink.objects.create(farmer=self.farm, agronomist=u)
        self.assertEqual(self.client.get(reverse("advisory:farm", args=[self.farm.pk])).status_code, 403)
        admin = f.user("admin")
        self.client.force_login(admin)
        self.client.post(reverse("advisory:agronomist_review", args=[u.agronomist.pk]), {"decision": "approve"})
        u.refresh_from_db()
        self.assertTrue(u.is_verified_agronomist)

    def test_agronomist_sees_only_linked_farms_and_no_sales(self):
        self.client.force_login(self.agro)
        self.assertEqual(self.client.get(reverse("advisory:farm", args=[self.farm.pk])).status_code, 403)
        FarmLink.objects.create(farmer=self.farm, agronomist=self.agro)
        page = self.client.get(reverse("advisory:farm", args=[self.farm.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Land 1")
        self.assertEqual(self.client.get(reverse("advisory:farm", args=[self.other_farm.pk])).status_code, 403)
        for name in ("orders:register", "orders:farmer_orders", "finance:reports", "orders:dispute_list"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)

    def test_farmer_links_and_removes_agronomist(self):
        self.client.force_login(self.farm)
        self.client.post(reverse("advisory:link_add"), {"agronomist": self.agro.pk})
        link = FarmLink.objects.get(farmer=self.farm, agronomist=self.agro, active=True)
        self.assertTrue(self.agro.notifications.filter(title__contains="New farm").exists())
        self.client.post(reverse("advisory:link_end", args=[link.pk]))
        link.refresh_from_db()
        self.assertFalse(link.active)
        self.client.force_login(self.agro)
        self.assertEqual(self.client.get(reverse("advisory:farm", args=[self.farm.pk])).status_code, 403)

    def test_visit_with_recommendation_reaches_farmer(self):
        FarmLink.objects.create(farmer=self.farm, agronomist=self.agro)
        self.client.force_login(self.agro)
        day = TODAY - timedelta(days=1)
        self.client.post(reverse("advisory:visit_add", args=[self.farm.pk]), {
            "field": self.field.pk, "visited_on": day, "problem": "pest", "problem_detail": "Fall armyworm",
            "severity": "high", "observations": "Window-pane damage on 20% of plants",
            "rec_kind": "spray", "rec_advice": "Spray emamectin 250 ml/ha", "rec_do_by": TODAY})
        visit = self.field.visits.get()
        self.assertEqual(timezone.localtime(visit.created_at).date(), day)  # dated on the visit day
        rec = self.farm.recommendations.get()
        self.assertEqual(rec.visit, visit)
        self.client.force_login(self.farm)
        self.client.post(reverse("advisory:rec_respond", args=[rec.pk]), {"status": "done", "note": "Sprayed"})
        rec.refresh_from_db()
        self.assertEqual(rec.status, "done")
        self.assertTrue(self.agro.notifications.filter(title__contains="advice done").exists())

    def test_pest_alert_reaches_growers_in_province_only(self):
        maize = Commodity.objects.get(code="MAIZE-W")
        Planting.objects.create(farmer=self.farm, field=self.field, commodity=maize, planted_on=TODAY,
                                area_ha=5, unit="ton", expected_yield=25, expected_harvest=TODAY + timedelta(days=150))
        kzn = f.farmer(province="KZN")
        kzn.farm.province = "KZN"
        kzn.farm.save()
        alert = PestAlert.objects.create(agronomist=self.agro, commodity=maize, province="FS", title="Fall armyworm",
                                         message="Scout", valid_until=TODAY + timedelta(days=10))
        self.assertEqual(send_alert(alert), 1)
        self.assertTrue(self.farm.notifications.filter(title__contains="Fall armyworm").exists())
        self.assertFalse(kzn.notifications.exists())


class SprayIntervalTests(TestCase):
    def setUp(self):
        sync_standards(Category, Commodity)
        self.farm = f.farmer()
        field = Field.objects.create(farmer=self.farm, name="Tunnel", size_ha=1, irrigated=True)
        self.p = Planting.objects.create(farmer=self.farm, field=field, commodity=Commodity.objects.get(code="TOMATO"),
                                         planted_on=START, area_ha=1, unit="crate", expected_yield=4000,
                                         expected_harvest=START + timedelta(days=100))

    def test_harvest_blocked_during_pre_harvest_interval(self):
        sprayed = TODAY - timedelta(days=2)
        s = f.spray(self.p, sprayed, phi_days=7)
        self.assertEqual(s.safe_from, sprayed + timedelta(days=7))
        h = Harvest(planting=self.p, harvested_on=TODAY, quantity=100, grade="Class 1")
        with self.assertRaises(ValidationError) as e:
            h.full_clean()
        self.assertIn("pre-harvest interval", str(e.exception))
        # before the spray, harvesting was fine
        Harvest(planting=self.p, harvested_on=sprayed - timedelta(days=1), quantity=100, grade="Class 1").full_clean()

    def test_registration_number_format(self):
        from advisory.models import RegisteredRemedy
        r = RegisteredRemedy(registration_no="12345", trade_name="X", active_ingredient="x", source="label")
        with self.assertRaises(ValidationError):
            r.full_clean()

    def test_crop_page_warns_not_to_harvest(self):
        f.spray(self.p, TODAY, phi_days=7)
        self.client.force_login(self.farm)
        page = self.client.get(reverse("farm:planting", args=[self.p.pk]))
        self.assertContains(page, "Do not harvest yet")


class PlannerTests(TestCase):
    def setUp(self):
        sync_standards(Category, Commodity)
        self.farm = f.farmer()
        self.field = Field.objects.create(farmer=self.farm, name="Garden", size_ha=Decimal("0.5"), irrigated=True)
        self.dry = Field.objects.create(farmer=self.farm, name="Veld", size_ha=10, irrigated=False)

    def test_dryland_field_does_not_suggest_vegetables(self):
        plan = plan_field(self.dry, TODAY)
        suitable = [o["commodity"].code for o in plan["options"] if o["suitable"]]
        self.assertNotIn("TOMATO", suitable)
        self.assertIn("MAIZE-W", suitable)

    def test_own_records_beat_the_guide(self):
        tom = Commodity.objects.get(code="TOMATO")
        p = Planting.objects.create(farmer=self.farm, field=self.field, commodity=tom, planted_on=START,
                                    area_ha=Decimal("0.5"), unit="crate", expected_yield=2000,
                                    expected_harvest=START + timedelta(days=100), status="completed")
        Harvest.objects.create(planting=p, harvested_on=START + timedelta(days=100), quantity=1500, grade="Class 1")
        CostEntry.objects.create(farmer=self.farm, planting=p, category="seed", amount=50000,
                                 incurred_on=START)
        opt = next(o for o in plan_field(self.field, TODAY)["options"] if o["commodity"].code == "TOMATO")
        self.assertEqual(opt["yield_ha"], Decimal("3000"))       # 1500 crates on 0.5 ha
        self.assertEqual(opt["y_src"], "your own past harvests")
        self.assertEqual(opt["cost_ha"], Decimal("100000.00"))
        # same family as the last crop -> rotation warning for potatoes
        pot = next(o for o in plan_field(self.field, TODAY)["options"] if o["commodity"].code == "POTATO")
        self.assertTrue(any("Rotation" in n for n in pot["notes"]))

    def test_low_soil_ph_is_flagged(self):
        SoilTest.objects.create(field=self.field, sampled_on=TODAY, lab="Lab", ph_kcl=Decimal("4.4"),
                                p_mg_kg=20, k_mg_kg=150, added_by=self.farm)
        opt = next(o for o in plan_field(self.field, TODAY)["options"] if o["commodity"].code == "SPINACH")
        self.assertTrue(any("lime first" in n for n in opt["notes"]))

    def test_planner_page_and_prefilled_planting(self):
        self.client.force_login(self.farm)
        page = self.client.get(reverse("insights:planner"))
        self.assertContains(page, "What should I plant next?")
        self.assertContains(page, "Plan this crop")
        tom = Commodity.objects.get(code="TOMATO")
        r = self.client.get(reverse("farm:plantings") + f"?field={self.field.pk}&commodity={tom.pk}&unit=crate")
        self.assertContains(r, "Filled in from the planting planner")


class TrendTests(TestCase):
    def test_monthly_lines_and_running_profit(self):
        farm, buyer = f.farmer(), f.user("buyer")
        l = f.listing(farm, qty="50", price="1000", harvest_date=START)
        f.cart_with(buyer, (l, 3))
        services.place_order(buyer, "collection", "card", card_number=CARD, order_date=START + timedelta(days=3))
        field = Field.objects.create(farmer=farm, name="A", size_ha=1)
        sync_standards(Category, Commodity)
        p = Planting.objects.create(farmer=farm, field=field, commodity=Commodity.objects.get(code="MAIZE-W"),
                                    planted_on=START, area_ha=1, unit="ton", expected_yield=5,
                                    expected_harvest=START + timedelta(days=150))
        CostEntry.objects.create(farmer=farm, planting=p, category="seed", amount=1000, incurred_on=START)
        t = farmer_trends(farm)
        self.assertEqual(t["sales"][0], 3000.0)
        self.assertEqual(t["costs"][0], 1000.0)
        self.assertEqual(t["running"][-1], 2000.0)
        self.assertEqual(t["orders"][0], 1)
        self.client.force_login(farm)
        page = self.client.get(reverse("insights:farm"))
        self.assertContains(page, 'id="inout"')
        self.assertContains(page, "Profit so far this season")


class RecordDatesTests(TestCase):
    def test_farm_record_is_dated_on_its_own_day(self):
        sync_standards(Category, Commodity)
        farm = f.farmer()
        field = Field.objects.create(farmer=farm, name="A", size_ha=2)
        self.client.force_login(farm)
        planted = START + timedelta(days=5)
        self.client.post(reverse("farm:plantings"), {
            "field": field.pk, "commodity": Commodity.objects.get(code="CABBAGE").pk, "planted_on": planted,
            "area_ha": "1", "expected_harvest": planted + timedelta(days=100), "unit": "crate",
            "expected_yield": "3000"})
        p = Planting.objects.get()
        self.assertEqual(timezone.localtime(p.created_at).date(), planted)
