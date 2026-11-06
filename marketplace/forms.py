from decimal import Decimal

from django import forms
from django.utils import timezone

from accounts.forms import StyledMixin
from accounts.models import Province

from .models import (Category, Commodity, Kind, Listing, ListingPhoto, LivestockDetail,
                     StockAdjustment, StockAlert, Unit, validate_image_size)


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleImageField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else ([data] if data else [])
        cleaned = []
        for f in files:
            name = f.name.lower()
            if not name.endswith((".jpg", ".jpeg", ".png")):
                raise forms.ValidationError(f"{f.name}: only JPG or PNG photos are allowed.")
            validate_image_size(f)
            cleaned.append(super().clean(f, initial))
        return cleaned


class ListingForm(StyledMixin, forms.ModelForm):
    past_date_fields = ("harvest_date",)
    photos = MultipleImageField(required=False, help_text="Up to 5 JPG/PNG photos, 5 MB each.")
    tier1_min = forms.DecimalField(required=False, min_value=Decimal("0.001"), label="Bulk tier 1: from quantity")
    tier1_price = forms.DecimalField(required=False, min_value=Decimal("0.01"), decimal_places=2, label="price per unit")
    tier2_min = forms.DecimalField(required=False, min_value=Decimal("0.001"), label="Bulk tier 2: from quantity")
    tier2_price = forms.DecimalField(required=False, min_value=Decimal("0.01"), decimal_places=2, label="price per unit")
    tier3_min = forms.DecimalField(required=False, min_value=Decimal("0.001"), label="Bulk tier 3: from quantity")
    tier3_price = forms.DecimalField(required=False, min_value=Decimal("0.01"), decimal_places=2, label="price per unit")

    class Meta:
        model = Listing
        fields = ["commodity", "variety", "grade", "unit", "price", "quantity_available",
                  "min_order_qty", "low_stock_threshold", "harvest_date", "expiry_date",
                  "near_expiry_discount_pct", "herd", "description"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "harvest_date": forms.DateInput(attrs={"type": "date"}),
            "expiry_date": forms.DateInput(attrs={"type": "date"}),
            "grade": forms.Select(),
        }
        labels = {"commodity": "Product (standard list)", "price": "Price per unit (R)",
                  "quantity_available": "Quantity available",
                  "near_expiry_discount_pct": "Near-expiry discount (%)"}

    def __init__(self, *args, kind=Kind.PRODUCE, farmer=None, harvest=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind
        self.harvest = harvest
        commodities = Commodity.objects.filter(category__kind=kind, is_active=True,
                                               category__is_active=True).select_related("category")
        self.fields["commodity"].queryset = commodities
        self.fields["commodity"].label_from_instance = lambda c: f"{c.category.name} – {c.name}"
        # Data for the page script: each product's allowed units and official grades.
        self.commodity_map = {c.pk: {"units": c.unit_list, "grades": c.grade_list,
                                     "perishable": c.is_perishable, "shelf": c.shelf_life_days,
                                     "ref": c.standard_reference} for c in commodities}
        all_grades = sorted({g for c in commodities for g in c.grade_list})
        self.fields["grade"].widget.choices = [("", "—")] + [(g, g) for g in all_grades]
        self.fields["grade"].help_text = "Only the official grades for the chosen product are shown."
        if kind == Kind.LIVESTOCK:
            self.fields["unit"].choices = [(Unit.HEAD, "head")]
            self.fields["unit"].initial = Unit.HEAD
            for f in ("harvest_date", "expiry_date", "near_expiry_discount_pct", "grade"):
                del self.fields[f]
            self.fields["quantity_available"].label = "Number of head"
            self.fields["price"].label = "Price per head (R)"
            self.fields["variety"].label = "Breed"
            from farm.models import Herd
            self.fields["herd"].queryset = Herd.objects.filter(farmer=farmer) if farmer else Herd.objects.none()
            self.fields["herd"].label = "Herd (from farm records)"
            self.fields["herd"].help_text = ("Linking a herd lets buyers see its treatments, and blocks "
                                             "sales while any treatment withdrawal period is running.")
        else:
            del self.fields["herd"]
            self.fields["unit"].choices = [(u, u) for u in (Unit.KG, Unit.TON, Unit.CRATE)]
            self.fields["variety"].label = "Cultivar / variety"
        self.fields["variety"].required = False
        if harvest is not None:
            # These come from the harvest record; the farmer doesn't type them again.
            for f in ("commodity", "unit", "harvest_date", "grade", "variety"):
                if f in self.fields:
                    self.fields[f].disabled = True
            self.fields["quantity_available"].help_text = (
                f"Harvested: {harvest.quantity.normalize():f} {harvest.planting.unit}. List all of it or part of it.")
            self.fields["quantity_available"].widget.attrs["max"] = f"{harvest.quantity:f}"
        if self.instance.pk:
            # Stock is changed through "Adjust stock" so every change has a reason
            # and cannot overwrite a sale happening at the same moment.
            del self.fields["quantity_available"]
            self.fields["unit"].disabled = True
            self.fields["commodity"].disabled = True
            for i, tier in enumerate(self.instance.price_tiers.all()[:3], start=1):
                self.fields[f"tier{i}_min"].initial = tier.min_quantity
                self.fields[f"tier{i}_price"].initial = tier.unit_price

    def clean_photos(self):
        photos = self.cleaned_data.get("photos") or []
        existing = self.instance.photos.count() if self.instance.pk else 0
        if existing + len(photos) > ListingPhoto.MAX_PER_LISTING:
            raise forms.ValidationError(
                f"A listing can have at most {ListingPhoto.MAX_PER_LISTING} photos "
                f"({existing} already uploaded).")
        return photos

    def clean(self):
        data = super().clean()
        tiers = []
        for i in (1, 2, 3):
            mn, pr = data.get(f"tier{i}_min"), data.get(f"tier{i}_price")
            if (mn is None) != (pr is None):
                self.add_error(f"tier{i}_price", "Fill both the quantity and the price, or neither.")
            elif mn is not None:
                tiers.append((mn, pr))
        mins = [t[0] for t in tiers]
        if len(mins) != len(set(mins)):
            self.add_error("tier1_min", "Each tier needs a different starting quantity.")
        price = data.get("price")
        for mn, pr in tiers:
            if price and pr >= price:
                self.add_error("tier1_price", "Bulk prices must be lower than the normal price.")
                break
        self.tiers = sorted(tiers)
        q = data.get("quantity_available")
        if self.harvest is not None and q is not None and q > self.harvest.quantity:
            self.add_error("quantity_available", "You cannot list more than you harvested.")
        if self.kind == Kind.PRODUCE and not self.instance.pk:
            if not data.get("harvest_date"):
                self.add_error("harvest_date", "Enter the harvest date.")
            c = data.get("commodity")
            if c and c.is_perishable and not data.get("expiry_date"):
                self.add_error("expiry_date", f"{c.name} is perishable: enter a best-before date.")
        exp = data.get("expiry_date")
        if exp and not self.instance.pk and exp < timezone.localdate():
            self.add_error("expiry_date", "Expiry date has already passed.")
        self._check_duplicate(data)
        return data

    def _check_duplicate(self, data):
        """Refuse a second active listing of the same batch; the farmer should add stock instead."""
        c = data.get("commodity")
        if not c or self.instance.pk or not self.instance.farmer_id:
            return
        same = Listing.objects.filter(farmer_id=self.instance.farmer_id, commodity=c, status=Listing.Status.ACTIVE,
                                      unit=data.get("unit") or Unit.HEAD)
        if self.kind == Kind.PRODUCE:
            same = same.filter(grade=data.get("grade") or "", harvest_date=data.get("harvest_date"))
            what = f"{c.name}{' ' + data['grade'] if data.get('grade') else ''} harvested on that date"
        else:
            herd = data.get("herd")
            same = same.filter(herd=herd) if herd else same.filter(herd__isnull=True,
                                                                   variety__iexact=data.get("variety") or "")
            what = f"{c.name} from this {'herd' if herd else 'breed'}"
        dup = same.first()
        if dup:
            self.add_error(None, f"You already list {what} as batch {dup.batch_number}. "
                                 f"Use 'Adjust stock' on that listing to add more instead of listing it again.")
            self.duplicate_of = dup


class LivestockForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = LivestockDetail
        exclude = ["listing", "species", "breed"]
        widgets = {"vaccination_notes": forms.Textarea(attrs={"rows": 2})}


class StockAdjustmentForm(StyledMixin, forms.Form):
    direction = forms.ChoiceField(choices=[("add", "Add stock"), ("remove", "Remove stock")])
    quantity = forms.DecimalField(min_value=Decimal("0.001"), max_digits=12, decimal_places=3)
    reason = forms.ChoiceField(choices=StockAdjustment.Reason.choices)
    note = forms.CharField(max_length=200, required=False)

    def __init__(self, *args, listing=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.listing = listing

    def clean(self):
        data = super().clean()
        qty, direction, reason = data.get("quantity"), data.get("direction"), data.get("reason")
        if qty is None:
            return data
        if self.listing.unit == Unit.HEAD and qty != qty.to_integral_value():
            self.add_error("quantity", "Livestock is counted in whole head.")
        if direction == "add" and reason not in (StockAdjustment.Reason.RESTOCK, StockAdjustment.Reason.RECOUNT):
            self.add_error("reason", "Adding stock must be a restock or a recount.")
        if direction == "remove" and reason == StockAdjustment.Reason.RESTOCK:
            self.add_error("reason", "Choose why stock is being removed.")
        if self.listing.is_livestock and reason == StockAdjustment.Reason.SPOILAGE:
            self.add_error("reason", "Use 'Animal death' or another reason for livestock.")
        if not self.listing.is_livestock and reason == StockAdjustment.Reason.DEATH:
            self.add_error("reason", "'Animal death' applies to livestock only.")
        if direction == "remove" and qty > self.listing.quantity_available:
            self.add_error("quantity", f"Only {self.listing.qty_display} in stock.")
        data["change"] = qty if direction == "add" else -qty
        return data


class CatalogFilterForm(StyledMixin, forms.Form):
    SORTS = [("newest", "Newest"), ("price_asc", "Price: low to high"),
             ("price_desc", "Price: high to low"), ("rating", "Best rated")]
    q = forms.CharField(required=False, label="Search")
    category = forms.ModelChoiceField(queryset=Category.objects.filter(is_active=True),
                                      required=False, empty_label="All categories", to_field_name="slug")
    commodity = forms.ModelChoiceField(queryset=Commodity.objects.filter(is_active=True),
                                       required=False, empty_label="All products", to_field_name="code",
                                       label="Product")
    province = forms.ChoiceField(choices=[("", "All provinces")] + Province.choices, required=False)
    grade = forms.ChoiceField(choices=[("", "Any grade")], required=False)
    min_price = forms.DecimalField(required=False, min_value=0, label="Min R")
    max_price = forms.DecimalField(required=False, min_value=0, label="Max R")
    favourites = forms.BooleanField(required=False, label="Only my favourite farms")
    sort = forms.ChoiceField(choices=SORTS, required=False)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        grades = sorted({g for c in Commodity.objects.filter(is_active=True) for g in c.grade_list})
        self.fields["grade"].choices = [("", "Any grade")] + [(g, g) for g in grades]
        if not (user and user.is_authenticated and user.is_buyer):
            del self.fields["favourites"]


class AddToCartForm(forms.Form):
    quantity = forms.DecimalField(min_value=Decimal("0.001"), max_digits=12, decimal_places=3)


class StockAlertForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = StockAlert
        fields = ["commodity", "province", "max_price"]
        labels = {"commodity": "Product", "max_price": "Only if price is at most (R)"}
        widgets = {"province": forms.Select(choices=[("", "Any province")] + Province.choices)}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["commodity"].queryset = Commodity.objects.filter(is_active=True)
