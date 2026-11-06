from decimal import Decimal
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import SiteSettings
from marketplace.models import money
from orders import services
from orders.models import Payment, Refund, SubOrder

from . import factories as f

S = SubOrder.Status
CARD = "4111111111111111"


class PricingTests(TestCase):
    def test_vat_rounding_example_from_srs(self):
        # BR-04: R1,234.57 of goods -> VAT R185.19, total R1,419.76
        vat = money(Decimal("1234.57") * SiteSettings.load().vat_rate)
        self.assertEqual(vat, Decimal("185.19"))
        self.assertEqual(Decimal("1234.57") + vat, Decimal("1419.76"))

    def test_vat_only_for_vat_registered_farmers(self):
        vat_farm, plain_farm = f.farmer(vat=True), f.farmer(vat=False)
        a = f.listing(vat_farm, qty="10", price="1000")
        b = f.listing(plain_farm, qty="10", price="1000")
        buyer = f.user("buyer")
        f.cart_with(buyer, (a, 1), (b, 1))
        order = services.place_order(buyer, "collection", "card", card_number=CARD)
        self.assertEqual(order.suborders.count(), 2, "one sub-order per farmer")
        self.assertEqual(order.suborders.get(farmer=vat_farm).vat_amount, Decimal("150.00"))
        self.assertEqual(order.suborders.get(farmer=plain_farm).vat_amount, Decimal("0.00"))
        self.assertEqual(order.grand_total, Decimal("2150.00"))

    def test_bulk_tier_price(self):
        farm = f.farmer()
        maize = f.listing(farm, qty="100", price="4000")
        maize.price_tiers.create(min_quantity=10, unit_price=Decimal("3700"))
        self.assertEqual(maize.unit_price_for(Decimal("12")) * 12, Decimal("44400"))
        self.assertEqual(maize.unit_price_for(Decimal("5")), Decimal("4000"))

    def test_delivery_fee(self):
        f.rates()
        farm = f.farmer()
        maize = f.listing(farm, qty="100", price="4000")
        buyer = f.user("buyer")
        f.cart_with(buyer, (maize, 2))
        order = services.place_order(buyer, "delivery", "card", "1 Main Rd", "FS", card_number=CARD)
        self.assertEqual(order.delivery_total, Decimal("500.00"))  # 300 + 2 t x 100

    def test_price_is_copied_onto_order(self):
        farm = f.farmer()
        maize = f.listing(farm, qty="100", price="4000")
        buyer = f.user("buyer")
        f.cart_with(buyer, (maize, 1))
        order = services.place_order(buyer, "collection", "card", card_number=CARD)
        maize.price = Decimal("9999")
        maize.save()
        item = order.suborders.get().items.get()
        self.assertEqual(item.unit_price, Decimal("4000"))


class LifecycleTests(TestCase):
    def setUp(self):
        self.farm = f.farmer()
        self.dist = f.user("distributor")
        self.admin = f.user("admin")
        self.buyer = f.user("buyer")
        f.rates()
        self.maize = f.listing(self.farm, qty="20", price="1000")
        f.cart_with(self.buyer, (self.maize, 4))
        self.order = services.place_order(self.buyer, "delivery", "card", "1 Main Rd", "FS", card_number=CARD)
        self.sub = self.order.suborders.get()

    def test_card_order_is_confirmed_without_farmer_approval(self):
        self.assertEqual(self.sub.status, S.APPROVED)
        self.assertTrue(self.sub.invoice_number.startswith("INV-"))
        self.assertTrue(self.farm.notifications.filter(title__contains="please pack").exists())

    def test_full_delivery_flow_and_commission(self):
        self.assertTrue(self.sub.invoice_number.startswith("INV-"))
        services.change_status(self.sub, S.PACKED, self.farm)
        services.schedule_delivery(self.sub, timezone.localdate(), "08:00-12:00", self.farm, distributor=self.dist)
        self.sub.refresh_from_db()
        services.change_status(self.sub, S.IN_TRANSIT, self.dist)
        with self.assertRaises(services.TransitionError):
            services.change_status(self.sub, S.DELIVERED, self.dist)  # note required
        services.change_status(self.sub, S.DELIVERED, self.dist, note="Received by Ayesha")
        services.change_status(self.sub, S.COMPLETED, self.buyer)
        self.assertEqual(self.sub.commission_amount, Decimal("200.00"))  # 5% of R4,000
        self.assertEqual(self.sub.history.count(), 6)

    def test_illegal_jump_refused(self):
        with self.assertRaises(services.TransitionError):
            services.change_status(self.sub, S.DELIVERED, self.dist, note="x")

    def test_online_card_order_confirmed_only_when_gateway_reports_payment(self):
        f.use_paystack()
        f.cart_with(self.buyer, (self.maize, 1))
        order = services.place_order(self.buyer, "collection", "card")
        sub = order.suborders.get()
        self.assertEqual(sub.status, S.PENDING)
        with self.assertRaises(services.TransitionError):
            services.change_status(sub, S.APPROVED, self.farm)   # nobody can confirm it by hand
        services.card_gateway_paid(order.payment, "PS123", note="Paid on Paystack")
        sub.refresh_from_db()
        self.assertEqual(sub.status, S.APPROVED)
        self.assertTrue(sub.invoice_number)

    def test_only_card_is_offered(self):
        from orders.forms import CheckoutForm
        self.assertEqual({v for v, _ in CheckoutForm().fields["payment_method"].choices}, {"card"})
        for method in ("eft", "cod"):
            form = CheckoutForm({"delivery_method": "collection", "payment_method": method,
                                 "order_date": timezone.localdate()})
            self.assertFalse(form.is_valid())
            self.assertIn("payment_method", form.errors)
        f.cart_with(self.buyer, (self.maize, 1))
        with self.assertRaises(services.CheckoutError):
            services.place_order(self.buyer, "collection", "cod")

    def test_farmer_schedules_available_driver_in_farm_province(self):
        from orders.forms import FarmerScheduleForm

        date = timezone.localdate()
        payload = {"date": date.isoformat(), "slot": "08:00-12:00", "distributor": self.dist.pk}
        form = FarmerScheduleForm(payload, suborder=self.sub)
        self.assertTrue(form.is_valid(), form.errors)
        self.client.force_login(self.farm)
        detail = self.client.get(reverse("orders:suborder", args=[self.sub.pk]))
        self.assertContains(detail, "Schedule the driver to collect")
        response = self.client.post(reverse("orders:schedule", args=[self.sub.pk]), payload)
        self.assertEqual(response.status_code, 302)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.distributor, self.dist)
        self.assertEqual(self.sub.scheduled_date, date)

        another_buyer = f.user("buyer")
        f.cart_with(another_buyer, (self.maize, 1))
        another_order = services.place_order(
            another_buyer, "delivery", "card", "1 Main Rd", "FS", card_number=CARD)
        another_sub = another_order.suborders.get()
        conflict = FarmerScheduleForm(payload, suborder=another_sub)
        self.assertFalse(conflict.is_valid())
        with self.assertRaisesMessage(services.TransitionError, "already scheduled"):
            services.schedule_delivery(
                another_sub, date, "08:00-12:00", self.farm, distributor=self.dist)

    def test_farmer_cannot_schedule_driver_from_another_province(self):
        wrong_province_driver = f.user("distributor", province="KZN")
        with self.assertRaisesMessage(services.TransitionError, "farm's province"):
            services.schedule_delivery(
                self.sub, timezone.localdate() + timedelta(days=1), "08:00-12:00",
                self.farm, distributor=wrong_province_driver)

    def test_cancel_paid_order_refunds_buyer_each_side_its_part(self):
        with self.captureOnCommitCallbacks(execute=True):
            services.change_status(self.sub, S.CANCELLED, self.buyer, note="Changed my mind")
        refund = Refund.objects.get()
        self.sub.refresh_from_db()
        self.assertEqual(refund.amount, self.sub.total)                       # the buyer gets everything back
        self.assertEqual(refund.farm_amount, self.sub.farm_share)             # the farm returns its share
        self.assertEqual(refund.commission_amount + refund.farm_amount, self.sub.goods_subtotal + self.sub.vat_amount)
        self.assertEqual(refund.delivery_amount, self.sub.delivery_fee)       # never delivered: fee returned
        self.assertEqual(refund.status, Refund.Status.PROCESSED)
        self.order.payment.refresh_from_db()
        self.assertEqual(self.order.payment.status, Payment.Status.REFUNDED)
        self.assertEqual(services.farm_debt(self.farm), refund.farm_amount)   # kept back from its next sales
        self.assertTrue(services.send_refund(refund.pk))                      # sending again changes nothing
        self.assertEqual(Refund.objects.count(), 1)

    def test_unpaid_card_order_auto_cancels_after_60_minutes(self):
        f.use_paystack()
        f.cart_with(self.buyer, (self.maize, 2))
        order = services.place_order(self.buyer, "collection", "card")
        sub = order.suborders.get()
        placed = timezone.now() - timedelta(minutes=61)
        SubOrder.objects.filter(pk=sub.pk).update(created_at=placed)
        cancelled, _ = services.run_timeouts()
        self.assertEqual(cancelled, 1)
        self.maize.refresh_from_db()
        self.assertEqual(self.maize.quantity_available, 16)  # the 2 t came back; the paid card order keeps 4 t
        sub.refresh_from_db()
        last = sub.history.get(to_status=S.CANCELLED)
        self.assertEqual(last.created_at, placed + timedelta(minutes=60))  # dated when the deadline passed

    def test_farm_can_cancel_confirmed_order_it_cannot_supply(self):
        self.client.force_login(self.farm)
        self.client.post(reverse("orders:suborder_action", args=[self.sub.pk, "reject"]),
                         {"note": "Hail damage"})
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, S.CANCELLED)
        self.assertTrue(Refund.objects.filter(suborder=self.sub).exists())

    def test_no_approve_action_for_farmer(self):
        self.client.force_login(self.farm)
        r = self.client.post(reverse("orders:suborder_action", args=[self.sub.pk, "approve"]))
        self.assertEqual(r.status_code, 403)
