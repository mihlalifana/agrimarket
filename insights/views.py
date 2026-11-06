from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from accounts.models import Province
from core.permissions import farmer_required
from farm.economics import herd_economics, planting_economics
from farm.models import Herd, Planting
from marketplace.models import Commodity

from . import services
from .trends import farmer_trends


@farmer_required
def farm_analytics(request):
    u = request.user
    trends = farmer_trends(u)
    by_prov, by_type, products = services.farmer_breakdowns(u)
    market = services.price_lookup()
    econ = [planting_economics(p, market) for p in
            Planting.objects.filter(farmer=u).select_related("commodity", "field")]
    econ += [herd_economics(h, market) for h in Herd.objects.filter(farmer=u).select_related("commodity")]
    econ = [e for e in econ if e["cost"] or e["revenue"]]
    econ.sort(key=lambda e: e["expected_profit"], reverse=True)
    waste_total, waste_by = services.farmer_wastage(u)
    # Advice for crops coming in: when is the market usually best?
    today = timezone.localdate()
    upcoming = []
    for p in Planting.objects.filter(farmer=u, status="growing", expected_harvest__gte=today,
                                     expected_harvest__lte=today + timedelta(days=240)).select_related("commodity", "field"):
        months, best = services.seasonality(p.commodity_id, p.unit)
        upcoming.append({"p": p, "best": best, "market": market.get((p.commodity_id, p.unit))})
    return render(request, "insights/farm_analytics.html", {
        "kpi": services.farmer_kpis(u), "products": products, "by_prov": by_prov, "by_type": by_type,
        "econ": econ, "waste_total": waste_total, "waste_by": waste_by, "upcoming": upcoming,
        "trends": trends, "min_farms": services.MIN_FARMS,
        "trend_rows": list(zip(trends["months"], trends["sales"], trends["costs"], trends["running"],
                               trends["orders"], trends["buyers"])),
        "chart": {"trends": trends,
                  "prov_labels": [p for p, _ in by_prov], "prov_values": [float(v) for _, v in by_prov],
                  "cat_labels": [t for t, _ in by_type], "cat_values": [float(v) for _, v in by_type]},
    })


@farmer_required
def price_guide(request):
    try:
        guide = services.price_guide(int(request.GET.get("commodity", 0)), request.GET.get("unit", ""),
                                     request.GET.get("grade", ""))
    except ValueError:
        guide = None
    return JsonResponse({"guide": guide, "min_farms": services.MIN_FARMS})


@farmer_required
def planner(request):
    """What should I plant next? Options per field for a chosen planting date."""
    from datetime import date
    from .planner import plan_farm
    today = timezone.localdate()
    try:
        day = date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        day = today
    day = max(today, min(day, today + timedelta(days=365)))
    plans = plan_farm(request.user, day)
    for p in plans:
        p["top"] = [o for o in p["options"] if o["suitable"]][:3]
        p["rest"] = [o for o in p["options"] if o not in p["top"]]
    return render(request, "insights/planner.html", {"plans": plans, "day": day, "today": today,
                                                     "max_day": today + timedelta(days=365),
                                                     "min_farms": services.MIN_FARMS})
