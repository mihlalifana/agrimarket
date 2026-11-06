from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from . import clock


class SiteSettings(models.Model):
    """Single-row table of business rules (NFR-08: settings, not code)."""

    vat_rate = models.DecimalField(
        max_digits=5, decimal_places=4, default=Decimal("0.1500"),
        validators=[MinValueValidator(0), MaxValueValidator(1)],
        help_text="0.1500 = 15%",
    )
    commission_rate = models.DecimalField(
        max_digits=5, decimal_places=4, default=Decimal("0.0500"),
        validators=[MinValueValidator(0), MaxValueValidator(1)],
        help_text="0.0500 = 5% of goods value (excl. VAT and delivery)",
    )
    near_expiry_days = models.PositiveSmallIntegerField(default=3)
    auto_complete_days = models.PositiveSmallIntegerField(default=3)
    dispute_window_days = models.PositiveSmallIntegerField(default=3)
    records_start = models.DateField(
        null=True, blank=True,
        help_text="Earliest date allowed for orders and farm records. Empty = 1 March of the current year.")
    allow_backdated_orders = models.BooleanField(
        default=True, help_text="Let buyers choose an earlier order date at checkout, to capture past sales.")
    # Card payments ----------------------------------------------------------------
    card_gateway = models.CharField(
        "card payments", max_length=16, default="paystack_test",
        choices=[("simulated", "Simulated test card (no internet needed)"),
                 ("paystack_test", "Paystack test mode (real gateway, no real money)"),
                 ("paystack_live", "Paystack live (real money)")])
    paystack_secret_key = models.CharField(
        "Paystack secret key", max_length=120, blank=True,
        help_text="Paystack dashboard → Settings → API Keys & Webhooks. Test mode uses the key starting sk_test_.")
    card_payment_minutes = models.PositiveSmallIntegerField(
        "minutes to pay by card", default=60,
        help_text="Card orders not paid online within this time are cancelled and the stock released.")
    max_failed_logins = models.PositiveSmallIntegerField(default=5)
    lockout_minutes = models.PositiveSmallIntegerField(default=15)

    class Meta:
        verbose_name = "site settings"
        verbose_name_plural = "site settings"
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="sitesettings_single_row"),
        ]

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    def date_window(cls):
        """(first allowed date, today) for dates of things that already happened."""
        today = timezone.localdate()
        start = cls.load().records_start or date(today.year, 3, 1)
        if start > today:  # e.g. in January/February use last year's March
            start = date(today.year - 1, 3, 1)
        return start, today

    def __str__(self):
        return "Site settings"


class Notification(models.Model):
    """In-app notification (FR-13)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="notifications")
    title = models.CharField(max_length=120)
    body = models.TextField(blank=True)
    link = models.CharField(max_length=255, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "is_read"])]

    def __str__(self):
        return f"{self.user} - {self.title}"


class MessageLog(models.Model):
    """Simulated SMS/email outbox (FR-13)."""

    class Channel(models.TextChoices):
        SMS = "sms", "SMS"
        EMAIL = "email", "Email"

    channel = models.CharField(max_length=5, choices=Channel.choices)
    recipient = models.CharField(max_length=254)
    subject = models.CharField(max_length=150, blank=True)
    text = models.TextField()
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.channel} to {self.recipient}"


class AuditLog(models.Model):
    """Append-only record of changes (FR-03).

    Rows can be created but never updated or deleted through the
    application.
    """

    class Action(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        DELETE = "delete", "Delete"
        STATUS = "status", "Status change"
        LOGIN = "login", "Login"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=10, choices=Action.choices)
    object_type = models.CharField(max_length=50)
    object_id = models.CharField(max_length=40)
    object_repr = models.CharField(max_length=200)
    field = models.CharField(max_length=60, blank=True)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["object_type", "object_id"])]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise PermissionDenied("Audit log entries cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Audit log entries cannot be deleted.")

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.object_repr}"


def validate_record_date(value, label="Date"):
    """Raise ValidationError unless value is inside the allowed record window."""
    from django.core.exceptions import ValidationError
    if value is None:
        return
    start, today = SiteSettings.date_window()
    if value > today:
        raise ValidationError(f"{label} cannot be in the future.")
    if value < start:
        raise ValidationError(f"{label} must be on or after {start:%d %B %Y}.")
