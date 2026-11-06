"""Add SAMPLE spray products to the remedy catalogue so the system can be demonstrated.

These are NOT real labels: the registration numbers and waiting periods are made up for the demo
and every entry says so. Replace them with products checked against real labels before real use.

  python manage.py load_sample_remedies                      (entries wait for verification)
  python manage.py load_sample_remedies --verify-as admin@agrimarket.test
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from advisory.models import RegisteredRemedy, RemedyUse
from marketplace.models import Commodity

SOURCE = "Sample data for the demo - not a real label"
# registration no, trade name, active ingredient, [(crop code, PHI days, rate, targets)]
SAMPLES = [
    ("L5021", "Mancozeb 800 WP", "mancozeb",
     [("TOMATO", 7, "2 kg/ha", "Early blight"), ("POTATO", 7, "2 kg/ha", "Early blight"),
      ("ONION", 7, "2 kg/ha", "Downy mildew")]),
    ("L5034", "Chlorantraniliprole 200 SC", "chlorantraniliprole",
     [("TOMATO", 3, "150 ml/ha", "Tomato leafminer (Tuta absoluta)"), ("CABBAGE", 3, "100 ml/ha", "Diamondback moth")]),
    ("L5047", "Lambda-cyhalothrin 50 EC", "lambda-cyhalothrin",
     [("CABBAGE", 7, "200 ml/ha", "Aphids"), ("ONION", 7, "250 ml/ha", "Thrips"),
      ("SPINACH", 7, "200 ml/ha", "Aphids")]),
    ("L5052", "Copper oxychloride 850 WP", "copper oxychloride",
     [("POTATO", 7, "2.5 kg/ha", "Late blight (preventive)"), ("TOMATO", 7, "2.5 kg/ha", "Bacterial spot"),
      ("SPINACH", 7, "2.5 kg/ha", "Downy mildew")]),
    ("L5068", "Emamectin benzoate 48 EC", "emamectin benzoate",
     [("MAIZE-W", 14, "250 ml/ha", "Fall armyworm"), ("MAIZE-Y", 14, "250 ml/ha", "Fall armyworm")]),
]


class Command(BaseCommand):
    help = "Add sample (not real) spray products with label waiting periods, for demonstrations."

    def add_arguments(self, parser):
        parser.add_argument("--verify-as", default="", help="E-mail of the admin who marks them verified.")

    @transaction.atomic
    def handle(self, verify_as="", **opts):
        admin = None
        if verify_as:
            admin = User.objects.filter(email=verify_as.lower(), role=User.Role.ADMIN).first()
            if admin is None:
                raise CommandError(f"No administrator with e-mail {verify_as}.")
        added = uses = 0
        for reg, name, ai, crops in SAMPLES:
            r, made = RegisteredRemedy.objects.get_or_create(
                registration_no=reg, defaults={"trade_name": name, "active_ingredient": ai, "source": SOURCE})
            added += made
            for code, phi, rate, targets in crops:
                c = Commodity.objects.filter(code=code).first()
                if c is None:
                    continue
                _, new = RemedyUse.objects.get_or_create(remedy=r, commodity=c, defaults={
                    "phi_days": phi, "max_rate": rate, "targets": targets})
                uses += new
            if admin and r.status != RegisteredRemedy.Status.VERIFIED:
                r.status, r.verified_by, r.verified_at, r.note = "verified", admin, timezone.now(), ""
                if r.source.startswith("Taken from spray records"):
                    r.source = SOURCE
                r.save()
        self.stdout.write(self.style.SUCCESS(
            f"{added} sample product(s) and {uses} crop use(s) added"
            + (", verified." if admin else "; verify them under Remedies.")))
