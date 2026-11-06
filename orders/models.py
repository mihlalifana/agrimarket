from decimal import Decimal

from django.conf import settings
from django.core.validators import (FileExtensionValidator, MaxValueValidator,
                                    MinValueValidator, RegexValidator)
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from accounts.models import Province
from marketplace.models import CENT, MONEY, QTY, Listing, Unit
from core import clock

User = settings.AUTH_USER_MODEL


class DeliveryRate(models.Model):
    """Admin-maintained delivery rate table (FR-07).

    Fee per farmer sub-order = base_fee + per_ton_fee x estimated tons.
    """

    province = models.CharField(max_length=3, choices=Province.choices, unique=True)
    base_fee = models.DecimalField(**MONEY, validators=[MinValueValidator(0)])
    per_ton_fee = models.DecimalField(**MONEY, validators=[MinValueValidator(0)])

    class Meta:
        ordering = ["province"]
        constraints = [
            models.CheckConstraint(condition=Q(base_fee__gte=0) & Q(per_ton_fee__gte=0),
                                   name="deliveryrate_not_negative"),
        ]

    def __str__(self):
        return self.get_province_display()


class Order(models.Model):
    class DeliveryMethod(models.TextChoices):
        DELIVERY = "delivery", "Delivery"
        COLLECTION = "collection", "Farm collection"

    class PaymentMethod(models.TextChoices):
        CARD = "card", "Card"
        COD = "cod", "Cash on delivery"   # no longer offered; kept so older orders still read correctly

    number = models.CharField(max_length=20, unique=True, null=True, editable=False)
    order_date = models.DateField(
        default=timezone.localdate, db_index=True,
        help_text="Date the sale happened. Reports and every timestamp of the order follow this date.")
    buyer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="orders")
    delivery_method = models.CharField(max_length=10, choices=DeliveryMethod.choices)
    delivery_address = models.TextField(blank=True)
    delivery_province = models.CharField(max_length=3, choices=Province.choices, blank=True)
    payment_method = models.CharField(max_length=4, choices=PaymentMethod.choices)
    goods_total = models.DecimalField(**MONEY, default=Decimal("0"))
    vat_total = models.DecimalField(**MONEY, default=Decimal("0"))
    delivery_total = models.DecimalField(**MONEY, default=Decimal("0"))
    grand_total = models.DecimalField(**MONEY, default=Decimal("0"))
    created_at = models.DateTimeField(default=clock.now, editable=False, db_index=True)
    # Internal only: the sale happened on an earlier day and was entered afterwards.
    # Never shown to users; it lets later steps (packing, delivery) also be dated in the past.
    recorded_late = models.BooleanField(default=False, editable=False)

    class Meta:
        ordering = ["-order_date", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(goods_total__gte=0) & Q(vat_total__gte=0)
                & Q(delivery_total__gte=0) & Q(grand_total__gte=0),
                name="order_totals_not_negative"),
            models.CheckConstraint(
                condition=Q(delivery_method="collection") | ~Q(delivery_address=""),
                name="order_delivery_needs_address"),
        ]

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new and not self.number:
            self.number = f"ORD-{self.order_date:%Y}-{self.pk:06d}"
            Order.objects.filter(pk=self.pk).update(number=self.number)

    def __str__(self):
        return self.number or f"Order {self.pk}"


class SubOrder(models.Model):
    """The part of an order supplied by one farmer (FR-07)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting payment"
        APPROVED = "approved", "Confirmed"
        PACKED = "packed", "Packed"
        IN_TRANSIT = "in_transit", "In transit"
        DELIVERED = "delivered", "Delivered"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        DISPUTED = "disputed", "Disputed"

    # FR-09: the only allowed moves.
    TRANSITIONS = {
        Status.PENDING: {Status.APPROVED, Status.CANCELLED},
        Status.APPROVED: {Status.PACKED, Status.CANCELLED},
        Status.PACKED: {Status.IN_TRANSIT, Status.DELIVERED},  # collection skips transit
        Status.IN_TRANSIT: {Status.DELIVERED},
        Status.DELIVERED: {Status.COMPLETED, Status.DISPUTED},
        Status.DISPUTED: {Status.COMPLETED},  # resolved by the farmer
        Status.COMPLETED: set(),
        Status.CANCELLED: set(),
    }
    OPEN_STATUSES = {Status.PENDING, Status.APPROVED, Status.PACKED, Status.IN_TRANSIT,
                     Status.DELIVERED, Status.DISPUTED}

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="suborders")
    farmer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="suborders")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING,
                              db_index=True)
    goods_subtotal = models.DecimalField(**MONEY)
    vat_amount = models.DecimalField(**MONEY)
    delivery_fee = models.DecimalField(**MONEY)
    commission_amount = models.DecimalField(**MONEY, default=Decimal("0"))
    # Card payments split by Paystack: the farm's share (goods + VAT - commission) went straight into
    # the farm's bank account; AgriMarket kept the commission and the delivery fee.
    paid_to_farm = models.BooleanField(default=False, editable=False)
    farm_share = models.DecimalField(**MONEY, null=True, blank=True, editable=False)
    # Kept back from the farm's share to repay the farm's part of earlier refunds (see Refund).
    recovered = models.DecimalField(**MONEY, default=Decimal("0"), editable=False)
    farmer_vat_number = models.CharField(max_length=10, blank=True)
    invoice_number = models.CharField(max_length=20, unique=True, null=True, editable=False)
    distributor = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="deliveries",
                                    limit_choices_to={"role": "distributor"})
    scheduled_date = models.DateField(null=True, blank=True)
    time_slot = models.CharField(max_length=20, blank=True)
    delivery_note = models.CharField(max_length=200, blank=True)
    rejection_reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)
    approved_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["order", "farmer"], name="one_suborder_per_farmer"),
            models.CheckConstraint(
                condition=Q(goods_subtotal__gte=0) & Q(vat_amount__gte=0)
                & Q(delivery_fee__gte=0) & Q(commission_amount__gte=0),
                name="suborder_amounts_not_negative"),
            models.CheckConstraint(
                condition=~Q(status="delivered") | ~Q(delivery_note=""),
                name="suborder_delivered_needs_note"),
            models.CheckConstraint(
                condition=~Q(status="completed") | Q(completed_at__isnull=False),
                name="suborder_completed_has_date"),
        ]

    def __str__(self):
        return f"{self.order.number} / {self.farmer}"

    @property
    def total(self):
        return self.goods_subtotal + self.vat_amount + self.delivery_fee

    @property
    def is_delivery(self):
        return self.order.delivery_method == Order.DeliveryMethod.DELIVERY

    @property
    def progress(self):
        """Steps for the status bar: [(label, 'done'|'now'|'todo'), ...]."""
        flow = [self.Status.PENDING, self.Status.APPROVED, self.Status.PACKED]
        if self.is_delivery:
            flow.append(self.Status.IN_TRANSIT)
        flow += [self.Status.DELIVERED, self.Status.COMPLETED]
        current = self.Status.DELIVERED if self.status == self.Status.DISPUTED else self.status
        if current not in flow:
            return []
        idx = flow.index(current)
        labels = {}
        if self.order.payment_method == "cod":
            labels[self.Status.PENDING] = "Order placed"   # cash orders never wait for payment
        if not self.is_delivery:
            labels[self.Status.DELIVERED] = "Collected"
        return [(labels.get(s, s.label), "done" if i < idx else "now" if i == idx else "todo")
                for i, s in enumerate(flow)]

    def can_move_to(self, new_status):
        return new_status in self.TRANSITIONS[self.status]

    @property
    def refunded_amount(self):
        return sum((r.amount for r in self.refunds.filter(status=Refund.Status.PROCESSED)),
                   Decimal("0"))


class OrderItem(models.Model):
    """A line on a sub-order. Name, unit and price are copied at purchase time
    so later edits to the listing never change a past order."""

    suborder = models.ForeignKey(SubOrder, on_delete=models.PROTECT, related_name="items")
    listing = models.ForeignKey(Listing, on_delete=models.PROTECT, related_name="order_items")
    product_name = models.CharField(max_length=100)
    product_code = models.CharField(max_length=20, blank=True)
    grade = models.CharField(max_length=20, blank=True)
    batch_number = models.CharField(max_length=20)
    unit = models.CharField(max_length=5, choices=Unit.choices)
    unit_price = models.DecimalField(**MONEY)
    quantity = models.DecimalField(**QTY)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=4)
    line_total = models.DecimalField(**MONEY)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="orderitem_quantity_positive"),
            models.CheckConstraint(condition=Q(unit_price__gt=0), name="orderitem_price_positive"),
            models.CheckConstraint(condition=Q(vat_rate__gte=0) & Q(vat_rate__lte=1),
                                   name="orderitem_vat_rate_range"),
            models.UniqueConstraint(fields=["suborder", "listing"], name="orderitem_one_line_per_listing"),
        ]

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"


class StatusChange(models.Model):
    """Timeline of a sub-order's status (FR-09)."""

    suborder = models.ForeignKey(SubOrder, on_delete=models.CASCADE, related_name="history")
    from_status = models.CharField(max_length=10, blank=True)
    to_status = models.CharField(max_length=10, choices=SubOrder.Status.choices)
    changed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["created_at", "id"]


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting payment"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"
        PARTIALLY_REFUNDED = "part_refund", "Partially refunded"

    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="payment")
    method = models.CharField(max_length=4, choices=Order.PaymentMethod.choices)
    status = models.CharField(max_length=11, choices=Status.choices, default=Status.PENDING)
    amount = models.DecimalField(**MONEY)
    # NFR-03: never the full card number.
    card_last4 = models.CharField(max_length=4, blank=True,
                                  validators=[RegexValidator(r"^\d{4}$")])
    card_brand = models.CharField(max_length=20, blank=True)
    # "simulated" = built-in test card; "paystack" = paid on Paystack's own page (card details never reach us)
    gateway = models.CharField(max_length=10, blank=True, default="")
    reference = models.CharField(max_length=40, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gte=0), name="payment_amount_not_negative"),
            models.CheckConstraint(
                condition=~Q(method="card") | ~Q(status="paid") | ~Q(card_last4="") | Q(gateway__in=["payfast", "paystack"]),
                name="payment_card_paid_has_last4"),
        ]

    def __str__(self):
        return f"{self.order} {self.get_status_display()}"

    @property
    def status_label(self):
        """What the buyer and farm should read: cash orders are not 'awaiting' anything, they are paid on handover."""
        if self.method == "cod" and self.status == self.Status.PENDING:
            return "Pay on collection" if self.order.delivery_method == "collection" else "Pay on delivery"
        return self.get_status_display()

    @property
    def badge(self):
        return "s-cod" if self.method == "cod" and self.status == self.Status.PENDING else f"s-{self.status}"

    @property
    def gateway_name(self):
        return {"paystack": "Paystack", "payfast": "PayFast"}.get(self.gateway, "the card gateway")

    @property
    def online(self):
        return self.gateway in ("payfast", "paystack")


class GatewayEvent(models.Model):
    """Every message to and from the card gateway, and what we made of it (kept for audits)."""

    class Kind(models.TextChoices):
        REDIRECT = "redirect", "Buyer sent to the card page"
        ITN = "itn", "Payment notification (ITN)"
        QUERY = "query", "Status check"
        RETURN = "return", "Buyer returned"
        CANCEL = "cancel", "Buyer cancelled the card page"

    payment = models.ForeignKey(Payment, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    status = models.CharField(max_length=20, blank=True, help_text="Gateway status, e.g. success")
    accepted = models.BooleanField(default=False)
    problems = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)
    ip = models.CharField(max_length=45, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()} {self.status}"


class Refund(models.Model):
    """Money given back to a buyer, through Paystack to the card they paid with.

    Each side returns its own part: the farm its share of the refunded goods, AgriMarket its
    commission on them and, if the goods were never delivered, the delivery fee. AgriMarket's
    Paystack balance sends the whole amount back at once; the farm's part is then kept back from
    the farm's next card sales (SubOrder.recovered).
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Being refunded"
        PROCESSED = "processed", "Refunded"

    suborder = models.ForeignKey(SubOrder, on_delete=models.PROTECT, related_name="refunds")
    amount = models.DecimalField(**MONEY, validators=[MinValueValidator(CENT)])
    farm_amount = models.DecimalField("farm's part", **MONEY, default=Decimal("0"))
    commission_amount = models.DecimalField("AgriMarket's commission returned", **MONEY, default=Decimal("0"))
    delivery_amount = models.DecimalField("delivery fee returned", **MONEY, default=Decimal("0"))
    reason = models.CharField(max_length=200)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    gateway_reference = models.CharField(max_length=40, blank=True)
    problem = models.CharField(max_length=300, blank=True, help_text="Why the last refund attempt failed.")
    processed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", help_text="Who decided the refund.")
    processed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="refund_amount_positive"),
            models.CheckConstraint(
                condition=Q(farm_amount__gte=0) & Q(commission_amount__gte=0) & Q(delivery_amount__gte=0),
                name="refund_parts_not_negative"),
            models.CheckConstraint(
                condition=Q(amount=F("farm_amount") + F("commission_amount") + F("delivery_amount")),
                name="refund_parts_add_up"),
        ]

    @property
    def platform_amount(self):
        return self.commission_amount + self.delivery_amount


class Dispute(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        FULL_REFUND = "full", "Resolved: full refund"
        PARTIAL_REFUND = "partial", "Resolved: partial refund"
        REJECTED = "rejected", "Resolved: claim rejected"

    suborder = models.OneToOneField(SubOrder, on_delete=models.PROTECT, related_name="dispute")
    raised_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    photo = models.ImageField(upload_to="disputes/%Y/%m/", blank=True,
                              validators=[FileExtensionValidator(["jpg", "jpeg", "png"])])
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    resolution_note = models.TextField(blank=True)
    refund_amount = models.DecimalField(**MONEY, default=Decimal("0"))
    resolved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(refund_amount__gte=0), name="dispute_refund_not_negative"),
            models.CheckConstraint(condition=Q(status="open") | ~Q(resolution_note=""),
                                   name="dispute_resolution_has_note"),
        ]


class Review(models.Model):
    """One review per listing per completed sub-order (FR-09)."""

    suborder = models.ForeignKey(SubOrder, on_delete=models.CASCADE, related_name="reviews")
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="reviews")
    buyer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reviews")
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True)
    is_hidden = models.BooleanField(default=False)
    hidden_reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["suborder", "listing"], name="one_review_per_purchase"),
            models.CheckConstraint(condition=Q(rating__gte=1) & Q(rating__lte=5), name="review_rating_1_to_5"),
            models.CheckConstraint(condition=Q(is_hidden=False) | ~Q(hidden_reason=""),
                                   name="review_hidden_has_reason"),
        ]


class QuoteRequest(models.Model):
    """A buyer asks farmers to quote for a large or specific order (RFQ)."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ACCEPTED = "accepted", "Quote accepted"
        CLOSED = "closed", "Closed"

    number = models.CharField(max_length=20, unique=True, null=True, editable=False)
    buyer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="quote_requests")
    commodity = models.ForeignKey("marketplace.Commodity", on_delete=models.PROTECT, related_name="quote_requests")
    grade = models.CharField(max_length=20, blank=True, help_text="Leave empty for any grade")
    quantity = models.DecimalField(**QTY, validators=[MinValueValidator(Decimal("0.001"))])
    unit = models.CharField(max_length=5, choices=Unit.choices)
    needed_by = models.DateField()
    delivery_province = models.CharField(max_length=3, choices=Province.choices)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN, db_index=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="rfq_quantity_positive"),
        ]

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new and not self.number:
            self.number = f"RFQ-{self.created_at:%Y}-{self.pk:05d}"
            QuoteRequest.objects.filter(pk=self.pk).update(number=self.number)

    def __str__(self):
        return self.number or f"RFQ {self.pk}"


class Quote(models.Model):
    """A farmer's price for a quote request, backed by one of their listings."""

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Not chosen"
        WITHDRAWN = "withdrawn", "Withdrawn"

    request = models.ForeignKey(QuoteRequest, on_delete=models.CASCADE, related_name="quotes")
    farmer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="quotes")
    listing = models.ForeignKey(Listing, on_delete=models.PROTECT, related_name="quotes")
    unit_price = models.DecimalField(**MONEY, validators=[MinValueValidator(CENT)])
    note = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=9, choices=Status.choices, default=Status.SUBMITTED)
    order = models.OneToOneField(Order, null=True, blank=True, on_delete=models.PROTECT, related_name="quote")
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["unit_price", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["request", "farmer"], name="one_quote_per_farmer"),
            models.CheckConstraint(condition=Q(unit_price__gt=0), name="quote_price_positive"),
            models.UniqueConstraint(fields=["request"], condition=Q(status="accepted"),
                                    name="one_accepted_quote_per_request"),
        ]

    @property
    def total(self):
        from marketplace.models import money
        return money(self.unit_price * self.request.quantity)
