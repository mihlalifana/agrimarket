"""Farm records: fields, plantings, harvests, herds, treatments and production costs."""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone

from marketplace.models import CENT, MONEY, QTY, Commodity, Kind, Unit
from core import clock

User = settings.AUTH_USER_MODEL


def season_for(day):
    """Season label = the year the crop was planted, e.g. '2026'."""
    return str(day.year)


def _window(errors, field, value, label):
    from core.models import validate_record_date
    try:
        validate_record_date(value, label)
    except ValidationError as e:
        errors[field] = e.messages[0]


class Field(models.Model):
    farmer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="fields")
    name = models.CharField(max_length=60)
    size_ha = models.DecimalField("Size (ha)", max_digits=9, decimal_places=2,
                                  validators=[MinValueValidator(Decimal("0.01"))])
    soil_type = models.CharField(max_length=40, blank=True)
    irrigated = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["farmer", "name"], name="field_name_unique_per_farm",
                                    violation_error_message="You already have a field with this name."),
            models.CheckConstraint(condition=Q(size_ha__gt=0), name="field_size_positive"),
        ]

    def __str__(self):
        return f"{self.name} ({self.size_ha} ha)"


class Planting(models.Model):
    class Status(models.TextChoices):
        GROWING = "growing", "Growing"
        HARVESTING = "harvesting", "Harvesting"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    farmer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="plantings")
    field = models.ForeignKey(Field, on_delete=models.PROTECT, related_name="plantings")
    commodity = models.ForeignKey(Commodity, on_delete=models.PROTECT, related_name="plantings",
                                  limit_choices_to={"category__kind": Kind.PRODUCE})
    cultivar = models.CharField(max_length=60, blank=True)
    season = models.CharField(max_length=7, editable=False)
    planted_on = models.DateField()
    area_ha = models.DecimalField("Area planted (ha)", max_digits=9, decimal_places=2,
                                  validators=[MinValueValidator(Decimal("0.01"))])
    expected_harvest = models.DateField()
    unit = models.CharField("Yield unit", max_length=5, choices=Unit.choices)
    expected_yield = models.DecimalField(**QTY, validators=[MinValueValidator(0)])
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.GROWING)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["-planted_on"]
        constraints = [
            models.CheckConstraint(condition=Q(area_ha__gt=0), name="planting_area_positive"),
            models.CheckConstraint(condition=Q(expected_yield__gte=0), name="planting_yield_not_negative"),
            models.CheckConstraint(condition=Q(expected_harvest__gte=models.F("planted_on")),
                                   name="planting_harvest_after_planting"),
            models.CheckConstraint(condition=~Q(unit="head"), name="planting_unit_not_head"),
            models.UniqueConstraint(fields=["field", "commodity", "planted_on"], name="planting_once_per_day",
                                    violation_error_message="This crop is already recorded on this field for that date."),
        ]

    def __str__(self):
        return f"{self.commodity} – {self.field.name} ({self.season})"

    def clean(self):
        errors = {}
        if self.field_id and self.area_ha and self.area_ha > self.field.size_ha:
            errors["area_ha"] = f"Area cannot be larger than the field ({self.field.size_ha} ha)."
        if self.planted_on and self.expected_harvest and self.expected_harvest < self.planted_on:
            errors["expected_harvest"] = "Expected harvest must be after planting."
        if self.commodity_id and self.unit and self.unit not in self.commodity.unit_list:
            errors["unit"] = f"{self.commodity} is measured in: {', '.join(self.commodity.unit_list)}."
        _window(errors, "planted_on", self.planted_on, "Planting date")
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.season = season_for(self.planted_on)
        super().save(*args, **kwargs)

    @property
    def harvested_qty(self):
        return self.harvests.aggregate(t=Sum("quantity"))["t"] or Decimal("0")

    @property
    def total_cost(self):
        return self.costs.aggregate(t=Sum("amount"))["t"] or Decimal("0")

    @property
    def days_to_harvest(self):
        return (self.expected_harvest - timezone.localdate()).days


class Harvest(models.Model):
    planting = models.ForeignKey(Planting, on_delete=models.PROTECT, related_name="harvests")
    harvested_on = models.DateField()
    quantity = models.DecimalField(**QTY, validators=[MinValueValidator(Decimal("0.001"))])
    grade = models.CharField(max_length=20, blank=True)
    notes = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["-harvested_on"]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="harvest_quantity_positive"),
            models.UniqueConstraint(fields=["planting", "harvested_on", "grade"], name="harvest_once_per_day_grade",
                                    violation_error_message="A harvest of this grade is already recorded for that date. "
                                                            "Edit the quantity instead of adding it again."),
        ]

    def __str__(self):
        return f"{self.planting.commodity} {self.quantity} {self.planting.unit} on {self.harvested_on}"

    def clean(self):
        errors = {}
        _window(errors, "harvested_on", self.harvested_on, "Harvest date")
        if self.planting_id:
            if self.harvested_on and self.harvested_on < self.planting.planted_on:
                errors["harvested_on"] = "Harvest date cannot be before planting."
            grades = self.planting.commodity.grade_list
            if grades and self.grade not in grades:
                errors["grade"] = f"Choose an official grade: {', '.join(grades)}."
            if self.harvested_on and self.planting.pk:
                from advisory.models import harvest_blocked_until
                spray = harvest_blocked_until(self.planting, self.harvested_on)
                if spray:
                    errors["harvested_on"] = (
                        f"{spray.product} was sprayed on {spray.applied_on:%d %b %Y}. Its pre-harvest interval "
                        f"({spray.phi_days} days, Act 36 label) ends on {spray.safe_from:%d %b %Y} — "
                        f"the crop may not be harvested before then.")
        if errors:
            raise ValidationError(errors)

    @property
    def unit(self):
        return self.planting.unit

    @property
    def is_listed(self):
        return hasattr(self, "listing") and self.listing is not None


class Herd(models.Model):
    """A group of animals managed together (or a single animal)."""

    farmer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="herds")
    name = models.CharField(max_length=60)
    commodity = models.ForeignKey(Commodity, on_delete=models.PROTECT, related_name="herds",
                                  limit_choices_to={"category__kind": Kind.LIVESTOCK})
    breed = models.CharField(max_length=60, blank=True)
    head_count = models.PositiveIntegerField("Number of animals at start",
                                             validators=[MinValueValidator(1)])
    started_on = models.DateField()
    tag_range = models.CharField("ID tags / brand", max_length=80, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["farmer", "name"], name="herd_name_unique_per_farm",
                                    violation_error_message="You already have a herd with this name."),
            models.CheckConstraint(condition=Q(head_count__gt=0), name="herd_head_count_positive"),
        ]

    def __str__(self):
        return f"{self.name} ({self.commodity})"

    def clean(self):
        errors = {}
        _window(errors, "started_on", self.started_on, "Start date")
        if errors:
            raise ValidationError(errors)

    @property
    def active_withdrawal(self):
        today = timezone.localdate()
        return self.treatments.filter(withdrawal_ends__gt=today).order_by("-withdrawal_ends").first()

    @property
    def total_cost(self):
        return self.costs.aggregate(t=Sum("amount"))["t"] or Decimal("0")


class Treatment(models.Model):
    """Stock remedy given to a herd. Its withdrawal period blocks sales of that herd."""

    class Kind(models.TextChoices):
        VACCINE = "vaccine", "Vaccination"
        TREATMENT = "treatment", "Treatment (medicine)"
        DIP = "dip", "Dipping / parasite control"
        DEWORM = "deworm", "Deworming"

    herd = models.ForeignKey(Herd, on_delete=models.PROTECT, related_name="treatments")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    remedy = models.CharField("Remedy / product name", max_length=80)
    registration_no = models.CharField(
        "Act 36 registration no.", max_length=30, blank=True,
        help_text="Registration number on the label (Act 36 of 1947), e.g. G1234")
    reason = models.CharField(max_length=120, blank=True)
    animals = models.CharField("Animals treated", max_length=120, blank=True,
                               help_text="Tag numbers, or leave empty for the whole herd")
    dose = models.CharField(max_length=60, blank=True)
    treated_on = models.DateField()
    withdrawal_days = models.PositiveSmallIntegerField(
        "Withdrawal period (days)", help_text="From the product label. 0 if none.")
    withdrawal_ends = models.DateField(editable=False)
    administered_by = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["-treated_on"]
        indexes = [models.Index(fields=["herd", "withdrawal_ends"])]
        constraints = [
            models.CheckConstraint(condition=Q(withdrawal_ends__gte=models.F("treated_on")),
                                   name="treatment_withdrawal_after_treatment"),
            models.UniqueConstraint(fields=["herd", "remedy", "treated_on"], name="treatment_once_per_day",
                                    violation_error_message="This treatment is already recorded for this herd on that date."),
        ]

    def __str__(self):
        return f"{self.remedy} on {self.treated_on}"

    def clean(self):
        errors = {}
        _window(errors, "treated_on", self.treated_on, "Treatment date")
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # First day the animals may be sold again.
        self.withdrawal_ends = self.treated_on + timedelta(days=self.withdrawal_days)
        super().save(*args, **kwargs)

    @property
    def in_withdrawal(self):
        return self.withdrawal_ends > timezone.localdate()


class CostEntry(models.Model):
    """A production cost for one planting or one herd (used for profit per product)."""

    class Category(models.TextChoices):
        SEED = "seed", "Seed / animals bought"
        FERTILISER = "fertiliser", "Fertiliser"
        CHEMICALS = "chemicals", "Crop chemicals"
        FEED = "feed", "Feed"
        VET = "vet", "Vet and medicine"
        LABOUR = "labour", "Labour"
        FUEL = "fuel", "Fuel and transport"
        IRRIGATION = "irrigation", "Water and electricity"
        PACKAGING = "packaging", "Packaging"
        MECHANISATION = "machinery", "Machinery and repairs"
        OTHER = "other", "Other"

    farmer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="costs")
    planting = models.ForeignKey(Planting, null=True, blank=True, on_delete=models.CASCADE,
                                 related_name="costs")
    herd = models.ForeignKey(Herd, null=True, blank=True, on_delete=models.CASCADE, related_name="costs")
    category = models.CharField(max_length=12, choices=Category.choices)
    description = models.CharField(max_length=120, blank=True)
    amount = models.DecimalField(**MONEY, validators=[MinValueValidator(CENT)])
    incurred_on = models.DateField()
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["-incurred_on"]
        verbose_name_plural = "cost entries"
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="cost_amount_positive"),
            # Exactly one of planting / herd.
            models.CheckConstraint(
                condition=(Q(planting__isnull=False) & Q(herd__isnull=True))
                | (Q(planting__isnull=True) & Q(herd__isnull=False)),
                name="cost_for_planting_or_herd"),
        ]

    def __str__(self):
        return f"{self.get_category_display()} R{self.amount}"

    def clean(self):
        errors = {}
        _window(errors, "incurred_on", self.incurred_on, "Cost date")
        same = CostEntry.objects.filter(farmer_id=self.farmer_id, planting_id=self.planting_id, herd_id=self.herd_id,
                                        category=self.category, amount=self.amount, incurred_on=self.incurred_on,
                                        description=self.description).exclude(pk=self.pk)
        if self.farmer_id and same.exists():
            errors["amount"] = "This exact cost is already recorded."
        if errors:
            raise ValidationError(errors)
