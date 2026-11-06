from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.http import FileResponse, HttpResponse
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core import clock
from core.models import SiteSettings
from core.services import notify
from core.permissions import (admin_required, buyer_required, distributor_required, farmer_required,
                              role_required)
from marketplace.models import Cart, Listing, fmt_qty

from . import services
from .forms import (CheckoutForm, DisputeForm, FarmerScheduleForm,
                    QuoteForm, QuoteRequestForm, ReasonForm, ResolveDisputeForm, ReviewForm)
from .invoice import render_invoice
from .models import Dispute, Order, OrderItem, Payment, Quote, QuoteRequest, Refund, Review, SubOrder

S = SubOrder.Status


# ---------------------------------------------------------------- buyer
def render_cart(request, form=None):
    """Cart and checkout on one page: items per farm on the left, order details on the right."""
    import json
    from accounts.models import Province
    cart, _ = Cart.objects.get_or_create(buyer=request.user)
    if form is None:
        # Drop lines that can no longer be bought and warn about reduced stock.
        for item in cart.items.select_related("listing"):
            if not Listing.objects.sellable().filter(pk=item.listing_id).exists():
                messages.warning(request, f"{item.listing.name} is no longer available and was removed.")
                item.delete()
            elif item.quantity > item.listing.quantity_available:
                messages.warning(request, f"Only {item.listing.qty_display} of {item.listing.name} "
                                          f"is left. Reduce the quantity before placing the order.")
        form = CheckoutForm(initial={"delivery_address": request.user.address,
                                     "delivery_province": request.user.province})
    quote = services.quote_cart(cart)
    # Delivery fee for every province, so the total updates as soon as the buyer picks one.
    fees = {}
    if quote["groups"]:
        for code, _label in Province.choices:
            q = services.quote_cart(cart, Order.DeliveryMethod.DELIVERY, code)
            fees[code] = None if q["errors"] else str(q["delivery"])
    return render(request, "marketplace/cart.html", {
        "cart": cart, "quote": quote, "form": form, "fees_json": json.dumps(fees),
        "gateway": services.uses_gateway() if services.card_available() else "",
        "gateway_name": "Paystack" if services.uses_gateway() else "",
        "goods_vat": str(quote["goods"] + quote["vat"])})


@buyer_required
def checkout(request):
    if request.method != "POST":
        return redirect("marketplace:cart")
    cart = Cart.objects.filter(buyer=request.user).first()
    if not cart or not cart.items.exists():
        messages.info(request, "Your cart is empty.")
        return redirect("marketplace:catalog")
    form = CheckoutForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        try:
            order = services.place_order(
                request.user, d["delivery_method"], d["payment_method"],
                delivery_address=d["delivery_address"].strip(),
                delivery_province=d["delivery_province"], card_number=d["card_number"],
                order_date=d["order_date"])
        except services.CheckoutError as e:
            form.add_error(None, f"Order not placed: {e}")
        else:
            if order.payment.online and order.payment.status == Payment.Status.PENDING:
                return redirect("orders:card_pay", pk=order.pk)
            messages.success(request, f"Order {order.number} placed.")
            return redirect("orders:order_detail", pk=order.pk)
    return render_cart(request, form=form)


# ---------------------------------------------------------------- card payments (Paystack)
def _card_order(request, pk):
    order = get_object_or_404(Order.objects.select_related("payment", "buyer"), pk=pk, buyer=request.user)
    if not order.payment.online:
        raise PermissionDenied
    return order


def _log(kind, payment, request=None, **kw):
    from .models import GatewayEvent
    ip = ""
    if request is not None:
        ip = (request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
              or request.META.get("REMOTE_ADDR", ""))
    return GatewayEvent.objects.create(kind=kind, payment=payment, ip=ip, **kw)


@buyer_required
def card_pay(request, pk):
    """Sends the buyer's browser to Paystack's payment page."""
    order = _card_order(request, pk)
    if order.payment.status != Payment.Status.PENDING or not order.suborders.filter(status=S.PENDING).exists():
        messages.info(request, "This order is not waiting for a card payment.")
        return redirect("orders:order_detail", pk=pk)
    return _paystack_start(request, order)


def _paystack_start(request, order):
    from . import paystack
    callback = request.build_absolute_uri(reverse("orders:card_return", args=[order.pk]))
    split = paystack.split_for(order)
    try:
        url, ref = paystack.start(order, callback, split=split)
    except paystack.PaystackError as e:
        # e.g. a farm's Paystack subaccount is no longer active: no payment is taken
        _log("redirect", order.payment, request, problems=str(e)[:500])
        messages.error(request, f"Could not start the card payment. {e}")
        return redirect("orders:order_detail", pk=order.pk)
    _log("redirect", order.payment, request, status="started",
         data={"reference": ref, "url": url, "split": {str(k): str(v[1]) for k, v in split.items()},
               "kept": {str(k): str(v[2]) for k, v in split.items()}})
    return redirect(url)


def _paystack_verify(request, order, refs):
    """Ask Paystack about each reference; confirm the order if one was paid in full. Returns True when paid."""
    from . import paystack
    for ref in refs:
        try:
            status, amount, ps_id, raw = paystack.verify(ref)
        except paystack.PaystackError as e:
            _log("query", order.payment, request, problems=str(e)[:500], data={"reference": ref})
            messages.error(request, f"Could not check the payment with Paystack. {e}")
            return False
        problems = "" if status != "success" or amount == order.payment.amount else \
            f"amount R{amount} does not match R{order.payment.amount}"
        ok = status == "success" and not problems
        _log("query", order.payment, request, status=status, accepted=ok, problems=problems,
             data={"reference": ref, "gateway_response": raw.get("gateway_response", ""), "id": ps_id})
        if ok:
            services.card_gateway_paid(order.payment, ps_id or ref, note="Paid on Paystack",
                                       split=services.split_of_reference(order.payment, ref))
            messages.success(request, f"Paystack confirms the payment — order {order.number} is confirmed.")
            return True
    messages.info(request, f"Paystack says the payment is {(status or 'not found').lower()} so far.")
    return False


@buyer_required
def card_return(request, pk):
    order = _card_order(request, pk)
    _log("return", order.payment, request, data=request.GET.dict())
    if order.payment.gateway == "paystack":
        ref = request.GET.get("reference") or request.GET.get("trxref")
        if ref and _paystack_verify(request, order, [ref]):
            return redirect("orders:order_detail", pk=pk)
    order.payment.refresh_from_db()
    if order.payment.status == Payment.Status.PAID:
        messages.success(request, f"Payment received — order {order.number} is confirmed.")
        return redirect("orders:order_detail", pk=pk)
    return render(request, "orders/card_waiting.html", {"order": order})


@buyer_required
def card_cancel(request, pk):
    order = _card_order(request, pk)
    _log("cancel", order.payment, request)
    messages.warning(request, "The card payment was cancelled. Your items are held for a short time — "
                              "use 'Pay now' to try again, or cancel the order.")
    return redirect("orders:order_detail", pk=pk)


@buyer_required
@require_POST
def card_check(request, pk):
    """Ask Paystack directly whether the buyer's payment went through."""
    order = _card_order(request, pk)
    refs = [e.data.get("reference") for e in order.payment.events.filter(kind="redirect").order_by("-pk")
            if isinstance(e.data, dict) and e.data.get("reference")]
    if not refs:
        messages.info(request, "No Paystack payment was started for this order yet — use 'Pay now'.")
    elif _paystack_verify(request, order, refs):
        return redirect("orders:order_detail", pk=pk)
    return redirect("orders:card_return", pk=pk)


@buyer_required
def my_orders(request):
    orders = (Order.objects.filter(buyer=request.user)
              .select_related("payment").prefetch_related("suborders__farmer__farm", "suborders__items"))
    page = Paginator(orders, 15).get_page(request.GET.get("page"))
    return render(request, "orders/my_orders.html", {"page": page})


@buyer_required
def order_detail(request, pk):
    order = get_object_or_404(Order.objects.select_related("buyer", "payment"), pk=pk)
    if order.buyer != request.user:
        raise PermissionDenied
    subs = (order.suborders.select_related("farmer__farm", "distributor")
            .prefetch_related("items", "history", "reviews"))
    reviewed = set(Review.objects.filter(suborder__order=order).values_list("suborder_id", "listing_id"))
    return render(request, "orders/order_detail.html",
                  {"order": order, "subs": subs, "site": SiteSettings.load(),
                   "dispute_form": DisputeForm(), "review_form": ReviewForm(),
                   "reviewed": reviewed})


@buyer_required
@require_POST
def raise_dispute(request, sub_pk):
    sub = get_object_or_404(SubOrder, pk=sub_pk, order__buyer=request.user)
    form = DisputeForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            services.raise_dispute(sub, request.user, form.cleaned_data["reason"],
                                   form.cleaned_data.get("photo"))
            messages.success(request, f"Complaint sent to {sub.farmer.farm.farm_name}. The farm will answer it here.")
        except services.TransitionError as e:
            messages.error(request, str(e))
    else:
        messages.error(request, "Describe the problem to send a complaint.")
    return redirect("orders:order_detail", pk=sub.order_id)


@buyer_required
@require_POST
def add_review(request, sub_pk, listing_pk):
    sub = get_object_or_404(SubOrder, pk=sub_pk, order__buyer=request.user, status=S.COMPLETED)
    item = get_object_or_404(sub.items, listing_id=listing_pk)
    form = ReviewForm(request.POST)
    if form.is_valid():
        moment = None
        if sub.order.recorded_late and sub.completed_at:
            moment = clock.at_date(timezone.localtime(sub.completed_at).date(), after=sub.completed_at)
        try:
            with clock.as_of(moment):
                Review.objects.create(suborder=sub, listing=item.listing, buyer=request.user,
                                      rating=form.cleaned_data["rating"], comment=form.cleaned_data["comment"])
            messages.success(request, "Thank you for your review.")
        except IntegrityError:
            messages.error(request, "You have already reviewed this purchase.")
    return redirect("orders:order_detail", pk=sub.order_id)


# ---------------------------------------------------------------- sub-orders
def _posted_date(request, name="on_date"):
    """Optional date posted with a step of a past sale."""
    from datetime import date
    raw = request.POST.get(name, "").strip()
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _can_view_sub(user, sub):
    return (sub.farmer_id == user.pk or sub.distributor_id == user.pk or sub.order.buyer_id == user.pk)


# action -> (new status, roles allowed, note required)
ACTIONS = {
    "reject": (S.CANCELLED, {"farmer"}, True),   # farmer can't supply after all
    "pack": (S.PACKED, {"farmer"}, False),
    "collected": (S.DELIVERED, {"farmer"}, True),
    "ship": (S.IN_TRANSIT, {"distributor"}, False),
    "deliver": (S.DELIVERED, {"distributor"}, True),
    "cancel": (S.CANCELLED, {"buyer"}, False),
    "confirm": (S.COMPLETED, {"buyer"}, False),
}


@role_required("farmer", "distributor", "buyer")
def suborder_detail(request, pk):
    sub = get_object_or_404(SubOrder.objects.select_related(
        "order__buyer", "order__payment", "farmer__farm", "distributor"), pk=pk)
    if not _can_view_sub(request.user, sub):
        raise PermissionDenied
    if request.user.is_buyer:
        return redirect("orders:order_detail", pk=sub.order_id)
    # POPIA: contact details only for parties to this order (NFR-06).
    return render(request, "orders/suborder_detail.html", {
        "sub": sub, "items": sub.items.all(), "history": sub.history.select_related("changed_by"),
        "farmer_schedule_form": FarmerScheduleForm(
            suborder=sub, initial={"date": sub.scheduled_date, "slot": sub.time_slot,
                                   "distributor": sub.distributor_id}),
        "note_form": ReasonForm(),
        "dispute": getattr(sub, "dispute", None), "refunds": sub.refunds.all(),
        "step_min": timezone.localtime(services.last_event(sub)).date() if sub.order.recorded_late else None,
    })


@role_required("farmer", "distributor", "buyer")
@require_POST
def suborder_action(request, pk, action):
    sub = get_object_or_404(SubOrder.objects.select_related("order", "farmer__farm"), pk=pk)
    if action not in ACTIONS:
        raise PermissionDenied
    new_status, roles, note_needed = ACTIONS[action]
    user = request.user
    role = user.role
    owns = {"farmer": sub.farmer_id == user.pk, "distributor": sub.distributor_id == user.pk,
            "buyer": sub.order.buyer_id == user.pk}.get(role, False)
    if role not in roles or not owns:
        raise PermissionDenied("You cannot perform this action on this order.")
    note = request.POST.get("note", "").strip()
    if note_needed and not note:
        messages.error(request, "Please add a note for this action.")
    elif action == "collected" and sub.is_delivery:
        messages.error(request, "This order is delivered by a distributor.")
    elif action == "reject" and sub.status != S.APPROVED:
        messages.error(request, "Only orders that are not packed yet can be cancelled by the farm.")
    else:
        try:
            with clock.as_of(services.step_moment(sub, _posted_date(request))):
                services.change_status(sub, new_status, user, note=note)
            messages.success(request, f"Order {sub.order.number} is now {S(new_status).label.lower()}.")
        except services.TransitionError as e:
            messages.error(request, str(e))
    if user.is_buyer:
        return redirect("orders:order_detail", pk=sub.order_id)
    return redirect("orders:suborder", pk=pk)


@farmer_required
def farmer_orders(request):
    status = request.GET.get("status", "")
    subs = (SubOrder.objects.filter(farmer=request.user)
            .select_related("order__buyer", "order__payment", "farmer__farm", "distributor")
            .prefetch_related("items"))
    if status:
        subs = subs.filter(status=status)
    page = Paginator(subs, 20).get_page(request.GET.get("page"))
    return render(request, "orders/suborder_list.html",
                  {"page": page, "status": status, "statuses": S.choices, "title": "Incoming orders"})


@distributor_required
def distributor_jobs(request):
    status = request.GET.get("status", "")
    subs = (SubOrder.objects.filter(distributor=request.user)
            .select_related("order__buyer", "order__payment", "farmer__farm", "distributor")
            .prefetch_related("items"))
    subs = subs.filter(status=status) if status else subs.filter(status__in=[S.APPROVED, S.PACKED, S.IN_TRANSIT])
    page = Paginator(subs.order_by("scheduled_date", "created_at"), 20).get_page(request.GET.get("page"))
    return render(request, "orders/suborder_list.html",
                  {"page": page, "status": status, "statuses": S.choices, "title": "My deliveries"})


@farmer_required
@require_POST
def schedule(request, pk):
    sub = get_object_or_404(SubOrder, pk=pk, farmer=request.user)
    form = FarmerScheduleForm(request.POST, suborder=sub)
    if form.is_valid():
        try:
            services.schedule_delivery(
                sub, form.cleaned_data["date"], form.cleaned_data["slot"], request.user,
                distributor=form.cleaned_data["distributor"])
            messages.success(request, "Delivery scheduled.")
        except services.TransitionError as e:
            messages.error(request, str(e))
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
    return redirect("orders:suborder", pk=pk)


@role_required("buyer", "farmer")
def invoice_pdf(request, pk):
    sub = get_object_or_404(SubOrder.objects.select_related("order__buyer", "farmer__farm"), pk=pk)
    if not _can_view_sub(request.user, sub):
        raise PermissionDenied
    if not sub.invoice_number:
        messages.info(request, "The invoice is issued once the payment is confirmed.")
        return redirect("orders:order_detail", pk=sub.order_id) if request.user.is_buyer \
            else redirect("orders:suborder", pk=pk)
    pdf = render_invoice(sub)
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{sub.invoice_number}.pdf"'
    return resp


# ---------------------------------------------------------------- disputes
@farmer_required
def dispute_list(request):
    """Complaints buyers raised about this farm's deliveries; open ones first."""
    disputes = (Dispute.objects.filter(suborder__farmer=request.user)
                .select_related("suborder__order__buyer", "raised_by").order_by("-status", "-created_at"))
    disputes = sorted(disputes, key=lambda d: (d.status != Dispute.Status.OPEN, -d.created_at.timestamp()))
    return render(request, "orders/dispute_list.html", {"disputes": disputes})


@role_required("farmer", "buyer")
def dispute_detail(request, pk):
    """The farm answers the complaint; the buyer can read the answer."""
    dispute = get_object_or_404(Dispute.objects.select_related("suborder__order__buyer", "suborder__farmer__farm"), pk=pk)
    sub = dispute.suborder
    if request.user not in (sub.farmer, sub.order.buyer):
        raise PermissionDenied
    form = ResolveDisputeForm(request.POST or None)
    if request.method == "POST" and request.user == sub.farmer and form.is_valid():
        d = form.cleaned_data
        try:
            services.resolve_dispute(dispute, request.user, d["outcome"], d["note"], d["refund_amount"] or 0)
            messages.success(request, "Your answer was sent to the buyer.")
            return redirect("orders:dispute_detail", pk=pk)
        except services.TransitionError as e:
            messages.error(request, str(e))
    return render(request, "orders/dispute_detail.html", {"dispute": dispute, "form": form, "sub": sub,
                                                          "refunds": sub.refunds.all()})


@admin_required
@require_POST
def hide_review(request, pk):
    review = get_object_or_404(Review, pk=pk)
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "Give a reason for hiding the review.")
    else:
        review.is_hidden, review.hidden_reason = True, reason
        review.save(update_fields=["is_hidden", "hidden_reason"])
        from core.models import AuditLog
        from core.services import audit
        audit(request.user, AuditLog.Action.UPDATE, review, field="is_hidden", new=reason)
        messages.success(request, "Review hidden.")
    return redirect("marketplace:listing", pk=review.listing_id)


# ---------------------------------------------------------------- reorder
@buyer_required
@require_POST
def reorder(request, pk):
    order = get_object_or_404(Order, pk=pk, buyer=request.user)
    added, skipped = services.reorder(order, request.user)
    if added:
        messages.success(request, "Added to your cart: " + "; ".join(added) + ".")
    for s in skipped:
        messages.warning(request, f"Not added: {s}.")
    return redirect("marketplace:cart" if added else "orders:order_detail", **({} if added else {"pk": pk}))


# ---------------------------------------------------------------- quote requests (RFQ)
@role_required("buyer", "farmer")
def rfq_list(request, form=None):
    """Quote requests. Buyers create new requests in a panel on this same page."""
    u = request.user
    if u.is_buyer:
        rows = QuoteRequest.objects.filter(buyer=u)
        title = "My quote requests"
        if form is None:
            initial = {"delivery_province": u.province}
            if request.GET.get("commodity"):
                initial["commodity"] = request.GET["commodity"]
            form = QuoteRequestForm(initial=initial, buyer=u)
    elif u.is_farmer:
        mine = set(Quote.objects.filter(farmer=u).values_list("request_id", flat=True))
        from marketplace.models import Listing
        from farm.models import Herd, Planting
        commodities = (set(Listing.objects.filter(farmer=u).values_list("commodity_id", flat=True))
                       | set(Planting.objects.filter(farmer=u).values_list("commodity_id", flat=True))
                       | set(Herd.objects.filter(farmer=u).values_list("commodity_id", flat=True)))
        rows = QuoteRequest.objects.filter(
            Q(status=QuoteRequest.Status.OPEN, commodity__in=commodities) | Q(pk__in=mine))
        title = "Quote requests for my products"
    else:
        rows = QuoteRequest.objects.all()
        title = "All quote requests"
    rows = rows.select_related("commodity", "buyer").annotate(n_quotes=Count("quotes"))
    return render(request, "orders/rfq_list.html", {
        "rows": rows, "title": title, "form": form,
        "open_form": bool(form is not None and (form.is_bound or request.GET.get("commodity")))})


@buyer_required
def rfq_create(request):
    if request.method != "POST":
        q = f"?commodity={request.GET['commodity']}" if request.GET.get("commodity") else ""
        return redirect(f"{reverse('orders:rfq_list')}{q}#new-request")
    form = QuoteRequestForm(request.POST, buyer=request.user)
    if form.is_valid():
        rfq = form.save(commit=False)
        rfq.buyer = request.user
        rfq.save()
        sent = services.open_rfq(rfq)
        messages.success(request, f"{rfq.number} posted. {sent} farm(s) that sell {rfq.commodity} "
                                  f"have been asked to quote.")
        return redirect(f"{reverse('orders:rfq_list')}?new={rfq.pk}")
    return rfq_list(request, form=form)


@role_required("buyer", "farmer")
def rfq_detail(request, pk):
    rfq = get_object_or_404(QuoteRequest.objects.select_related("commodity", "buyer"), pk=pk)
    u = request.user
    if u.is_buyer and rfq.buyer_id != u.pk:
        raise PermissionDenied
    my_quote = Quote.objects.filter(request=rfq, farmer=u).first() if u.is_farmer else None
    qform = None
    if u.is_farmer and rfq.status == QuoteRequest.Status.OPEN:
        qform = QuoteForm(request.POST or None, farmer=u, rfq=rfq,
                          initial={"listing": my_quote.listing_id, "unit_price": my_quote.unit_price,
                                   "note": my_quote.note} if my_quote else None)
        if request.method == "POST" and qform.is_valid():
            if not u.is_verified_farmer:
                raise PermissionDenied
            d = qform.cleaned_data
            try:
                services.submit_quote(rfq, u, d["listing"], d["unit_price"], d["note"])
                messages.success(request, "Your quote was sent to the buyer.")
                return redirect("orders:rfq_detail", pk=pk)
            except services.TransitionError as e:
                messages.error(request, str(e))
    # Buyers see all quotes; a farmer sees only their own (prices of others stay private).
    quotes = rfq.quotes.select_related("farmer__farm", "listing")
    if u.is_farmer:
        quotes = quotes.filter(farmer=u)
    from insights.services import price_guide
    guide = price_guide(rfq.commodity_id, rfq.unit, rfq.grade)
    accept_form = CheckoutForm(initial={"delivery_address": rfq.buyer.address,
                                        "delivery_province": rfq.delivery_province}) if u.is_buyer else None
    return render(request, "orders/rfq_detail.html", {
        "rfq": rfq, "quotes": quotes, "qform": qform, "my_quote": my_quote, "guide": guide,
        "accept_form": accept_form, "gateway": services.uses_gateway()})


@buyer_required
@require_POST
def quote_accept(request, pk):
    quote = get_object_or_404(Quote.objects.select_related("request"), pk=pk, request__buyer=request.user)
    form = CheckoutForm(request.POST)
    if not form.is_valid():
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
        return redirect("orders:rfq_detail", pk=quote.request_id)
    d = form.cleaned_data
    try:
        order = services.accept_quote(quote, request.user, d["delivery_method"], d["payment_method"],
                                      d["delivery_address"].strip(), d["delivery_province"], d["card_number"])
    except (services.CheckoutError, services.TransitionError) as e:
        messages.error(request, f"Quote not accepted: {e}")
        return redirect("orders:rfq_detail", pk=quote.request_id)
    if order.payment.online and order.payment.status == Payment.Status.PENDING:
        messages.success(request, f"Quote accepted. Pay for order {order.number} to confirm it.")
        return redirect("orders:card_pay", pk=order.pk)
    messages.success(request, f"Quote accepted. Order {order.number} created at the quoted price.")
    return redirect("orders:order_detail", pk=order.pk)


@buyer_required
@require_POST
def rfq_close(request, pk):
    rfq = get_object_or_404(QuoteRequest, pk=pk, buyer=request.user)
    if QuoteRequest.objects.filter(pk=pk, status=QuoteRequest.Status.OPEN).update(status=QuoteRequest.Status.CLOSED):
        rfq.quotes.filter(status=Quote.Status.SUBMITTED).update(status=Quote.Status.DECLINED)
        messages.info(request, f"{rfq.number} closed.")
    return redirect("orders:rfq_list")


# ---------------------------------------------------------------- order register
def _register_lines(request):
    """Order lines visible to this user, filtered by the query string."""
    from accounts.models import BuyerType, User
    from marketplace.models import Commodity
    u = request.user
    lines = (OrderItem.objects.select_related(
        "suborder__order__buyer", "suborder__order__payment", "suborder__farmer__farm",
        "suborder__distributor").order_by("-suborder__order__order_date", "-suborder__order__created_at", "id"))
    if u.is_farmer:
        lines = lines.filter(suborder__farmer=u)
    g = request.GET
    f = {k: g.get(k, "").strip() for k in ("start", "end", "q", "buyer", "farm", "product", "status", "payment",
                                           "buyer_type")}
    if f["start"]:
        lines = lines.filter(suborder__order__order_date__gte=f["start"])
    if f["end"]:
        lines = lines.filter(suborder__order__order_date__lte=f["end"])
    if f["q"]:
        lines = lines.filter(Q(suborder__order__number__icontains=f["q"]) | Q(batch_number__icontains=f["q"])
                             | Q(suborder__order__buyer__first_name__icontains=f["q"])
                             | Q(suborder__order__buyer__last_name__icontains=f["q"])
                             | Q(suborder__order__buyer__email__icontains=f["q"]))
    if f["buyer"].isdigit():
        lines = lines.filter(suborder__order__buyer_id=f["buyer"])
    if f["farm"].isdigit() and not u.is_farmer:
        lines = lines.filter(suborder__farmer_id=f["farm"])
    if f["product"]:
        lines = lines.filter(product_code=f["product"])
    if f["status"]:
        lines = lines.filter(suborder__status=f["status"])
    if f["payment"]:
        lines = lines.filter(suborder__order__payment__status=f["payment"])
    if f["buyer_type"]:
        lines = lines.filter(suborder__order__buyer__buyer_type=f["buyer_type"])
    options = {
        "farms": [] if u.is_farmer else User.objects.filter(role="farmer", farm__isnull=False)
        .select_related("farm").order_by("farm__farm_name"),
        "buyers": User.objects.filter(role="buyer", orders__suborders__in=SubOrder.objects.filter(
            farmer=u) if u.is_farmer else SubOrder.objects.all()).distinct().order_by("first_name"),
        "products": Commodity.objects.filter(code__in=lines.values("product_code")).order_by("name"),
        "statuses": S.choices, "payments": Payment.Status.choices, "buyer_types": BuyerType.choices,
    }
    return lines, f, options


def _line_row(it):
    from marketplace.models import money
    sub, order = it.suborder, it.suborder.order
    buyer, farm, pay = order.buyer, sub.farmer.farm, getattr(order, "payment", None)
    when = order.order_date
    return {
        "it": it, "sub": sub, "order": order, "buyer": buyer, "farm": farm, "pay": pay, "when": when,
        "vat": money(it.line_total * it.vat_rate),
        "time": timezone.localtime(order.created_at),
        "csv": [when.strftime("%Y-%m-%d"), timezone.localtime(order.created_at).strftime("%H:%M"), order.number, buyer.get_full_name(), buyer.email,
                buyer.get_buyer_type_display(), buyer.get_province_display(), farm.farm_name,
                farm.get_province_display(), farm.vat_number or "not VAT registered", it.product_name,
                it.product_code, it.grade, it.batch_number, fmt_qty(it.quantity), it.unit, it.unit_price,
                it.line_total, f"{it.vat_rate * 100:.0f}%", money(it.line_total * it.vat_rate),
                order.get_delivery_method_display(),
                (order.delivery_address.replace("\n", ", ") + ", " + order.get_delivery_province_display())
                if order.delivery_method == "delivery" else "Farm collection",
                sub.distributor.get_full_name() if sub.distributor else "",
                sub.scheduled_date.strftime("%Y-%m-%d") if sub.scheduled_date else "",
                order.get_payment_method_display(), pay.get_status_display() if pay else "",
                sub.get_status_display(), sub.invoice_number or "",
                timezone.localtime(sub.delivered_at).strftime("%Y-%m-%d %H:%M") if sub.delivered_at else "",
                timezone.localtime(sub.completed_at).strftime("%Y-%m-%d") if sub.completed_at else ""],
    }


REGISTER_HEADER = ["Order date", "Time", "Order no", "Buyer", "Buyer email", "Buyer type", "Buyer province", "Farm",
                   "Farm province", "Farm VAT no", "Product", "Product code", "Grade", "Batch", "Quantity", "Unit",
                   "Unit price (R)", "Line total excl. VAT (R)", "VAT rate", "VAT (R)", "Delivery method",
                   "Delivery to", "Distributor", "Delivery date", "Payment method", "Payment status",
                   "Order status", "Invoice no", "Delivered", "Completed"]


@farmer_required
def order_register(request):
    """Every order line: who bought what, from whom, when, and its status (with CSV export)."""
    import csv
    from django.db.models import Sum
    lines, f, options = _register_lines(request)
    if request.GET.get("export") == "csv":
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="order-register-{timezone.localdate():%Y%m%d}.csv"'
        w = csv.writer(resp)
        w.writerow(REGISTER_HEADER)
        for it in lines.iterator(chunk_size=500):
            w.writerow(_line_row(it)["csv"])
        return resp
    totals = lines.aggregate(goods=Sum("line_total"), n=Count("id"))
    totals["orders"] = lines.values("suborder__order").distinct().count()
    page = Paginator(lines, 50).get_page(request.GET.get("page"))
    rows = [_line_row(it) for it in page]
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "orders/register.html", {"page": page, "rows": rows, "f": f, "totals": totals,
                                                    "params": params.urlencode(), **options})
