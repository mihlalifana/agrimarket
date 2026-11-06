"""Market price rules (price guide, planner data) and farmer analytics.

Privacy rule: market-wide figures for a product are only shown when at least
MIN_FARMS different farms sold it in the period. With fewer farms, someone
could work out a single farm's sales, so the figures are hidden.
"""
from calendar import month_abbr
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Max, Min, Q, Sum
from django.utils import timezone

from accounts.models import BuyerType, Province
from finance.services import monthly_series, wastage_report
from marketplace.models import Commodity, Listing, SearchLog, StockAlert, money
from orders.models import OrderItem, Payment, QuoteRequest, SubOrder

MIN_FARMS = 3
ZERO = Decimal("0")
PROVINCES = dict(Province.choices)


def _items(start, end, province=None):
    qs = OrderItem.objects.filter(
        suborder__order__payment__status__in=[Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED],
    ).exclude(suborder__status=SubOrder.Status.CANCELLED).filter(
        suborder__order__order_date__gte=start, suborder__order__order_date__lte=end)
    if province:
        qs = qs.filter(suborder__farmer__farm__province=province)
    return qs


def _stats(qs, keys):
    rows = (qs.values(*keys)
            .annotate(qty=Sum("quantity"), revenue=Sum("line_total"), low=Min("unit_price"),
                      high=Max("unit_price"), farms=Count("suborder__farmer", distinct=True),
                      orders=Count("suborder", distinct=True)))
    out = {}
    for r in rows:
        key = tuple(r[k] for k in keys)
        r["avg"] = money(r["revenue"] / r["qty"]) if r["qty"] else None
        r["low"], r["high"], r["revenue"] = money(r["low"]), money(r["high"]), money(r["revenue"])
        r["visible"] = r["farms"] >= MIN_FARMS
        out[key] = r
    return out


def pct_change(new, old):
    if not old:
        return None
    return round(float((new - old) / old * 100), 1)


def price_lookup(days=90):
    """{(commodity_id, unit): average price} for products with enough farms."""
    today = timezone.localdate()
    stats = _stats(_items(today - timedelta(days=days - 1), today), ["listing__commodity", "unit"])
    return {k: v["avg"] for k, v in stats.items() if v["visible"]}


def price_guide(commodity_id, unit, grade="", days=90):
    """Recent market price for the listing form, or None if too few farms."""
    today = timezone.localdate()
    base = _items(today - timedelta(days=days - 1), today).filter(listing__commodity=commodity_id, unit=unit)
    if grade:
        g = _stats(base.filter(listing__grade=grade), ["unit"]).get((unit,))
        if g and g["visible"]:
            return {"scope": f"grade {grade}", **_public(g)}
    c = _stats(base, ["unit"]).get((unit,))
    if c and c["visible"]:
        return {"scope": "all grades", **_public(c)}
    return None


def _public(r):
    return {"avg": str(r["avg"]), "low": str(r["low"]), "high": str(r["high"]), "farms": r["farms"],
            "qty": str(r["qty"])}


def seasonality(commodity_id, unit):
    """Average price per calendar month over all history (only months with enough farms)."""
    qs = OrderItem.objects.filter(
        suborder__order__payment__status__in=[Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED],
        listing__commodity=commodity_id, unit=unit,
    ).exclude(suborder__status=SubOrder.Status.CANCELLED)
    by_month = {}
    for it in qs.values("suborder__order__order_date", "quantity", "line_total", "suborder__farmer"):
        m = it["suborder__order__order_date"].month
        b = by_month.setdefault(m, {"qty": ZERO, "rev": ZERO, "farms": set()})
        b["qty"] += it["quantity"]
        b["rev"] += it["line_total"]
        b["farms"].add(it["suborder__farmer"])
    months = []
    for m in range(1, 13):
        b = by_month.get(m)
        ok = b and len(b["farms"]) >= MIN_FARMS and b["qty"]
        months.append({"month": month_abbr[m], "avg": money(b["rev"] / b["qty"]) if ok else None})
    priced = [x for x in months if x["avg"]]
    best = sorted(priced, key=lambda x: x["avg"], reverse=True)[:3]
    return months, [b["month"] for b in best]


def unmet_demand(days=30):
    """Products buyers want but can't find: empty searches, alerts and open quote requests."""
    since = timezone.now() - timedelta(days=days)
    signals = {}

    def add(cid, key, n=1):
        if cid:
            signals.setdefault(cid, {"searches": 0, "alerts": 0, "rfqs": 0})[key] += n

    for r in (SearchLog.objects.filter(created_at__gte=since, results=0, commodity__isnull=False)
              .values("commodity").annotate(n=Count("id"))):
        add(r["commodity"], "searches", r["n"])
    for r in StockAlert.objects.filter(is_active=True).values("commodity").annotate(n=Count("id")):
        add(r["commodity"], "alerts", r["n"])
    for r in (QuoteRequest.objects.filter(status=QuoteRequest.Status.OPEN)
              .values("commodity").annotate(n=Count("id"))):
        add(r["commodity"], "rfqs", r["n"])
    names = {c.pk: c for c in Commodity.objects.filter(pk__in=signals)}
    rows = []
    for cid, s in signals.items():
        available = Listing.objects.sellable().filter(commodity=cid).count()
        rows.append({"commodity": names[cid], **s, "score": s["searches"] + 2 * s["alerts"] + 3 * s["rfqs"],
                     "available": available})
    rows.sort(key=lambda r: (r["available"] > 0, -r["score"]))
    return rows[:10]


# ---------------------------------------------------------------- one farmer's own analytics
def farmer_kpis(farmer):
    today = timezone.localdate()
    this_start = today.replace(day=1)
    last_end = this_start - timedelta(days=1)
    last_start = last_end.replace(day=1)

    def rev(a, b):
        return money(_items(a, b).filter(suborder__farmer=farmer).aggregate(t=Sum("line_total"))["t"] or ZERO)

    from core.models import SiteSettings
    year_start, _ = SiteSettings.date_window()
    subs = SubOrder.objects.filter(
        farmer=farmer,
        order__payment__status__in=[Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED],
    ).exclude(status=SubOrder.Status.CANCELLED)
    ytd = subs.filter(order__order_date__gte=year_start)
    buyers = (subs.values("order__buyer").annotate(n=Count("id")))
    this, last = rev(this_start, today), rev(last_start, last_end)
    best = (_items(year_start, today).filter(suborder__farmer=farmer)
            .values("suborder__order__order_date__month").annotate(t=Sum("line_total")).order_by("-t").first())
    ytd_rev = money(ytd.aggregate(t=Sum("goods_subtotal"))["t"] or ZERO)
    ytd_orders = ytd.count()
    return {
        "this_month": this, "last_month": last, "vs_last": pct_change(this, last),
        "since": year_start,
        "best_month": (date(today.year, best["suborder__order__order_date__month"], 1) if best else None),
        "best_month_total": money(best["t"]) if best else ZERO,
        "ytd": ytd_rev, "ytd_orders": ytd_orders,
        "avg_order": money(ytd_rev / ytd_orders) if ytd_orders else ZERO,
        "buyers": buyers.count(), "repeat_buyers": sum(1 for b in buyers if b["n"] > 1),
    }


def farmer_breakdowns(farmer):
    from core.models import SiteSettings
    start, today = SiteSettings.date_window()
    qs = _items(start, today).filter(suborder__farmer=farmer)
    by_prov = [(PROVINCES.get(r["suborder__order__buyer__province"], "—"), r["t"])
               for r in qs.values("suborder__order__buyer__province").annotate(t=Sum("line_total")).order_by("-t")]
    types = dict(BuyerType.choices)
    by_type = [(types.get(r["suborder__order__buyer__buyer_type"], "Not stated"), r["t"])
               for r in qs.values("suborder__order__buyer__buyer_type").annotate(t=Sum("line_total")).order_by("-t")]
    market = price_lookup(days=90)
    products = []
    for r in (qs.values("listing__commodity", "unit").annotate(qty=Sum("quantity"), rev=Sum("line_total"))
              .order_by("-rev")):
        c = Commodity.objects.get(pk=r["listing__commodity"])
        avg = money(r["rev"] / r["qty"]) if r["qty"] else None
        m = market.get((c.pk, r["unit"]))
        products.append({"commodity": c, "unit": r["unit"], "qty": r["qty"], "revenue": r["rev"], "avg": avg,
                         "market": m, "vs_market": pct_change(avg, m) if (avg and m) else None})
    return by_prov, by_type, products


def farmer_wastage(farmer):
    from core.models import SiteSettings
    start, today = SiteSettings.date_window()
    rows, total, by_reason = wastage_report(start, today, farmer)
    return total, by_reason


def farmer_series(farmer):
    return monthly_series(farmer=farmer)
