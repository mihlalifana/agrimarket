from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core import clock
from core.models import AuditLog
from core.permissions import farmer_required
from core.services import audit
from insights.services import price_lookup, seasonality

from .economics import herd_economics, planting_economics
from .forms import CostForm, FieldForm, HarvestForm, HerdForm, PlantingForm, TreatmentForm
from .models import CostEntry, Field, Harvest, Herd, Planting, Treatment


RECORD_DATE = {"Planting": "planted_on", "Harvest": "harvested_on", "Herd": "started_on",
               "Treatment": "treated_on", "CostEntry": "incurred_on", "SoilTest": "sampled_on",
               "FieldVisit": "visited_on", "SprayRecord": "applied_on"}


def _save(request, form, what, **extra):
    """Save a farm record, showing any problem (duplicate, date outside the window) on the form itself.

    The record is stamped with its own date (a harvest from June is created in June).
    """
    obj = form.save(commit=False)
    for k, v in extra.items():
        setattr(obj, k, v)
    day = getattr(obj, RECORD_DATE.get(obj.__class__.__name__, ""), None)
    try:
        obj.full_clean(exclude=[f.name for f in obj._meta.fields if not f.editable])
        with clock.as_of(clock.at_date(day) if day else None):
            obj.save()
    except ValidationError as e:
        for field, errs in getattr(e, "message_dict", {None: e.messages}).items():
            for err in errs:
                form.add_error(field if field in form.fields else None, err)
        return None
    except IntegrityError:
        form.add_error(None, f"This {what} is already recorded.")
        return None
    audit(request.user, AuditLog.Action.CREATE, obj)
    messages.success(request, f"{what.capitalize()} saved.")
    return obj


@farmer_required
def home(request):
    u = request.user
    today = timezone.localdate()
    plantings = Planting.objects.filter(farmer=u).select_related("field", "commodity")
    current = plantings.filter(status__in=[Planting.Status.GROWING, Planting.Status.HARVESTING])
    growing = plantings.filter(status=Planting.Status.GROWING)
    due = growing.filter(expected_harvest__lte=today + timedelta(days=30)).order_by("expected_harvest")
    herds = Herd.objects.filter(farmer=u).select_related("commodity")
    in_withdrawal = (Treatment.objects.filter(herd__farmer=u, withdrawal_ends__gt=today)
                     .select_related("herd").order_by("withdrawal_ends"))
    unlisted = (Harvest.objects.filter(planting__farmer=u, listing__isnull=True)
                .select_related("planting__commodity", "planting__field"))
    fields = Field.objects.filter(farmer=u)
    month_costs = (CostEntry.objects.filter(farmer=u, incurred_on__gte=today.replace(day=1))
                   .aggregate(t=Sum("amount"))["t"] or 0)
    return render(request, "farm/home.html", {
        "fields": fields, "total_ha": sum(f.size_ha for f in fields), "growing": growing, "due": due,
        "current": current,
        "herds": herds, "in_withdrawal": in_withdrawal, "unlisted": unlisted, "month_costs": month_costs,
        "recent_costs": CostEntry.objects.filter(farmer=u).select_related("planting__commodity", "herd")[:6],
    })


# ---------------------------------------------------------------- fields
@farmer_required
def fields(request, edit=None):
    """Fields list with 'add' and per-row 'edit' forms on the same page."""
    form = FieldForm(request.POST or None) if edit is None else FieldForm()
    if request.method == "POST" and edit is None and form.is_valid():
        obj = _save(request, form, "field", farmer=request.user)
        if obj:
            return redirect(f"{reverse('farm:fields')}?new={obj.pk}")
    rows = list(Field.objects.filter(farmer=request.user).prefetch_related("plantings__commodity"))
    for f in rows:
        f.edit_form = edit if (edit is not None and edit.instance.pk == f.pk) else FieldForm(instance=f, prefix=f"f{f.pk}")
    return render(request, "farm/fields.html", {"form": form, "rows": rows})


@farmer_required
def field_edit(request, pk):
    obj = get_object_or_404(Field, pk=pk, farmer=request.user)
    if request.method != "POST":
        return redirect("farm:fields")
    form = FieldForm(request.POST, instance=obj, prefix=f"f{obj.pk}")
    if form.is_valid():
        form.save()
        messages.success(request, f"{obj.name} updated.")
        return redirect(f"{reverse('farm:fields')}?new={obj.pk}")
    return fields(request, edit=form)


# ---------------------------------------------------------------- crops
@farmer_required
def plantings(request):
    if not Field.objects.filter(farmer=request.user).exists():
        messages.info(request, "Add a field first, then record what you planted on it.")
        return redirect("farm:fields")
    # The planting planner links here with the plan filled in (?field=..&commodity=..).
    keys = ("field", "commodity", "planted_on", "area_ha", "expected_harvest", "unit", "expected_yield")
    initial = {k: request.GET[k] for k in keys if request.GET.get(k)}
    form = PlantingForm(request.POST or None, farmer=request.user, initial=initial)
    if request.method == "POST" and form.is_valid():
        obj = _save(request, form, "planting", farmer=request.user)
        if obj:
            return redirect(f"{reverse('farm:plantings')}?new={obj.pk}")
    season = request.GET.get("season", "")
    rows = Planting.objects.filter(farmer=request.user).select_related("field", "commodity")
    seasons = sorted(set(rows.values_list("season", flat=True)), reverse=True)
    if season:
        rows = rows.filter(season=season)
    return render(request, "farm/plantings.html", {"form": form, "rows": rows, "seasons": seasons,
                                                   "season": season, "prefilled": bool(initial)})


@farmer_required
def planting_detail(request, pk):
    """The crop's record. Harvests are recorded on the Harvest page, not here."""
    p = get_object_or_404(Planting.objects.select_related("field", "commodity"), pk=pk, farmer=request.user)
    sform = PlantingForm(instance=p, farmer=request.user, prefix="s")
    if request.method == "POST" and "update" in request.POST:
        sform = PlantingForm(request.POST, instance=p, farmer=request.user, prefix="s")
        if sform.is_valid():
            sform.save()
            messages.success(request, "Planting updated.")
            return redirect("farm:planting", pk=pk)
    cform = CostForm(farmer=request.user, initial={"target": f"p{p.pk}", "incurred_on": timezone.localdate()})
    econ = planting_economics(p, market=price_lookup())
    months, best = seasonality(p.commodity_id, p.unit)
    from advisory.forms import SprayForm
    from advisory.models import harvest_blocked_until
    return render(request, "farm/planting_detail.html", {
        "p": p, "sform": sform, "today": timezone.localdate(), "cform": cform, "econ": econ,
        "sprays": p.sprays.select_related("recommended_by"), "blocked": harvest_blocked_until(p, timezone.localdate()),
        "spray_form": SprayForm(planting=p, initial={"applied_on": timezone.localdate()}),
        "recs": p.recommendations.select_related("agronomist"),
        "harvests": p.harvests.all(), "costs": p.costs.all(), "best_months": best,
        "listings": [h.listing for h in p.harvests.all() if h.is_listed],
    })


# ---------------------------------------------------------------- harvest
def _ready_crops(farmer, today):
    """Crops whose expected harvest date has arrived and that are not finished or failed."""
    return (Planting.objects.filter(farmer=farmer, expected_harvest__lte=today,
                                    status__in=[Planting.Status.GROWING, Planting.Status.HARVESTING])
            .select_related("field", "commodity").order_by("expected_harvest"))


@farmer_required
def harvests(request):
    """Harvest page: only crops due for harvest. Start harvesting, record each harvest, then list it for sale."""
    from advisory.models import harvest_blocked_until
    u = request.user
    today = timezone.localdate()
    ready = _ready_crops(u, today)
    crop_pk = request.POST.get("planting") or request.GET.get("crop")
    selected = ready.filter(pk=crop_pk).first() if str(crop_pk or "").isdigit() else None
    page = reverse("farm:harvests")
    hform = None
    if request.method == "POST":
        action = request.POST.get("action")
        if selected is None:
            messages.error(request, "That crop is not due for harvest yet, or it is already finished.")
            return redirect(page)
        if action == "start":
            if selected.status == Planting.Status.GROWING:
                spray = harvest_blocked_until(selected, today)
                if spray:
                    messages.error(request, f"Do not harvest yet: {spray.product} was sprayed on "
                                            f"{spray.applied_on:%d %b}. Its pre-harvest interval ends on "
                                            f"{spray.safe_from:%d %b %Y}.")
                    return redirect(page)
                selected.status = Planting.Status.HARVESTING
                selected.save(update_fields=["status"])
                audit(u, AuditLog.Action.UPDATE, selected, field="status", old="growing", new="harvesting")
                messages.success(request, f"You are harvesting {selected.commodity} on {selected.field.name}. "
                                          f"Record each harvest below.")
            return redirect(f"{page}?crop={selected.pk}#record")
        if action == "finish":
            if selected.status != Planting.Status.HARVESTING or not selected.harvests.exists():
                messages.error(request, "Record at least one harvest before finishing.")
                return redirect(f"{page}?crop={selected.pk}#record")
            selected.status = Planting.Status.COMPLETED
            selected.save(update_fields=["status"])
            audit(u, AuditLog.Action.UPDATE, selected, field="status", old="harvesting", new="completed")
            messages.success(request, f"Harvest of {selected.commodity} on {selected.field.name} finished: "
                                      f"{selected.harvested_qty.normalize():f} {selected.unit} in total.")
            return redirect(page)
        if action == "record":
            if selected.status != Planting.Status.HARVESTING:
                messages.error(request, "Click Start harvesting first.")
                return redirect(page)
            hform = HarvestForm(request.POST, planting=selected, prefix="h")
            if hform.is_valid():
                h = _save(request, hform, "harvest", planting=selected)
                if h:
                    messages.success(request, f"Harvest saved: {h.quantity.normalize():f} {selected.unit} of "
                                              f"{selected.commodity}. You can list it for sale now.")
                    return redirect(f"{page}?crop={selected.pk}&new={h.pk}#harvested")
    if selected is not None and selected.status == Planting.Status.HARVESTING and hform is None:
        hform = HarvestForm(planting=selected, prefix="h", initial={"harvested_on": today})
    for p in ready:
        p.blocked = harvest_blocked_until(p, today)
    harvested = (Harvest.objects.filter(planting__farmer=u)
                 .select_related("planting__commodity", "planting__field", "listing")
                 .order_by("-harvested_on", "-pk"))
    not_due = Planting.objects.filter(farmer=u, status=Planting.Status.GROWING, expected_harvest__gt=today)
    return render(request, "farm/harvests.html", {
        "ready": ready, "selected": selected, "hform": hform,
        "blocked": harvest_blocked_until(selected, today) if selected else None,
        "unlisted": [h for h in harvested if not h.is_listed],
        "listed": [h for h in harvested if h.is_listed][:10],
        "not_due": not_due.count(),
        "next_due": not_due.order_by("expected_harvest").first(),
    })


# ---------------------------------------------------------------- livestock
@farmer_required
def herds(request):
    form = HerdForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = _save(request, form, "herd", farmer=request.user)
        if obj:
            return redirect(f"{reverse('farm:herds')}?new={obj.pk}")
    rows = Herd.objects.filter(farmer=request.user).select_related("commodity")
    return render(request, "farm/herds.html", {"form": form, "rows": rows})


@farmer_required
def herd_detail(request, pk):
    h = get_object_or_404(Herd.objects.select_related("commodity"), pk=pk, farmer=request.user)
    tform = TreatmentForm(request.POST or None, farmer=request.user, herd=h, prefix="t",
                          initial={"treated_on": timezone.localdate()})
    if request.method == "POST" and tform.is_valid():
        t = _save(request, tform, "treatment")
        if t:
            if t.in_withdrawal:
                messages.warning(request, f"{h.name} is in a withdrawal period until "
                                          f"{t.withdrawal_ends:%d %b %Y}. Its listings are hidden from "
                                          f"buyers until then.")
            return redirect("farm:herd", pk=pk)
    cform = CostForm(farmer=request.user, initial={"target": f"h{h.pk}", "incurred_on": timezone.localdate()})
    return render(request, "farm/herd_detail.html", {
        "h": h, "tform": tform, "cform": cform, "treatments": h.treatments.all(),
        "costs": h.costs.all(), "econ": herd_economics(h, market=price_lookup()),
        "listings": h.listings.all(), "withdrawal": h.active_withdrawal,
    })


@farmer_required
def treatments(request):
    rows = Treatment.objects.filter(herd__farmer=request.user).select_related("herd__commodity")
    only = request.GET.get("active")
    if only:
        rows = rows.filter(withdrawal_ends__gt=timezone.localdate())
    return render(request, "farm/treatments.html", {"rows": rows, "only": only})


# ---------------------------------------------------------------- costs
@farmer_required
def costs(request):
    form = CostForm(request.POST or None, farmer=request.user,
                    initial={"incurred_on": timezone.localdate()})
    if request.method == "POST" and form.is_valid():
        if _save(request, form, "cost", farmer=request.user):
            nxt = request.POST.get("next", "")
            return redirect(nxt if nxt.startswith("/") else "farm:costs")
    if request.method == "POST" and not form.is_valid():
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
        nxt = request.POST.get("next", "")
        if nxt.startswith("/"):
            return redirect(nxt)
    rows = CostEntry.objects.filter(farmer=request.user).select_related("planting__commodity",
                                                                      "planting__field", "herd")
    by_cat = rows.values("category").annotate(t=Sum("amount")).order_by("-t")
    labels = dict(CostEntry.Category.choices)
    return render(request, "farm/costs.html", {
        "form": form, "rows": rows[:100], "total": rows.aggregate(t=Sum("amount"))["t"] or 0,
        "by_cat": [(labels[r["category"]], r["t"]) for r in by_cat]})


@farmer_required
@require_POST
def cost_delete(request, pk):
    c = get_object_or_404(CostEntry, pk=pk, farmer=request.user)
    audit(request.user, AuditLog.Action.DELETE, c, old=f"R{c.amount}")
    c.delete()
    messages.info(request, "Cost removed.")
    nxt = request.POST.get("next", "")
    return redirect(nxt if nxt.startswith("/") else "farm:costs")
