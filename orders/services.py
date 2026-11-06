"""Order business rules: checkout, status changes, payments, refunds, disputes."""
from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core import clock

from core.models import AuditLog, SiteSettings
from core.services import audit, notify
from marketplace.models import Cart, Listing, Unit, fmt_qty, money
from marketplace.services import OutOfStock, return_stock, take_stock

from .models import (DeliveryRate, Dispute, GatewayEvent, Order, OrderItem, Payment, Refund,
                     StatusChange, SubOrder)

S = SubOrder.Status


def available_distributors(province, date=None, slot="", exclude_suborder=None):
    """Active distributors in a province who have no open job in the requested slot."""
    from accounts.models import User

    drivers = User.objects.filter(role=User.Role.DISTRIBUTOR, is_active=True, province=province)
    if date and slot:
        busy = SubOrder.objects.filter(
            distributor__isnull=False, scheduled_date=date, time_slot=slot,
            status__in=SubOrder.OPEN_STATUSES,
        )
        if exclude_suborder is not None:
            busy = busy.exclude(pk=exclude_suborder.pk)
        drivers = drivers.exclude(pk__in=busy.values("distributor_id"))
    return drivers.order_by("first_name", "last_name", "email")


class CheckoutError(Exception):
    pass


# ---------------------------------------------------------------- payments
TEST_DECLINED_CARD = "4000000000000002"


def luhn_ok(number):
    digits = [int(d) for d in number]
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def card_brand(number):
    if number.startswith("4"):
        return "Visa"
    if number[:2] in {"51", "52", "53", "54", "55"} or "2221" <= number[:4] <= "2720":
        return "Mastercard"
    if number[:2] in {"34", "37"}:
        return "American Express"
    return "Card"


def simulate_card_charge(card_number):
    """Built-in card check used only to record past (backdated) card sales (FR-08). Returns (last4, brand)."""
    number = "".join(ch for ch in (card_number or "") if ch.isdigit())
    if not 13 <= len(number) <= 19 or not luhn_ok(number):
        raise CheckoutError("Card number is not valid. Check the digits and try again.")
    if number == TEST_DECLINED_CARD:
        raise CheckoutError("The card was declined by the bank (test decline card).")
    return number[-4:], card_brand(number)


# ---------------------------------------------------------------- pricing
def delivery_fee(province, tons):
    rate = DeliveryRate.objects.filter(province=province).first()
    if rate is None:
        raise CheckoutError("Delivery to this province is not available yet. "
                            "Choose farm collection or another province.")
    return money(rate.base_fee + rate.per_ton_fee * tons)


def quote_lines(lines, delivery_method=None, province=None):
    """Totals per farmer for a list of lines.

    Each line is a dict with listing, quantity and optionally unit_price (a
    fixed price, e.g. from an accepted quote) and item (the cart item).
    """
    rules = SiteSettings.load()
    groups = OrderedDict()
    for line in sorted(lines, key=lambda l: (l["listing"].farmer_id, l["listing"].pk)):
        groups.setdefault(line["listing"].farmer, []).append(line)

    result = {"groups": [], "goods": Decimal("0"), "vat": Decimal("0"),
              "delivery": Decimal("0"), "errors": []}
    for farmer, items in groups.items():
        goods = Decimal("0")
        tons = Decimal("0")
        out = []
        for line in items:
            listing, qty = line["listing"], line["quantity"]
            unit_price = line.get("unit_price") or listing.unit_price_for(qty)
            line_total = money(unit_price * qty)
            goods += line_total
            tons += qty * listing.tons_per_unit
            out.append({**line, "unit_price": unit_price, "line_total": line_total})
        vat_rate = rules.vat_rate if farmer.farm.is_vat_registered else Decimal("0")
        vat = money(goods * vat_rate)
        fee = Decimal("0")
        if delivery_method == Order.DeliveryMethod.DELIVERY and province:
            try:
                fee = delivery_fee(province, tons)
            except CheckoutError as e:
                result["errors"].append(str(e))
        result["groups"].append({"farmer": farmer, "lines": out, "goods": goods,
                                 "vat_rate": vat_rate, "vat": vat, "delivery": fee, "tons": tons})
        result["goods"] += goods
        result["vat"] += vat
        result["delivery"] += fee
    result["total"] = result["goods"] + result["vat"] + result["delivery"]
    return result


def cart_lines(cart):
    return [{"listing": i.listing, "quantity": i.quantity, "item": i}
            for i in cart.items.select_related("listing__farmer__farm", "listing__livestock")]


def quote_cart(cart, delivery_method=None, province=None):
    """Totals shown in the cart and charged at checkout, grouped per farmer."""
    return quote_lines(cart_lines(cart), delivery_method, province)


def validate_line(listing, quantity, check_min_order=True, on=None):
    """Rules every cart line must pass (FR-07, FR-05).

    `on` is the order date: a past sale is checked against the stock as it was then.
    """
    quantity = Decimal(quantity)
    if not Listing.objects.sellable(on=on).filter(pk=listing.pk).exists():
        if listing.withdrawal_until:
            raise CheckoutError(f"{listing.name} is under a treatment withdrawal period until "
                                f"{listing.withdrawal_until:%d %b %Y} and cannot be sold yet.")
        raise CheckoutError(f"{listing.name} is no longer for sale.")
    if quantity <= 0:
        raise CheckoutError("Quantity must be more than zero.")
    if listing.unit == Unit.HEAD and quantity != quantity.to_integral_value():
        raise CheckoutError("Livestock is sold in whole head.")
    if check_min_order and quantity < listing.min_order_qty:
        raise CheckoutError(f"Minimum order for {listing.name} is "
                            f"{fmt_qty(listing.min_order_qty)} {listing.unit}.")
    if quantity > listing.quantity_available:
        raise CheckoutError(f"Only {listing.qty_display} of {listing.name} available.")


# ---------------------------------------------------------------- checkout
def check_order_date(order_date, lines):
    """Order date must be inside the records window and not before the stock was harvested."""
    from django.core.exceptions import ValidationError
    from core.models import validate_record_date
    try:
        validate_record_date(order_date, "Order date")
    except ValidationError as e:
        raise CheckoutError(e.messages[0]) from e
    for line in lines:
        l = line["listing"]
        if l.harvest_date and order_date < l.harvest_date:
            raise CheckoutError(f"{l.name} ({l.batch_number}) was only harvested on {l.harvest_date:%d %b %Y}; "
                                f"the order date cannot be earlier.")


def _create_order(buyer, lines, delivery_method, payment_method, delivery_address,
                  delivery_province, card_number, check_min_order=True, order_date=None):
    """Shared by checkout and accepted quotes. Must run inside a transaction.

    A sale from an earlier day is recorded as if it happened on that day: every
    timestamp (order, history, payment, invoice, notifications) carries that date.
    """
    today = timezone.localdate()
    order_date = order_date or today
    if order_date != today and not SiteSettings.load().allow_backdated_orders:
        raise CheckoutError("Choosing an earlier order date is switched off.")
    check_order_date(order_date, lines)
    late = order_date < today
    outer = clock.override()
    if late:
        # keep a moment chosen by the caller for that same day, otherwise pick one
        moment = outer if outer and timezone.localtime(outer).date() == order_date else clock.at_date(order_date)
    else:
        moment = None
    with clock.as_of(moment):
        return _build_order(buyer, lines, delivery_method, payment_method, delivery_address,
                            delivery_province, card_number, check_min_order, order_date, late)


def _build_order(buyer, lines, delivery_method, payment_method, delivery_address,
                 delivery_province, card_number, check_min_order, order_date, late):
    if delivery_method == Order.DeliveryMethod.DELIVERY and not (delivery_address and delivery_province):
        raise CheckoutError("Enter a delivery address and province.")
    if payment_method != Order.PaymentMethod.CARD:
        raise CheckoutError("Only card payments are accepted.")
    card_last4 = brand = ""
    # A card sale from an earlier day was already paid, so it is recorded with the built-in card.
    # A card sale today goes to Paystack when it is switched on in Settings.
    via_gateway = (payment_method == Order.PaymentMethod.CARD and not late and uses_gateway())
    if payment_method == Order.PaymentMethod.CARD and not via_gateway:
        card_last4, brand = simulate_card_charge(card_number)

    quote = quote_lines(lines, delivery_method, delivery_province)
    if quote["errors"]:
        raise CheckoutError(quote["errors"][0])
    if via_gateway:
        missing = [g["farmer"].farm.farm_name for g in quote["groups"] if not g["farmer"].farm.paystack_subaccount]
        if missing:
            raise CheckoutError(f"{', '.join(missing)} can't receive card payments yet (no bank account set up "
                                f"with Paystack). Remove its items or try again later.")

    is_delivery = delivery_method == Order.DeliveryMethod.DELIVERY
    order = Order.objects.create(
        buyer=buyer, order_date=order_date, recorded_late=late,
        delivery_method=delivery_method, payment_method=payment_method,
        delivery_address=delivery_address if is_delivery else "",
        delivery_province=delivery_province if is_delivery else "")

    for group in quote["groups"]:
        farmer = group["farmer"]
        sub = SubOrder.objects.create(
            order=order, farmer=farmer, goods_subtotal=group["goods"], vat_amount=group["vat"],
            delivery_fee=group["delivery"],
            farmer_vat_number=farmer.farm.vat_number if farmer.farm.is_vat_registered else "")
        for line in group["lines"]:
            listing, qty = line["listing"], line["quantity"]
            validate_line(listing, qty, check_min_order=check_min_order, on=order_date)
            try:
                take_stock(listing, qty)
            except OutOfStock as e:
                raise CheckoutError(str(e)) from e
            OrderItem.objects.create(
                suborder=sub, listing=listing, product_name=listing.name,
                product_code=listing.commodity.code, grade=listing.grade,
                batch_number=listing.batch_number, unit=listing.unit,
                unit_price=line["unit_price"], quantity=qty, vat_rate=group["vat_rate"],
                line_total=line["line_total"])
        StatusChange.objects.create(suborder=sub, to_status=S.PENDING, changed_by=buyer,
                                    note="Order placed")

    order.goods_total = quote["goods"]
    order.vat_total = quote["vat"]
    order.delivery_total = quote["delivery"]
    order.grand_total = quote["total"]
    order.save(update_fields=["goods_total", "vat_total", "delivery_total", "grand_total"])

    paid_now = payment_method == Order.PaymentMethod.CARD and not via_gateway
    Payment.objects.create(
        order=order, method=payment_method, amount=order.grand_total,
        status=Payment.Status.PAID if paid_now else Payment.Status.PENDING,
        gateway=via_gateway if via_gateway else ("simulated" if paid_now else ""),
        card_last4=card_last4, card_brand=brand, confirmed_at=clock.now() if paid_now else None)
    audit(buyer, AuditLog.Action.CREATE, order, new=f"total R{order.grand_total}, order date {order_date:%Y-%m-%d}")

    notify(buyer, f"Order {order.number} placed", f"Total R{order.grand_total}.",
           link=f"/orders/{order.pk}/", email=True)
    # No approval step: a card already paid (simulated card, past sale) is confirmed straight away.
    # A Paystack payment is confirmed when Paystack reports it (see card_gateway_paid).
    if not via_gateway:
        _paid_direct(order)
        confirm_order(order)
    else:
        minutes = SiteSettings.load().card_payment_minutes
        for sub in order.suborders.select_related("farmer"):
            notify(sub.farmer, f"New order {order.number} (awaiting card payment)",
                   f"{sub.items.count()} item(s), R{sub.total}. Stock is reserved; you will be told when "
                   f"the payment is confirmed. Unpaid orders are cancelled after {minutes} minutes.",
                   link=f"/orders/sub/{sub.pk}/")
    return order


def card_available():
    """False while an online gateway is chosen but its account details are not filled in yet."""
    from core.models import SiteSettings
    s = SiteSettings.load()
    if s.card_gateway.startswith("paystack"):
        return bool(s.paystack_secret_key.strip())
    return True


def uses_gateway():
    """'paystack' when card payments go to Paystack, else '' (simulated card)."""
    from core.models import SiteSettings
    mode = SiteSettings.load().card_gateway
    return "paystack" if mode.startswith("paystack") else ""


def setup_payout_account(farm):
    """Create or update the farm's Paystack subaccount so card money goes straight to its bank.

    Returns (ok, message). Does nothing (None, "") unless Paystack is the card gateway, the
    farm is verified and its bank details are complete.
    """
    from core.models import SiteSettings
    from . import paystack
    cfg = paystack.config()
    if not (cfg.enabled and cfg.secret_key) or farm.status != "approved":
        return None, ""
    if not (farm.bank_code and farm.bank_account):
        farm.payout_account_note = "Add your bank and account number to receive card payments directly."
        farm.save(update_fields=["payout_account_note"])
        return False, farm.payout_account_note
    try:
        code = paystack.save_subaccount(farm, SiteSettings.load().commission_rate, cfg)
    except paystack.PaystackError as e:
        farm.payout_account_note = f"Not set up: {e}"[:300]
        farm.save(update_fields=["payout_account_note"])
        return False, farm.payout_account_note
    old = farm.paystack_subaccount
    farm.paystack_subaccount = code
    farm.payout_account_note = "Ready: Paystack pays your share of card payments straight into your bank account."
    farm.save(update_fields=["paystack_subaccount", "payout_account_note"])
    if old != code:
        audit(None, AuditLog.Action.UPDATE, farm, field="paystack_subaccount", old=old, new=code)
    return True, farm.payout_account_note


def split_of_reference(payment, reference):
    """{sub-order id: (farm share, amount kept back)} sent to Paystack with this payment reference."""
    for e in payment.events.filter(kind="redirect").order_by("-pk"):
        d = e.data if isinstance(e.data, dict) else {}
        if d.get("reference") == reference:
            kept = d.get("kept") or {}
            return {int(k): (Decimal(v), Decimal(kept.get(k, "0"))) for k, v in (d.get("split") or {}).items()}
    return {}


def _record_split(order, split):
    """Mark the sub-orders whose share Paystack paid straight to the farm."""
    for sub in order.suborders.filter(pk__in=list(split)):
        share, kept = split[sub.pk]
        SubOrder.objects.filter(pk=sub.pk).update(paid_to_farm=True, farm_share=share, recovered=kept)


def _paid_direct(order):
    """A card sale paid without the gateway split (simulated card, past sale): the farm is treated as
    paid its share straight away, like a split payment."""
    rate = SiteSettings.load().commission_rate
    for sub in order.suborders.all():
        share = sub.goods_subtotal + sub.vat_amount - money(sub.goods_subtotal * rate)
        SubOrder.objects.filter(pk=sub.pk).update(paid_to_farm=True, farm_share=share)


def farm_debt(farmer):
    """What a farm still owes AgriMarket for its part of refunds AgriMarket already sent back to buyers.

    It is kept back from the farm's next card sales (SubOrder.recovered).
    """
    from django.db.models import Sum
    owed = Refund.objects.filter(suborder__farmer=farmer, status=Refund.Status.PROCESSED,
                                 suborder__paid_to_farm=True).aggregate(t=Sum("farm_amount"))["t"] or Decimal("0")
    repaid = SubOrder.objects.filter(farmer=farmer, paid_to_farm=True).aggregate(t=Sum("recovered"))["t"] or Decimal("0")
    return max(owed - repaid, Decimal("0"))


@transaction.atomic
def card_gateway_paid(payment, pf_payment_id, note="", split=None):
    """The gateway says the card payment went through: mark it paid and confirm the order (once).

    `split` = {sub-order id: share} when Paystack paid those farms directly.
    """
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status == Payment.Status.PAID:
        return False
    # FAILED = the order was cancelled before paying; a late payment is still recorded and refunded below.
    if payment.status not in (Payment.Status.PENDING, Payment.Status.FAILED) or not payment.online:
        raise TransitionError("This payment is not waiting for an online card payment.")
    name = payment.gateway_name
    prefix = "PS"
    note = note or f"Paid on {name}"
    order = payment.order
    if order.suborders.filter(status=S.PENDING).count() == 0:
        # paid after the order was already cancelled: keep the money on record for a refund
        payment.status, payment.reference = Payment.Status.PAID, f"{prefix}{pf_payment_id}"[:40]
        payment.confirmed_at = clock.now()
        payment.save(update_fields=["status", "reference", "confirmed_at"])
        _record_split(order, split or {})
        for sub in order.suborders.filter(status=S.CANCELLED):
            if not sub.refunds.exists():
                create_refund(sub, sub.goods_subtotal + sub.vat_amount, True, None,
                              f"Paid on {name} after the order had been cancelled")
        return False
    payment.status = Payment.Status.PAID
    payment.reference = f"{prefix}{pf_payment_id}"[:40]
    payment.card_brand = name
    payment.confirmed_at = clock.now()
    payment.save(update_fields=["status", "reference", "card_brand", "confirmed_at"])
    _record_split(order, split or {})
    audit(None, AuditLog.Action.STATUS, payment, field="status", old="pending", new=f"paid via {name} {pf_payment_id}")
    notify(order.buyer, f"Payment received for {order.number}", f"R{payment.amount} paid by card on {name}.",
           link=f"/orders/{order.pk}/", email=True)
    confirm_order(order, note=note)
    return True


def confirm_order(order, user=None, note=""):
    """Confirm every sub-order still awaiting payment and tell the farmers to pack."""
    for sub in order.suborders.filter(status=S.PENDING).select_related("farmer"):
        change_status(sub, S.APPROVED, user, note=note or (
            "Cash on delivery" if order.payment_method == Order.PaymentMethod.COD else "Payment received"))
        notify(sub.farmer, f"New order {order.number} — please pack",
               f"{sub.items.count()} item(s), R{sub.total}. "
               + ("Arrange the driver when it is packed." if sub.is_delivery
                  else "The buyer will collect from the farm."),
               link=f"/orders/sub/{sub.pk}/", sms=True)


@transaction.atomic
def place_order(buyer, delivery_method, payment_method, delivery_address="",
                delivery_province="", card_number="", order_date=None):
    """Create the order, take stock and record payment — all or nothing (NFR-04).

    If any line cannot be filled (e.g. someone else bought the last item a
    moment ago) the whole transaction is rolled back: no order, no payment,
    no stock change.
    """
    cart = Cart.objects.filter(buyer=buyer).first()
    if cart is None or not cart.items.exists():
        raise CheckoutError("Your cart is empty.")
    order = _create_order(buyer, cart_lines(cart), delivery_method, payment_method,
                          delivery_address, delivery_province, card_number, order_date=order_date)
    cart.items.all().delete()
    return order


# ---------------------------------------------------------------- buyer tools
def reorder(order, buyer):
    """Copy a past order's items into the cart. Returns (added, skipped) names."""
    from marketplace.models import CartItem
    cart, _ = Cart.objects.get_or_create(buyer=buyer)
    added, skipped = [], []
    for item in OrderItem.objects.filter(suborder__order=order).select_related("listing"):
        listing = item.listing
        if not Listing.objects.sellable().filter(pk=listing.pk).exists():
            skipped.append(f"{item.product_name} (no longer available)")
            continue
        qty = min(item.quantity, listing.quantity_available)
        if listing.unit == Unit.HEAD:
            qty = qty.to_integral_value()
        if qty < listing.min_order_qty:
            skipped.append(f"{item.product_name} (only {listing.qty_display} left)")
            continue
        CartItem.objects.update_or_create(cart=cart, listing=listing, defaults={"quantity": qty})
        added.append(item.product_name + ("" if qty == item.quantity else f" (reduced to {fmt_qty(qty)})"))
    return added, skipped


def rfq_farmers(rfq):
    """Verified farmers who sell or produce the requested commodity."""
    from accounts.models import User
    from farm.models import Herd, Planting
    ids = set(Listing.objects.filter(commodity=rfq.commodity).values_list("farmer_id", flat=True))
    ids |= set(Planting.objects.filter(commodity=rfq.commodity).values_list("farmer_id", flat=True))
    ids |= set(Herd.objects.filter(commodity=rfq.commodity).values_list("farmer_id", flat=True))
    return User.objects.filter(pk__in=ids, is_active=True, farm__status="approved")


def open_rfq(rfq):
    sent = 0
    for farmer in rfq_farmers(rfq):
        notify(farmer, f"Quote request {rfq.number}: {rfq.commodity}",
               f"{fmt_qty(rfq.quantity)} {rfq.unit}{' ' + rfq.grade if rfq.grade else ''} needed by "
               f"{rfq.needed_by:%d %b %Y}, delivered to {rfq.get_delivery_province_display()}.",
               link=f"/orders/quotes/{rfq.pk}/", sms=True)
        sent += 1
    return sent


def submit_quote(rfq, farmer, listing, unit_price, note=""):
    from .models import Quote, QuoteRequest
    if rfq.status != QuoteRequest.Status.OPEN:
        raise TransitionError("This request is no longer open.")
    if listing.farmer_id != farmer.pk or listing.commodity_id != rfq.commodity_id:
        raise TransitionError("Choose one of your listings for this product.")
    if listing.unit != rfq.unit:
        raise TransitionError(f"The buyer wants the quantity in {rfq.unit}; choose a listing sold per {rfq.unit}.")
    if rfq.grade and listing.grade != rfq.grade:
        raise TransitionError(f"The buyer asked for grade {rfq.grade}.")
    if not Listing.objects.sellable().filter(pk=listing.pk).exists():
        raise TransitionError("That listing is not available for sale.")
    if listing.quantity_available < Decimal(rfq.quantity):
        raise TransitionError(f"That listing only has {listing.qty_display}; the buyer needs "
                              f"{fmt_qty(rfq.quantity)} {rfq.unit}.")
    quote, created = Quote.objects.update_or_create(
        request=rfq, farmer=farmer,
        defaults={"listing": listing, "unit_price": unit_price, "note": note,
                  "status": Quote.Status.SUBMITTED})
    notify(rfq.buyer, f"New quote for {rfq.number}",
           f"{farmer.farm.farm_name}: R{unit_price}/{rfq.unit} ({listing.grade or listing.commodity.name}).",
           link=f"/orders/quotes/{rfq.pk}/")
    audit(farmer, AuditLog.Action.CREATE if created else AuditLog.Action.UPDATE, quote,
          new=f"R{unit_price}/{rfq.unit}")
    return quote


@transaction.atomic
def accept_quote(quote, buyer, delivery_method, payment_method, delivery_address="",
                 delivery_province="", card_number=""):
    """Turn the chosen quote into an order at the quoted price (stock taken atomically)."""
    from .models import Quote, QuoteRequest
    rfq = quote.request
    if rfq.buyer_id != buyer.pk:
        raise TransitionError("This is not your request.")
    # Lock the request: only one acceptance can succeed.
    if not QuoteRequest.objects.filter(pk=rfq.pk, status=QuoteRequest.Status.OPEN).update(
            status=QuoteRequest.Status.ACCEPTED):
        raise TransitionError("A quote has already been accepted for this request.")
    if quote.status != Quote.Status.SUBMITTED:
        raise TransitionError("This quote is no longer valid.")
    lines = [{"listing": quote.listing, "quantity": Decimal(rfq.quantity), "unit_price": Decimal(quote.unit_price)}]
    order = _create_order(buyer, lines, delivery_method, payment_method, delivery_address,
                          delivery_province, card_number, check_min_order=False)
    quote.status = Quote.Status.ACCEPTED
    quote.order = order
    quote.save(update_fields=["status", "order"])
    for other in rfq.quotes.exclude(pk=quote.pk).filter(status=Quote.Status.SUBMITTED):
        other.status = Quote.Status.DECLINED
        other.save(update_fields=["status"])
        notify(other.farmer, f"{rfq.number}: another quote was chosen",
               "Thank you for quoting. The buyer chose another offer.", link=f"/orders/quotes/{rfq.pk}/")
    notify(quote.farmer, f"Your quote for {rfq.number} was accepted",
           f"Order {order.number} has been created at R{quote.unit_price}/{rfq.unit}.",
           link=f"/orders/incoming/", sms=True)
    return order


# ---------------------------------------------------------------- status changes
class TransitionError(Exception):
    pass


def last_event(sub):
    """When the most recent step of this sub-order happened."""
    last = sub.history.order_by("-created_at").values_list("created_at", flat=True).first()
    return last or sub.created_at


def step_moment(sub, day):
    """Moment to record a step of a past sale on `day` (None = now).

    Only used for sales entered afterwards; the day may not be before the
    previous step or after today.
    """
    if not sub.order.recorded_late or day is None:
        return None
    prev = last_event(sub)
    prev_day = timezone.localtime(prev).date()
    if day < prev_day:
        raise TransitionError(f"The date cannot be before the previous step ({prev_day:%d %b %Y}).")
    if day > timezone.localdate():
        raise TransitionError("The date cannot be in the future.")
    return clock.at_date(day, after=prev)


def payment_cleared(sub):
    pay = getattr(sub.order, "payment", None)
    return sub.order.payment_method == Order.PaymentMethod.COD or (
        pay is not None and pay.status == Payment.Status.PAID)


@transaction.atomic
def change_status(sub, new_status, user, note=""):
    """Move a sub-order to a new status, enforcing FR-09.

    The UPDATE only succeeds if the row still has the status we read, so two
    people clicking at the same moment cannot both act (e.g. stock restored twice).
    """
    old_status = sub.status
    if not sub.can_move_to(new_status):
        raise TransitionError(
            f"Cannot change from {sub.get_status_display()} to {S(new_status).label}.")
    if new_status == S.APPROVED and not payment_cleared(sub):
        raise TransitionError("Payment has not been confirmed yet.")
    if new_status == S.IN_TRANSIT and not sub.is_delivery:
        raise TransitionError("Collection orders are not shipped.")
    if new_status == S.DELIVERED and not note:
        raise TransitionError("A delivery note (e.g. name of the person who received it) is required.")

    now = clock.now()
    fields = {"status": new_status}
    if new_status == S.APPROVED:
        fields["approved_at"] = now
    if new_status == S.DELIVERED:
        fields["delivered_at"] = now
        fields["delivery_note"] = note[:200]
    if new_status == S.COMPLETED:
        fields["completed_at"] = now
        if sub.paid_to_farm and sub.farm_share is not None:   # Paystack already kept it at payment time
            fields["commission_amount"] = sub.goods_subtotal + sub.vat_amount - sub.farm_share - sub.recovered
        else:
            fields["commission_amount"] = money(sub.goods_subtotal * SiteSettings.load().commission_rate)
    if new_status == S.CANCELLED and note:
        fields["rejection_reason"] = note[:200]

    if not SubOrder.objects.filter(pk=sub.pk, status=old_status).update(**fields):
        raise TransitionError("This order was changed by someone else. Refresh and try again.")
    sub.refresh_from_db()

    if new_status == S.APPROVED and not sub.invoice_number:
        sub.invoice_number = f"INV-{now:%Y}-{sub.pk:06d}"
        sub.save(update_fields=["invoice_number"])

    if new_status == S.DELIVERED and sub.order.payment_method == Order.PaymentMethod.COD:
        payment = getattr(sub.order, "payment", None)
        if payment and payment.status == Payment.Status.PENDING:
            payment.status = Payment.Status.PAID
            payment.confirmed_at = now
            payment.save(update_fields=["status", "confirmed_at"])
            audit(user, AuditLog.Action.STATUS, payment, field="status",
                  old=Payment.Status.PENDING, new=payment.status)

    if new_status == S.CANCELLED:
        for item in sub.items.select_related("listing"):
            return_stock(item.listing, item.quantity)
        pay = getattr(sub.order, "payment", None)
        if pay and pay.status in (Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED):
            create_refund(sub, sub.goods_subtotal + sub.vat_amount, True, user,
                          f"Cancelled: {note or 'no reason given'}")
        elif (pay and pay.status == Payment.Status.PENDING
              and not sub.order.suborders.exclude(status=S.CANCELLED).exists()):
            # The whole order was cancelled before the card was paid: the payment will never happen.
            pay.status = Payment.Status.FAILED
            pay.save(update_fields=["status"])

    StatusChange.objects.create(suborder=sub, from_status=old_status, to_status=new_status,
                                changed_by=user, note=note[:200])
    audit(user, AuditLog.Action.STATUS, sub, field="status", old=old_status, new=new_status)

    label = S(new_status).label
    notify(sub.order.buyer, f"{sub.order.number}: {label}",
           f"Your items from {sub.farmer.farm.farm_name} are now {label.lower()}."
           + (f" Note: {note}" if note else ""),
           link=f"/orders/{sub.order.pk}/", sms=new_status in (S.APPROVED, S.IN_TRANSIT, S.CANCELLED))
    if user != sub.farmer:
        notify(sub.farmer, f"{sub.order.number}: {label}", note, link=f"/orders/sub/{sub.pk}/")
    return sub


@transaction.atomic
def schedule_delivery(sub, date, slot, user, distributor=None):
    sub = SubOrder.objects.select_for_update().get(pk=sub.pk)
    if not sub.is_delivery:
        raise TransitionError("Farm collection orders do not need a delivery schedule.")
    if sub.status not in (S.APPROVED, S.PACKED):
        raise TransitionError("Only confirmed or packed orders can be scheduled.")
    earliest = sub.order.order_date if sub.order.recorded_late else timezone.localdate()
    if date < earliest:
        raise TransitionError(f"The delivery date cannot be before {earliest:%d %b %Y}.")

    if not user.is_farmer or sub.farmer_id != user.pk:
        raise TransitionError("Only the farmer can schedule this delivery.")
    selected_driver = distributor
    if selected_driver is None or not (
            selected_driver.is_distributor and selected_driver.is_active
            and selected_driver.province == sub.farmer.farm.province):
        raise TransitionError("Choose an active distributor from the farm's province.")

    selected_driver.__class__.objects.select_for_update().get(pk=selected_driver.pk)
    if not available_distributors(sub.farmer.farm.province, date, slot, sub).filter(
            pk=selected_driver.pk).exists():
        raise TransitionError("That driver is already scheduled for this time in the farm's province.")

    old_driver = sub.distributor
    sub.distributor = selected_driver
    sub.scheduled_date, sub.time_slot = date, slot
    sub.save(update_fields=["distributor", "scheduled_date", "time_slot"])
    if old_driver != selected_driver:
        audit(user, AuditLog.Action.UPDATE, sub, field="distributor", old=old_driver, new=selected_driver)
    audit(user, AuditLog.Action.UPDATE, sub, field="scheduled_date", new=f"{date} {slot}")
    msg = f"Delivery of {sub.order.number} scheduled for {date:%d %b %Y}, {slot}."
    notify(sub.order.buyer, "Delivery scheduled", msg, link=f"/orders/{sub.order.pk}/", sms=True)
    notify(sub.farmer, "Delivery scheduled", msg, link=f"/orders/sub/{sub.pk}/")
    notify(selected_driver, f"New scheduled collection {sub.order.number}",
           f"Collect from {sub.farmer.farm.farm_name}, {sub.farmer.farm.town} on "
           f"{date:%d %b %Y}, {slot}.", link=f"/orders/sub/{sub.pk}/", sms=True)


# ---------------------------------------------------------------- payments / refunds
@transaction.atomic
def create_refund(sub, goods_amount, include_delivery, user, reason):
    """Give a buyer money back; each side returns its own part.

    goods_amount is the part of the goods + VAT being refunded. The farm returns its share of it,
    AgriMarket its commission on it, and the delivery fee too when the goods were never delivered.
    Paystack sends the whole amount back to the buyer's card from AgriMarket's balance (after the
    transaction commits); the farm's part is kept back from its next card sales.
    """
    gross = sub.goods_subtotal + sub.vat_amount
    goods_amount = min(Decimal(goods_amount), gross)
    if sub.paid_to_farm and sub.farm_share is not None:
        commission = gross - sub.farm_share - sub.recovered
    else:
        commission = money(sub.goods_subtotal * SiteSettings.load().commission_rate)
    commission_back = money(commission * goods_amount / gross) if gross else Decimal("0")
    delivery_back = sub.delivery_fee if include_delivery else Decimal("0")
    refund = Refund.objects.create(
        suborder=sub, amount=goods_amount + delivery_back, farm_amount=goods_amount - commission_back,
        commission_amount=commission_back, delivery_amount=delivery_back, reason=reason[:200],
        processed_by=user if getattr(user, "pk", None) else None)
    audit(user, AuditLog.Action.CREATE, refund, new=f"R{refund.amount} (farm R{refund.farm_amount}, "
                                                     f"AgriMarket R{refund.platform_amount})")
    transaction.on_commit(lambda: send_refund(refund.pk))
    return refund


def send_refund(refund_pk):
    """Send a refund to the buyer (through Paystack for Paystack payments). Safe to call again."""
    from . import paystack
    with transaction.atomic():
        refund = Refund.objects.select_for_update().select_related("suborder__order__payment", "suborder__farmer").get(pk=refund_pk)
        if refund.status == Refund.Status.PROCESSED:
            return True
        pay = refund.suborder.order.payment
        ref = ""
        if pay.gateway == "paystack" and pay.reference.startswith("PS"):
            try:
                ref = paystack.refund(pay.reference[2:], refund.amount, note=refund.reason)
            except paystack.PaystackError as e:
                refund.problem = str(e)[:300]
                refund.save(update_fields=["problem"])
                return False
        elif pay.method == Order.PaymentMethod.COD:
            ref = "cash returned by the farm"
        refund.status, refund.gateway_reference, refund.problem = Refund.Status.PROCESSED, ref[:40], ""
        refund.processed_at = clock.now()
        refund.save(update_fields=["status", "gateway_reference", "problem", "processed_at"])
        total_refunded = sum(r.amount for r in Refund.objects.filter(
            suborder__order=pay.order, status=Refund.Status.PROCESSED))
        pay.status = (Payment.Status.REFUNDED if total_refunded >= pay.amount
                      else Payment.Status.PARTIALLY_REFUNDED)
        pay.save(update_fields=["status"])
        audit(None, AuditLog.Action.STATUS, refund, field="status", new=f"refunded {ref}")
        order = refund.suborder.order
        notify(order.buyer, "Refund sent", f"R{refund.amount} for {order.number} is on its way back to your card.",
               link=f"/orders/{order.pk}/", email=True)
        if refund.farm_amount:
            notify(refund.suborder.farmer, f"Refund on {order.number}",
                   f"R{refund.amount} was refunded to the buyer. Your part, R{refund.farm_amount}, is kept back "
                   f"from your next card sales.", link=f"/orders/sub/{refund.suborder_id}/")
    return True


def retry_refunds():
    """Hourly job: send refunds that Paystack could not take earlier."""
    done = 0
    for pk in Refund.objects.filter(status=Refund.Status.PENDING).values_list("pk", flat=True):
        done += bool(send_refund(pk))
    return done


# ---------------------------------------------------------------- disputes
@transaction.atomic
def raise_dispute(sub, user, reason, photo=None):
    rules = SiteSettings.load()
    if sub.status != S.DELIVERED:
        raise TransitionError("Only delivered orders can be disputed.")
    if sub.delivered_at and clock.now() > sub.delivered_at + timedelta(days=rules.dispute_window_days):
        raise TransitionError(f"Disputes must be raised within {rules.dispute_window_days} days of delivery.")
    change_status(sub, S.DISPUTED, user, note="Dispute raised")
    dispute = Dispute.objects.create(suborder=sub, raised_by=user, reason=reason, photo=photo or "")
    notify(sub.farmer, f"Complaint on {sub.order.number}: please answer it", reason[:150],
           link=f"/orders/disputes/{dispute.pk}/", sms=True)
    return dispute


@transaction.atomic
def resolve_dispute(dispute, user, outcome, note, refund_amount=Decimal("0")):
    """The farmer answers the buyer's complaint: full refund, partial refund or rejected (with a reason)."""
    if dispute.status != Dispute.Status.OPEN:
        raise TransitionError("This dispute is already resolved.")
    sub = dispute.suborder
    if user != sub.farmer:
        raise TransitionError("Only the farm that supplied the order can answer this complaint.")
    if not note:
        raise TransitionError("Explain your decision to the buyer.")
    gross = sub.goods_subtotal + sub.vat_amount
    if outcome == Dispute.Status.FULL_REFUND:
        refund_amount = gross
    elif outcome == Dispute.Status.PARTIAL_REFUND:
        if not (Decimal("0") < refund_amount < gross):
            raise TransitionError(f"A partial refund must be between R0 and R{gross}.")
    else:
        refund_amount = Decimal("0")
    dispute.status, dispute.resolution_note = outcome, note
    dispute.refund_amount = refund_amount
    dispute.resolved_by, dispute.resolved_at = user, clock.now()
    dispute.save()
    change_status(sub, S.COMPLETED, user, note=f"Complaint answered: {dispute.get_status_display()}")
    sub.refresh_from_db()
    if refund_amount > 0:
        create_refund(sub, refund_amount, False, user, f"Complaint: {note[:180]}")
    notify(sub.order.buyer, f"Answer to your complaint on {sub.order.number}",
           f"{dispute.get_status_display()}. {note}", link=f"/orders/{sub.order.pk}/", email=True)
    return dispute


# ---------------------------------------------------------------- scheduled jobs
def paystack_paid_already(payment):
    """Before a card order is cancelled for non-payment, ask Paystack about every payment the buyer started.

    True  - one was paid in full: the order is confirmed now (buyer closed the browser after paying).
    False - none was paid: the order may be cancelled.
    None  - Paystack could not be reached: leave the order for the next run.
    """
    from . import paystack
    refs = [e.data.get("reference") for e in payment.events.filter(kind="redirect").order_by("-pk")
            if isinstance(e.data, dict) and e.data.get("reference")]
    for ref in refs:
        try:
            status, amount, ps_id, raw = paystack.verify(ref)
        except paystack.PaystackError:
            return None
        GatewayEvent.objects.create(payment=payment, kind="query", status=status,
                                    accepted=status == "success" and amount == payment.amount,
                                    data={"reference": ref, "by": "timeout check"})
        if status == "success" and amount == payment.amount:
            card_gateway_paid(payment, ps_id or ref, note="Paid on Paystack (confirmed by the timeout check)",
                              split=split_of_reference(payment, ref))
            return True
    return False


def run_timeouts(now=None):
    """Cancel unpaid card orders and complete delivered ones (FR-10).

    Each change is dated when the deadline actually passed, not when the job ran.
    Past sales entered afterwards are not auto-cancelled: they were already paid.
    """
    now = now or timezone.now()
    rules = SiteSettings.load()
    cancelled = completed = 0
    limit = timedelta(minutes=rules.card_payment_minutes)
    unpaid = SubOrder.objects.filter(status=S.PENDING, order__recorded_late=False,
                                     created_at__lt=now - limit).select_related("order")
    for sub in unpaid:
        what = f"{rules.card_payment_minutes} minutes"
        if getattr(sub.order, "payment", None) and sub.order.payment.gateway == "paystack":
            paid = paystack_paid_already(sub.order.payment)
            if paid is None:   # Paystack could not be asked: try again on the next run, never cancel blindly
                continue
            if paid:           # the buyer paid but never came back to AgriMarket
                continue
        try:
            with clock.as_of(min(sub.created_at + limit, now)):
                change_status(sub, S.CANCELLED, None, note=f"Payment not received within {what}")
            cancelled += 1
        except TransitionError:
            pass
    retry_refunds()
    for sub in SubOrder.objects.filter(status=S.DELIVERED,
                                       delivered_at__lt=now - timedelta(days=rules.auto_complete_days)):
        try:
            with clock.as_of(sub.delivered_at + timedelta(days=rules.auto_complete_days)):
                change_status(sub, S.COMPLETED, None, note="Completed automatically")
            completed += 1
        except TransitionError:
            pass
    return cancelled, completed
