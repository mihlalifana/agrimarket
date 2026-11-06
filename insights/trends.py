"""Time series for the sales graphs (line charts).

All money is goods value excluding VAT, counted on the order date, for paid
orders that were not cancelled — the same rule as every other sales figure.
"""
from collections import defaultdict
from datetime import date, timedelta

from django.db.models import Count, Sum
from django.db.models.functions import TruncMonth

from core.models import SiteSettings

from .services import MIN_FARMS, _items

TOP_PRODUCTS = 4


def _months(start, end):
    """[date(y, m, 1), ...] from start's month to end's month."""
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(date(y, m, 1))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _f(v):
    return round(float(v), 2) if v is not None else None


def farmer_trends(farmer):
    """Everything the farmer's sales graphs need, in one JSON-ready dict."""
    from farm.models import CostEntry

    start, today = SiteSettings.date_window()
    months = _months(start, today)
    labels = [m.strftime("%b") for m in months]
    mine = _items(start, today).filter(suborder__farmer=farmer)

    # 1. Money in vs money out per month, and the running profit.
    sales = {r["m"].date() if hasattr(r["m"], "date") else r["m"]: r["t"] for r in
             mine.annotate(m=TruncMonth("suborder__order__order_date")).values("m").annotate(t=Sum("line_total"))}
    costs = {r["m"].date() if hasattr(r["m"], "date") else r["m"]: r["t"] for r in
             CostEntry.objects.filter(farmer=farmer, incurred_on__gte=start, incurred_on__lte=today)
             .annotate(m=TruncMonth("incurred_on")).values("m").annotate(t=Sum("amount"))}
    sales_s = [_f(sales.get(m, 0)) for m in months]
    cost_s = [_f(costs.get(m, 0)) for m in months]
    running, total = [], 0.0
    for s, c in zip(sales_s, cost_s):
        total += s - c
        running.append(round(total, 2))

    # 2. Weekly sales for the last 12 weeks (Monday-start weeks).
    week0 = today - timedelta(days=today.weekday()) - timedelta(weeks=11)
    weekly = defaultdict(float)
    for r in mine.filter(suborder__order__order_date__gte=week0).values("suborder__order__order_date") \
            .annotate(t=Sum("line_total")):
        d = r["suborder__order__order_date"]
        weekly[d - timedelta(days=d.weekday())] += float(r["t"])
    weeks = [week0 + timedelta(weeks=i) for i in range(12)]
    week_labels = [w.strftime("%d %b") for w in weeks]
    week_values = [round(weekly.get(w, 0.0), 2) for w in weeks]

    # 3. Sales per month by product: the top products, the rest as "Other".
    by_prod = defaultdict(lambda: defaultdict(float))
    totals = defaultdict(float)
    for r in mine.annotate(m=TruncMonth("suborder__order__order_date")) \
            .values("m", "listing__commodity__name").annotate(t=Sum("line_total")):
        m = r["m"].date() if hasattr(r["m"], "date") else r["m"]
        name = r["listing__commodity__name"]
        by_prod[name][m] += float(r["t"])
        totals[name] += float(r["t"])
    ranked = sorted(totals, key=totals.get, reverse=True)
    product_series = [{"name": n, "values": [round(by_prod[n].get(m, 0.0), 2) for m in months]}
                      for n in ranked[:TOP_PRODUCTS]]
    if len(ranked) > TOP_PRODUCTS:
        product_series.append({"name": "Other", "values": [
            round(sum(by_prod[n].get(m, 0.0) for n in ranked[TOP_PRODUCTS:]), 2) for m in months]})

    # 4. My average price vs the market average, per product per month.
    #    Market months with fewer than MIN_FARMS farms selling are left empty (privacy rule).
    market_items = _items(start, today)
    prices = []
    for r in mine.values("listing__commodity", "listing__commodity__name", "unit") \
            .annotate(rev=Sum("line_total")).order_by("-rev")[:6]:
        cid, unit = r["listing__commodity"], r["unit"]
        own = {x["m"].date() if hasattr(x["m"], "date") else x["m"]: (x["r"], x["q"]) for x in
               mine.filter(listing__commodity=cid, unit=unit)
               .annotate(m=TruncMonth("suborder__order__order_date")).values("m")
               .annotate(r=Sum("line_total"), q=Sum("quantity"))}
        mkt = {x["m"].date() if hasattr(x["m"], "date") else x["m"]: (x["r"], x["q"], x["f"]) for x in
               market_items.filter(listing__commodity=cid, unit=unit)
               .annotate(m=TruncMonth("suborder__order__order_date")).values("m")
               .annotate(r=Sum("line_total"), q=Sum("quantity"),
                         f=Count("suborder__farmer", distinct=True))}
        prices.append({
            "name": f"{r['listing__commodity__name']} (R/{unit})",
            "mine": [_f(own[m][0] / own[m][1]) if m in own and own[m][1] else None for m in months],
            "market": [_f(mkt[m][0] / mkt[m][1]) if m in mkt and mkt[m][2] >= MIN_FARMS and mkt[m][1]
                       else None for m in months],
        })

    # 5. Orders and different buyers per month.
    activity = {r["m"].date() if hasattr(r["m"], "date") else r["m"]: r for r in
                farmer.suborders.filter(order__order_date__gte=start)
                .filter(pk__in=mine.values("suborder"))
                .annotate(m=TruncMonth("order__order_date")).values("m")
                .annotate(n=Count("id"), b=Count("order__buyer", distinct=True))}
    return {
        "months": labels, "sales": sales_s, "costs": cost_s, "running": running,
        "weeks": week_labels, "weekly": week_values,
        "products": product_series, "prices": prices,
        "orders": [activity[m]["n"] if m in activity else 0 for m in months],
        "buyers": [activity[m]["b"] if m in activity else 0 for m in months],
    }


def platform_trends():
    """Sales and orders per month for the whole marketplace."""
    start, today = SiteSettings.date_window()
    months = _months(start, today)
    rows = {r["m"].date() if hasattr(r["m"], "date") else r["m"]: r for r in
            _items(start, today).annotate(m=TruncMonth("suborder__order__order_date")).values("m")
            .annotate(t=Sum("line_total"), n=Count("suborder__order", distinct=True),
                      f=Count("suborder__farmer", distinct=True))}
    return {"months": [m.strftime("%b") for m in months],
            "sales": [_f(rows[m]["t"]) if m in rows else 0 for m in months],
            "orders": [rows[m]["n"] if m in rows else 0 for m in months],
            "farms": [rows[m]["f"] if m in rows else 0 for m in months]}
