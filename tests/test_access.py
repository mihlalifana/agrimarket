from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from core.models import AuditLog
from core.services import audit

from . import factories as f


class AccessTests(TestCase):
    def test_farmer_cannot_edit_another_farmers_listing(self):
        a, b = f.farmer(), f.farmer()
        listing = f.listing(a)
        self.client.login(email=b.email, password=f.PW)
        r = self.client.get(reverse("marketplace:listing_edit", args=[listing.pk]))
        self.assertEqual(r.status_code, 403)

    def test_buyer_cannot_open_admin_pages(self):
        buyer = f.user("buyer")
        self.client.login(email=buyer.email, password=f.PW)
        for name in ("accounts:user_list", "core:audit_log", "finance:income", "core:settings"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)

    def test_unverified_farmer_cannot_list(self):
        farmer = f.farmer(status="pending")
        self.client.login(email=farmer.email, password=f.PW)
        self.assertEqual(self.client.get(reverse("marketplace:listing_create")).status_code, 403)

    def test_unverified_farmers_listings_hidden(self):
        farmer = f.farmer(status="pending")
        f.listing(farmer, name="Hidden maize")
        self.assertNotContains(self.client.get("/"), "Hidden maize")

    def test_lockout_after_five_failures(self):
        u = f.user("buyer")
        url = reverse("accounts:login")
        for _ in range(5):
            self.client.post(url, {"email": u.email, "password": "wrong"})
        r = self.client.post(url, {"email": u.email, "password": f.PW})
        self.assertContains(r, "locked")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_registration_requires_popia_consent(self):
        r = self.client.post(reverse("accounts:register_buyer"), {
            "first_name": "A", "last_name": "B", "email": "a@b.za", "phone": "0821234567",
            "province": "GP", "password1": "farm2026x", "password2": "farm2026x"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "required")

    def test_audit_log_cannot_be_changed(self):
        u = f.user("admin")
        audit(u, AuditLog.Action.UPDATE, u, field="x", old="1", new="2")
        entry = AuditLog.objects.get()
        entry.new_value = "tampered"
        with self.assertRaises(Exception):
            entry.save()
        with self.assertRaises(Exception):
            entry.delete()

    def test_cart_rejects_more_than_available(self):
        farm, buyer = f.farmer(), f.user("buyer")
        l = f.listing(farm, qty="6")
        self.client.login(email=buyer.email, password=f.PW)
        self.client.post(reverse("marketplace:cart_add", args=[l.pk]), {"quantity": "8"})
        self.assertFalse(buyer.cart.items.exists())


class CheckoutViewTests(TestCase):
    def setUp(self):
        f.rates()
        self.farm, self.buyer = f.farmer(), f.user("buyer")
        self.l = f.listing(self.farm, qty="5")
        f.cart_with(self.buyer, (self.l, 1))
        self.client.login(email=self.buyer.email, password=f.PW)
        self.data = {"delivery_method": "delivery", "delivery_address": "1 Rd", "delivery_province": "GP",
                     "payment_method": "card", "card_number": "4111111111111111", "card_expiry": "12/30",
                     "card_cvv": "123"}

    def test_cart_and_checkout_are_one_page(self):
        self.assertRedirects(self.client.get(reverse("orders:checkout")), reverse("marketplace:cart"))
        r = self.client.get(reverse("marketplace:cart"))
        self.assertContains(r, "Place order")

    def test_errors_stay_on_cart_page_and_card_not_shown(self):
        from orders.models import Order
        r = self.client.post(reverse("orders:checkout"), {**self.data, "card_number": "1234"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Cart and checkout")
        self.assertFalse(Order.objects.exists())
        r = self.client.post(reverse("orders:checkout"), self.data)
        self.assertEqual(Order.objects.count(), 1)

    def test_same_form_cannot_be_saved_twice(self):
        from orders.models import Order
        data = {**self.data, "_ftoken": "abc-123"}
        self.client.post(reverse("orders:checkout"), data)
        f.cart_with(self.buyer, (self.l, 1))  # even with a full cart again
        r = self.client.post(reverse("orders:checkout"), data, HTTP_REFERER="/cart/")
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(r.status_code, 302)


class OrderRegisterTests(TestCase):
    def setUp(self):
        self.a, self.b = f.farmer(), f.farmer()
        self.buyer = f.user("buyer", buyer_type="retail")
        la, lb = f.listing(self.a, qty="10"), f.listing(self.b, qty="10")
        f.cart_with(self.buyer, (la, 2), (lb, 3))
        from orders import services
        self.order = services.place_order(self.buyer, "collection", "card", card_number="4111111111111111")

    def test_farmer_sees_only_own_lines(self):
        self.client.login(email=self.a.email, password=f.PW)
        rows = self.client.get(reverse("orders:register") + "?export=csv").content.decode().strip().splitlines()
        self.assertEqual(len(rows), 2)
        self.assertNotIn(self.b.farm.farm_name, rows[1])

    def test_buyer_cannot_open_register(self):
        self.client.login(email=self.buyer.email, password=f.PW)
        self.assertEqual(self.client.get(reverse("orders:register")).status_code, 403)


class AdminScopeTests(TestCase):
    """The administrator manages accounts and platform setup and sees AgriMarket's income totals only."""

    def test_admin_has_no_sales_pages(self):
        admin = f.user("admin")
        self.client.force_login(admin)
        for name in ("orders:dispute_list", "orders:register", "finance:reports", "finance:earnings",
                     "orders:rfq_list", "orders:farmer_orders"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)
        self.assertEqual(self.client.get(reverse("finance:income")).status_code, 200)
        page = self.client.get(reverse("core:dashboard"))
        self.assertContains(page, "Farms to verify")
        self.assertNotContains(page, "Sales this month")
        for name in ("accounts:user_list", "accounts:farmer_queue", "core:settings", "core:audit_log"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_accountant_role_no_longer_exists(self):
        from accounts.models import User
        self.assertNotIn("accountant", User.Role.values)

    def test_admin_unlocks_account(self):
        from datetime import timedelta
        from django.utils import timezone
        admin = f.user("admin")
        locked = f.user("buyer")
        locked.failed_login_count, locked.locked_until = 5, timezone.now() + timedelta(minutes=10)
        locked.save()
        self.client.force_login(admin)
        self.assertContains(self.client.get(reverse("core:dashboard")), "Unlock")
        self.client.post(reverse("accounts:unlock", args=[locked.pk]))
        locked.refresh_from_db()
        self.assertFalse(locked.is_locked)


class PrivateFileTests(TestCase):
    """Dispute photos can only be opened by the buyer and the farm involved (NFR-06)."""

    def test_dispute_photo_is_private(self):
        import shutil, tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from orders import services
        from orders.models import SubOrder
        S = SubOrder.Status
        tmp = tempfile.mkdtemp()
        try:
            with override_settings(MEDIA_ROOT=tmp):
                farm = f.farmer()
                buyer = f.user("buyer")
                listing = f.listing(farm, qty="10", price="100")
                f.cart_with(buyer, (listing, 1))
                order = services.place_order(buyer, "collection", "card", card_number="4111111111111111")
                sub = order.suborders.get()
                services.change_status(sub, S.PACKED, farm)
                services.change_status(sub, S.DELIVERED, farm, note="Collected by T. Buyer")
                dispute = services.raise_dispute(sub, buyer, "Bags were wet",
                                                 photo=SimpleUploadedFile("wet.jpg", b"\xff\xd8\xff test", "image/jpeg"))
                url = "/media/" + dispute.photo.name
                self.client.force_login(buyer)
                self.assertEqual(self.client.get(url).status_code, 200)            # the buyer who complained
                self.client.force_login(farm)
                self.assertEqual(self.client.get(url).status_code, 200)            # the farm involved
                self.client.force_login(f.user("buyer"))
                self.assertEqual(self.client.get(url).status_code, 403)            # another buyer
                self.client.force_login(f.user("admin"))
                self.assertEqual(self.client.get(url).status_code, 403)            # the administrator
                self.client.logout()
                self.assertNotEqual(self.client.get(url).status_code, 200)         # anonymous
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PasswordResetTests(TestCase):
    """Forgot password: e-mailed link, new password works, link can't be reused, unknown e-mails reveal nothing (FR-01)."""

    def test_reset_flow(self):
        import re
        from django.core import mail
        from django.test import override_settings
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            u = f.user("buyer")
            u.set_password("OldPass2026!")
            u.save()
            r = self.client.post(reverse("accounts:password_reset"), {"email": u.email})
            self.assertRedirects(r, reverse("accounts:password_reset_done"))
            self.assertEqual(len(mail.outbox), 1)
            self.assertEqual(mail.outbox[0].subject, "Reset your AgriMarket password")
            path = re.search(r"https?://[^/\s]+(/accounts/reset/\S+)", mail.outbox[0].body).group(1)
            r = self.client.get(path)                                   # opens the form
            r = self.client.post(r["Location"], {"new_password1": "NewPass2026!", "new_password2": "NewPass2026!"})
            self.assertRedirects(r, reverse("accounts:login"))
            u.refresh_from_db()
            self.assertTrue(u.check_password("NewPass2026!"))
            self.assertEqual(self.client.get(path).status_code, 200)    # used link shows "invalid", sets nothing
            self.assertNotIn("new_password1", self.client.get(path).content.decode())
            self.client.post(reverse("accounts:password_reset"), {"email": "nobody@nowhere.test"})
            self.assertEqual(len(mail.outbox), 1)                       # no e-mail, same "check your e-mail" page


class ComplaintPageTests(TestCase):
    """Only the farm that supplied the order answers a complaint; the buyer reads the answer."""

    def test_only_the_farm_answers(self):
        from orders import services
        from orders.models import Dispute, SubOrder
        S = SubOrder.Status
        farm, other, buyer = f.farmer(), f.farmer(), f.user("buyer")
        listing = f.listing(farm, qty="10", price="100")
        f.cart_with(buyer, (listing, 2))
        sub = services.place_order(buyer, "collection", "card", card_number="4111111111111111").suborders.get()
        services.change_status(sub, S.PACKED, farm)
        services.change_status(sub, S.DELIVERED, farm, note="Collected by T. Buyer")
        dispute = services.raise_dispute(sub, buyer, "One bag was torn")
        url = reverse("orders:dispute_detail", args=[dispute.pk])
        self.client.force_login(other)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(buyer)
        self.assertContains(self.client.get(url), "Waiting for the farm")
        self.client.post(url, {"outcome": "rejected", "note": "I refuse"})      # a buyer can't answer it
        self.assertEqual(Dispute.objects.get().status, "open")
        self.client.force_login(farm)
        self.assertContains(self.client.get(reverse("orders:dispute_list")), "Waiting for your answer")
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, {"outcome": "full", "note": "Sorry, full refund."})
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, "full")
        self.assertEqual(dispute.refund_amount, Decimal("200.00"))
        self.assertTrue(buyer.notifications.filter(title__startswith="Answer to your complaint").exists())
