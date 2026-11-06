from django.contrib.auth.models import AbstractUser, UserManager
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Length
from django.utils import timezone

models.CharField.register_lookup(Length)  # allows vat_number__length in the constraint below


class Province(models.TextChoices):
    EC = "EC", "Eastern Cape"
    FS = "FS", "Free State"
    GP = "GP", "Gauteng"
    KZN = "KZN", "KwaZulu-Natal"
    LP = "LP", "Limpopo"
    MP = "MP", "Mpumalanga"
    NC = "NC", "Northern Cape"
    NW = "NW", "North West"
    WC = "WC", "Western Cape"


sa_phone_validator = RegexValidator(
    r"^(\+27|0)[6-8][0-9]{8}$",
    "Enter a South African mobile number, e.g. 0821234567 or +27821234567.",
)


class EmailUserManager(UserManager):
    """Users log in with their email; username is set to the email."""

    def _create_user(self, username, email, password, **extra):
        email = self.normalize_email(email or username).lower()
        return super()._create_user(email, email, password, **extra)

    def create_user(self, email=None, password=None, **extra):
        extra.setdefault("username", email)
        username = extra.pop("username")
        return super().create_user(username, email, password, **extra)

    def create_superuser(self, email=None, password=None, **extra):
        extra.setdefault("username", email)
        extra.setdefault("role", User.Role.ADMIN)
        username = extra.pop("username")
        return super().create_superuser(username, email, password, **extra)


class BuyerType(models.TextChoices):
    RETAIL = "retail", "Supermarket / retailer"
    PROCESSOR = "processor", "Mill / processor"
    BUTCHERY = "butchery", "Butchery / abattoir / feedlot"
    HOSPITALITY = "hospitality", "Restaurant / hospitality"
    WHOLESALE = "wholesale", "Wholesaler / market agent"
    FARMER = "farmer", "Other farmer"
    INDIVIDUAL = "individual", "Individual / household"


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        FARMER = "farmer", "Farmer"
        BUYER = "buyer", "Buyer"
        DISTRIBUTOR = "distributor", "Distributor"
        AGRONOMIST = "agronomist", "Agronomist"

    email = models.EmailField(unique=True)
    role = models.CharField(max_length=12, choices=Role.choices, default=Role.BUYER)
    phone = models.CharField(max_length=13, validators=[sa_phone_validator])
    province = models.CharField(max_length=3, choices=Province.choices)
    address = models.TextField(blank=True)
    buyer_type = models.CharField(max_length=12, choices=BuyerType.choices, blank=True,
                                  help_text="Type of business (buyers only)")
    popia_consent_at = models.DateTimeField(null=True, blank=True)
    failed_login_count = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    anonymised_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = EmailUserManager()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(role__in=["admin", "farmer", "buyer", "distributor", "agronomist"]),
                                   name="user_role_valid"),
            models.UniqueConstraint(fields=["phone"], condition=~models.Q(phone=""), name="user_phone_unique",
                                    violation_error_message="This phone number is already registered."),
        ]

    def __str__(self):
        return self.get_full_name() or self.email

    def save(self, *args, **kwargs):
        self.email = (self.email or "").lower()
        self.username = self.email or self.username
        super().save(*args, **kwargs)

    # Role helpers ------------------------------------------------------
    @property
    def is_admin_role(self):
        return self.role == self.Role.ADMIN or self.is_superuser

    @property
    def is_farmer(self):
        return self.role == self.Role.FARMER

    @property
    def is_buyer(self):
        return self.role == self.Role.BUYER

    @property
    def is_distributor(self):
        return self.role == self.Role.DISTRIBUTOR

    @property
    def is_agronomist(self):
        return self.role == self.Role.AGRONOMIST

    @property
    def is_verified_agronomist(self):
        return (self.is_agronomist and hasattr(self, "agronomist")
                and self.agronomist.status == "approved")

    @property
    def is_verified_farmer(self):
        return (self.is_farmer and hasattr(self, "farm")
                and self.farm.status == FarmerProfile.Status.APPROVED)

    @property
    def is_locked(self):
        return self.locked_until is not None and self.locked_until > timezone.now()


class FarmerProfile(models.Model):
    """Farm details for a farmer account (FR-02)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending verification"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="farm")
    farm_name = models.CharField(max_length=120)
    town = models.CharField(max_length=80)
    province = models.CharField(max_length=3, choices=Province.choices)
    brand_mark = models.CharField(
        max_length=30, blank=True,
        help_text="Registered livestock brand mark (required to sell livestock).")
    is_vat_registered = models.BooleanField(default=False)
    vat_number = models.CharField(
        max_length=10, blank=True,
        validators=[RegexValidator(r"^4\d{9}$", "A South African VAT number has exactly 10 digits and starts with 4.")])
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    rejection_reason = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    # Where card money is paid. With Paystack each farm has its own subaccount: Paystack pays the
    # farm's share of every card payment straight into this bank account and keeps AgriMarket's part.
    bank_name = models.CharField(max_length=60, blank=True)
    bank_code = models.CharField("bank", max_length=10, blank=True)
    bank_account = models.CharField("bank account number", max_length=20, blank=True,
                                    validators=[RegexValidator(r"^\d{6,16}$", "Enter the account number: 6 to 16 digits.")])
    paystack_subaccount = models.CharField(max_length=40, blank=True, editable=False,
                                           help_text="Paystack subaccount code, e.g. ACCT_abc123.")
    payout_account_note = models.CharField(max_length=300, blank=True, editable=False,
                                           help_text="Paystack's answer the last time the payout account was set up.")

    @property
    def paid_direct(self):
        """True when Paystack pays this farm's share of card payments straight into its bank account."""
        return bool(self.paystack_subaccount)

    class Meta:
        constraints = [
            # A VAT-registered farmer must have a VAT number, and vice versa.
            models.CheckConstraint(
                condition=(models.Q(is_vat_registered=True) & ~models.Q(vat_number=""))
                | (models.Q(is_vat_registered=False) & models.Q(vat_number="")),
                name="farm_vat_number_matches_flag",
            ),
            # SARS format, checked by the database too: empty, or 10 characters starting with 4
            # (the form and the model validator also check that they are all digits).
            models.CheckConstraint(
                condition=models.Q(vat_number="")
                | (models.Q(vat_number__startswith="4") & models.Q(vat_number__length=10)),
                name="farm_vat_number_sa_format",
            ),
            # A rejection must carry a reason.
            models.CheckConstraint(
                condition=~models.Q(status="rejected") | ~models.Q(rejection_reason=""),
                name="farm_rejection_has_reason",
            ),
        ]

    def __str__(self):
        return self.farm_name
