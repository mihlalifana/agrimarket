"""Profit per crop and per herd: costs from farm records, income from real sales."""
from decimal import Decimal

from django.db.models import Sum

from marketplace.models import Listing, money

from .models import Planting
from orders.models import OrderItem, Payment, SubOrder

ZERO = Decimal("0")


def _sales(items):
    agg = items.filter(suborder__order__payment__status__in=[
        Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED,
    ]).exclude(suborder__status=SubOrder.Status.CANCELLED).aggregate(
        qty=Sum("quantity"), revenue=Sum("line_total"))
    return agg["qty"] or ZERO, agg["revenue"] or ZERO


def planting_economics(p, market=None):
    cost = p.total_cost
    harvested = p.harvested_qty
    items = OrderItem.objects.filter(listing__harvest__planting=p)
    sold, revenue = _sales(items)
    in_stock = (Listing.objects.filter(harvest__planting=p, status=Listing.Status.ACTIVE)
                .aggregate(t=Sum("quantity_available"))["t"] or ZERO)
    cost_per_unit = money(cost / harvested) if harvested else None
    avg_price = money(revenue / sold) if sold else None
    data = {
        "obj": p, "label": f"{p.commodity.name} – {p.field.name}", "unit": p.unit,
        "season": f"planted {p.planted_on:%d %b %Y}",
        "cost": cost, "produced": harvested, "sold": sold, "revenue": revenue, "in_stock": in_stock,
        "cost_per_unit": cost_per_unit, "avg_price": avg_price,
        "margin_per_unit": (avg_price - cost_per_unit) if (avg_price and cost_per_unit) else None,
        "profit": revenue - cost,
        "per_ha": money((revenue - cost) / p.area_ha) if p.area_ha else None,
        "commodity": p.commodity,
        "finished": p.status in (Planting.Status.COMPLETED, Planting.Status.FAILED),
        "listing_price": Listing.objects.filter(harvest__planting=p, status=Listing.Status.ACTIVE)
        .values_list("price", flat=True).first(),
    }
    if market is not None:
        data["market_avg"] = market.get((p.commodity_id, p.unit))
    return _explain(data)


def herd_economics(h, market=None):
    cost = h.total_cost
    items = OrderItem.objects.filter(listing__herd=h)
    sold, revenue = _sales(items)
    produced = max(Decimal(h.head_count), sold)
    in_stock = (Listing.objects.filter(herd=h, status=Listing.Status.ACTIVE)
                .aggregate(t=Sum("quantity_available"))["t"] or ZERO)
    data = {
        "obj": h, "label": h.name, "season": f"since {h.started_on:%d %b %Y}", "unit": "head",
        "cost": cost, "produced": produced, "sold": sold, "revenue": revenue,
        "in_stock": in_stock,
        "cost_per_unit": money(cost / produced) if produced else None,
        "avg_price": money(revenue / sold) if sold else None, "profit": revenue - cost, "per_ha": None,
        "commodity": h.commodity,
        "listing_price": Listing.objects.filter(herd=h, status=Listing.Status.ACTIVE)
        .values_list("price", flat=True).first(),
    }
    data["margin_per_unit"] = (data["avg_price"] - data["cost_per_unit"]
                               if data["avg_price"] and data["cost_per_unit"] else None)
    if market is not None:
        data["market_avg"] = market.get((h.commodity_id, "head"))
    return _explain(data)


def _explain(d):
    """Add plain-language figures: what the unsold stock is worth and the expected final profit."""
    d["cost"], d["revenue"], d["profit"] = money(d["cost"]), money(d["revenue"]), money(d["profit"])
    price_for_rest = d.get("market_avg") or d["avg_price"] or d.get("listing_price")
    d["price_for_rest"] = price_for_rest
    d["rest_value"] = money(d["in_stock"] * price_for_rest) if (d["in_stock"] and price_for_rest) else ZERO
    d["expected_profit"] = money(d["revenue"] + d["rest_value"] - d["cost"])
    d["pct_sold"] = int(min(100, d["sold"] / d["produced"] * 100)) if d["produced"] else 0
    top = max(d["cost"], d["revenue"] + d["rest_value"], Decimal("1"))
    d["bar_cost"] = int(d["cost"] / top * 100)
    d["bar_income"] = int(d["revenue"] / top * 100)
    d["bar_rest"] = int(d["rest_value"] / top * 100)
    if not d["revenue"] and not d["cost"]:
        d["status"], d["tone"] = "Nothing recorded yet", "grey"
    elif not d["produced"]:
        d["status"], d["tone"] = "Still growing", "grey"
    elif not d["revenue"]:
        d["status"], d["tone"] = "Not sold yet", "wheat"
    elif d["profit"] >= 0:
        d["status"], d["tone"] = "Making money", "ok"
    elif not d.get("finished", True):
        d["status"], d["tone"] = "Still harvesting — costs not covered yet", "wheat"
    elif d["expected_profit"] > 0:
        d["status"], d["tone"] = "Costs not covered yet", "wheat"
    else:
        d["status"], d["tone"] = "Losing money", "danger"
    return d
