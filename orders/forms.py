from decimal import Decimal

from django import forms
from django.utils import timezone

from accounts.forms import StyledMixin
from accounts.models import Province, User

from .models import Dispute, Order


class CheckoutForm(StyledMixin, forms.Form):
    order_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), required=False,
                                 label="Order date",
                                 help_text="The day the sale took place.")
    delivery_method = forms.ChoiceField(choices=Order.DeliveryMethod.choices,
                                        widget=forms.RadioSelect, initial=Order.DeliveryMethod.DELIVERY)
    delivery_address = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    delivery_province = forms.ChoiceField(choices=[("", "Choose province")] + Province.choices,
                                          required=False)
    payment_method = forms.ChoiceField(choices=[(Order.PaymentMethod.CARD, "Card")], widget=forms.RadioSelect,
                                       initial=Order.PaymentMethod.CARD)
    card_number = forms.CharField(required=False, max_length=23,
                                  widget=forms.TextInput(attrs={"autocomplete": "off",
                                                                "inputmode": "numeric",
                                                                "placeholder": "Card number"}))
    card_expiry = forms.CharField(required=False, max_length=5,
                                  widget=forms.TextInput(attrs={"placeholder": "MM/YY"}))
    card_cvv = forms.CharField(required=False, max_length=4,
                               widget=forms.PasswordInput(attrs={"autocomplete": "off"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from core.models import SiteSettings
        start, today = SiteSettings.date_window()
        f = self.fields["order_date"]
        f.initial = today
        f.widget.attrs.update({"min": start.isoformat(), "max": today.isoformat()})
        f.help_text = (f"The day the sale took place ({start:%d %b %Y} to today)."
                       if SiteSettings.load().allow_backdated_orders else "")
        if not SiteSettings.load().allow_backdated_orders:
            f.widget = forms.HiddenInput()

    def clean_order_date(self):
        from core.models import validate_record_date
        d = self.cleaned_data.get("order_date") or timezone.localdate()
        validate_record_date(d, "Order date")
        return d

    def clean(self):
        d = super().clean()
        from .services import card_available
        if not card_available():
            raise forms.ValidationError("Card payments are not set up yet, so orders can't be placed right now. "
                                        "Please try again later.")
        if d.get("delivery_method") == Order.DeliveryMethod.DELIVERY:
            if not d.get("delivery_address", "").strip():
                self.add_error("delivery_address", "Enter the delivery address.")
            if not d.get("delivery_province"):
                self.add_error("delivery_province", "Choose the delivery province.")
        from .services import uses_gateway
        past = d.get("order_date") and d["order_date"] < timezone.localdate()
        if d.get("payment_method") == Order.PaymentMethod.CARD and (past or not uses_gateway()):
            if not d.get("card_number"):
                self.add_error("card_number", "Enter the card number.")
            exp = d.get("card_expiry", "")
            try:
                mm, yy = exp.split("/")
                mm, yy = int(mm), 2000 + int(yy)
                today = timezone.localdate()
                if not 1 <= mm <= 12 or (yy, mm) < (today.year, today.month):
                    raise ValueError
            except ValueError:
                self.add_error("card_expiry", "Enter a valid future expiry date as MM/YY.")
            cvv = d.get("card_cvv", "")
            if not (cvv.isdigit() and len(cvv) in (3, 4)):
                self.add_error("card_cvv", "Enter the 3 or 4 digit security code.")
        return d


class ReasonForm(StyledMixin, forms.Form):
    note = forms.CharField(max_length=200, required=False, widget=forms.TextInput())


class ScheduleForm(StyledMixin, forms.Form):
    SLOTS = [("08:00-12:00", "Morning 08:00-12:00"), ("12:00-16:00", "Afternoon 12:00-16:00"),
             ("16:00-18:00", "Late 16:00-18:00")]
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    slot = forms.ChoiceField(choices=SLOTS)

    def clean_date(self):
        d = self.cleaned_data["date"]
        if d <= timezone.localdate():
            raise forms.ValidationError("Delivery date must be tomorrow or later.")
        return d


class FarmerScheduleForm(StyledMixin, forms.Form):
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    slot = forms.ChoiceField(choices=ScheduleForm.SLOTS)
    distributor = forms.ModelChoiceField(
        queryset=User.objects.none(), label="Available driver",
        help_text="Only active drivers in your farm's province without another job in this slot are shown.")

    def __init__(self, *args, suborder, **kwargs):
        super().__init__(*args, **kwargs)
        from .services import available_distributors

        province = suborder.farmer.farm.province
        date = self.initial.get("date")
        slot = self.initial.get("slot", "")
        if self.is_bound:
            try:
                date = self.fields["date"].clean(self.data.get("date"))
            except forms.ValidationError:
                date = None
            slot = self.data.get("slot", "")
        if date and slot in dict(ScheduleForm.SLOTS):
            self.fields["distributor"].queryset = available_distributors(
                province, date, slot, exclude_suborder=suborder)
        else:
            self.fields["distributor"].queryset = available_distributors(province)

class DisputeForm(StyledMixin, forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), max_length=1000)
    photo = forms.ImageField(required=False)


class ResolveDisputeForm(StyledMixin, forms.Form):
    outcome = forms.ChoiceField(label="Your answer", choices=[
        (Dispute.Status.FULL_REFUND, "Refund the goods in full"),
        (Dispute.Status.PARTIAL_REFUND, "Refund part of it"),
        (Dispute.Status.REJECTED, "Reject the complaint")])
    refund_amount = forms.DecimalField(required=False, min_value=Decimal("0.01"), decimal_places=2,
                                       help_text="Only for a partial refund (goods + VAT).")
    note = forms.CharField(label="Message to the buyer", widget=forms.Textarea(attrs={"rows": 2}))


class ReviewForm(StyledMixin, forms.Form):
    rating = forms.TypedChoiceField(coerce=int, choices=[(i, f"{i} ★") for i in range(5, 0, -1)])
    comment = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), max_length=1000)


class QuoteRequestForm(StyledMixin, forms.ModelForm):
    class Meta:
        from .models import QuoteRequest
        model = QuoteRequest
        fields = ["commodity", "grade", "quantity", "unit", "needed_by", "delivery_province", "notes"]
        widgets = {"needed_by": forms.DateInput(attrs={"type": "date"}), "grade": forms.Select(),
                   "notes": forms.Textarea(attrs={"rows": 2})}
        labels = {"commodity": "Product (standard list)"}

    def __init__(self, *args, buyer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.buyer = buyer
        from marketplace.models import Commodity
        cs = Commodity.objects.filter(is_active=True).select_related("category")
        self.fields["commodity"].queryset = cs
        grades = sorted({g for c in cs for g in c.grade_list})
        self.fields["grade"].widget.choices = [("", "Any grade")] + [(g, g) for g in grades]
        self.commodity_map = {c.pk: {"units": c.unit_list, "grades": c.grade_list} for c in cs}

    def clean(self):
        d = super().clean()
        c, unit, grade, qty = d.get("commodity"), d.get("unit"), d.get("grade"), d.get("quantity")
        if c and unit and unit not in c.unit_list:
            self.add_error("unit", f"{c.name} is sold by: {', '.join(c.unit_list)}.")
        if c and grade and grade not in c.grade_list:
            self.add_error("grade", f"Official grades for {c.name}: {', '.join(c.grade_list) or 'none'}.")
        if unit == "head" and qty is not None and qty != qty.to_integral_value():
            self.add_error("quantity", "Livestock is counted in whole head.")
        nb = d.get("needed_by")
        if nb and nb <= timezone.localdate():
            self.add_error("needed_by", "Choose a date after today.")
        if self.buyer and c and qty:
            from .models import QuoteRequest
            dup = QuoteRequest.objects.filter(buyer=self.buyer, commodity=c, grade=grade or "", quantity=qty,
                                              unit=unit, status=QuoteRequest.Status.OPEN).first()
            if dup:
                self.add_error(None, f"You already have an open request for this ({dup.number}).")
        return d


class QuoteForm(StyledMixin, forms.Form):
    listing = forms.ModelChoiceField(queryset=None, label="Supply from my listing")
    unit_price = forms.DecimalField(min_value=Decimal("0.01"), decimal_places=2, max_digits=12,
                                    label="My price per unit (R)")
    note = forms.CharField(max_length=200, required=False, label="Note to buyer",
                           help_text="e.g. delivery time, loading arrangements")

    def __init__(self, *args, farmer=None, rfq=None, **kwargs):
        super().__init__(*args, **kwargs)
        from marketplace.models import Listing
        qs = Listing.objects.sellable().filter(farmer=farmer, commodity=rfq.commodity, unit=rfq.unit)
        if rfq.grade:
            qs = qs.filter(grade=rfq.grade)
        self.fields["listing"].queryset = qs
        self.fields["listing"].label_from_instance = (
            lambda l: f"{l.batch_number} · {l.grade or l.name} · {l.qty_display} in stock · listed at R{l.price}")
