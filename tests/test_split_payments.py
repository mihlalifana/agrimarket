"""Card money goes straight to the farm (Paystack split payments); AgriMarket keeps its 5%.

Refunds: each side returns its part; the farm's part is kept back from its next sales.

Paystack is mocked with its real reply shapes, so these tests run offline.
"""
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from finance import services as fin
from orders import paystack, services
from orders.models import GatewayEvent, Order, Refund, SubOrder

from . import factories as f

S = SubOrder.Status


class FakePaystack:
    """Answers like Paystack; records every call. `refuse_split` makes initialize reject a split."""

    def __init__(self, refuse_split=False, subaccount_error=None):
        self.calls = []
        self.refuse_split = refuse_split
        self.subaccount_error = subaccount_error
        self.amount = None

    def __call__(self, method, path, cfg, payload=None):
        self.calls.append((method, path, payload))
        if path.startswith("/bank"):
            return {"status": True, "data": [{"code": "250655", "name": "First National Bank", "active": True},
                                             {"code": "470010", "name": "Capitec Bank", "active": True}]}
        if path.startswith("/subaccount"):
            if self.subaccount_error:
                raise paystack.PaystackError(self.subaccount_error)
            return {"status": True, "data": {"subaccount_code": "ACCT_farm1"}}
        if path == "/transaction/initialize":
            if self.refuse_split and "split" in payload:
                raise paystack.PaystackError("Paystack said: Invalid subaccount code (HTTP 400)")
            self.amount = payload["amount"]
            return {"status": True, "data": {"authorization_url": "https://checkout.paystack.com/abc",
                                             "reference": payload["reference"]}}
        if path == "/refund":
            return {"status": True, "data": {"id": 555, "status": "pending"}}
        if path.startswith("/transaction/verify/"):
            return {"status": True, "data": {"status": "success", "amount": self.amount, "id": 987,
                                             "gateway_response": "Approved"}}
        raise AssertionError(f"unexpected call {method} {path}")

    def payloads(self, path):
        return [p for m, pth, p in self.calls if pth == path]


class SplitBase(TestCase):
    def setUp(self):
        f.use_paystack()
        paystack._bank_cache.update(at=0, banks=None)
        self.farm_a = f.farmer(vat=True)                     # VAT registered, has a payout account
        self.farm_b = f.farmer(payout_account=False)         # no bank account yet
        self.maize = f.listing(self.farm_a, qty="10", price="1000")
        self.beans = f.listing(self.farm_b, qty="10", price="500")
        self.buyer = f.user("buyer")

    def pay_by_card(self, fake, *lines):
        f.cart_with(self.buyer, *lines)
        self.client.force_login(self.buyer)
        with mock.patch("orders.paystack._api", side_effect=fake):
            self.client.post(reverse("orders:checkout"), {"delivery_method": "collection", "payment_method": "card",
                                                          "order_date": timezone.localdate()})
            order = Order.objects.latest("pk")
            self.client.get(reverse("orders:card_pay", args=[order.pk]))
            ref = fake.payloads("/transaction/initialize")[-1]["reference"]
            self.client.get(reverse("orders:card_return", args=[order.pk]) + f"?reference={ref}")
        return order

    def complete(self, sub):
        services.change_status(sub, S.PACKED, sub.farmer)
        services.change_status(sub, S.DELIVERED, sub.farmer, note="Collected by T. Buyer")
        services.change_status(sub, S.COMPLETED, sub.order.buyer)
        sub.refresh_from_db()
        return sub


class PayoutAccountTests(SplitBase):
    def test_verified_farm_gets_a_paystack_subaccount(self):
        farm = self.farm_b.farm
        farm.bank_code, farm.bank_account = "470010", "1234567890"
        farm.save()
        fake = FakePaystack()
        with mock.patch("orders.paystack._api", side_effect=fake):
            ok, note = services.setup_payout_account(farm)
        self.assertTrue(ok)
        farm.refresh_from_db()
        self.assertEqual(farm.paystack_subaccount, "ACCT_farm1")
        sent = fake.payloads("/subaccount")[0]
        self.assertEqual(sent["settlement_bank"], "470010")
        self.assertEqual(sent["account_number"], "1234567890")
        self.assertEqual(sent["percentage_charge"], 5.0)              # AgriMarket's commission

    def test_paystack_refusal_is_shown_and_nothing_saved(self):
        farm = self.farm_b.farm
        farm.bank_code, farm.bank_account = "470010", "1"
        farm.save()
        with mock.patch("orders.paystack._api", side_effect=FakePaystack(
                subaccount_error="Paystack said: Account number is invalid (HTTP 400)")):
            ok, note = services.setup_payout_account(farm)
        self.assertFalse(ok)
        farm.refresh_from_db()
        self.assertEqual(farm.paystack_subaccount, "")
        self.assertIn("Account number is invalid", farm.payout_account_note)

    def test_farmer_adds_bank_on_profile(self):
        self.client.force_login(self.farm_b)
        u = self.farm_b
        with mock.patch("orders.paystack._api", side_effect=FakePaystack()):
            self.client.post(reverse("accounts:profile"), {
                "u-first_name": u.first_name, "u-last_name": u.last_name, "u-phone": u.phone,
                "u-province": u.province, "u-address": "Farm road",
                "f-farm_name": u.farm.farm_name, "f-town": "Bloemfontein", "f-province": "FS", "f-brand_mark": "ABC",
                "f-bank_code": "250655", "f-bank_account": "62000000002"})
        u.farm.refresh_from_db()
        self.assertEqual(u.farm.bank_name, "First National Bank")
        self.assertEqual(u.farm.paystack_subaccount, "ACCT_farm1")

    def test_farm_without_payout_account_is_not_for_sale(self):
        from marketplace.models import Listing
        self.assertTrue(Listing.objects.sellable().filter(pk=self.maize.pk).exists())
        self.assertFalse(Listing.objects.sellable().filter(pk=self.beans.pk).exists())
        f.cart_with(self.buyer, (self.beans, 1))
        with self.assertRaises(services.CheckoutError):
            services.place_order(self.buyer, "collection", "card")


class SplitPaymentTests(SplitBase):
    def test_card_payment_is_split_and_farm_paid_directly(self):
        fake = FakePaystack()
        order = self.pay_by_card(fake, (self.maize, 2))
        init = fake.payloads("/transaction/initialize")[0]
        self.assertEqual(init["amount"], 230000)                          # R2 000 + 15% VAT, in cents
        self.assertEqual(init["split"], {"type": "flat", "bearer_type": "account",
                                         "subaccounts": [{"subaccount": self.farm_a.farm.paystack_subaccount,
                                                          "share": 220000}]})
        sub = order.suborders.get()
        self.assertEqual(sub.status, S.APPROVED)
        self.assertTrue(sub.paid_to_farm)
        self.assertEqual(sub.farm_share, Decimal("2200.00"))             # 2 000 + 300 VAT - 100 commission
        sub = self.complete(sub)
        self.assertEqual(sub.commission_amount, Decimal("100.00"))
        month = fin.farm_month(self.farm_a, timezone.localdate())
        self.assertEqual((month["sales"], month["commission"], month["paid"]),
                         (Decimal("2300.00"), Decimal("100.00"), Decimal("2200.00")))
        self.assertEqual(fin.platform_income(1)[0]["income"], Decimal("100.00"))

    def test_split_refused_takes_no_payment(self):
        fake = FakePaystack(refuse_split=True)
        order = self.pay_by_card(fake, (self.maize, 1))
        sub = order.suborders.get()
        self.assertEqual(sub.status, S.PENDING)                            # nothing paid, nothing confirmed
        self.assertFalse(sub.paid_to_farm)
        self.assertTrue(GatewayEvent.objects.filter(problems__icontains="subaccount").exists())


class RefundTests(SplitBase):
    def test_cancelled_order_refunded_through_paystack_and_recovered_from_next_sale(self):
        fake = FakePaystack()
        order = self.pay_by_card(fake, (self.maize, 1))
        sub = order.suborders.get()
        with mock.patch("orders.paystack._api", side_effect=fake), self.captureOnCommitCallbacks(execute=True):
            services.change_status(sub, S.CANCELLED, self.farm_a, note="Hail damaged the crop")
        refund = Refund.objects.get(suborder=sub)
        self.assertEqual(refund.status, Refund.Status.PROCESSED)
        sent = fake.payloads("/refund")[0]
        self.assertEqual(sent["amount"], 115000)                            # the buyer gets all R1 150 back
        self.assertEqual((refund.farm_amount, refund.commission_amount), (Decimal("1100.00"), Decimal("50.00")))
        self.assertEqual(services.farm_debt(self.farm_a), Decimal("1100.00"))

        fake2 = FakePaystack()
        order2 = self.pay_by_card(fake2, (self.maize, 2))                  # next sale: R2 200 share
        split = fake2.payloads("/transaction/initialize")[0]["split"]["subaccounts"][0]
        self.assertEqual(split["share"], 110000)                           # R1 100 kept back to repay the refund
        sub2 = order2.suborders.get()
        self.assertEqual((sub2.farm_share, sub2.recovered), (Decimal("1100.00"), Decimal("1100.00")))
        self.assertEqual(services.farm_debt(self.farm_a), Decimal("0"))
        sub2 = self.complete(sub2)
        self.assertEqual(sub2.commission_amount, Decimal("100.00"))        # commission is still 5% of goods

    def test_farmer_answers_complaint_with_partial_refund(self):
        fake = FakePaystack()
        order = self.pay_by_card(fake, (self.maize, 2))
        sub = order.suborders.get()
        services.change_status(sub, S.PACKED, self.farm_a)
        services.change_status(sub, S.DELIVERED, self.farm_a, note="Collected by T. Buyer")
        dispute = services.raise_dispute(sub, self.buyer, "Two bags were wet")
        with self.assertRaises(services.TransitionError):
            services.resolve_dispute(dispute, f.farmer(), "partial", "Not my farm", Decimal("100"))
        with mock.patch("orders.paystack._api", side_effect=fake), self.captureOnCommitCallbacks(execute=True):
            services.resolve_dispute(dispute, self.farm_a, "partial", "Sorry, we refund two bags.", Decimal("460"))
        sub.refresh_from_db()
        self.assertEqual(sub.status, S.COMPLETED)
        refund = Refund.objects.get(suborder=sub)
        self.assertEqual(refund.amount, Decimal("460.00"))
        self.assertEqual(refund.delivery_amount, Decimal("0"))               # it was delivered
        self.assertEqual(refund.commission_amount, Decimal("20.00"))         # AgriMarket returns its 5% of R400 goods
        self.assertEqual(refund.farm_amount, Decimal("440.00"))
        self.assertEqual(fin.platform_income(1)[0]["income"], Decimal("80.00"))   # R100 commission - R20 given back

    def test_paystack_refusal_is_retried_by_the_hourly_job(self):
        order = self.pay_by_card(FakePaystack(), (self.maize, 1))
        sub = order.suborders.get()

        def refuse(method, path, cfg, payload=None):
            raise paystack.PaystackError("Paystack said: Insufficient balance (HTTP 400)")
        with mock.patch("orders.paystack._api", side_effect=refuse), self.captureOnCommitCallbacks(execute=True):
            services.change_status(sub, S.CANCELLED, self.buyer, note="Changed my mind")
        refund = Refund.objects.get()
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertIn("Insufficient balance", refund.problem)
        self.assertEqual(services.farm_debt(self.farm_a), Decimal("0"))       # nothing owed until it is sent
        with mock.patch("orders.paystack._api", side_effect=FakePaystack()):
            services.run_timeouts()
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.PROCESSED)


class UnpaidCancelTests(SplitBase):
    def test_unpaid_order_cancelled_marks_payment_failed_without_refund(self):
        from orders.models import Payment
        f.cart_with(self.buyer, (self.maize, 1))
        with mock.patch("orders.paystack._api", side_effect=FakePaystack()):
            order = services.place_order(self.buyer, "collection", "card")
        sub = order.suborders.get()
        services.change_status(sub, S.CANCELLED, self.buyer, note="Changed my mind")
        order.payment.refresh_from_db()
        self.assertEqual(order.payment.status, Payment.Status.FAILED)
        self.assertFalse(Refund.objects.filter(suborder=sub).exists())

    def test_late_payment_after_cancel_is_recorded_and_refunded(self):
        from orders.models import Payment
        f.cart_with(self.buyer, (self.maize, 1))
        with mock.patch("orders.paystack._api", side_effect=FakePaystack()):
            order = services.place_order(self.buyer, "collection", "card")
        sub = order.suborders.get()
        services.change_status(sub, S.CANCELLED, None, note="Payment not received within 60 minutes")
        with mock.patch("orders.paystack._api", side_effect=FakePaystack()), self.captureOnCommitCallbacks(execute=True):
            services.card_gateway_paid(order.payment, "987")
        order.payment.refresh_from_db()
        self.assertIn(order.payment.status, (Payment.Status.PAID, Payment.Status.REFUNDED))
        self.assertTrue(Refund.objects.filter(suborder=sub).exists())


class SavedDetailsTests(SplitBase):
    """Bank details and the Paystack key are entered once and kept."""

    def test_key_from_environment_when_none_saved(self):
        from core.models import SiteSettings
        s = SiteSettings.load()
        s.paystack_secret_key = ""
        s.save()
        with mock.patch.dict("os.environ", {"PAYSTACK_SECRET_KEY": "sk_test_fromfile"}):
            self.assertEqual(paystack.config().secret_key, "sk_test_fromfile")

    def test_saving_key_sets_up_farms_with_saved_bank_details(self):
        farm = self.farm_b.farm
        farm.bank_code, farm.bank_account = "470010", "1234567890"
        farm.save()
        admin = f.user("admin")
        self.client.force_login(admin)
        with mock.patch("orders.paystack._api", side_effect=FakePaystack()):
            self.client.post(reverse("core:settings"), {"form": "gateway", "g-card_gateway": "paystack_test",
                                                        "g-paystack_secret_key": "sk_test_abc",
                                                        "g-card_payment_minutes": "60"})
        farm.refresh_from_db()
        self.assertTrue(farm.paystack_subaccount)
        self.assertEqual(farm.bank_account, "1234567890")

    def test_farmer_with_saved_bank_is_not_asked_again(self):
        farm = self.farm_b.farm
        farm.bank_code, farm.bank_account, farm.bank_name = "470010", "1234567890", "Capitec Bank"
        farm.payout_account_note = "Not set up: Paystack said: Account number is invalid (HTTP 400)"
        farm.save()
        self.client.force_login(self.farm_b)
        page = self.client.get(reverse("core:dashboard"))
        self.assertContains(page, "Your bank details are saved")
        self.assertContains(page, "account ending 7890")
        self.assertNotContains(page, "Add your bank account")
