"""Agronomist advisory: crop advice the farmer has asked for.

An agronomist only sees farms whose farmer linked them (POPIA: the farmer
decides who sees their records) and never sees buyers, orders, payments or
bank details. The agronomist adds soil tests, field visits, recommendations
and spray programmes; the farmer decides what to do with them.

Spray records enforce the pre-harvest interval (PHI) printed on every
pesticide label registered under Act 36 of 1947: produce may not be harvested
before the PHI has passed.
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from accounts.models import Province
from core import clock

User = settings.AUTH_USER_MODEL


def _window(errors, field, value, label):
    from core.models import validate_record_date
    try:
        validate_record_date(value, label)
    except ValidationError as e:
        errors[field] = e.messages[0]


class AgronomistProfile(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for verification"
        APPROVED = "approved", "Verified"
        REJECTED = "rejected", "Rejected"

    class Body(models.TextChoices):
        SACNASP = "sacnasp", "SACNASP (registered natural scientist)"
        SASCP = "sascp", "SASCP member (crop production society)"
        OTHER = "other", "Other professional body"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="agronomist")
    body = models.CharField("Professional body", max_length=10, choices=Body.choices)
    registration_no = models.CharField(
        "Registration / membership number", max_length=30,
        help_text="Checked by the administrator before you can advise farms.")
    qualification = models.CharField(max_length=120, help_text="e.g. BSc Agric (Agronomy), Stellenbosch")
    company = models.CharField(max_length=120, blank=True, help_text="Co-op, input supplier or own practice")
    crops = models.CharField("Crops you advise on", max_length=200, blank=True,
                             help_text="e.g. maize, soya, vegetables")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    rejection_reason = models.CharField(max_length=200, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["body", "registration_no"], name="agronomist_registration_unique",
                                    violation_error_message="This registration number is already used."),
        ]

    def __str__(self):
        return f"{self.user.get_full_name()} ({self.registration})"

    @property
    def registration(self):
        return f"{self.get_body_display().split(' ')[0]} {self.registration_no}"


class FarmLink(models.Model):
    """A farmer gives an agronomist access to their farm records."""

    farmer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="agronomist_links")
    agronomist = models.ForeignKey(User, on_delete=models.CASCADE, related_name="farm_links")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["farmer", "agronomist"], condition=Q(active=True),
                                    name="one_active_link_per_pair",
                                    violation_error_message="This agronomist already advises your farm."),
        ]

    def __str__(self):
        return f"{self.agronomist} → {self.farmer}"


def linked(agronomist, farmer):
    return FarmLink.objects.filter(agronomist=agronomist, farmer=farmer, active=True).exists()


class SoilTest(models.Model):
    """Laboratory soil analysis for one field (top 0–20 cm)."""

    field = models.ForeignKey("farm.Field", on_delete=models.CASCADE, related_name="soil_tests")
    sampled_on = models.DateField()
    lab = models.CharField("Laboratory", max_length=80)
    ph_kcl = models.DecimalField("pH (KCl)", max_digits=3, decimal_places=1,
                                 validators=[MinValueValidator(Decimal("3.0")), MaxValueValidator(Decimal("9.5"))])
    p_mg_kg = models.DecimalField("Phosphorus P (mg/kg, Bray 1)", max_digits=6, decimal_places=1,
                                  validators=[MinValueValidator(0)])
    k_mg_kg = models.DecimalField("Potassium K (mg/kg)", max_digits=6, decimal_places=1,
                                  validators=[MinValueValidator(0)])
    organic_carbon = models.DecimalField("Organic carbon (%)", max_digits=4, decimal_places=2,
                                         null=True, blank=True, validators=[MinValueValidator(0)])
    notes = models.CharField(max_length=200, blank=True)
    added_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["-sampled_on"]
        constraints = [
            models.UniqueConstraint(fields=["field", "sampled_on"], name="soil_test_once_per_day",
                                    violation_error_message="A soil test for this field on that date is already recorded."),
        ]

    def __str__(self):
        return f"{self.field.name} soil test {self.sampled_on}"

    def clean(self):
        errors = {}
        _window(errors, "sampled_on", self.sampled_on, "Sample date")
        if errors:
            raise ValidationError(errors)

    @property
    def ph_note(self):
        if self.ph_kcl < Decimal("5.0"):
            return "Acidic — lime is likely needed for most crops."
        if self.ph_kcl > Decimal("7.5"):
            return "Alkaline — watch for zinc and iron shortages."
        return "Within the range most crops prefer."

    @property
    def p_note(self):
        return "Low phosphorus" if self.p_mg_kg < 20 else "Phosphorus adequate"


class FieldVisit(models.Model):
    class Problem(models.TextChoices):
        NONE = "none", "No problem found"
        PEST = "pest", "Insect pest"
        DISEASE = "disease", "Disease"
        WEED = "weed", "Weeds"
        NUTRIENT = "nutrient", "Nutrient shortage"
        WATER = "water", "Water stress"
        WEATHER = "weather", "Weather damage (frost, hail)"
        OTHER = "other", "Other"

    class Severity(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High — act now"

    farmer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="field_visits")
    agronomist = models.ForeignKey(User, on_delete=models.PROTECT, related_name="visits_made")
    field = models.ForeignKey("farm.Field", on_delete=models.CASCADE, related_name="visits")
    planting = models.ForeignKey("farm.Planting", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="visits")
    visited_on = models.DateField()
    growth_stage = models.CharField(max_length=60, blank=True, help_text="e.g. V6 (6 leaves), flowering, fruit set")
    problem = models.CharField(max_length=10, choices=Problem.choices, default=Problem.NONE)
    problem_detail = models.CharField(max_length=120, blank=True, help_text="e.g. Fall armyworm, early blight")
    severity = models.CharField(max_length=6, choices=Severity.choices, blank=True)
    observations = models.TextField()
    photo = models.ImageField(upload_to="visits/%Y/%m/", blank=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["-visited_on", "-created_at"]

    def __str__(self):
        return f"Visit {self.field.name} {self.visited_on}"

    def clean(self):
        errors = {}
        _window(errors, "visited_on", self.visited_on, "Visit date")
        if self.problem != self.Problem.NONE and not self.severity:
            errors["severity"] = "How serious is it?"
        if self.planting_id and self.field_id and self.planting.field_id != self.field_id:
            errors["planting"] = "This crop is not on the chosen field."
        if errors:
            raise ValidationError(errors)


class Recommendation(models.Model):
    class Kind(models.TextChoices):
        FERTILISER = "fertiliser", "Fertiliser / lime"
        SPRAY = "spray", "Spray (pest, disease, weeds)"
        IRRIGATION = "irrigation", "Irrigation"
        PLANTING = "planting", "Planting (crop, cultivar, density)"
        HARVEST = "harvest", "Harvest timing"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        OPEN = "open", "To do"
        DONE = "done", "Done"
        DECLINED = "declined", "Not done"

    farmer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="recommendations")
    agronomist = models.ForeignKey(User, on_delete=models.PROTECT, related_name="recommendations_made")
    field = models.ForeignKey("farm.Field", null=True, blank=True, on_delete=models.CASCADE,
                              related_name="recommendations")
    planting = models.ForeignKey("farm.Planting", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="recommendations")
    visit = models.ForeignKey(FieldVisit, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="recommendations")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    advice = models.TextField(help_text="What to do, how much and how — e.g. 150 kg/ha LAN top-dressing at 6 weeks")
    do_by = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    farmer_note = models.CharField(max_length=200, blank=True)
    done_on = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["status", "do_by", "-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.advice[:40]}"

    @property
    def overdue(self):
        return self.status == self.Status.OPEN and self.do_by and self.do_by < timezone.localdate()


REG_NO = RegexValidator(r"^L\d{3,5}$", "Act 36 numbers look like L1234 (on the label).")


class RegisteredRemedy(models.Model):
    """An agricultural remedy (pesticide) in AgriMarket's verified catalogue.

    Entries are copied from the product label / the Registrar's register under Act 36 of 1947.
    Only VERIFIED entries can be used in a spray record; the label's PHI for the crop is taken
    from RemedyUse, so nobody types a waiting period or registration number by hand.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for verification"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn (registration cancelled)"

    registration_no = models.CharField("Act 36 registration no.", max_length=12, unique=True,
                                       validators=[REG_NO])
    trade_name = models.CharField(max_length=80)
    active_ingredient = models.CharField(max_length=120)
    holder = models.CharField("Registration holder", max_length=120, blank=True)
    source = models.CharField(
        "Source checked", max_length=200,
        help_text="Where the details were checked, e.g. 'Product label, revision 2024' or 'Registrar list, Oct 2026'.")
    label_photo = models.ImageField(upload_to="remedy_labels/", blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    submitted_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="remedies_submitted")
    verified_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="remedies_verified")
    verified_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True, help_text="Reason for rejection or withdrawal")
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["trade_name"]
        constraints = [
            models.CheckConstraint(condition=~Q(status="verified") | Q(verified_at__isnull=False),
                                   name="remedy_verified_has_date"),
            models.CheckConstraint(condition=~Q(status__in=["withdrawn", "rejected"]) | ~Q(note=""),
                                   name="remedy_withdrawn_has_reason"),
        ]

    def __str__(self):
        return f"{self.trade_name} ({self.registration_no})"

    def use_for(self, commodity):
        return self.uses.filter(commodity=commodity).first()


class RemedyUse(models.Model):
    """One crop on the label, with that crop's pre-harvest interval (PHI)."""

    remedy = models.ForeignKey(RegisteredRemedy, on_delete=models.CASCADE, related_name="uses")
    commodity = models.ForeignKey("marketplace.Commodity", on_delete=models.PROTECT, related_name="remedy_uses",
                                  verbose_name="Crop")
    phi_days = models.PositiveSmallIntegerField("Pre-harvest interval (days)", validators=[MaxValueValidator(365)],
                                                help_text="As printed on the label for this crop.")
    max_rate = models.CharField("Label rate", max_length=60, blank=True, help_text="e.g. 150-250 ml/ha")
    targets = models.CharField("Pests / diseases on the label", max_length=200, blank=True)

    class Meta:
        ordering = ["commodity__name"]
        constraints = [
            models.UniqueConstraint(fields=["remedy", "commodity"], name="remedy_use_once_per_crop",
                                    violation_error_message="This crop is already on the label entry."),
        ]

    def __str__(self):
        return f"{self.remedy} on {self.commodity}: PHI {self.phi_days} days"


def usable_remedies(commodity):
    """Verified catalogue entries registered for this crop, with that crop's PHI."""
    return (RemedyUse.objects.filter(commodity=commodity, remedy__status=RegisteredRemedy.Status.VERIFIED)
            .select_related("remedy").order_by("remedy__trade_name"))


class SprayRecord(models.Model):
    """A pesticide application on a crop, with its pre-harvest interval (PHI).

    The product must come from the verified catalogue and be registered for this crop. The
    registration number, active ingredient and PHI are copied from the catalogue when the
    record is saved (a snapshot, so a later catalogue change never rewrites history).
    """

    planting = models.ForeignKey("farm.Planting", on_delete=models.CASCADE, related_name="sprays")
    remedy = models.ForeignKey(RegisteredRemedy, null=True, blank=True, on_delete=models.PROTECT,
                               related_name="sprays", verbose_name="Product (from the verified list)")
    product = models.CharField("Product (trade name)", max_length=80, editable=False)
    registration_no = models.CharField("Act 36 registration no.", max_length=12, editable=False)
    active_ingredient = models.CharField(max_length=120, editable=False)
    target = models.CharField(max_length=80, help_text="Pest, disease or weed it was used against")
    rate = models.CharField(max_length=40, help_text="e.g. 250 ml/ha")
    applied_on = models.DateField()
    phi_days = models.PositiveSmallIntegerField(
        "Pre-harvest interval (days)", validators=[MaxValueValidator(365)], editable=False,
        help_text="Taken from the label entry for this crop.")
    safe_from = models.DateField(editable=False)
    applied_by = models.CharField(max_length=80, blank=True)
    recommended_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name="sprays_recommended")
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["-applied_on"]
        constraints = [
            models.UniqueConstraint(fields=["planting", "product", "applied_on"], name="spray_once_per_day",
                                    violation_error_message="This spray is already recorded for that date."),
        ]

    def __str__(self):
        return f"{self.product} on {self.planting} ({self.applied_on})"

    def label_use(self):
        """The verified label entry for this product on this crop, or a ValidationError."""
        if self.remedy_id is None:
            raise ValidationError({"remedy": "Choose the product from the verified list."})
        r = self.remedy
        if r.status != RegisteredRemedy.Status.VERIFIED:
            raise ValidationError({"remedy": f"{r} is not verified for use ({r.get_status_display().lower()})."})
        use = r.use_for(self.planting.commodity) if self.planting_id else None
        if use is None:
            crop = self.planting.commodity if self.planting_id else "this crop"
            raise ValidationError({"remedy": f"{r} is not registered for use on {crop}. "
                                             f"Off-label use is not allowed (Act 36 of 1947)."})
        return use

    def clean(self):
        errors = {}
        _window(errors, "applied_on", self.applied_on, "Spray date")
        if self.planting_id and self.applied_on and self.applied_on < self.planting.planted_on:
            errors["applied_on"] = "The spray date cannot be before the crop was planted."
        if self._state.adding:
            try:
                self.label_use()
            except ValidationError as e:
                errors.update(e.message_dict)
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self._state.adding:
            # New records always take the product details and PHI from the verified label entry.
            use = self.label_use()
            self.product = self.remedy.trade_name[:80]
            self.registration_no = self.remedy.registration_no
            self.active_ingredient = self.remedy.active_ingredient
            self.phi_days = use.phi_days
        self.safe_from = self.applied_on + timedelta(days=self.phi_days)
        super().save(*args, **kwargs)

    @property
    def in_phi(self):
        return self.safe_from > timezone.localdate()


def harvest_blocked_until(planting, day):
    """The latest PHI end date that `day` falls inside, or None if harvesting is allowed."""
    s = (SprayRecord.objects.filter(planting=planting, applied_on__lte=day, safe_from__gt=day)
         .order_by("-safe_from").first())
    return s


class PestAlert(models.Model):
    """An agronomist warns all farmers growing a crop in a province."""

    class Severity(models.TextChoices):
        WATCH = "watch", "Watch"
        WARNING = "warning", "Warning"
        OUTBREAK = "outbreak", "Outbreak"

    agronomist = models.ForeignKey(User, on_delete=models.PROTECT, related_name="pest_alerts")
    commodity = models.ForeignKey("marketplace.Commodity", on_delete=models.PROTECT, related_name="pest_alerts",
                                  limit_choices_to={"category__kind": "produce"})
    province = models.CharField(max_length=3, choices=Province.choices)
    severity = models.CharField(max_length=8, choices=Severity.choices, default=Severity.WARNING)
    title = models.CharField(max_length=100, help_text="e.g. Fall armyworm in young maize")
    message = models.TextField(help_text="What to look for and what to do")
    valid_until = models.DateField(help_text="When the alert stops showing")
    farms_told = models.PositiveIntegerField(default=0, editable=False)
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.get_province_display()})"

    @property
    def is_current(self):
        return self.valid_until >= timezone.localdate()
