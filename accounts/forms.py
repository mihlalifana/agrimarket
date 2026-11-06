from django import forms
from django.contrib.auth import authenticate, password_validation
from django.utils import timezone

from .models import FarmerProfile, Province, User



VAT_MESSAGE = ("A South African VAT number has exactly 10 digits and starts with 4 (e.g. 4123456789). "
               "Check it on your SARS VAT registration notice.")


def normalise_vat(value):
    """Keep only the digits a farmer types: '4123 456 789', '4123-456-789' or 'VAT 4123456789'."""
    v = (value or "").strip().upper()
    if v.startswith("VAT"):
        v = v[3:]
    return "".join(ch for ch in v if ch not in " -./:")


def valid_vat(v):
    return len(v) == 10 and v.isdigit() and v.startswith("4")

class StyledMixin:
    """Adds CSS classes so templates stay simple.

    Date fields listed in past_date_fields get the record window
    (1 March – today by default) as min/max in the date picker.
    """
    past_date_fields = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.past_date_fields:
            from core.models import SiteSettings
            start, today = SiteSettings.date_window()
            for name in self.past_date_fields:
                if name in self.fields:
                    self.fields[name].widget.attrs.update({"min": start.isoformat(), "max": today.isoformat()})
        for field in self.fields.values():
            w = field.widget
            if isinstance(w, forms.CheckboxInput):
                w.attrs.setdefault("class", "check")
            else:
                w.attrs.setdefault("class", "input")


class BaseRegistrationForm(StyledMixin, forms.ModelForm):
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput,
                                help_text="At least 8 characters with a letter and a digit.")
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput)
    popia_consent = forms.BooleanField(
        label="I agree that AgriMarket may store and use my personal information as described "
              "in the privacy notice (POPIA).", required=True)

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "phone", "province", "address"]
        widgets = {"address": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in ("first_name", "last_name", "phone", "province"):
            self.fields[f].required = True

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("Email already registered.")
        return email

    def clean_phone(self):
        phone = self.cleaned_data["phone"].strip().replace(" ", "")
        # +27821234567 and 0821234567 are the same number
        local = "0" + phone[3:] if phone.startswith("+27") else phone
        if User.objects.filter(phone__in=[local, "+27" + local[1:]]).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This phone number is already registered.")
        return phone

    def clean(self):
        data = super().clean()
        p1, p2 = data.get("password1"), data.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "Passwords do not match.")
        if p1:
            try:
                password_validation.validate_password(p1, self.instance)
            except forms.ValidationError as e:
                self.add_error("password1", e)
        return data

    def save_user(self, role):
        user = self.save(commit=False)
        user.role = role
        user.username = user.email
        user.popia_consent_at = timezone.now()
        user.set_password(self.cleaned_data["password1"])
        user.save()
        return user


class BuyerRegistrationForm(BaseRegistrationForm):
    class Meta(BaseRegistrationForm.Meta):
        fields = ["first_name", "last_name", "email", "phone", "buyer_type", "province", "address"]
        labels = {"buyer_type": "Type of buyer"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["buyer_type"].required = True
        self.fields["buyer_type"].help_text = "Helps farmers understand who buys their products."


class FarmerRegistrationForm(BaseRegistrationForm):
    farm_name = forms.CharField(max_length=120)
    town = forms.CharField(max_length=80, label="Farm town")
    brand_mark = forms.CharField(max_length=30, required=False,
                                 help_text="Required if you will sell livestock.")
    is_vat_registered = forms.BooleanField(required=False, label="I am VAT registered")
    vat_number = forms.CharField(
        max_length=20, required=False, label="VAT number",
        help_text="As on your SARS VAT registration: 10 digits starting with 4, e.g. 4123456789. Spaces are fine.",
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off", "placeholder": "4xxxxxxxxx"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["province"].label = "Province (where the farm is)"

    def clean_vat_number(self):
        return normalise_vat(self.cleaned_data.get("vat_number"))

    def clean(self):
        data = super().clean()
        name, town = (data.get("farm_name") or "").strip(), (data.get("town") or "").strip()
        if name and town and FarmerProfile.objects.filter(farm_name__iexact=name, town__iexact=town).exists():
            self.add_error("farm_name", f"{name} in {town} is already registered. If this is your farm, log in "
                                        f"with that account instead.")
        vat = (data.get("vat_number") or "").strip()
        if vat and FarmerProfile.objects.filter(vat_number=vat).exists():
            self.add_error("vat_number", "This VAT number is already registered to another farm.")
        if data.get("is_vat_registered") and not vat:
            self.add_error("vat_number", "Enter your VAT number.")
        if vat and not data.get("is_vat_registered"):
            self.add_error("is_vat_registered", "Tick this box if you have a VAT number.")
        if vat and not valid_vat(vat):
            self.add_error("vat_number", VAT_MESSAGE)
        return data

    def save_farmer(self):
        user = self.save_user(User.Role.FARMER)
        d = self.cleaned_data
        FarmerProfile.objects.create(
            user=user, farm_name=d["farm_name"].strip(), town=d["town"].strip(), province=user.province,
            brand_mark=d["brand_mark"], is_vat_registered=d["is_vat_registered"],
            vat_number=d["vat_number"].strip() if d["is_vat_registered"] else "")
        return user


class LoginForm(StyledMixin, forms.Form):
    email = forms.EmailField()
    password = forms.CharField(widget=forms.PasswordInput)

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user = None
        super().__init__(*args, **kwargs)

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        self.user = authenticate(self.request, email=data["email"], password=data["password"])
        if getattr(self.request, "login_locked", False):
            raise forms.ValidationError(
                "Too many failed attempts. This account is locked for 15 minutes.")
        if self.user is None:
            raise forms.ValidationError("Invalid email or password.")
        return data


class ProfileForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "phone", "province", "address", "buyer_type"]
        widgets = {"address": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.is_buyer:
            del self.fields["buyer_type"]


class FarmProfileForm(StyledMixin, forms.ModelForm):
    """Farm details plus the bank account Paystack pays the farm's card money into."""
    bank_code = forms.ChoiceField(label="Bank", required=False)

    class Meta:
        model = FarmerProfile
        fields = ["farm_name", "town", "province", "brand_mark", "bank_code", "bank_account"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from orders import paystack
        self.banks = dict(paystack.banks())
        choices = [("", "Choose your bank")] + list(self.banks.items())
        if self.instance.bank_code and self.instance.bank_code not in self.banks:
            choices.append((self.instance.bank_code, self.instance.bank_name or self.instance.bank_code))
        self.fields["bank_code"].choices = choices
        self.fields["bank_account"].help_text = ("Card payments for your sales are paid by Paystack straight "
                                                 "into this account, less AgriMarket's commission.")

    def clean(self):
        d = super().clean()
        if bool(d.get("bank_code")) != bool(d.get("bank_account")):
            self.add_error("bank_account" if d.get("bank_code") else "bank_code",
                           "Give both the bank and the account number.")
        return d

    def save(self, commit=True):
        farm = super().save(commit=False)
        farm.bank_name = self.banks.get(farm.bank_code, farm.bank_name) if farm.bank_code else ""
        if commit:
            farm.save()
        return farm


class StaffCreateForm(BaseRegistrationForm):
    field_order = ["role", "first_name", "last_name", "email", "phone", "province", "address"]
    role = forms.ChoiceField(choices=[(User.Role.DISTRIBUTOR, "Distributor"),
                                      (User.Role.ADMIN, "Admin")])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["popia_consent"].label = "The staff member has agreed to the privacy notice."


class FarmerReviewForm(StyledMixin, forms.Form):
    decision = forms.ChoiceField(choices=[("approve", "Approve"), ("reject", "Reject")])
    reason = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def clean(self):
        data = super().clean()
        if data.get("decision") == "reject" and not data.get("reason", "").strip():
            self.add_error("reason", "A reason is required when rejecting.")
        return data
