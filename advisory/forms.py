from django import forms

from accounts.forms import BaseRegistrationForm, StyledMixin
from accounts.models import User
from farm.models import Field, Planting
from marketplace.models import Commodity, Kind

from .models import (AgronomistProfile, FieldVisit, PestAlert, Recommendation, RegisteredRemedy, RemedyUse,
                     SoilTest, SprayRecord)

DATE = forms.DateInput(attrs={"type": "date"})


class AgronomistRegistrationForm(BaseRegistrationForm):
    body = forms.ChoiceField(label="Professional body", choices=AgronomistProfile.Body.choices)
    registration_no = forms.CharField(label="Registration / membership number", max_length=30)
    qualification = forms.CharField(max_length=120, help_text="e.g. BSc Agric (Agronomy)")
    company = forms.CharField(max_length=120, required=False, help_text="Co-op, input supplier or own practice")
    crops = forms.CharField(label="Crops you advise on", max_length=200, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["province"].label = "Province where you work"

    def clean(self):
        data = super().clean()
        if AgronomistProfile.objects.filter(body=data.get("body"),
                                            registration_no__iexact=(data.get("registration_no") or "").strip()).exists():
            self.add_error("registration_no", "This registration number is already registered.")
        return data

    def save_agronomist(self):
        user = self.save_user(User.Role.AGRONOMIST)
        d = self.cleaned_data
        AgronomistProfile.objects.create(user=user, body=d["body"], registration_no=d["registration_no"].strip(),
                                         qualification=d["qualification"], company=d["company"], crops=d["crops"])
        return user


class LinkForm(StyledMixin, forms.Form):
    agronomist = forms.ModelChoiceField(queryset=User.objects.none(), label="Agronomist",
                                        help_text="Verified agronomists, those in your province first.")

    def __init__(self, *args, farmer=None, **kwargs):
        super().__init__(*args, **kwargs)
        from django.db.models import Case, IntegerField, Value, When
        qs = (User.objects.filter(role=User.Role.AGRONOMIST, is_active=True, agronomist__status="approved")
              .exclude(farm_links__farmer=farmer, farm_links__active=True)
              .select_related("agronomist")
              .annotate(here=Case(When(province=farmer.province, then=Value(0)), default=Value(1),
                                  output_field=IntegerField()))
              .order_by("here", "first_name"))
        self.fields["agronomist"].queryset = qs
        self.fields["agronomist"].label_from_instance = (
            lambda u: f"{u.get_full_name()} — {u.agronomist.company or u.agronomist.qualification}, "
                      f"{u.get_province_display()}")


class SoilTestForm(StyledMixin, forms.ModelForm):
    past_date_fields = ("sampled_on",)

    class Meta:
        model = SoilTest
        fields = ["field", "sampled_on", "lab", "ph_kcl", "p_mg_kg", "k_mg_kg", "organic_carbon", "notes"]
        widgets = {"sampled_on": DATE}

    def __init__(self, *args, farmer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["field"].queryset = Field.objects.filter(farmer=farmer)


class VisitForm(StyledMixin, forms.ModelForm):
    """A field visit, with an optional recommendation written at the same time."""
    past_date_fields = ("visited_on",)
    rec_kind = forms.ChoiceField(label="Recommendation (optional)", required=False,
                                 choices=[("", "No recommendation")] + Recommendation.Kind.choices)
    rec_advice = forms.CharField(label="What the farmer should do", required=False,
                                 widget=forms.Textarea(attrs={"rows": 2}))
    rec_do_by = forms.DateField(label="Do by", required=False, widget=DATE)

    class Meta:
        model = FieldVisit
        fields = ["field", "planting", "visited_on", "growth_stage", "problem", "problem_detail", "severity",
                  "observations", "photo"]
        widgets = {"visited_on": DATE, "observations": forms.Textarea(attrs={"rows": 3})}
        labels = {"planting": "Crop (optional)"}

    def __init__(self, *args, farmer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["field"].queryset = Field.objects.filter(farmer=farmer)
        self.fields["planting"].queryset = (Planting.objects.filter(farmer=farmer)
                                            .exclude(status=Planting.Status.COMPLETED)
                                            .select_related("commodity", "field"))

    def clean(self):
        d = super().clean()
        if d.get("rec_kind") and not (d.get("rec_advice") or "").strip():
            self.add_error("rec_advice", "Write the recommendation.")
        return d


class RecommendationForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = Recommendation
        fields = ["field", "planting", "kind", "advice", "do_by"]
        widgets = {"do_by": DATE, "advice": forms.Textarea(attrs={"rows": 2})}
        labels = {"planting": "Crop (optional)", "field": "Field (optional)"}

    def __init__(self, *args, farmer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["field"].queryset = Field.objects.filter(farmer=farmer)
        self.fields["planting"].queryset = (Planting.objects.filter(farmer=farmer)
                                            .exclude(status=Planting.Status.COMPLETED)
                                            .select_related("commodity", "field"))


class ResponseForm(StyledMixin, forms.Form):
    status = forms.ChoiceField(choices=[(Recommendation.Status.DONE, "Done"),
                                        (Recommendation.Status.DECLINED, "Not doing it")])
    note = forms.CharField(max_length=200, required=False)


class RemedyChoice(forms.ModelChoiceField):
    def label_from_instance(self, r):
        return f"{r.trade_name} ({r.registration_no}) - {r.active_ingredient}"


class SprayForm(StyledMixin, forms.ModelForm):
    """The product is chosen from the verified catalogue. Registration number, active ingredient and
    the PHI are not typed: they come from the label entry for the chosen crop."""
    past_date_fields = ("applied_on",)
    remedy = RemedyChoice(queryset=RegisteredRemedy.objects.none(), label="Product (verified list)",
                          help_text="Only verified products registered for this crop. Registration number and "
                                    "pre-harvest interval are taken from the label.")

    class Meta:
        model = SprayRecord
        fields = ["planting", "remedy", "target", "rate", "applied_on", "applied_by"]
        widgets = {"applied_on": DATE}
        labels = {"planting": "Crop"}

    def __init__(self, *args, farmer=None, planting=None, **kwargs):
        super().__init__(*args, **kwargs)
        verified = RegisteredRemedy.objects.filter(status=RegisteredRemedy.Status.VERIFIED)
        if planting is not None:
            self.instance.planting = planting
            del self.fields["planting"]
            self.fields["remedy"].queryset = verified.filter(uses__commodity=planting.commodity).distinct()
            crops = [planting]
        else:
            crops = (Planting.objects.filter(farmer=farmer).exclude(status=Planting.Status.COMPLETED)
                     .select_related("commodity", "field"))
            self.fields["planting"].queryset = crops
            self.fields["remedy"].queryset = verified.filter(
                uses__commodity__in={p.commodity_id for p in crops}).distinct()
        # {planting id: {remedy id: "PHI 7 days, label rate ..."}} so the page can show only the
        # products registered for the chosen crop, with the label's waiting period.
        uses = RemedyUse.objects.filter(remedy__status=RegisteredRemedy.Status.VERIFIED,
                                        commodity__in={p.commodity_id for p in crops})
        by_crop = {}
        for u in uses:
            by_crop.setdefault(u.commodity_id, {})[u.remedy_id] = (
                f"Label: pre-harvest interval {u.phi_days} days" + (f", rate {u.max_rate}" if u.max_rate else ""))
        self.remedy_map = {p.pk: by_crop.get(p.commodity_id, {}) for p in crops}
        if not self.fields["remedy"].queryset.exists():
            self.fields["remedy"].help_text = ("The verified list has no product for these crops yet. Send the product "
                                               "for verification (link below), or ask the administrator to verify "
                                               "it under Remedies.")

    @property
    def remedy_map_json(self):
        """The map as JSON that is safe inside a <script> tag."""
        import json
        data = {str(k): {str(r): t for r, t in v.items()} for k, v in self.remedy_map.items()}
        return (json.dumps(data).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


class RemedyForm(StyledMixin, forms.ModelForm):
    """Admin (or an agronomist suggesting a product) enters the label details once."""

    class Meta:
        model = RegisteredRemedy
        fields = ["trade_name", "registration_no", "active_ingredient", "holder", "source", "label_photo"]

    def clean_registration_no(self):
        return self.cleaned_data["registration_no"].strip().upper()


class RemedyUseForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = RemedyUse
        fields = ["commodity", "phi_days", "max_rate", "targets"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from marketplace.models import Commodity, Kind
        self.fields["commodity"].queryset = Commodity.objects.filter(category__kind=Kind.PRODUCE, is_active=True)


class PestAlertForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = PestAlert
        fields = ["commodity", "province", "severity", "title", "message", "valid_until"]
        widgets = {"valid_until": DATE, "message": forms.Textarea(attrs={"rows": 3})}
        labels = {"commodity": "Crop"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["commodity"].queryset = Commodity.objects.filter(category__kind=Kind.PRODUCE, is_active=True)

    def clean_valid_until(self):
        from django.utils import timezone
        d = self.cleaned_data["valid_until"]
        if d < timezone.localdate():
            raise forms.ValidationError("Choose today or a later date.")
        return d


class ReviewAgronomistForm(StyledMixin, forms.Form):
    decision = forms.ChoiceField(choices=[("approve", "Approve"), ("reject", "Reject")])
    reason = forms.CharField(required=False, max_length=200)

    def clean(self):
        d = super().clean()
        if d.get("decision") == "reject" and not (d.get("reason") or "").strip():
            self.add_error("reason", "A reason is required when rejecting.")
        return d
