"""Paystack test mode: start, verify on return, amount check, check button, settings key test."""
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import SiteSettings
from orders import paystack
from orders.models import GatewayEvent, Order, Payment, SubOrder

from . import factories as f

S = SubOrder.Status


class PaystackBase(TestCase):
    def setUp(self):
        s = SiteSettings.load()
        s.card_gateway = "paystack_test"
        s.paystack_secret_key = "sk_test_abc123"
        s.save()
        self.farm = f.farmer()
        self.buyer = f.user("buyer")
        self.maize = f.listing(self.farm, qty="20", price="1000")
        f.cart_with(self.buyer, (self.maize, 2))
        self.client.force_login(self.buyer)

    def checkout(self):
        return self.client.post(reverse("orders:checkout"), {
            "delivery_method": "collection", "payment_method": "card", "order_date": timezone.localdate()})

    def api(self, replies):
        calls = []

        def fake(method, path, cfg, payload=None):
            calls.append((method, path, payload))
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return mock.patch("orders.paystack._api", side_effect=fake), calls


class PaystackTests(PaystackBase):
    def test_checkout_sends_buyer_to_paystack_then_verifies(self):
        patch, calls = self.api([{"status": True, "data": {"authorization_url": "https://checkout.paystack.com/xyz",
                                                           "reference": "ORD-1-1"}}])
        with patch:
            r = self.checkout()
            order = Order.objects.get()
            self.assertRedirects(r, reverse("orders:card_pay", args=[order.pk]), fetch_redirect_response=False)
            r = self.client.get(reverse("orders:card_pay", args=[order.pk]))
        self.assertEqual(r["Location"], "https://checkout.paystack.com/xyz")
        payload = calls[0][2]
        self.assertEqual(payload["amount"], int(order.grand_total * 100))   # cents
        self.assertEqual(payload["currency"], "ZAR")
        self.assertEqual(order.payment.gateway, "paystack")
        self.assertEqual(order.payment.status, Payment.Status.PENDING)

        cents = int(order.grand_total * 100)
        patch, _ = self.api([{"status": True, "data": {"status": "success", "amount": cents, "id": 4099,
                                                      "gateway_response": "Approved"}}])
        with patch:
            r = self.client.get(reverse("orders:card_return", args=[order.pk]) + "?reference=ORD-1-1")
        self.assertRedirects(r, reverse("orders:order_detail", args=[order.pk]), fetch_redirect_response=False)
        order.payment.refresh_from_db()
        self.assertEqual(order.payment.status, Payment.Status.PAID)
        self.assertEqual(order.payment.reference, "PS4099")
        self.assertEqual(order.suborders.get().status, S.APPROVED)

    def test_wrong_amount_is_not_accepted(self):
        patch, _ = self.api([{"status": True, "data": {"authorization_url": "https://x", "reference": "R1"}},
                             {"status": True, "data": {"status": "success", "amount": 100, "id": 1}}])
        with patch:
            self.checkout()
            order = Order.objects.get()
            self.client.get(reverse("orders:card_pay", args=[order.pk]))
            self.client.get(reverse("orders:card_return", args=[order.pk]) + "?reference=R1")
        order.payment.refresh_from_db()
        self.assertEqual(order.payment.status, Payment.Status.PENDING)
        self.assertIn("does not match", GatewayEvent.objects.filter(kind="query").get().problems)

    def test_check_button_uses_started_reference(self):
        cents = None
        patch, _ = self.api([{"status": True, "data": {"authorization_url": "https://x", "reference": "R7"}}])
        with patch:
            self.checkout()
            order = Order.objects.get()
            self.client.get(reverse("orders:card_pay", args=[order.pk]))
        cents = int(order.grand_total * 100)
        patch, calls = self.api([{"status": True, "data": {"status": "success", "amount": cents, "id": 77}}])
        with patch:
            self.client.post(reverse("orders:card_check", args=[order.pk]))
        self.assertIn("/transaction/verify/R7", calls[0][1])
        order.payment.refresh_from_db()
        self.assertEqual(order.payment.status, Payment.Status.PAID)

    def test_paystack_error_is_shown_not_crashed(self):
        patch, _ = self.api([paystack.PaystackError("Paystack said: Invalid key (HTTP 401)")])
        with patch:
            self.checkout()
            order = Order.objects.get()
            r = self.client.get(reverse("orders:card_pay", args=[order.pk]), follow=True)
        self.assertContains(r, "Invalid key")

    def test_currency_fallback(self):
        patch, calls = self.api([paystack.PaystackError("Paystack said: Currency not supported by merchant"),
                                 {"status": True, "data": {"authorization_url": "https://x", "reference": "R2"}}])
        with patch:
            self.checkout()
            order = Order.objects.get()
            self.client.get(reverse("orders:card_pay", args=[order.pk]))
        self.assertNotIn("currency", calls[1][2])

    def test_sample_email_replaced(self):
        u = f.user("buyer")
        u.email = "buyer@agrimarket.test"
        self.assertEqual(paystack._email(u), f"buyer{u.pk}@example.com")

    def test_settings_key_check(self):
        admin = f.user("admin")
        self.client.force_login(admin)
        with mock.patch("orders.paystack._api", return_value={"status": True, "data": []}):
            r = self.client.post(reverse("core:settings"), {"form": "pftest"}, follow=True)
        self.assertContains(r, "Paystack accepted the secret key")


class PaystackTimeoutTests(PaystackBase):
    """A buyer who paid but closed the browser must not lose the order to the 60-minute timeout."""

    def _start(self):
        patch, _ = self.api([{"status": True, "data": {"authorization_url": "https://x", "reference": "R9"}}])
        with patch:
            self.checkout()
            order = Order.objects.get()
            self.client.get(reverse("orders:card_pay", args=[order.pk]))
        from datetime import timedelta
        SubOrder.objects.filter(order=order).update(created_at=timezone.now() - timedelta(minutes=61))
        return order

    def test_paid_order_is_confirmed_not_cancelled(self):
        from orders import services
        order = self._start()
        cents = int(order.grand_total * 100)
        patch, _ = self.api([{"status": True, "data": {"status": "success", "amount": cents, "id": 55}}])
        with patch:
            cancelled, _ = services.run_timeouts()
        self.assertEqual(cancelled, 0)
        self.assertEqual(order.suborders.get().status, S.APPROVED)

    def test_unpaid_order_is_cancelled(self):
        from orders import services
        order = self._start()
        patch, _ = self.api([{"status": True, "data": {"status": "abandoned", "amount": 0}}])
        with patch:
            cancelled, _ = services.run_timeouts()
        self.assertEqual(cancelled, 1)

    def test_paystack_unreachable_leaves_order_for_next_run(self):
        from orders import services
        order = self._start()
        patch, _ = self.api([paystack.PaystackError("Could not reach Paystack")])
        with patch:
            cancelled, _ = services.run_timeouts()
        self.assertEqual(cancelled, 0)
        self.assertEqual(order.suborders.get().status, S.PENDING)
