"""Load agricultural remedies into the catalogue from a CSV file (e.g. the Registrar's list saved as CSV).

Columns (header row required):
  registration_no, trade_name, active_ingredient, holder, crop, phi_days, rate, targets
One row per product and crop; a product with several crops has several rows. `crop` is the product
code (e.g. TOMATO) or name (e.g. Tomatoes) from the standard product list.

Imported products wait for verification by the administrator, unless --verify-as gives the e-mail of
the admin who checked the source list, e.g.:
  python manage.py import_remedies register.csv --source "Registrar list, Oct 2026" --verify-as admin@agrimarket.test
"""
import csv

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import User
from advisory.models import RegisteredRemedy, RemedyUse
from core.models import AuditLog
from core.services import audit
from marketplace.models import Commodity

COLUMNS = ["registration_no", "trade_name", "active_ingredient", "holder", "crop", "phi_days", "rate", "targets"]


class Command(BaseCommand):
    help = "Import agricultural remedies (Act 36) and their label pre-harvest intervals from a CSV file."

    def add_arguments(self, parser):
        parser.add_argument("csv_file")
        parser.add_argument("--source", default="", help="Where the list came from (shown on each entry).")
        parser.add_argument("--verify-as", default="", help="E-mail of the admin who checked the list.")

    @transaction.atomic
    def handle(self, csv_file, source="", verify_as="", **opts):
        admin = None
        if verify_as:
            admin = User.objects.filter(email=verify_as.lower(), role=User.Role.ADMIN).first()
            if admin is None:
                raise CommandError(f"No administrator with e-mail {verify_as}.")
        try:
            with open(csv_file, newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
        except OSError as e:
            raise CommandError(str(e)) from e
        if rows and set(COLUMNS) - set(rows[0].keys()):
            raise CommandError(f"Missing columns: {', '.join(sorted(set(COLUMNS) - set(rows[0].keys())))}")
        made = uses = skipped = 0
        for n, row in enumerate(rows, start=2):
            reg = (row["registration_no"] or "").strip().upper()
            crop = (row["crop"] or "").strip()
            commodity = Commodity.objects.filter(Q(code__iexact=crop) | Q(name__iexact=crop)).first()
            try:
                phi = int(row["phi_days"])
            except (TypeError, ValueError):
                phi = None
            if not reg or commodity is None or phi is None or not 0 <= phi <= 365:
                self.stderr.write(f"Line {n}: skipped (registration number, crop '{crop}' or PHI not valid).")
                skipped += 1
                continue
            remedy = RegisteredRemedy.objects.filter(registration_no=reg).first()
            if remedy is None:
                remedy = RegisteredRemedy(registration_no=reg, trade_name=row["trade_name"].strip()[:80],
                                          active_ingredient=row["active_ingredient"].strip()[:120],
                                          holder=(row.get("holder") or "").strip()[:120],
                                          source=(source or f"Imported from {csv_file}")[:200])
                if admin:
                    remedy.status, remedy.verified_by, remedy.verified_at = "verified", admin, timezone.now()
                try:
                    remedy.full_clean(exclude=["label_photo"])
                except ValidationError as e:
                    self.stderr.write(f"Line {n}: skipped ({'; '.join(e.messages)}).")
                    skipped += 1
                    continue
                remedy.save()
                audit(admin, AuditLog.Action.CREATE, remedy, field="remedy import", new=str(remedy))
                made += 1
            _, created = RemedyUse.objects.update_or_create(
                remedy=remedy, commodity=commodity,
                defaults={"phi_days": phi, "max_rate": (row.get("rate") or "").strip()[:60],
                          "targets": (row.get("targets") or "").strip()[:200]})
            uses += created
        self.stdout.write(self.style.SUCCESS(
            f"{made} product(s) added, {uses} crop use(s) added, {skipped} line(s) skipped. "
            + ("Marked verified." if admin else "They wait for verification under Remedies.")))
