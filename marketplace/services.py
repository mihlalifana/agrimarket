"""Stock movements.

All changes to Listing.quantity_available go through these functions. Each
one is a single conditional UPDATE statement, so the check ("is there
enough?") and the change ("take it") happen together inside the database.
Two buyers racing for the last item cannot both succeed: the second UPDATE
finds the condition false and changes nothing. A CHECK constraint
(quantity_available >= 0) backs this up at database level.
"""
from decimal import Decimal

from django.db.models import F
from django.db.models.functions import Round
from django.utils import timezone

from core.models import AuditLog
from core.services import audit, notify

from .models import Listing, StockAdjustment, fmt_qty


class OutOfStock(Exception):
    def __init__(self, listing, available):
        self.listing = listing
        self.available = available
        super().__init__(
            f"Only {fmt_qty(available)} {listing.unit} of {listing.name} "
            f"({listing.batch_number}) is available now.")


def take_stock(listing, quantity):
    """Atomically remove quantity from a listing, or raise OutOfStock."""
    quantity = Decimal(quantity)
    updated = (Listing.objects
               .filter(pk=listing.pk, status=Listing.Status.ACTIVE,
                       quantity_available__gte=quantity)
               .update(quantity_available=Round(F("quantity_available") - quantity, 3),
                       updated_at=timezone.now()))
    listing.refresh_from_db(fields=["quantity_available", "status", "low_stock_alerted"])
    if not updated:
        raise OutOfStock(listing, listing.quantity_available
                         if listing.status == Listing.Status.ACTIVE else Decimal("0"))
    check_low_stock(listing)
    return listing


def return_stock(listing, quantity):
    """Put quantity back, e.g. after a cancelled order."""
    Listing.objects.filter(pk=listing.pk).update(
        quantity_available=Round(F("quantity_available") + Decimal(quantity), 3),
        updated_at=timezone.now())
    listing.refresh_from_db(fields=["quantity_available", "low_stock_alerted"])
    if listing.low_stock_alerted and not listing.is_low_stock:
        Listing.objects.filter(pk=listing.pk).update(low_stock_alerted=False)
        listing.low_stock_alerted = False
    return listing


def check_low_stock(listing):
    """Notify the farmer once when stock reaches the threshold (FR-05)."""
    if listing.is_low_stock and not listing.low_stock_alerted:
        # Conditional update so only one request sends the alert.
        if Listing.objects.filter(pk=listing.pk, low_stock_alerted=False).update(low_stock_alerted=True):
            listing.low_stock_alerted = True
            notify(listing.farmer, f"Low stock: {listing.name}",
                   f"{listing.batch_number} has {listing.qty_display} left "
                   f"(threshold {fmt_qty(listing.low_stock_threshold)}).",
                   link=f"/farmer/listings/{listing.pk}/edit/")
    elif not listing.is_low_stock and listing.low_stock_alerted:
        Listing.objects.filter(pk=listing.pk).update(low_stock_alerted=False)
        listing.low_stock_alerted = False


def adjust_stock(listing, change, reason, user, note=""):
    """Manual adjustment with a reason (FR-05). Negative change = loss."""
    change = Decimal(change)
    was_empty = listing.quantity_available == 0
    if change < 0:
        take_stock(listing, -change)
    else:
        return_stock(listing, change)
        check_low_stock(listing)
        if was_empty:
            announce_listing(listing, restock=True)
    adj = StockAdjustment.objects.create(
        listing=listing, quantity_change=change, unit_price_at_time=listing.price,
        reason=reason, note=note, created_by=user)
    audit(user, AuditLog.Action.UPDATE, listing, field="quantity_available",
          old=listing.quantity_available - change, new=listing.quantity_available)
    return adj


def announce_listing(listing, restock=False):
    """Tell buyers about new or restocked stock: matching alerts and farm followers."""
    from .models import FavouriteFarm, StockAlert
    if not Listing.objects.sellable().filter(pk=listing.pk).exists():
        return 0
    sent = 0
    alerted = set()
    link = f"/listing/{listing.pk}/"
    farm = listing.farmer.farm
    for alert in StockAlert.objects.filter(commodity=listing.commodity_id, is_active=True).select_related("buyer"):
        if alert.matches(listing):
            notify(alert.buyer, f"{listing.commodity.name} is available",
                   f"{farm.farm_name} ({farm.town}) has {listing.qty_display} at "
                   f"R{listing.current_price}/{listing.unit}.", link=link, sms=True)
            alert.is_active = False
            alert.notified_at = timezone.now()
            alert.save(update_fields=["is_active", "notified_at"])
            alerted.add(alert.buyer_id)
            sent += 1
    for fav in FavouriteFarm.objects.filter(farmer=listing.farmer).select_related("buyer"):
        if fav.buyer_id in alerted:
            continue
        notify(fav.buyer, f"{'Restocked' if restock else 'New'} at {farm.farm_name}",
               f"{listing.name}: {listing.qty_display} at R{listing.current_price}/{listing.unit}.", link=link)
        sent += 1
    return sent
