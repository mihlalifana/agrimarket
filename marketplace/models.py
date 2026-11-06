from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import (FileExtensionValidator, MaxValueValidator,
                                    MinValueValidator)
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core import clock
from django.utils.text import slugify

QTY = dict(max_digits=12, decimal_places=3)   # quantities: up to 999 999 999.999
MONEY = dict(max_digits=12, decimal_places=2)  # rand amounts: up to R9 999 999 999.99
CENT = Decimal("0.01")


def money(value):
    """Round to cents, half up (BR-04)."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


class Kind(models.TextChoices):
    PRODUCE = "produce", "Produce"
    LIVESTOCK = "livestock", "Livestock"


class Unit(models.TextChoices):
    KG = "kg", "kg"
    TON = "ton", "ton"
    CRATE = "crate", "crate"
    HEAD = "head", "head"


# Estimated weight in tons per unit, used for delivery fees.
UNIT_TONS = {Unit.KG: Decimal("0.001"), Unit.TON: Decimal("1"), Unit.CRATE: Decimal("0.02")}


class Category(models.Model):
    name = models.CharField(max_length=60, unique=True)
    slug = models.SlugField(max_length=60, unique=True, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PRODUCE)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Commodity(models.Model):
    """A standard product with its allowed units and official grade table.

    Every listing refers to one commodity, so the same product is always
    described the same way and can be compared across farms.
    """

    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="commodities")
    code = models.CharField(max_length=20, unique=True,
                            help_text="Short standard code, e.g. MAIZE-W")
    name = models.CharField(max_length=60, unique=True)
    units = models.CharField(max_length=40, help_text="Allowed units, comma separated: kg,ton,crate,head")
    grades = models.TextField(blank=True, help_text="Official grades, one per line. Empty = no grade table.")
    standard_reference = models.CharField(max_length=120, blank=True)
    is_perishable = models.BooleanField(default=False)
    shelf_life_days = models.PositiveSmallIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category__name", "name"]
        verbose_name_plural = "commodities"

    def __str__(self):
        return self.name

    @property
    def kind(self):
        return self.category.kind

    @property
    def unit_list(self):
        return [u.strip() for u in self.units.split(",") if u.strip()]

    @property
    def grade_list(self):
        return [g.strip() for g in self.grades.splitlines() if g.strip()]

    def clean(self):
        units = self.unit_list
        bad = [u for u in units if u not in Unit.values]
        if not units or bad:
            raise ValidationError({"units": f"Use only: {', '.join(Unit.values)}."})
        if self.category_id:
            is_live = self.category.kind == Kind.LIVESTOCK
            if is_live and units != [Unit.HEAD]:
                raise ValidationError({"units": "Livestock commodities are sold per head only."})
            if not is_live and Unit.HEAD in units:
                raise ValidationError({"units": "Produce cannot be sold per head."})
        self.code = self.code.upper().strip()


class ListingQuerySet(models.QuerySet):
    def sellable(self, on=None):
        """Listings a buyer may see and buy (FR-06, FR-05) on a day (default today).

        Livestock whose herd is still inside a treatment withdrawal period is
        excluded, so it cannot be bought. `on` lets a past sale be checked
        against the stock as it was that day.
        """
        from django.db.models import Exists, OuterRef
        from farm.models import Treatment
        today = on or timezone.localdate()
        withdrawing = Treatment.objects.filter(herd=OuterRef("herd"), treated_on__lte=today,
                                               withdrawal_ends__gt=today)
        qs = self.filter(
            status=Listing.Status.ACTIVE,
            quantity_available__gt=0,
            farmer__is_active=True,
            farmer__farm__status="approved",
            category__is_active=True,
            commodity__is_active=True,
        ).filter(Q(expiry_date__isnull=True) | Q(expiry_date__gte=today)).exclude(
            Exists(withdrawing))
        from orders.services import uses_gateway
        if on is None and uses_gateway() == "paystack":
            # Buyers pay by card and Paystack pays the farm directly: a farm needs its payout account.
            qs = qs.exclude(farmer__farm__paystack_subaccount="")
        return qs


class Listing(models.Model):
    """One batch of produce or one lot of livestock offered by a farmer."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ARCHIVED = "archived", "Archived"

    farmer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                               related_name="listings", limit_choices_to={"role": "farmer"})
    commodity = models.ForeignKey(Commodity, on_delete=models.PROTECT, related_name="listings")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="listings",
                                 editable=False)
    kind = models.CharField(max_length=10, choices=Kind.choices, editable=False)
    variety = models.CharField(max_length=60, blank=True,
                               help_text="Cultivar or breed, e.g. Mondial, Bonsmara")
    # Traceability: where the stock came from (farm records).
    harvest = models.OneToOneField("farm.Harvest", null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="listing")
    herd = models.ForeignKey("farm.Herd", null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="listings")
    batch_number = models.CharField(max_length=20, unique=True, null=True, editable=False)
    name = models.CharField(max_length=100, editable=False)
    description = models.TextField(blank=True)
    unit = models.CharField(max_length=5, choices=Unit.choices)
    price = models.DecimalField(**MONEY, validators=[MinValueValidator(CENT)],
                                help_text="Price per unit in rand (per head for livestock)")
    quantity_available = models.DecimalField(**QTY, validators=[MinValueValidator(0)])
    min_order_qty = models.DecimalField(**QTY, default=Decimal("1"),
                                        validators=[MinValueValidator(Decimal("0.001"))])
    low_stock_threshold = models.DecimalField(**QTY, default=Decimal("0"),
                                              validators=[MinValueValidator(0)])
    grade = models.CharField(max_length=20, blank=True,
                             help_text="Official grade from the commodity's grade table")
    harvest_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    near_expiry_discount_pct = models.PositiveSmallIntegerField(
        default=0, validators=[MaxValueValidator(90)],
        help_text="Automatic discount (%) during the near-expiry window.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    low_stock_alerted = models.BooleanField(default=False, editable=False)
    expiry_alerted = models.BooleanField(default=False, editable=False)
    created_at = models.DateTimeField(default=clock.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ListingQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "category"]),
            models.Index(fields=["expiry_date"]),
            models.Index(fields=["farmer", "status"]),
            models.Index(fields=["commodity", "status"]),
        ]
        constraints = [
            # Stock can never go below zero: the database refuses it even if
            # application code has a bug. This is what guarantees that when
            # one item is left, only one buyer can get it.
            models.CheckConstraint(condition=Q(quantity_available__gte=0),
                                   name="listing_quantity_not_negative"),
            models.CheckConstraint(condition=Q(price__gt=0), name="listing_price_positive"),
            models.CheckConstraint(condition=Q(min_order_qty__gt=0),
                                   name="listing_min_order_positive"),
            models.CheckConstraint(condition=Q(low_stock_threshold__gte=0),
                                   name="listing_threshold_not_negative"),
            models.CheckConstraint(condition=Q(near_expiry_discount_pct__lte=90),
                                   name="listing_discount_max_90"),
            # Livestock is sold per head; produce never is.
            models.CheckConstraint(
                condition=(Q(kind="livestock") & Q(unit="head")) | (Q(kind="produce") & ~Q(unit="head")),
                name="listing_unit_matches_kind",
            ),
            models.CheckConstraint(
                condition=Q(expiry_date__isnull=True) | Q(harvest_date__isnull=True)
                | Q(expiry_date__gte=F("harvest_date")),
                name="listing_expiry_after_harvest",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.batch_number or 'new'})"

    # Validation ---------------------------------------------------------
    def clean(self):
        errors = {}
        if self.commodity_id:
            c = self.commodity
            self.category = c.category
            if self.unit and self.unit not in c.unit_list:
                errors["unit"] = f"{c.name} is sold by: {', '.join(c.unit_list)}."
            grades = c.grade_list
            if grades and self.grade not in grades:
                errors["grade"] = f"Choose an official grade for {c.name}: {', '.join(grades)}."
            if not grades:
                self.grade = ""
        if self.category_id:
            self.kind = self.category.kind
            if self.kind == Kind.LIVESTOCK and self.unit != Unit.HEAD:
                errors["unit"] = "Livestock is sold per head."
            if self.kind == Kind.PRODUCE and self.unit == Unit.HEAD:
                errors["unit"] = "Produce is sold by kg, ton or crate."
        old_harvest = (Listing.objects.filter(pk=self.pk).values_list("harvest_date", flat=True).first()
                       if self.pk else None)
        if self.harvest_date and (self.pk is None or self.harvest_date != old_harvest):
            from core.models import validate_record_date
            try:
                validate_record_date(self.harvest_date, "Harvest date")
            except ValidationError as e:
                errors["harvest_date"] = e.messages[0]
        if self.harvest_date and self.expiry_date and self.expiry_date <= self.harvest_date:
            errors["expiry_date"] = "Expiry date must be after the harvest date."
        if self.unit == Unit.HEAD:
            for f in ("quantity_available", "min_order_qty", "low_stock_threshold"):
                v = getattr(self, f)
                if v is not None and v != v.to_integral_value():
                    errors[f] = "Livestock is counted in whole head."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.commodity_id:
            self.category_id = self.commodity.category_id
            self.name = self.commodity.name + (f" ({self.variety})" if self.variety else "")
        if self.category_id:
            self.kind = self.category.kind
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new and not self.batch_number:
            # Unique, read-only batch number (FR-05).
            self.batch_number = f"BAT-{self.created_at:%Y}-{self.pk:06d}"
            Listing.objects.filter(pk=self.pk).update(batch_number=self.batch_number)

    # Derived values -----------------------------------------------------
    @property
    def is_livestock(self):
        return self.kind == Kind.LIVESTOCK

    @property
    def is_expired(self):
        return self.expiry_date is not None and self.expiry_date < timezone.localdate()

    @property
    def is_near_expiry(self):
        from core.models import SiteSettings
        if not self.expiry_date or self.is_expired:
            return False
        days = SiteSettings.load().near_expiry_days
        return self.expiry_date <= timezone.localdate() + timedelta(days=days)

    @property
    def withdrawal_until(self):
        """Last day of an active treatment withdrawal on this listing's herd, or None."""
        if not self.herd_id:
            return None
        today = timezone.localdate()
        t = (self.herd.treatments.filter(withdrawal_ends__gt=today)
             .order_by("-withdrawal_ends").first())
        return t.withdrawal_ends if t else None

    @property
    def is_low_stock(self):
        return self.quantity_available <= self.low_stock_threshold

    def unit_price_for(self, quantity):
        """Price per unit for a quantity: bulk tier, then near-expiry discount."""
        price = self.price
        tier = (self.price_tiers.filter(min_quantity__lte=quantity)
                .order_by("-min_quantity").first())
        if tier:
            price = tier.unit_price
        if self.near_expiry_discount_pct and self.is_near_expiry:
            price = money(price * (100 - self.near_expiry_discount_pct) / 100)
        return price

    @property
    def current_price(self):
        return self.unit_price_for(self.min_order_qty)

    @property
    def tons_per_unit(self):
        if self.unit == Unit.HEAD:
            detail = getattr(self, "livestock", None)
            return (detail.avg_weight_kg / 1000) if detail else Decimal("0.5")
        return UNIT_TONS[self.unit]

    @property
    def qty_display(self):
        return f"{fmt_qty(self.quantity_available)} {self.unit}"


def fmt_qty(qty):
    """1.500 -> '1.5', 20.000 -> '20'."""
    s = f"{Decimal(qty):f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


class PriceTier(models.Model):
    """Bulk price: from min_quantity upward the unit price is unit_price (FR-05)."""

    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="price_tiers")
    min_quantity = models.DecimalField(**QTY, validators=[MinValueValidator(Decimal("0.001"))])
    unit_price = models.DecimalField(**MONEY, validators=[MinValueValidator(CENT)])

    class Meta:
        ordering = ["min_quantity"]
        constraints = [
            models.UniqueConstraint(fields=["listing", "min_quantity"], name="tier_unique_min_qty"),
            models.CheckConstraint(condition=Q(min_quantity__gt=0), name="tier_min_qty_positive"),
            models.CheckConstraint(condition=Q(unit_price__gt=0), name="tier_price_positive"),
        ]


class LivestockDetail(models.Model):
    """Extra facts for a livestock lot (FR-05)."""

    class Sex(models.TextChoices):
        MALE = "male", "Male"
        FEMALE = "female", "Female"
        MIXED = "mixed", "Mixed lot"

    class Vaccination(models.TextChoices):
        UP_TO_DATE = "up_to_date", "Up to date"
        PARTIAL = "partial", "Partially vaccinated"
        NONE = "none", "Not vaccinated"

    listing = models.OneToOneField(Listing, on_delete=models.CASCADE, related_name="livestock")
    species = models.CharField(max_length=40)
    breed = models.CharField(max_length=60)
    sex = models.CharField(max_length=6, choices=Sex.choices)
    age_months = models.PositiveSmallIntegerField(validators=[MaxValueValidator(360)])
    avg_weight_kg = models.DecimalField(max_digits=7, decimal_places=2,
                                        validators=[MinValueValidator(Decimal("0.1"))])
    tag_or_brand = models.CharField("ID tag / brand mark", max_length=60)
    vaccination_status = models.CharField(max_length=10, choices=Vaccination.choices)
    vaccination_notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(avg_weight_kg__gt=0), name="livestock_weight_positive"),
        ]

    def __str__(self):
        return f"{self.breed} {self.species}"


def validate_image_size(f):
    if f.size > 5 * 1024 * 1024:
        raise ValidationError("Each photo must be 5 MB or smaller.")


class ListingPhoto(models.Model):
    MAX_PER_LISTING = 5

    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="photos")
    image = models.ImageField(upload_to="listings/%Y/%m/", validators=[
        FileExtensionValidator(["jpg", "jpeg", "png"]), validate_image_size])
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]


class StockAdjustment(models.Model):
    """Manual stock change with a reason (FR-05); feeds the wastage report."""

    class Reason(models.TextChoices):
        SPOILAGE = "spoilage", "Spoilage"
        EXPIRED = "expired", "Expired / written off"
        DEATH = "death", "Animal death"
        THEFT = "theft", "Theft"
        RECOUNT = "recount", "Recount correction"
        RESTOCK = "restock", "Restock / new harvest"

    LOSS_REASONS = {Reason.SPOILAGE, Reason.EXPIRED, Reason.DEATH, Reason.THEFT}

    listing = models.ForeignKey(Listing, on_delete=models.PROTECT, related_name="adjustments")
    quantity_change = models.DecimalField(**QTY)
    unit_price_at_time = models.DecimalField(**MONEY)
    reason = models.CharField(max_length=10, choices=Reason.choices)
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   related_name="+")
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=~Q(quantity_change=0), name="adjustment_not_zero"),
        ]

    @property
    def value_lost(self):
        if self.reason in self.LOSS_REASONS and self.quantity_change < 0:
            return money(-self.quantity_change * self.unit_price_at_time)
        return Decimal("0.00")


class Cart(models.Model):
    """Saved in the database so it survives logout (FR-07)."""

    buyer = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                 related_name="cart")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Cart of {self.buyer}"


class CartItem(models.Model):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="items")
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="+")
    quantity = models.DecimalField(**QTY, validators=[MinValueValidator(Decimal("0.001"))])
    added_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["added_at"]
        constraints = [
            models.UniqueConstraint(fields=["cart", "listing"], name="cart_one_line_per_listing"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="cartitem_quantity_positive"),
        ]

    @property
    def unit_price(self):
        return self.listing.unit_price_for(self.quantity)

    @property
    def line_total(self):
        return money(self.unit_price * self.quantity)


class FavouriteFarm(models.Model):
    """A buyer following a farm (buyer tools)."""

    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="favourite_farms")
    farmer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="followers")
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["buyer", "farmer"], name="favourite_farm_once"),
            models.CheckConstraint(condition=~Q(buyer=F("farmer")), name="favourite_not_self"),
        ]


class StockAlert(models.Model):
    """'Notify me when available' for a commodity (optionally province / max price)."""

    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="stock_alerts")
    commodity = models.ForeignKey(Commodity, on_delete=models.CASCADE, related_name="alerts")
    province = models.CharField(max_length=3, blank=True)
    max_price = models.DecimalField(**MONEY, null=True, blank=True,
                                    validators=[MinValueValidator(CENT)])
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)
    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["buyer", "commodity", "province"],
                                    condition=Q(is_active=True), name="one_active_alert_per_product"),
        ]

    def matches(self, listing):
        if self.province and listing.farmer.farm.province != self.province:
            return False
        if self.max_price is not None and listing.current_price > self.max_price:
            return False
        return True


class SearchLog(models.Model):
    """Searches buyers made, used to find unmet demand for the planting planner."""

    query = models.CharField(max_length=100)
    commodity = models.ForeignKey(Commodity, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="searches")
    province = models.CharField(max_length=3, blank=True)
    results = models.PositiveIntegerField()
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="+")
    created_at = models.DateTimeField(default=clock.now, editable=False, db_index=True)

    class Meta:
        ordering = ["-created_at"]
