from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import User
from core import clock
from core.models import AuditLog
from core.permissions import buyer_required, farmer_required, verified_farmer_required
from core.services import audit
from orders.models import Review, SubOrder

from .forms import (AddToCartForm, CatalogFilterForm, ListingForm, LivestockForm,
                    StockAdjustmentForm, StockAlertForm)
from .models import (Cart, CartItem, Commodity, FavouriteFarm, Kind, Listing, ListingPhoto,
                     PriceTier, SearchLog, StockAlert, fmt_qty)
from .services import OutOfStock, adjust_stock, announce_listing, check_low_stock


def _with_ratings(qs):
    return qs.annotate(
        avg_rating=Avg("reviews__rating", filter=Q(reviews__is_hidden=False)),
        review_count=Count("reviews", filter=Q(reviews__is_hidden=False), distinct=True))


# ---------------------------------------------------------------- catalog
def catalog(request):
    form = CatalogFilterForm(request.GET or None, user=request.user)
    listings = _with_ratings(Listing.objects.sellable().select_related(
        "category", "commodity", "farmer__farm").prefetch_related("photos"))
    query = ""
    if form.is_valid():
        d = form.cleaned_data
        query = d["q"].strip()
        if query:
            listings = listings.filter(Q(name__icontains=query) | Q(description__icontains=query)
                                       | Q(commodity__name__icontains=query) | Q(variety__icontains=query)
                                       | Q(livestock__breed__icontains=query)
                                       | Q(livestock__species__icontains=query))
        if d["category"]:
            listings = listings.filter(category=d["category"])
        if d["commodity"]:
            listings = listings.filter(commodity=d["commodity"])
        if d["province"]:
            listings = listings.filter(farmer__farm__province=d["province"])
        if d["grade"]:
            listings = listings.filter(grade=d["grade"])
        if d["min_price"] is not None:
            listings = listings.filter(price__gte=d["min_price"])
        if d["max_price"] is not None:
            listings = listings.filter(price__lte=d["max_price"])
        if d.get("favourites"):
            listings = listings.filter(farmer__followers__buyer=request.user)
        sort = d["sort"] or "newest"
        # Record searches so the planting planner can show unmet demand.
        if (query or d["commodity"]) and not request.GET.get("page"):
            commodity = d["commodity"] or Commodity.objects.filter(
                Q(name__icontains=query) | Q(code__iexact=query)).first()
            SearchLog.objects.create(query=(query or d["commodity"].name)[:100], commodity=commodity,
                                     province=d["province"] or "", results=listings.count(),
                                     user=request.user if request.user.is_authenticated else None)
            form.searched_commodity = commodity
    else:
        sort = "newest"
    order = {"newest": ["-created_at"], "price_asc": ["price", "-created_at"],
             "price_desc": ["-price", "-created_at"],
             "rating": ["-avg_rating", "-review_count", "-created_at"]}[sort]
    listings = listings.order_by(*order).distinct()
    page = Paginator(listings, 12).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "marketplace/catalog.html",
                  {"form": form, "page": page, "params": params.urlencode(),
                   "searched_commodity": getattr(form, "searched_commodity", None),
                   "waiting_for_payout": not page.paginator.count and _hidden_for_payout()})


def _hidden_for_payout():
    """True when stock exists but is hidden only because farms have no Paystack payout account yet."""
    from orders.services import uses_gateway
    if uses_gateway() != "paystack":
        return False
    return Listing.objects.filter(status=Listing.Status.ACTIVE, quantity_available__gt=0,
                                  farmer__farm__status="approved", farmer__is_active=True,
                                  farmer__farm__paystack_subaccount="").exists()


def listing_detail(request, pk):
    listing = get_object_or_404(_with_ratings(Listing.objects.select_related(
        "category", "commodity", "farmer__farm", "harvest__planting__field", "herd")
        .prefetch_related("photos", "price_tiers")), pk=pk)
    is_owner = request.user.is_authenticated and request.user == listing.farmer
    staff = request.user.is_authenticated and request.user.is_admin_role
    sellable = Listing.objects.sellable().filter(pk=pk).exists()
    sold_out = (listing.status == Listing.Status.ACTIVE and listing.quantity_available == 0
                and not listing.is_expired)
    withdrawal_until = listing.withdrawal_until
    if not (sellable or is_owner or staff or sold_out or withdrawal_until):
        messages.info(request, "That listing is no longer for sale.")
        return redirect("marketplace:catalog")
    # Other batches of the same product and grade from this farmer, earliest expiry first (FEFO).
    siblings = (Listing.objects.sellable().filter(farmer=listing.farmer, commodity=listing.commodity,
                                                  grade=listing.grade)
                .exclude(pk=pk).order_by("expiry_date"))
    reviews = listing.reviews.filter(is_hidden=False).select_related("buyer")
    treatments = listing.herd.treatments.all()[:10] if listing.herd_id else []
    is_fav = (request.user.is_authenticated and request.user.is_buyer
              and FavouriteFarm.objects.filter(buyer=request.user, farmer=listing.farmer).exists())
    form = AddToCartForm(initial={"quantity": listing.min_order_qty})
    return render(request, "marketplace/listing_detail.html",
                  {"listing": listing, "reviews": reviews, "form": form, "siblings": siblings,
                   "is_owner": is_owner, "sellable": sellable, "sold_out": sold_out,
                   "withdrawal_until": withdrawal_until, "treatments": treatments, "is_fav": is_fav})


def farmer_page(request, pk):
    farmer = get_object_or_404(User.objects.select_related("farm"), pk=pk, role=User.Role.FARMER,
                               farm__status="approved", is_active=True)
    listings = _with_ratings(Listing.objects.sellable().filter(farmer=farmer)
                             .select_related("category").prefetch_related("photos"))
    stats = Review.objects.filter(listing__farmer=farmer, is_hidden=False).aggregate(
        avg=Avg("rating"), count=Count("id"))
    is_fav = (request.user.is_authenticated and request.user.is_buyer
              and FavouriteFarm.objects.filter(buyer=request.user, farmer=farmer).exists())
    return render(request, "marketplace/farmer_page.html",
                  {"farmer": farmer, "listings": listings, "stats": stats, "is_fav": is_fav,
                   "followers": farmer.followers.count()})


# ---------------------------------------------------------------- buyer tools
@buyer_required
@require_POST
def toggle_favourite(request, farmer_pk):
    farmer = get_object_or_404(User, pk=farmer_pk, role=User.Role.FARMER)
    fav = FavouriteFarm.objects.filter(buyer=request.user, farmer=farmer).first()
    if fav:
        fav.delete()
        messages.info(request, f"Removed {farmer.farm.farm_name} from your favourite farms.")
    else:
        FavouriteFarm.objects.create(buyer=request.user, farmer=farmer)
        messages.success(request, f"You will be notified when {farmer.farm.farm_name} lists new stock.")
    nxt = request.POST.get("next", "")
    return redirect(nxt if nxt.startswith("/") else "marketplace:favourites")


@buyer_required
def favourites(request):
    favs = FavouriteFarm.objects.filter(buyer=request.user).select_related("farmer__farm")
    rows = [(f, Listing.objects.sellable().filter(farmer=f.farmer).count()) for f in favs]
    return render(request, "marketplace/favourites.html", {"rows": rows})


@buyer_required
def alerts(request):
    initial = {}
    if request.GET.get("commodity"):
        initial["commodity"] = request.GET["commodity"]
    if request.GET.get("province"):
        initial["province"] = request.GET["province"]
    form = StockAlertForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        alert = form.save(commit=False)
        alert.buyer = request.user
        if StockAlert.objects.filter(buyer=request.user, commodity=alert.commodity,
                                     province=alert.province, is_active=True).exists():
            messages.info(request, "You already have that alert.")
        else:
            alert.save()
            messages.success(request, f"We will notify you when {alert.commodity} is available.")
            # Already in stock? Tell the buyer straight away.
            for listing in Listing.objects.sellable().filter(commodity=alert.commodity):
                if alert.matches(listing):
                    messages.info(request, f"{alert.commodity} is already available — "
                                           f"see the marketplace.")
                    break
        return redirect("marketplace:alerts")
    items = StockAlert.objects.filter(buyer=request.user).select_related("commodity")
    return render(request, "marketplace/alerts.html", {"form": form, "items": items})


@buyer_required
@require_POST
def alert_delete(request, pk):
    get_object_or_404(StockAlert, pk=pk, buyer=request.user).delete()
    messages.info(request, "Alert removed.")
    return redirect("marketplace:alerts")


# ---------------------------------------------------------------- cart
def _cart(user):
    cart, _ = Cart.objects.get_or_create(buyer=user)
    return cart


@buyer_required
@require_POST
def cart_add(request, pk):
    listing = get_object_or_404(Listing.objects.sellable(), pk=pk)
    form = AddToCartForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a valid quantity.")
        return redirect("marketplace:listing", pk=pk)
    cart = _cart(request.user)
    existing = cart.items.filter(listing=listing).first()
    qty = form.cleaned_data["quantity"] + (existing.quantity if existing else 0)
    error = _qty_error(listing, qty)
    if error:
        messages.error(request, error)
        return redirect("marketplace:listing", pk=pk)
    CartItem.objects.update_or_create(cart=cart, listing=listing, defaults={"quantity": qty})
    messages.success(request, f"Added {fmt_qty(qty)} {listing.unit} of {listing.name} to your cart.")
    return redirect("marketplace:cart")


def _qty_error(listing, qty):
    if listing.unit == "head" and qty != qty.to_integral_value():
        return "Livestock is sold in whole head."
    if qty < listing.min_order_qty:
        return f"Minimum order is {fmt_qty(listing.min_order_qty)} {listing.unit}."
    if qty > listing.quantity_available:
        return f"Only {listing.qty_display} available."
    return None


@buyer_required
def cart_view(request):
    from orders.views import render_cart
    return render_cart(request)


@buyer_required
@require_POST
def cart_update(request, item_id):
    item = get_object_or_404(CartItem.objects.select_related("listing"), pk=item_id,
                             cart__buyer=request.user)
    if "remove" in request.POST:
        item.delete()
        messages.info(request, f"Removed {item.listing.name}.")
        return redirect("marketplace:cart")
    form = AddToCartForm(request.POST)
    if form.is_valid():
        qty = form.cleaned_data["quantity"]
        error = _qty_error(item.listing, qty)
        if error:
            messages.error(request, f"{item.listing.name}: {error}")
        else:
            item.quantity = qty
            item.save(update_fields=["quantity"])
    else:
        messages.error(request, "Enter a valid quantity.")
    return redirect("marketplace:cart")


# ---------------------------------------------------------------- farmer stock
@farmer_required
def my_listings(request, forms_ctx=None):
    """My stock: list of listings with the 'add produce' and 'add livestock' forms on the same page."""
    from farm.models import Harvest

    status = request.GET.get("status", "active")
    listings = (_with_ratings(Listing.objects.filter(farmer=request.user, status=status))
                .select_related("category", "herd").order_by("expiry_date", "-created_at"))
    unlisted_harvests = (Harvest.objects.filter(planting__farmer=request.user, listing__isnull=True)
                         .select_related("planting__commodity", "planting__field"))
    ctx = {"listings": listings, "status": status, "unlisted_harvests": unlisted_harvests}
    if request.user.is_verified_farmer:
        ctx.update(forms_ctx or _create_forms(request))
    return render(request, "marketplace/my_listings.html", ctx)


def _own_listing(request, pk, **filters):
    """Return the listing, or 403 if it belongs to another farmer (NFR-02)."""
    listing = get_object_or_404(Listing, pk=pk, **filters)
    if listing.farmer_id != request.user.pk:
        raise PermissionDenied("You can only manage your own listings.")
    return listing


def _save_tiers(listing, tiers):
    listing.price_tiers.all().delete()
    PriceTier.objects.bulk_create([PriceTier(listing=listing, min_quantity=m, unit_price=p)
                                   for m, p in tiers])


def _create_forms(request, kind=None, data=None, files=None):
    """Build the produce and livestock 'add' forms for the My stock page.

    Links from farm records (?harvest=<id> or ?herd=<id>) pre-fill the matching
    form so the farmer doesn't type the same details again.
    """
    from farm.models import Harvest, Herd
    farm = request.user.farm
    src = request.POST if data is not None else request.GET
    harvest = herd = None
    p_initial, l_initial = {}, {}
    ls_initial = {"tag_or_brand": farm.brand_mark, "vaccination_status": "up_to_date"}
    hid = src.get("harvest") or src.get("from_harvest")
    if hid:
        harvest = Harvest.objects.filter(pk=hid, planting__farmer=request.user, listing__isnull=True).first()
    if harvest:
        p = harvest.planting
        shelf = p.commodity.shelf_life_days
        p_initial = {"commodity": p.commodity_id, "variety": p.cultivar, "grade": harvest.grade, "unit": p.unit,
                     "quantity_available": harvest.quantity, "harvest_date": harvest.harvested_on,
                     "expiry_date": (max(harvest.harvested_on + timedelta(days=shelf), timezone.localdate()
                                         + timedelta(days=1)) if shelf else None)}
    if request.GET.get("herd"):
        herd = Herd.objects.filter(pk=request.GET["herd"], farmer=request.user).first()
        if herd:
            l_initial = {"commodity": herd.commodity_id, "variety": herd.breed, "herd": herd.pk}
            ls_initial["tag_or_brand"] = herd.tag_range or farm.brand_mark
    open_panel = kind or ("produce" if harvest else "livestock" if herd else "")
    pform = ListingForm(data if kind == Kind.PRODUCE else None, files if kind == Kind.PRODUCE else None,
                        instance=Listing(farmer=request.user, kind=Kind.PRODUCE), kind=Kind.PRODUCE,
                        farmer=request.user, initial=p_initial, harvest=harvest, prefix="p")
    lform_main = ListingForm(data if kind == Kind.LIVESTOCK else None, files if kind == Kind.LIVESTOCK else None,
                             instance=Listing(farmer=request.user, kind=Kind.LIVESTOCK), kind=Kind.LIVESTOCK,
                             farmer=request.user, initial=l_initial, prefix="l")
    ldetail = LivestockForm(data if kind == Kind.LIVESTOCK else None, prefix="ls", initial=ls_initial)
    return {"pform": pform, "lform_main": lform_main, "ldetail": ldetail, "harvest": harvest,
            "open_panel": open_panel, "has_brand": bool(farm.brand_mark)}


@verified_farmer_required
def listing_create(request):
    """Saves a new listing from the My stock page. Errors are shown on that same page."""
    if request.method != "POST":
        params = request.GET.copy()
        kind = params.pop("kind", ["produce"])[-1]
        return redirect(f"{reverse('marketplace:my_listings')}?{params.urlencode()}#add-{kind}")
    kind = request.POST.get("kind", Kind.PRODUCE)
    kind = kind if kind in Kind.values else Kind.PRODUCE
    if kind == Kind.LIVESTOCK and not request.user.farm.brand_mark:
        messages.error(request, "Add your registered brand mark to your profile before selling livestock.")
        return redirect("accounts:profile")
    ctx = _create_forms(request, kind=kind, data=request.POST, files=request.FILES)
    form = ctx["pform"] if kind == Kind.PRODUCE else ctx["lform_main"]
    lform = ctx["ldetail"] if kind == Kind.LIVESTOCK else None
    harvest = ctx["harvest"] if kind == Kind.PRODUCE else None
    if form.is_valid() and (lform is None or lform.is_valid()):
        listing = form.instance
        hd = form.cleaned_data.get("harvest_date")
        try:
            # Stock harvested on an earlier day is listed as of that day.
            with clock.as_of(clock.at_date(hd) if hd else None), transaction.atomic():
                if harvest:
                    listing.harvest = harvest
                listing = form.save()
                if lform:
                    detail = lform.save(commit=False)
                    detail.listing = listing
                    detail.species = listing.commodity.category.name
                    detail.breed = listing.variety or listing.commodity.name
                    detail.save()
                _save_tiers(listing, form.tiers)
                for i, f in enumerate(form.cleaned_data["photos"]):
                    ListingPhoto.objects.create(listing=listing, image=f, position=i)
                check_low_stock(listing)
                audit(request.user, AuditLog.Action.CREATE, listing,
                      new=f"{listing.qty_display} @ R{listing.price}")
        except (IntegrityError, ValidationError) as e:
            form.add_error(None, f"Could not save the listing: {e}")
        else:
            sent = announce_listing(listing)
            messages.success(request, f"Listed {listing.name} as batch {listing.batch_number}."
                             + (f" {sent} interested buyer(s) were notified." if sent else ""))
            if listing.withdrawal_until:
                messages.warning(request, f"This herd is under a treatment withdrawal period. The listing "
                                          f"becomes visible to buyers on {listing.withdrawal_until:%d %b %Y}.")
            return redirect(f"{reverse('marketplace:my_listings')}?new={listing.pk}")
    return my_listings(request, forms_ctx=ctx)


@verified_farmer_required
def listing_edit(request, pk):
    listing = _own_listing(request, pk)
    old = {f: getattr(listing, f) for f in ("name", "price", "grade", "description",
                                            "min_order_qty", "low_stock_threshold", "expiry_date",
                                            "near_expiry_discount_pct")}
    form = ListingForm(request.POST or None, request.FILES or None, instance=listing, kind=listing.kind,
                       farmer=request.user)
    detail = getattr(listing, "livestock", None)
    lform = LivestockForm(request.POST or None, instance=detail, prefix="ls") if listing.is_livestock else None
    if request.method == "POST" and form.is_valid() and (lform is None or lform.is_valid()):
        with transaction.atomic():
            listing = form.save()
            if lform:
                d = lform.save(commit=False)
                d.listing = listing
                d.species = listing.commodity.category.name
                d.breed = listing.variety or listing.commodity.name
                d.save()
            _save_tiers(listing, form.tiers)
            start = listing.photos.count()
            for i, f in enumerate(form.cleaned_data["photos"]):
                ListingPhoto.objects.create(listing=listing, image=f, position=start + i)
            for f, v in old.items():
                if getattr(listing, f) != v:
                    audit(request.user, AuditLog.Action.UPDATE, listing, field=f, old=v,
                          new=getattr(listing, f))
            check_low_stock(listing)
        messages.success(request, "Listing updated.")
        return redirect(f"{reverse('marketplace:my_listings')}?new={listing.pk}")
    return render(request, "marketplace/listing_form.html",
                  {"form": form, "lform": lform, "kind": listing.kind, "listing": listing,
                   "adjust_form": StockAdjustmentForm(listing=listing),
                   "adjustments": listing.adjustments.select_related("created_by")[:10]})


@farmer_required
@require_POST
def photo_delete(request, pk):
    photo = get_object_or_404(ListingPhoto, pk=pk, listing__farmer=request.user)
    listing_pk = photo.listing_id
    photo.image.delete(save=False)
    photo.delete()
    return redirect("marketplace:listing_edit", pk=listing_pk)


@verified_farmer_required
@require_POST
def listing_adjust(request, pk):
    listing = _own_listing(request, pk)
    form = StockAdjustmentForm(request.POST, listing=listing)
    if form.is_valid():
        d = form.cleaned_data
        try:
            adjust_stock(listing, d["change"], d["reason"], request.user, d["note"])
            messages.success(request, f"Stock is now {listing.qty_display}.")
        except OutOfStock as e:
            messages.error(request, str(e))
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
    return redirect("marketplace:listing_edit", pk=pk)


@farmer_required
@require_POST
def listing_remove(request, pk):
    """Delete if never ordered; otherwise archive so past orders keep their link (FR-05)."""
    listing = _own_listing(request, pk)
    has_orders = listing.order_items.exists()
    if has_orders or listing.adjustments.exists():
        listing.status = Listing.Status.ARCHIVED
        listing.save(update_fields=["status", "updated_at"])
        audit(request.user, AuditLog.Action.UPDATE, listing, field="status", old="active", new="archived")
        open_orders = listing.order_items.filter(suborder__status__in=SubOrder.OPEN_STATUSES).exists()
        messages.info(request, f"{listing.name} was archived" +
                      (" — it still appears on open orders." if open_orders else "."))
    else:
        audit(request.user, AuditLog.Action.DELETE, listing)
        listing.delete()
        messages.info(request, "Listing deleted.")
    return redirect("marketplace:my_listings")


@farmer_required
@require_POST
def listing_restore(request, pk):
    listing = _own_listing(request, pk, status=Listing.Status.ARCHIVED)
    listing.status = Listing.Status.ACTIVE
    listing.save(update_fields=["status", "updated_at"])
    audit(request.user, AuditLog.Action.UPDATE, listing, field="status", old="archived", new="active")
    messages.success(request, f"{listing.name} is active again.")
    return redirect("marketplace:my_listings")
