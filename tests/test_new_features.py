"""Tests for standards, farm records, buyer tools and market price rules."""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Notification
from farm.economics import planting_economics
from farm.models import CostEntry, Field, Harvest, Herd, Planting, Treatment
from insights import services as insights
from marketplace.models import Category, Commodity, FavouriteFarm, Listing, StockAlert
from marketplace.standards import sync_standards
from orders import services
from orders.models import Order, Quote, QuoteRequest, SubOrder

from . import factories as f

CARD = "4111111111111111"
TODAY = timezone.localdate()


class StandardsTests(TestCase):
    def setUp(self):
        sync_standards(Category, Commodity)
        self.maize = Commodity.objects.get(code="MAIZE-W")
        self.farmer = f.farmer()

    def test_official_maize_grades_loaded(self):
        self.assertEqual(self.maize.grade_list, ["WM1", "WM2", "WM3"])
        self.assertEqual(Commodity.objects.get(code="TOMATO").grade_list[:3], ["Class 1", "Class 2", "Class 3"])

    def test_listing_rejects_grade_not_in_standard(self):
        l = Listing(farmer=self.farmer, commodity=self.maize, unit="ton", price=4000, quantity_available=10,
                    grade="Premium")
        with self.assertRaises(ValidationError) as e:
            l.full_clean()
        self.assertIn("grade", e.exception.message_dict)

    def test_listing_rejects_unit_not_allowed(self):
        l = Listing(farmer=self.farmer, commodity=self.maize, unit="crate", price=4000, quantity_available=10,
                    grade="WM1")
        with self.assertRaises(ValidationError) as e:
            l.full_clean()
        self.assertIn("unit", e.exception.message_dict)

    def test_name_and_category_come_from_standard(self):
        l = Listing.objects.create(farmer=self.farmer, commodity=self.maize, unit="ton", price=4000,
                                   quantity_available=10, grade="WM1", variety="PAN 5R-591")
        self.assertEqual(l.name, "White maize (PAN 5R-591)")
        self.assertEqual(l.category.name, "Grains & Oilseeds")


class FarmRecordTests(TestCase):
    def setUp(self):
        sync_standards(Category, Commodity)
        self.farmer = f.farmer()
        self.field = Field.objects.create(farmer=self.farmer, name="Land 3", size_ha=40)
        self.planting = Planting.objects.create(
            farmer=self.farmer, field=self.field, commodity=Commodity.objects.get(code="MAIZE-W"),
            planted_on=TODAY - timedelta(days=180), area_ha=40, expected_harvest=TODAY - timedelta(days=5),
            unit="ton", expected_yield=200)

    def test_planting_area_cannot_exceed_field(self):
        p = Planting(farmer=self.farmer, field=self.field, commodity=self.planting.commodity,
                     planted_on=TODAY, area_ha=55, expected_harvest=TODAY + timedelta(days=150),
                     unit="ton", expected_yield=100)
        with self.assertRaises(ValidationError):
            p.full_clean(exclude=["season"])

    def test_season_label(self):
        self.assertTrue(self.planting.season.startswith(str((TODAY - timedelta(days=180)).year)[:2]))

    def test_list_from_harvest_links_origin(self):
        h = Harvest.objects.create(planting=self.planting, harvested_on=TODAY - timedelta(days=2),
                                   quantity=180, grade="WM1")
        self.client.login(email=self.farmer.email, password=f.PW)
        url = reverse("marketplace:listing_create")
        r = self.client.post(url, {
            "kind": "produce", "from_harvest": h.pk, "p-price": "4200",
            "p-quantity_available": "180", "p-min_order_qty": "1", "p-low_stock_threshold": "10",
            "p-near_expiry_discount_pct": "0"})
        self.assertEqual(r.status_code, 302, r.content[:500])
        h.refresh_from_db()
        self.assertTrue(h.is_listed)
        self.assertEqual(h.listing.quantity_available, 180)

    def test_unlisted_harvest_is_available_on_my_stock_page(self):
        h = Harvest.objects.create(planting=self.planting, harvested_on=TODAY - timedelta(days=2),
                                   quantity=75, grade="WM1")
        self.client.force_login(self.farmer)

        response = self.client.get(reverse("marketplace:my_listings"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(h, response.context["unlisted_harvests"])
        self.assertContains(response, f"?harvest={h.pk}#add-produce")

    def test_profit_per_crop(self):
        CostEntry.objects.create(farmer=self.farmer, planting=self.planting, category="seed", amount=100000,
                                 incurred_on=TODAY - timedelta(days=170))
        h = Harvest.objects.create(planting=self.planting, harvested_on=TODAY - timedelta(days=2),
                                   quantity=100, grade="WM1")
        l = Listing.objects.create(farmer=self.farmer, commodity=self.planting.commodity, unit="ton", price=4000,
                                   quantity_available=100, grade="WM1", harvest=h)
        buyer = f.user("buyer")
        f.cart_with(buyer, (l, 30))
        services.place_order(buyer, "collection", "card", card_number=CARD)
        e = planting_economics(self.planting)
        self.assertEqual(e["cost_per_unit"], Decimal("1000.00"))      # R100,000 / 100 t
        self.assertEqual(e["revenue"], Decimal("120000.00"))          # 30 t x R4,000
        self.assertEqual(e["profit"], Decimal("20000.00"))
        self.assertEqual(e["margin_per_unit"], Decimal("3000.00"))
        self.assertEqual(insights.farmer_kpis(self.farmer)["this_month"], Decimal("120000.00"))

    def test_unpaid_order_is_not_counted_as_crop_income(self):
        h = Harvest.objects.create(planting=self.planting, harvested_on=TODAY - timedelta(days=2),
                                   quantity=100, grade="WM1")
        listing = Listing.objects.create(farmer=self.farmer, commodity=self.planting.commodity, unit="ton",
                                         price=4000, quantity_available=100, grade="WM1", harvest=h)
        buyer = f.user("buyer")
        f.cart_with(buyer, (listing, 30))
        f.use_paystack()
        services.place_order(buyer, "collection", "card")   # card payment not finished yet

        e = planting_economics(self.planting)

        self.assertEqual(e["sold"], Decimal("0"))
        self.assertEqual(e["revenue"], Decimal("0"))
        self.assertEqual(insights.farmer_kpis(self.farmer)["this_month"], Decimal("0"))

    def test_cost_must_belong_to_one_crop_or_herd(self):
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            CostEntry.objects.create(farmer=self.farmer, category="seed", amount=10, incurred_on=TODAY)


class WithdrawalTests(TestCase):
    def setUp(self):
        sync_standards(Category, Commodity)
        self.farmer = f.farmer()
        self.herd = Herd.objects.create(farmer=self.farmer, name="Ewes", commodity=Commodity.objects.get(code="SHEEP"),
                                        head_count=20, started_on=TODAY - timedelta(days=200))
        self.listing = f.listing(self.farmer, qty="10", kind="livestock", herd=self.herd,
                                 commodity=Commodity.objects.get(code="SHEEP"))
        self.buyer = f.user("buyer")

    def test_animal_in_withdrawal_cannot_be_bought(self):
        f.cart_with(self.buyer, (self.listing, 1))
        Treatment.objects.create(herd=self.herd, kind="treatment", remedy="Terramycin LA", treated_on=TODAY - timedelta(days=3),
                                 withdrawal_days=28)
        self.assertFalse(Listing.objects.sellable().filter(pk=self.listing.pk).exists())
        with self.assertRaisesMessage(services.CheckoutError, "withdrawal"):
            services.place_order(self.buyer, "collection", "card", card_number=CARD)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.quantity_available, 10)

    def test_sellable_again_after_withdrawal(self):
        Treatment.objects.create(herd=self.herd, kind="treatment", remedy="X", treated_on=TODAY - timedelta(days=30),
                                 withdrawal_days=28)
        self.assertTrue(Listing.objects.sellable().filter(pk=self.listing.pk).exists())


class BuyerToolTests(TestCase):
    def setUp(self):
        sync_standards(Category, Commodity)
        self.tom = Commodity.objects.get(code="TOMATO")
        self.farmer = f.farmer()
        self.buyer = f.user("buyer")

    def test_alert_fires_once_when_product_listed(self):
        StockAlert.objects.create(buyer=self.buyer, commodity=self.tom)
        from marketplace.services import announce_listing
        l = Listing.objects.create(farmer=self.farmer, commodity=self.tom, unit="crate", price=130,
                                   quantity_available=50, grade="Class 1")
        self.assertEqual(announce_listing(l), 1)
        alert = StockAlert.objects.get()
        self.assertFalse(alert.is_active)
        self.assertTrue(Notification.objects.filter(user=self.buyer, title__contains="Tomatoes").exists())
        self.assertEqual(announce_listing(l), 0)

    def test_followers_notified_of_new_stock(self):
        FavouriteFarm.objects.create(buyer=self.buyer, farmer=self.farmer)
        from marketplace.services import announce_listing
        l = Listing.objects.create(farmer=self.farmer, commodity=self.tom, unit="crate", price=130,
                                   quantity_available=50, grade="Class 1")
        announce_listing(l)
        self.assertTrue(Notification.objects.filter(user=self.buyer, title__startswith="New at").exists())

    def test_reorder_copies_available_items(self):
        l = Listing.objects.create(farmer=self.farmer, commodity=self.tom, unit="crate", price=130,
                                   quantity_available=50, grade="Class 1")
        f.cart_with(self.buyer, (l, 10))
        order = services.place_order(self.buyer, "collection", "card", card_number=CARD)
        added, skipped = services.reorder(order, self.buyer)
        self.assertEqual(len(added), 1)
        self.assertEqual(self.buyer.cart.items.get().quantity, 10)

    def test_quote_request_flow_and_only_one_acceptance(self):
        a, b = f.farmer(), f.farmer()
        la = Listing.objects.create(farmer=a, commodity=self.tom, unit="crate", price=140, quantity_available=100, grade="Class 1")
        lb = Listing.objects.create(farmer=b, commodity=self.tom, unit="crate", price=150, quantity_available=100, grade="Class 1")
        rfq = QuoteRequest.objects.create(buyer=self.buyer, commodity=self.tom, quantity=60, unit="crate",
                                          needed_by=TODAY + timedelta(days=7), delivery_province="GP")
        self.assertEqual(services.open_rfq(rfq), 2)
        qa = services.submit_quote(rfq, a, la, Decimal("120"))
        qb = services.submit_quote(rfq, b, lb, Decimal("118"))
        order = services.accept_quote(qb, self.buyer, "collection", "card", card_number=CARD)
        item = order.suborders.get().items.get()
        self.assertEqual(item.unit_price, Decimal("118"))
        lb.refresh_from_db()
        self.assertEqual(lb.quantity_available, 40)
        qa.refresh_from_db()
        self.assertEqual(qa.status, Quote.Status.DECLINED)
        with self.assertRaises(services.TransitionError):
            services.accept_quote(qa, self.buyer, "collection", "card", card_number=CARD)
        self.assertEqual(Order.objects.count(), 1)

    def test_quote_needs_enough_stock(self):
        l = Listing.objects.create(farmer=self.farmer, commodity=self.tom, unit="crate", price=140,
                                   quantity_available=20, grade="Class 1")
        rfq = QuoteRequest.objects.create(buyer=self.buyer, commodity=self.tom, quantity=60, unit="crate",
                                          needed_by=TODAY + timedelta(days=7), delivery_province="GP")
        with self.assertRaises(services.TransitionError):
            services.submit_quote(rfq, self.farmer, l, Decimal("120"))


class InsightTests(TestCase):
    def setUp(self):
        sync_standards(Category, Commodity)
        self.tom = Commodity.objects.get(code="TOMATO")
        self.buyer = f.user("buyer")

    def _sell(self, farmer, price, qty=10):
        l = Listing.objects.create(farmer=farmer, commodity=self.tom, unit="crate", price=price,
                                   quantity_available=100, grade="Class 1")
        f.cart_with(self.buyer, (l, qty))
        services.place_order(self.buyer, "collection", "card", card_number=CARD)

    def test_hidden_until_three_farms(self):
        self._sell(f.farmer(), 120)
        self._sell(f.farmer(), 130)
        self.assertIsNone(insights.price_guide(self.tom.pk, "crate"))
        self.assertEqual(insights.price_lookup(), {})
        self._sell(f.farmer(), 140)
        self.assertEqual(insights.price_guide(self.tom.pk, "crate")["farms"], 3)
        self.assertEqual(insights.price_lookup()[(self.tom.pk, "crate")], Decimal("130.00"))

    def test_cancelled_sales_not_counted(self):
        for p in (120, 130, 140):
            self._sell(f.farmer(), p)
        sub = SubOrder.objects.first()
        services.change_status(sub, SubOrder.Status.CANCELLED, self.buyer)
        self.assertIsNone(insights.price_guide(self.tom.pk, "crate"))  # only 2 farms left

    def test_market_insights_page_removed(self):
        farmer = f.farmer()
        self.client.login(email=farmer.email, password=f.PW)
        self.assertEqual(self.client.get("/insights/").status_code, 404)
        self.assertNotContains(self.client.get(reverse("core:dashboard")), "Market insights")
