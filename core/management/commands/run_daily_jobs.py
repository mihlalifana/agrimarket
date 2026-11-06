"""Scheduled jobs. Run every hour, e.g. with Windows Task Scheduler or cron:

    python manage.py run_daily_jobs
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from datetime import timedelta

from django.utils import timezone

from core.services import notify
from marketplace.models import Listing, StockAdjustment
from marketplace.services import adjust_stock
from orders.services import run_timeouts


class Command(BaseCommand):
    help = "Expiry alerts, expired stock write-off, order time-outs."

    def handle(self, *args, **opts):
        today = timezone.localdate()
        alerts = written_off = 0

        # FR-05: near-expiry alert, once per batch.
        for listing in Listing.objects.filter(status=Listing.Status.ACTIVE, expiry_alerted=False,
                                              expiry_date__gte=today, quantity_available__gt=0):
            if listing.is_near_expiry:
                notify(listing.farmer, f"{listing.name} expires {listing.expiry_date:%d %b}",
                       f"Batch {listing.batch_number} has {listing.qty_display} left.",
                       link=f"/farmer/listings/{listing.pk}/edit/", sms=True)
                Listing.objects.filter(pk=listing.pk).update(expiry_alerted=True)
                alerts += 1

        # FR-05 / FR-15: expired stock is hidden and written off as wastage.
        for listing in Listing.objects.filter(expiry_date__lt=today, quantity_available__gt=0):
            with transaction.atomic():
                adjust_stock(listing, -listing.quantity_available, StockAdjustment.Reason.EXPIRED,
                             listing.farmer, note="Automatic write-off after expiry date")
            notify(listing.farmer, f"{listing.name} expired",
                   f"Batch {listing.batch_number} was written off as wastage.")
            written_off += 1

        # Herds whose treatment withdrawal ends today: their listings are back on sale.
        from farm.models import Planting, Treatment
        from marketplace.services import announce_listing
        released = 0
        for t in Treatment.objects.filter(withdrawal_ends=today).select_related("herd"):
            notify(t.herd.farmer, f"{t.herd.name} can be sold again",
                   f"The withdrawal period for {t.remedy} ended today.", link=f"/farm/livestock/{t.herd.pk}/")
            for listing in t.herd.listings.filter(status=Listing.Status.ACTIVE):
                announce_listing(listing, restock=True)
                released += 1

        # Harvest reminders 7 days ahead.
        for p in Planting.objects.filter(status="growing", expected_harvest=today + timedelta(days=7)):
            notify(p.farmer, f"{p.commodity} on {p.field.name} due for harvest in 7 days",
                   "From that date it appears on Farm records → Harvest, where you record it and list it for sale.",
                   link=f"/farm/crops/{p.pk}/")
        # Due today: the crop now appears on the Harvest page.
        for p in Planting.objects.filter(status="growing", expected_harvest=today):
            notify(p.farmer, f"{p.commodity} on {p.field.name} is due for harvest",
                   "Open Farm records → Harvest, click Start harvesting, then record the harvest.",
                   link=f"/farm/harvest/?crop={p.pk}#record")

        cancelled, completed = run_timeouts()
        self.stdout.write(self.style.SUCCESS(
            f"{alerts} expiry alert(s), {written_off} batch(es) written off, {released} listing(s) "
            f"released from withdrawal, "
            f"{cancelled} order(s) auto-cancelled, {completed} auto-completed."))
