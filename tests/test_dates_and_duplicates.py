"""Past-dated orders, the 1 March – today window, and duplicate protection."""
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import SiteSettings
from farm.models import CostEntry, Field, Harvest, Herd, Planting, Treatment
from finance import services as fin
from marketplace.models import Category, Commodity, Listing
from marketplace.standards import sync_standards
from orders import services
from orders.models import Order, SubOrder

from . import factories as f

CARD = "4111111111111111"
TODAY = timezone.localdate()
# Default window start (same rule as SiteSettings.date_window, without touching the database at import).
START = date(TODAY.year, 3, 1) if date(TODAY.year, 3, 1) <= TODAY else date(TODAY.year - 1, 3, 1)


class BackdatedOrderTests(TestCase):
    def setUp(self):
        self.farm, self.buyer = f.farmer(), f.user("buyer")
        self.l = f.listing(self.farm, qty="50", harvest_date=START)

    def test_past_order_counts_in_its_month(self):
        past = START + timedelta(days=20)
        f.cart_with(self.buyer, (self.l, 2))
        order = services.place_order(self.buyer, "collection", "card", card_number=CARD, order_date=past)
        self.assertEqual(order.order_date, past)
        self.assertTrue(order.number.startswith(f"ORD-{past:%Y}-"))
        # Every timestamp belongs to the order date - nothing says it was entered later.
        local = lambda dt: timezone.localtime(dt).date()
        sub = order.suborders.get()
        self.assertEqual(local(order.created_at), past)
        self.assertEqual(local(sub.approved_at), past)
        self.assertEqual(local(order.payment.confirmed_at), past)
        self.assertEqual({local(h.created_at) for h in sub.history.all()}, {past})
        self.assertEqual({local(n.created_at) for n in self.farm.notifications.all()}, {past})
        self.client.force_login(self.buyer)
        page = self.client.get(reverse("orders:order_detail", args=[order.pk])).content.decode()
        self.assertNotIn("captured", page.lower())
        self.assertNotIn("entered later", page.lower())
        rows, totals = fin.sales_report(past, past)
        self.assertEqual(totals["goods"], Decimal("200.00"))
        self.assertEqual(fin.sales_report(TODAY, TODAY)[1]["goods"], 0)

    def test_later_steps_of_a_past_sale_take_the_chosen_date(self):
        past = START + timedelta(days=20)
        f.cart_with(self.buyer, (self.l, 2))
        order = services.place_order(self.buyer, "collection", "card", card_number=CARD, order_date=past)
        sub = order.suborders.get()
        self.client.force_login(self.farm)
        page = self.client.get(reverse("orders:suborder", args=[sub.pk]))
        self.assertContains(page, 'name="on_date"')
        packed_on, collected_on = past + timedelta(days=1), past + timedelta(days=2)
        self.client.post(reverse("orders:suborder_action", args=[sub.pk, "pack"]), {"on_date": packed_on})
        self.client.post(reverse("orders:suborder_action", args=[sub.pk, "collected"]),
                         {"on_date": collected_on, "note": "Collected by T. Nkosi"})
        sub.refresh_from_db()
        self.assertEqual(sub.status, SubOrder.Status.DELIVERED)
        self.assertEqual(timezone.localtime(sub.delivered_at).date(), collected_on)
        self.assertEqual(timezone.localtime(sub.history.get(to_status="packed").created_at).date(), packed_on)
        # a step cannot be dated before the previous one
        self.client.post(reverse("orders:suborder_action", args=[sub.pk, "pack"]), {"on_date": past})
        services.run_timeouts()
        sub.refresh_from_db()
        self.assertEqual(sub.status, SubOrder.Status.COMPLETED)
        self.assertEqual(timezone.localtime(sub.completed_at).date(), collected_on + timedelta(days=3))

    def test_todays_order_has_no_date_field_on_steps(self):
        f.cart_with(self.buyer, (self.l, 1))
        order = services.place_order(self.buyer, "collection", "card", card_number=CARD)
        self.client.force_login(self.farm)
        page = self.client.get(reverse("orders:suborder", args=[order.suborders.get().pk]))
        self.assertNotContains(page, 'name="on_date"')

    def test_dates_outside_window_refused(self):
        f.cart_with(self.buyer, (self.l, 1))
        for bad in (START - timedelta(days=1), TODAY + timedelta(days=1)):
            with self.assertRaises(services.CheckoutError):
                services.place_order(self.buyer, "collection", "card", card_number=CARD, order_date=bad)
        self.assertFalse(Order.objects.exists())

    def test_not_before_harvest(self):
        late = f.listing(self.farm, qty="5", harvest_date=TODAY - timedelta(days=2))
        f.cart_with(self.buyer, (late, 1))
        with self.assertRaisesMessage(services.CheckoutError, "harvested"):
            services.place_order(self.buyer, "collection", "card", card_number=CARD,
                                 order_date=TODAY - timedelta(days=10))

    def test_backdated_pending_order_not_auto_cancelled(self):
        f.cart_with(self.buyer, (self.l, 1))
        services.place_order(self.buyer, "collection", "card", card_number=CARD, order_date=START)
        cancelled, _ = services.run_timeouts()
        self.assertEqual(cancelled, 0)  # 48 hours count from when it was captured, not the order date

    def test_switch_off_backdating(self):
        s = SiteSettings.load(); s.allow_backdated_orders = False; s.save()
        f.cart_with(self.buyer, (self.l, 1))
        with self.assertRaises(services.CheckoutError):
            services.place_order(self.buyer, "collection", "card", card_number=CARD, order_date=START)


class RecordWindowTests(TestCase):
    def setUp(self):
        sync_standards(Category, Commodity)
        self.farmer = f.farmer()
        self.field = Field.objects.create(farmer=self.farmer, name="Land 1", size_ha=10)

    def test_planting_before_window_refused(self):
        p = Planting(farmer=self.farmer, field=self.field, commodity=Commodity.objects.get(code="MAIZE-W"),
                     planted_on=START - timedelta(days=1), area_ha=5, expected_harvest=TODAY,
                     unit="ton", expected_yield=10)
        with self.assertRaises(ValidationError):
            p.full_clean(exclude=["season"])

    def test_treatment_and_cost_in_future_refused(self):
        herd = Herd.objects.create(farmer=self.farmer, name="H", commodity=Commodity.objects.get(code="GOAT"),
                                   head_count=5, started_on=START)
        with self.assertRaises(ValidationError):
            Treatment(herd=herd, kind="vaccine", remedy="X", treated_on=TODAY + timedelta(days=1),
                      withdrawal_days=0).full_clean(exclude=["withdrawal_ends"])
        with self.assertRaises(ValidationError):
            CostEntry(farmer=self.farmer, herd=herd, category="feed", amount=10,
                      incurred_on=START - timedelta(days=3)).full_clean()


class DuplicateTests(TestCase):
    def setUp(self):
        sync_standards(Category, Commodity)
        self.farmer = f.farmer()
        self.maize = Commodity.objects.get(code="MAIZE-W")
        self.field = Field.objects.create(farmer=self.farmer, name="Land 1", size_ha=10)

    def test_same_planting_twice_refused(self):
        kw = dict(farmer=self.farmer, field=self.field, commodity=self.maize, planted_on=START, area_ha=5,
                  expected_harvest=TODAY, unit="ton", expected_yield=10)
        Planting.objects.create(**kw)
        with self.assertRaises(ValidationError):
            Planting(**kw).full_clean(exclude=["season"])

    def test_same_cost_twice_refused(self):
        p = Planting.objects.create(farmer=self.farmer, field=self.field, commodity=self.maize, planted_on=START,
                                    area_ha=5, expected_harvest=TODAY, unit="ton", expected_yield=10)
        kw = dict(farmer=self.farmer, planting=p, category="seed", amount=Decimal("100"), incurred_on=START)
        CostEntry.objects.create(**kw)
        with self.assertRaises(ValidationError):
            CostEntry(**kw).full_clean()

    def test_same_listing_twice_refused(self):
        from marketplace.forms import ListingForm
        Listing.objects.create(farmer=self.farmer, commodity=self.maize, unit="ton", price=4000,
                               quantity_available=10, grade="WM1", harvest_date=START)
        form = ListingForm({"p-commodity": self.maize.pk, "p-grade": "WM1", "p-unit": "ton", "p-price": "4100",
                            "p-quantity_available": "5", "p-min_order_qty": "1", "p-low_stock_threshold": "0",
                            "p-harvest_date": START.isoformat(), "p-near_expiry_discount_pct": "0"},
                           instance=Listing(farmer=self.farmer, kind="produce"), kind="produce", prefix="p")
        self.assertFalse(form.is_valid())
        self.assertIn("Adjust stock", str(form.non_field_errors()))

    def test_phone_registered_twice_refused(self):
        existing = f.user("buyer")
        from accounts.forms import BuyerRegistrationForm
        local = existing.phone
        form = BuyerRegistrationForm({"first_name": "A", "last_name": "B", "email": "new@x.za",
                                      "phone": "+27" + local[1:], "buyer_type": "retail", "province": "GP",
                                      "password1": "farm2026x", "password2": "farm2026x", "popia_consent": "on"})
        self.assertFalse(form.is_valid())
        self.assertIn("phone", form.errors)
