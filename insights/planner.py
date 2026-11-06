"""Planting planner: "What should I plant next on this field?"

For every crop that suits the field, the planner works out what one planting
would earn if it went in on the chosen date, and explains every figure:

* yield per hectare - the farm's own past harvests, else the average of at
  least 3 other farms, else the crop guide (dryland or irrigated);
* input cost per hectare - the farm's own recorded costs, else other farms,
  else the guide;
* price - the market average in the month the crop will be ready (seasonal
  pattern), else today's market average, else the farm's own sales, else the
  guide;
* warnings: harvest glut (several farms harvesting the same crop in the same
  weeks), crop rotation, planting outside the usual months, soil pH from the
  latest soil test, no irrigation for a vegetable;
* good signs: buyers searching or asking quotes for a crop nobody has.

It never shows another farm's figures unless at least MIN_FARMS farms are
pooled together.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum

from farm.crop_guide import CROP_GUIDE, MONTHS, month_names
from farm.models import Planting
from marketplace.models import Commodity, money

from .services import MIN_FARMS, price_lookup, seasonality, unmet_demand

ZERO = Decimal("0")
CLASH_DAYS = 21
ACTIVE = [Planting.Status.GROWING, Planting.Status.HARVESTING]


def convert(value, frm, to):
    if value is None or frm == to:
        return value
    if (frm, to) == ("ton", "kg"):
        return value * 1000
    if (frm, to) == ("kg", "ton"):
        return value / 1000
    return None


def _per_ha(plantings, what):
    """Total harvested (or cost) per hectare across completed plantings."""
    area = qty = ZERO
    farms = set()
    for p in plantings:
        v = p.harvested_qty if what == "yield" else p.total_cost
        if v:
            area += p.area_ha
            qty += v
            farms.add(p.farmer_id)
    return (qty / area if area else None), farms


def clears_on(p):
    """When a planting is expected to leave the field: first harvest plus the picking period."""
    span = CROP_GUIDE.get(p.commodity.code, {}).get("span", 14)
    return p.expected_harvest + timedelta(days=span)


def _occupying(field, day):
    return [p for p in Planting.objects.filter(field=field, status__in=ACTIVE).select_related("commodity")
            if clears_on(p) > day]


def free_area(field, day):
    busy = sum((p.area_ha for p in _occupying(field, day)), ZERO)
    return max(field.size_ha - busy, ZERO)


def busy_until(field, day=None):
    from django.utils import timezone
    ps = _occupying(field, day or timezone.localdate())
    return max(clears_on(p) for p in ps) if ps else None


def plan_field(field, day, market=None, demand=None):
    """Options for one field from `day`, best first."""
    from advisory.models import Recommendation, SoilTest

    farmer = field.farmer
    market = market if market is not None else price_lookup()
    demand = demand if demand is not None else {r["commodity"].pk: r for r in unmet_demand()}
    area = free_area(field, day)
    soil = SoilTest.objects.filter(field=field).first()
    last = (Planting.objects.filter(field=field, planted_on__lte=day).select_related("commodity")
            .order_by("-planted_on").first())
    advice = list(Recommendation.objects.filter(field=field, kind=Recommendation.Kind.PLANTING,
                                                status=Recommendation.Status.OPEN))
    commodities = {c.code: c for c in Commodity.objects.filter(code__in=CROP_GUIDE, is_active=True)}
    options = []
    for code, g in CROP_GUIDE.items():
        c = commodities.get(code)
        if c is None:
            continue
        own_all = Planting.objects.filter(farmer=farmer, commodity=c)
        recent = own_all.order_by("-planted_on").first()
        unit = recent.unit if recent and recent.unit in c.unit_list else g["unit"]
        notes, good = [], []

        # yield per ha
        own_done = list(own_all.filter(unit=unit, status=Planting.Status.COMPLETED).prefetch_related("harvests"))
        y, _ = _per_ha(own_done, "yield")
        y_src = "your own past harvests"
        if y is None:
            others = (Planting.objects.filter(commodity=c, unit=unit, status=Planting.Status.COMPLETED)
                      .exclude(farmer=farmer).prefetch_related("harvests"))
            oy, farms = _per_ha(others, "yield")
            if oy is not None and len(farms) >= MIN_FARMS:
                y, y_src = oy, f"average of {len(farms)} farms"
        suitable = not (g["irrigation"] and not field.irrigated)
        if not suitable:
            notes.append("Needs irrigation — this field is dryland.")
        if y is not None and not suitable:
            y, y_src = None, ""
        if y is None:
            gy = g["yield_"][1 if field.irrigated else 0]
            if gy is None:
                gy = g["yield_"][1] * Decimal("0.3")
            y = convert(gy, g["unit"], unit)
            y_src = ("crop guide, irrigated" if field.irrigated else
                     "crop guide, dryland" if suitable else "rough guess without irrigation")
        if y is None:
            continue

        # cost per ha
        cost, _ = _per_ha(own_done, "cost")
        c_src = "your own recorded costs"
        if cost is None:
            others = Planting.objects.filter(commodity=c, status=Planting.Status.COMPLETED).exclude(farmer=farmer)
            oc, farms = _per_ha(others, "cost")
            if oc is not None and len(farms) >= MIN_FARMS:
                cost, c_src = oc, f"average of {len(farms)} farms"
        if cost is None:
            cost = g["cost"][1 if field.irrigated or not suitable else 0] or g["cost"][1]
            c_src = "crop guide"

        # timing
        days = g["days"]
        if own_done:
            firsts = [(p.harvests.order_by("harvested_on").first().harvested_on - p.planted_on).days
                      for p in own_done if p.harvests.exists()]
            if firsts:
                days = int(sum(firsts) / len(firsts))
        ready = day + timedelta(days=days)

        # price when ready
        months, best = seasonality(c.pk, unit)
        seasonal = months[ready.month - 1]["avg"]
        if seasonal:
            price, p_src = seasonal, f"market average in {MONTHS[ready.month]}"
        elif market.get((c.pk, unit)):
            price, p_src = market[(c.pk, unit)], "today's market average"
        else:
            own_sales = (c.listings.filter(farmer=farmer, unit=unit).aggregate(r=Sum("order_items__line_total"),
                                                                               q=Sum("order_items__quantity")))
            if own_sales["q"]:
                price, p_src = money(own_sales["r"] / own_sales["q"]), "your own selling price"
            else:
                price, p_src = convert(g["price"], g["unit"], unit), "crop guide price"
        if price is None:
            continue
        if best and MONTHS[ready.month] in best:
            good.append(f"Ready in {MONTHS[ready.month]}, one of the best-priced months ({', '.join(best)}).")
        elif best:
            notes.append(f"Ready in {MONTHS[ready.month]}; prices are usually better in {', '.join(best)}.")

        income_ha = y * price
        profit_ha = income_ha - cost
        breakeven = cost / y if y else None

        # harvest clash with other farms
        clash = (Planting.objects.filter(commodity=c, status__in=ACTIVE,
                                         expected_harvest__range=(ready - timedelta(days=CLASH_DAYS),
                                                                  ready + timedelta(days=CLASH_DAYS)))
                 .exclude(farmer=farmer))
        clash_farms = clash.values("farmer").distinct().count()
        clash_note = ""
        if clash_farms >= MIN_FARMS:
            vol = sum((convert(p.expected_yield, p.unit, unit) or ZERO) for p in clash)
            clash_note = (f"{clash_farms} other farms expect about {vol:,.0f} {unit} around the same weeks "
                          f"— prices may drop.")
        elif clash_farms:
            clash_note = "A few other farms expect to harvest this crop around the same time."
        if clash_note:
            notes.append(clash_note)

        # rotation
        if last and last.commodity.code in CROP_GUIDE:
            last_family = CROP_GUIDE[last.commodity.code]["family"]
            if last_family == g["family"]:
                notes.append(f"Rotation: {last.commodity.name.lower()} was the last crop here "
                             f"({g['family'].split(' (')[0].lower()}) — pests and diseases carry over.")
            elif "Legumes" in last_family and "Grasses" in g["family"]:
                good.append(f"Follows {last.commodity.name.lower()}: nitrogen left in the soil lowers fertiliser cost.")

        # planting window
        if day.month not in g["months"]:
            notes.append(f"Usually planted in {month_names(g['months'])}.")

        # soil
        if soil:
            lo, hi = g["ph"]
            if soil.ph_kcl < lo:
                notes.append(f"Soil pH {soil.ph_kcl} is below what {c.name.lower()} needs ({lo}+): lime first.")
            elif soil.ph_kcl > hi:
                notes.append(f"Soil pH {soil.ph_kcl} is high for {c.name.lower()} (up to {hi}).")

        # demand
        d = demand.get(c.pk)
        if d and d["score"]:
            bits = []
            if d["searches"]:
                bits.append(f"{d['searches']} searches found nothing")
            if d["rfqs"]:
                bits.append(f"{d['rfqs']} open quote request{'s' if d['rfqs'] > 1 else ''}")
            if d["alerts"]:
                bits.append(f"{d['alerts']} buyers waiting for stock")
            good.append("Buyers are asking: " + ", ".join(bits) + ".")

        risk = len([n for n in notes if not n.startswith("Rotation")]) + (2 if not suitable else 0)
        options.append({
            "commodity": c, "unit": unit, "days": days, "ready": ready,
            "yield_ha": y, "y_src": y_src, "cost_ha": money(cost), "c_src": c_src,
            "price": price, "p_src": p_src, "income_ha": money(income_ha), "profit_ha": money(profit_ha),
            "breakeven": money(breakeven) if breakeven else None,
            "area": area, "total": money(profit_ha * area), "volume": y * area,
            "notes": notes, "good": good, "suitable": suitable,
            "fit": ("Not suitable" if not suitable else "Check first" if notes and profit_ha > 0
                    else "Good fit" if profit_ha > 0 else "Loses money"),
            "score": profit_ha * area * (Decimal("0.4") if not suitable else Decimal("1") - Decimal("0.12") * risk),
        })
    options.sort(key=lambda o: o["score"], reverse=True)
    return {"field": field, "day": day, "area": area, "busy_until": busy_until(field, day), "soil": soil,
            "last": last, "advice": advice, "options": options}


def plan_farm(farmer, day):
    market = price_lookup()
    demand = {r["commodity"].pk: r for r in unmet_demand()}
    return [plan_field(f, day, market, demand) for f in farmer.fields.all()]
