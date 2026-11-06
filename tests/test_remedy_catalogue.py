"""Sprays use only verified catalogue products; the label PHI for the crop sets the harvest restriction."""
import csv
import os
import tempfile
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from advisory.models import AgronomistProfile, FarmLink, RegisteredRemedy, RemedyUse, SprayRecord
from farm.models import Field, Planting
from marketplace.models import Category, Commodity
from marketplace.standards import sync_standards

from . import factories as f

TODAY = timezone.localdate()
START = date(TODAY.year, 3, 1) if date(TODAY.year, 3, 1) <= TODAY else date(TODAY.year - 1, 3, 1)


class RemedyCatalogueTests(TestCase):
    def setUp(self):
        sync_standards(Category, Commodity)
        self.tomato = Commodity.objects.get(code="TOMATO")
        self.cabbage = Commodity.objects.get(code="CABBAGE")
        self.farm = f.farmer()
        field = Field.objects.create(farmer=self.farm, name="Tunnel", size_ha=2, irrigated=True)
        self.p = Planting.objects.create(farmer=self.farm, field=field, commodity=self.tomato, planted_on=START,
                                         area_ha=1, unit="crate", expected_yield=4000,
                                         expected_harvest=TODAY + timedelta(days=20))
        self.agro = f.user("agronomist")
        AgronomistProfile.objects.create(user=self.agro, body="sacnasp", registration_no="400100/15",
                                         qualification="BSc Agric", status="approved")
        FarmLink.objects.create(farmer=self.farm, agronomist=self.agro)
        self.mancozeb = f.remedy(self.tomato, phi_days=7)

    def post_spray(self, user, remedy, **extra):
        self.client.force_login(user)
        url = (reverse("advisory:spray_add_for", args=[self.farm.pk]) if user.role == "agronomist"
               else reverse("advisory:spray_add"))
        data = {"planting": self.p.pk, "remedy": remedy.pk, "target": "Early blight", "rate": "2 kg/ha",
                "applied_on": (TODAY - timedelta(days=1)).isoformat(), "applied_by": "Team"}
        data.update(extra)
        return self.client.post(url, data)

    def test_phi_and_registration_come_from_the_label(self):
        # a typed PHI or registration number is ignored: the form doesn't even accept them
        self.post_spray(self.agro, self.mancozeb, phi_days="0", registration_no="L9999")
        s = SprayRecord.objects.get()
        self.assertEqual(s.phi_days, 7)
        self.assertEqual(s.registration_no, "L5021")
        self.assertEqual(s.safe_from, TODAY - timedelta(days=1) + timedelta(days=7))
        self.assertEqual(s.recommended_by, self.agro)

    def test_off_label_crop_is_refused(self):
        cabbage_only = f.remedy(self.cabbage, phi_days=3, reg="L6001", name="Cabbage-only 50 EC")
        self.post_spray(self.agro, cabbage_only)
        self.assertFalse(SprayRecord.objects.exists())
        with self.assertRaises(ValidationError) as e:
            SprayRecord(planting=self.p, remedy=cabbage_only, target="x", rate="1", applied_on=TODAY).full_clean()
        self.assertIn("not registered for use on", str(e.exception))

    def test_unverified_or_withdrawn_product_is_refused(self):
        pending = f.remedy(self.tomato, phi_days=1, reg="L6002", name="New product", status="pending")
        self.post_spray(self.farm, pending)
        withdrawn = f.remedy(self.tomato, phi_days=1, reg="L6003", name="Old product", status="withdrawn")
        self.post_spray(self.farm, withdrawn)
        self.assertFalse(SprayRecord.objects.exists())

    def test_backend_refuses_save_without_catalogue_product(self):
        with self.assertRaises(ValidationError):
            SprayRecord.objects.create(planting=self.p, target="x", rate="1", applied_on=TODAY)

    def test_form_lists_only_verified_products_for_the_crop(self):
        f.remedy(self.cabbage, phi_days=3, reg="L6001", name="Cabbage-only 50 EC")
        f.remedy(self.tomato, phi_days=1, reg="L6002", name="Waiting product", status="pending")
        self.client.force_login(self.farm)
        page = self.client.get(reverse("farm:planting", args=[self.p.pk]))
        self.assertContains(page, "Mancozeb 800 WP (L5021)")
        self.assertNotContains(page, "Cabbage-only")
        self.assertNotContains(page, "Waiting product")

    def test_catalogue_change_does_not_rewrite_past_sprays(self):
        self.post_spray(self.farm, self.mancozeb)
        RemedyUse.objects.filter(remedy=self.mancozeb).update(phi_days=21)
        self.assertEqual(SprayRecord.objects.get().phi_days, 7)

    def test_agronomist_sends_product_and_admin_verifies(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        gif = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x00\x00\x00\x00\x00,"
               b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")
        self.client.force_login(self.agro)
        self.client.post(reverse("advisory:remedy_request"), {
            "r-trade_name": "Copper 850 WP", "r-registration_no": "l7001", "r-active_ingredient": "copper oxychloride",
            "r-source": "Label photo", "r-label_photo": SimpleUploadedFile("label.gif", gif, "image/gif"),
            "u-commodity": self.tomato.pk, "u-phi_days": "14"})
        r = RegisteredRemedy.objects.get(registration_no="L7001")
        self.assertEqual(r.status, "pending")
        self.post_spray(self.agro, r)                                   # not usable yet
        self.assertFalse(SprayRecord.objects.exists())
        admin = f.user("admin")
        self.client.force_login(admin)
        self.client.post(reverse("advisory:remedy_action", args=[r.pk]), {"action": "verify"})
        r.refresh_from_db()
        self.assertEqual(r.status, "verified")
        self.assertEqual(r.verified_by, admin)
        self.post_spray(self.agro, r)
        self.assertEqual(SprayRecord.objects.get().phi_days, 14)

    def test_only_admin_can_verify(self):
        r = f.remedy(self.tomato, reg="L6004", name="X", status="pending")
        self.client.force_login(self.agro)
        res = self.client.post(reverse("advisory:remedy_action", args=[r.pk]), {"action": "verify"})
        self.assertEqual(res.status_code, 403)
        r.refresh_from_db()
        self.assertEqual(r.status, "pending")

    def test_withdraw_needs_reason_and_blocks_new_sprays(self):
        admin = f.user("admin")
        self.client.force_login(admin)
        url = reverse("advisory:remedy_action", args=[self.mancozeb.pk])
        self.client.post(url, {"action": "withdraw"})
        self.mancozeb.refresh_from_db()
        self.assertEqual(self.mancozeb.status, "verified")
        self.client.post(url, {"action": "withdraw", "note": "Registration cancelled by the Registrar"})
        self.mancozeb.refresh_from_db()
        self.assertEqual(self.mancozeb.status, "withdrawn")
        self.post_spray(self.farm, self.mancozeb)
        self.assertFalse(SprayRecord.objects.exists())

    def test_csv_import(self):
        admin = f.user("admin")
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["registration_no", "trade_name", "active_ingredient", "holder", "crop", "phi_days", "rate",
                        "targets"])
            w.writerow(["L8001", "Product A", "abamectin", "Co", "TOMATO", "3", "50 ml/ha", "Red spider mite"])
            w.writerow(["L8001", "Product A", "abamectin", "Co", "Cabbage", "7", "", ""])
            w.writerow(["BAD", "Product B", "x", "", "TOMATO", "3", "", ""])
            name = fh.name
        try:
            call_command("import_remedies", name, "--source", "Test list", "--verify-as", admin.email)
        finally:
            os.unlink(name)
        r = RegisteredRemedy.objects.get(registration_no="L8001")
        self.assertEqual(r.status, "verified")
        self.assertEqual(sorted(r.uses.values_list("phi_days", flat=True)), [3, 7])
        self.assertFalse(RegisteredRemedy.objects.filter(trade_name="Product B").exists())
