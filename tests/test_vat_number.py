"""South African VAT numbers: 10 digits starting with 4, checked in the form, the model and the database."""
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.forms import FarmerRegistrationForm, normalise_vat, valid_vat
from accounts.models import FarmerProfile

from . import factories as f


def form_data(vat, registered=True):
    return {"first_name": "Thandi", "last_name": "Dube", "email": "thandi@farm.co.za", "phone": "0821234567",
            "province": "FS", "address": "Plot 4, Bethlehem", "password1": "Plaas2026x", "password2": "Plaas2026x",
            "popia_consent": "on", "farm_name": "Dube Plaas", "town": "Bethlehem",
            "is_vat_registered": "on" if registered else "", "vat_number": vat}


class VatNumberTests(TestCase):
    def test_typed_spaces_and_dashes_are_cleaned(self):
        self.assertEqual(normalise_vat("4123 456 789"), "4123456789")
        self.assertEqual(normalise_vat("VAT 4123-456-789"), "4123456789")
        form = FarmerRegistrationForm(data=form_data("4123 456 780"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["vat_number"], "4123456780")

    def test_wrong_length_prefix_or_letters_refused(self):
        for bad in ["412345678", "41234567890", "5123456789", "41234X6789", "0123456789"]:
            self.assertFalse(valid_vat(normalise_vat(bad)), bad)
            form = FarmerRegistrationForm(data=form_data(bad))
            self.assertFalse(form.is_valid(), bad)
            self.assertIn("vat_number", form.errors, bad)

    def test_number_needed_only_when_registered(self):
        self.assertIn("vat_number", FarmerRegistrationForm(data=form_data("")).errors)
        self.assertIn("is_vat_registered", FarmerRegistrationForm(data=form_data("4123456780", False)).errors)

    def test_model_and_database_refuse_bad_numbers(self):
        farm = f.farmer(vat=True).farm
        farm.vat_number = "41234X6789"
        with self.assertRaises(ValidationError):
            farm.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            FarmerProfile.objects.filter(pk=farm.pk).update(vat_number="512345678")
