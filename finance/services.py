"""Financial calculations: farm earnings, AgriMarket income and reports (FR-15)."""
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Coalesce, TruncDay, TruncMonth
from django.utils import timezone

from accounts.models import User
from core.models import AuditLog
from core.services import audit, notify
from marketplace.models import StockAdjustment, money
from orders.models import Order, OrderItem, Payment, Refund, SubOrder

ZERO = Decimal("0")
S = SubOrder.Status
MONEY_OUT = DecimalField(max_digits=14, decimal_places=2)


def _sum(qs, field):
    return qs.aggregate(v=Coalesce(Sum(field), ZERO, output_field=MONEY_OUT))["v"]


def day_bounds(start, end):
    """Aware datetimes covering whole local days start..end inclusive."""
    tz = timezone.get_current_timezone()
    return (timezone.make_aware(datetime.combine(start, time.min), tz),
            timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz))


def month_bounds(period):
    first = period.replace(day=1)
    last = first.replace(day=monthrange(first.year, first.month)[1])
    return first, last


# ---------------------------------------------------------------- farm earnings and AgriMarket income
def farm_month(farmer, period):
    """A farm's money for one month (by completion date).

    sales       goods + VAT of sub-orders completed in the month
    commission  AgriMarket's commission on them (kept by Paystack at payment time)
    paid        what Paystack paid into the farm's bank for them
    kept_back   part of the farm's share kept back to repay earlier refunds
    refunds     the farm's part of refunds sent to buyers this month
    """
    first, last = month_bounds(period)
    lo, hi = day_bounds(first, last)
    done = SubOrder.objects.filter(farmer=farmer, status=S.COMPLETED, completed_at__gte=lo, completed_at__lt=hi)
    sales = _sum(done, "goods_subtotal") + _sum(done, "vat_amount")
    refunds = _sum(Refund.objects.filter(suborder__farmer=farmer, status=Refund.Status.PROCESSED,
                                         processed_at__gte=lo, processed_at__lt=hi), "farm_amount")
    return {"period": first, "orders": done.count(), "sales": sales, "commission": _sum(done, "commission_amount"),
            "paid": _sum(done.filter(paid_to_farm=True), "farm_share"), "kept_back": _sum(done, "recovered"),
            "refunds": refunds}


def farm_earnings(farmer, months=6):
    """The farm's last `months` months, newest first."""
    today = timezone.localdate().replace(day=1)
    out, d = [], today
    for _ in range(months):
        out.append(farm_month(farmer, d))
        d = (d - timedelta(days=1)).replace(day=1)
    return out


def platform_income(months=6):
    """AgriMarket's own income per month: commission and delivery fees on completed orders,
    less the commission it gave back on refunds. Totals only, no individual sales."""
    today = timezone.localdate().replace(day=1)
    out, d = [], today
    for _ in range(months):
        first, last = month_bounds(d)
        lo, hi = day_bounds(first, last)
        done = SubOrder.objects.filter(status=S.COMPLETED, completed_at__gte=lo, completed_at__lt=hi)
        refunds = Refund.objects.filter(status=Refund.Status.PROCESSED, processed_at__gte=lo, processed_at__lt=hi)
        commission = _sum(done, "commission_amount")
        given_back = _sum(refunds.filter(suborder__status=S.COMPLETED), "commission_amount")
        delivery = _sum(done, "delivery_fee")
        out.append({"period": first, "orders": done.count(), "commission": commission,
                    "commission_back": given_back, "delivery": delivery,
                    "income": commission - given_back + delivery, "refunds": _sum(refunds, "amount")})
        d = (first - timedelta(days=1)).replace(day=1)
    return out


# ---------------------------------------------------------------- reports
def live_suborders(start, end, farmer=None):
    """Sub-orders (not cancelled) whose order date is between start and end inclusive."""
    qs = SubOrder.objects.filter(
        order__payment__status__in=[Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED],
        order__order_date__gte=start, order__order_date__lte=end,
    ).exclude(status=S.CANCELLED)
    return qs.filter(farmer=farmer) if farmer else qs


def sales_report(start, end, group="day", farmer=None):
    trunc = TruncDay if group == "day" else TruncMonth
    rows = (live_suborders(start, end, farmer)
            .annotate(period=trunc("order__order_date"))
            .values("period")
            .annotate(orders=Count("order", distinct=True), goods=Sum("goods_subtotal"),
                      vat=Sum("vat_amount"), delivery=Sum("delivery_fee"))
            .order_by("period"))
    rows = list(rows)
    for r in rows:
        r["total"] = r["goods"] + r["vat"] + r["delivery"]
    totals = {k: sum((r[k] for r in rows), ZERO) for k in ("goods", "vat", "delivery", "total")}
    totals["orders"] = live_suborders(start, end, farmer).values("order").distinct().count()
    return rows, totals


def best_sellers(start, end, by="revenue", limit=10, farmer=None):
    items = OrderItem.objects.filter(
        suborder__order__payment__status__in=[Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED],
        suborder__order__order_date__gte=start, suborder__order__order_date__lte=end,
    ).exclude(suborder__status=S.CANCELLED)
    if farmer:
        items = items.filter(suborder__farmer=farmer)
    rows = (items.values("product_name", "unit")
            .annotate(quantity=Sum("quantity"), revenue=Sum("line_total"),
                      orders=Count("suborder", distinct=True))
            .order_by("-revenue" if by == "revenue" else "-quantity", "product_name"))
    return list(rows[:limit])


def wastage_report(start, end, farmer=None):
    lo, hi = day_bounds(start, end)
    adj = (StockAdjustment.objects
           .filter(reason__in=StockAdjustment.LOSS_REASONS, quantity_change__lt=0,
                   created_at__gte=lo, created_at__lt=hi)
           .select_related("listing__farmer__farm"))
    if farmer:
        adj = adj.filter(listing__farmer=farmer)
    rows = list(adj)
    total = sum((a.value_lost for a in rows), ZERO)
    by_reason = {}
    for a in rows:
        by_reason[a.get_reason_display()] = by_reason.get(a.get_reason_display(), ZERO) + a.value_lost
    return rows, total, by_reason


def monthly_series(months=None, farmer=None):
    """Sales per month from the start of the records window to this month, gaps filled with zero."""
    from core.models import SiteSettings
    start, today = SiteSettings.date_window()
    first = start.replace(day=1)
    if months:
        first = max(first, (today.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1))
    months = (today.year - first.year) * 12 + today.month - first.month + 1
    rows, _ = sales_report(first, today, group="month", farmer=farmer)
    found = {r["period"].strftime("%Y-%m"): r["goods"] for r in rows}
    labels, values = [], []
    y, m = first.year, first.month
    for _ in range(months):
        key = f"{y}-{m:02d}"
        labels.append(date(y, m, 1).strftime("%b %Y"))
        values.append(float(found.get(key, 0)))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return labels, values


def breakdown(start, end, field):
    """Goods sales by category or by farm province."""
    key = {"category": "listing__category__name", "province": "suborder__farmer__farm__province"}[field]
    rows = (OrderItem.objects.filter(
            suborder__order__payment__status__in=[Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED],
            suborder__order__order_date__gte=start, suborder__order__order_date__lte=end,
    ).exclude(suborder__status=S.CANCELLED)
            .values(key).annotate(revenue=Sum("line_total")).order_by("-revenue"))
    return [(r[key], r["revenue"]) for r in rows]
