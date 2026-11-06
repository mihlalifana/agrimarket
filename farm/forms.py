from django import forms

from accounts.forms import StyledMixin
from marketplace.models import Commodity, Kind

from .models import CostEntry, Field, Harvest, Herd, Planting, Treatment

DATE = forms.DateInput(attrs={"type": "date"})


class FieldForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = Field
        fields = ["name", "size_ha", "soil_type", "irrigated", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}


class PlantingForm(StyledMixin, forms.ModelForm):
    past_date_fields = ("planted_on",)
    class Meta:
        model = Planting
        fields = ["field", "commodity", "cultivar", "planted_on", "area_ha", "expected_harvest",
                  "unit", "expected_yield", "status", "notes"]
        widgets = {"planted_on": DATE, "expected_harvest": DATE, "notes": forms.Textarea(attrs={"rows": 2})}
        labels = {"commodity": "Crop (standard list)", "expected_yield": "Expected yield"}

    def __init__(self, *args, farmer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["field"].queryset = Field.objects.filter(farmer=farmer)
        self.fields["commodity"].queryset = Commodity.objects.filter(category__kind=Kind.PRODUCE, is_active=True)
        self.fields["unit"].choices = [(u, u) for u in ("ton", "kg", "crate")]
        self.commodity_map = {c.pk: {"units": [u for u in c.unit_list if u != "head"], "grades": []}
                              for c in self.fields["commodity"].queryset}
        if not self.instance.pk:
            del self.fields["status"]


class HarvestForm(StyledMixin, forms.ModelForm):
    past_date_fields = ("harvested_on",)
    class Meta:
        model = Harvest
        fields = ["harvested_on", "quantity", "grade", "notes"]
        widgets = {"harvested_on": DATE, "grade": forms.Select()}

    def __init__(self, *args, planting=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.planting = planting
        grades = planting.commodity.grade_list if planting else []
        self.fields["quantity"].label = f"Quantity ({planting.unit})" if planting else "Quantity"
        if grades:
            self.fields["grade"].widget.choices = [("", "Choose grade")] + [(g, g) for g in grades]
            self.fields["grade"].required = True
        else:
            del self.fields["grade"]


class HerdForm(StyledMixin, forms.ModelForm):
    past_date_fields = ("started_on",)
    class Meta:
        model = Herd
        fields = ["name", "commodity", "breed", "head_count", "started_on", "tag_range", "notes"]
        widgets = {"started_on": DATE, "notes": forms.Textarea(attrs={"rows": 2})}
        labels = {"commodity": "Type of animal (standard list)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["commodity"].queryset = Commodity.objects.filter(category__kind=Kind.LIVESTOCK, is_active=True)


class TreatmentForm(StyledMixin, forms.ModelForm):
    past_date_fields = ("treated_on",)
    class Meta:
        model = Treatment
        fields = ["herd", "kind", "remedy", "registration_no", "reason", "animals", "dose",
                  "treated_on", "withdrawal_days", "administered_by"]
        widgets = {"treated_on": DATE}

    def __init__(self, *args, farmer=None, herd=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["herd"].queryset = Herd.objects.filter(farmer=farmer)
        if herd is not None:  # on the herd's own page: don't ask which herd again
            self.fields["herd"].initial = herd.pk
            self.fields["herd"].widget = forms.HiddenInput()


class CostForm(StyledMixin, forms.ModelForm):
    past_date_fields = ("incurred_on",)
    target = forms.ChoiceField(label="For")

    class Meta:
        model = CostEntry
        fields = ["target", "category", "description", "amount", "incurred_on"]
        widgets = {"incurred_on": DATE}
        labels = {"amount": "Amount (R)"}

    def __init__(self, *args, farmer=None, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "Choose crop or herd")]
        choices += [(f"p{p.pk}", f"Crop: {p}") for p in
                    Planting.objects.filter(farmer=farmer).select_related("commodity", "field")]
        choices += [(f"h{h.pk}", f"Herd: {h}") for h in Herd.objects.filter(farmer=farmer).select_related("commodity")]
        self.fields["target"].choices = choices
        self.farmer = farmer

    def clean(self):
        data = super().clean()
        t = data.get("target", "")
        self.instance.planting = self.instance.herd = None
        if t.startswith("p"):
            self.instance.planting = Planting.objects.filter(pk=t[1:], farmer=self.farmer).first()
        elif t.startswith("h"):
            self.instance.herd = Herd.objects.filter(pk=t[1:], farmer=self.farmer).first()
        if not (self.instance.planting or self.instance.herd):
            self.add_error("target", "Choose the crop or herd this cost belongs to.")
        return data
