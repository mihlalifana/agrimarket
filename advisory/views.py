from django.contrib import messages
from django.contrib.auth import login
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import User
from core import clock
from core.models import AuditLog
from core.permissions import admin_required, farmer_required, role_required, verified_agronomist_required
from core.services import audit, notify
from farm.models import Field, Planting

from .forms import (AgronomistRegistrationForm, LinkForm, PestAlertForm, RecommendationForm, RemedyForm,
                    RemedyUseForm, ResponseForm, ReviewAgronomistForm, SoilTestForm, SprayForm, VisitForm)
from .services import send_alert
from .models import (AgronomistProfile, FarmLink, FieldVisit, PestAlert, Recommendation, RegisteredRemedy,
                     RemedyUse, SoilTest, SprayRecord, linked)


# ---------------------------------------------------------------- helpers
def _farm_for(request, farmer_pk=None):
    """The farmer whose records are being worked on, checking access.

    A farmer works on their own farm; an agronomist only on farms linked to them.
    """
    u = request.user
    if u.is_farmer:
        return u
    if u.is_agronomist and u.is_verified_agronomist and farmer_pk:
        farmer = get_object_or_404(User, pk=farmer_pk, role=User.Role.FARMER)
        if linked(u, farmer):
            return farmer
    raise PermissionDenied("You do not advise this farm.")


def _back(request, farmer):
    nxt = request.POST.get("next", "")
    if nxt.startswith("/"):
        return redirect(nxt)
    if request.user.is_agronomist:
        return redirect("advisory:farm", farmer_pk=farmer.pk)
    return redirect("advisory:home")


def _save(request, obj, form, what):
    """full_clean + save dated on the record's own day; errors go back as messages."""
    day = next((getattr(obj, f) for f in ("sampled_on", "visited_on", "applied_on") if hasattr(obj, f)), None)
    try:
        obj.full_clean(exclude=[f.name for f in obj._meta.fields if not f.editable])
        with clock.as_of(clock.at_date(day) if day else None):
            obj.save()
    except ValidationError as e:
        for errs in e.message_dict.values():
            for err in errs:
                messages.error(request, err)
        return None
    except IntegrityError:
        messages.error(request, f"This {what} is already recorded.")
        return None
    audit(request.user, AuditLog.Action.CREATE, obj)
    return obj


def _form_errors(request, form):
    for name, errs in form.errors.items():
        label = form.fields[name].label if name in form.fields else ""
        for e in errs:
            messages.error(request, f"{label}: {e}" if label else e)


def crop_summary(planting):
    """Yield and cost per hectare for the agronomist's crop table."""
    harvested = planting.harvested_qty
    cost = planting.total_cost
    area = planting.area_ha
    return {
        "p": planting, "harvested": harvested,
        "yield_ha": harvested / area if area and harvested else None,
        "expected_ha": planting.expected_yield / area if area else None,
        "cost_ha": cost / area if area and cost else None,
        "pct_of_expected": int(harvested / planting.expected_yield * 100) if planting.expected_yield and harvested else None,
        "in_phi": [s for s in planting.sprays.all() if s.in_phi],
        "last_visit": planting.visits.order_by("-visited_on").first(),
    }


# ---------------------------------------------------------------- registration and verification
def register_agronomist(request):
    form = AgronomistRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save_agronomist()
        audit(user, AuditLog.Action.CREATE, user, new="agronomist registered")
        for admin in User.objects.filter(role=User.Role.ADMIN, is_active=True):
            notify(admin, "Agronomist awaiting verification", user.get_full_name(), link="/advice/admin/agronomists/")
        login(request, user, backend="accounts.backends.EmailBackend")
        messages.success(request, "Registration received. You can advise farms once an administrator has "
                                  "checked your professional registration.")
        return redirect("core:dashboard")
    return render(request, "accounts/register.html", {"form": form, "kind": "Agronomist"})


@admin_required
def agronomist_queue(request):
    status = request.GET.get("status", AgronomistProfile.Status.PENDING)
    people = AgronomistProfile.objects.select_related("user", "reviewed_by").filter(status=status)
    return render(request, "advisory/agronomist_queue.html", {
        "people": people, "status": status, "statuses": AgronomistProfile.Status.choices,
        "form": ReviewAgronomistForm()})


@admin_required
@require_POST
def agronomist_review(request, pk):
    prof = get_object_or_404(AgronomistProfile.objects.select_related("user"), pk=pk)
    form = ReviewAgronomistForm(request.POST)
    if not form.is_valid():
        _form_errors(request, form)
        return redirect("advisory:agronomist_queue")
    approve = form.cleaned_data["decision"] == "approve"
    old = prof.status
    prof.status = AgronomistProfile.Status.APPROVED if approve else AgronomistProfile.Status.REJECTED
    prof.rejection_reason = "" if approve else form.cleaned_data["reason"].strip()
    prof.reviewed_at, prof.reviewed_by = timezone.now(), request.user
    prof.save()
    audit(request.user, AuditLog.Action.STATUS, prof, field="status", old=old, new=prof.status)
    notify(prof.user, "Registration verified" if approve else "Registration not approved",
           "Farmers can now add you as their agronomist." if approve else prof.rejection_reason,
           link="/dashboard/", email=True)
    messages.success(request, f"{prof.user.get_full_name()} {'approved' if approve else 'rejected'}.")
    return redirect("advisory:agronomist_queue")


# ---------------------------------------------------------------- farmer: my advice
@farmer_required
def home(request):
    u = request.user
    today = timezone.localdate()
    links = FarmLink.objects.filter(farmer=u, active=True).select_related("agronomist__agronomist")
    crops = set(Planting.objects.filter(farmer=u).exclude(status=Planting.Status.COMPLETED)
                .values_list("commodity_id", flat=True))
    alerts = PestAlert.objects.filter(province=u.province, valid_until__gte=today).select_related(
        "commodity", "agronomist")
    return render(request, "advisory/home.html", {
        "links": links, "link_form": LinkForm(farmer=u),
        "open_recs": Recommendation.objects.filter(farmer=u, status=Recommendation.Status.OPEN)
        .select_related("agronomist", "field", "planting__commodity"),
        "done_recs": Recommendation.objects.filter(farmer=u).exclude(status=Recommendation.Status.OPEN)
        .select_related("agronomist", "field", "planting__commodity")[:10],
        "visits": FieldVisit.objects.filter(farmer=u).select_related("agronomist", "field", "planting__commodity")[:10],
        "soil": SoilTest.objects.filter(field__farmer=u).select_related("field", "added_by"),
        "sprays": SprayRecord.objects.filter(planting__farmer=u).select_related("planting__commodity",
                                                                               "planting__field")[:20],
        "alerts": [a for a in alerts if a.commodity_id in crops] + [a for a in alerts if a.commodity_id not in crops],
        "my_crops": crops,
        "soil_form": SoilTestForm(farmer=u, initial={"sampled_on": today}),
        "spray_form": SprayForm(farmer=u, initial={"applied_on": today}),
        "response_form": ResponseForm(), "today": today,
    })


@farmer_required
@require_POST
def link_add(request):
    form = LinkForm(request.POST, farmer=request.user)
    if form.is_valid():
        agro = form.cleaned_data["agronomist"]
        try:
            FarmLink.objects.create(farmer=request.user, agronomist=agro)
        except IntegrityError:
            messages.info(request, "This agronomist already advises your farm.")
        else:
            audit(request.user, AuditLog.Action.CREATE, request.user, field="agronomist", new=agro.email)
            notify(agro, f"New farm: {request.user.farm.farm_name}",
                   f"{request.user.get_full_name()} ({request.user.farm.town}) added you as their agronomist.",
                   link=f"/advice/farms/{request.user.pk}/", sms=True)
            messages.success(request, f"{agro.get_full_name()} can now see your farm records (not your sales, "
                                      f"buyers or bank details).")
    else:
        _form_errors(request, form)
    return redirect("advisory:home")


@role_required("farmer", "agronomist")
@require_POST
def link_end(request, pk):
    link = get_object_or_404(FarmLink, pk=pk, active=True)
    if request.user not in (link.farmer, link.agronomist):
        raise PermissionDenied
    link.active, link.ended_at = False, timezone.now()
    link.save(update_fields=["active", "ended_at"])
    other = link.agronomist if request.user == link.farmer else link.farmer
    notify(other, "Advisory link ended", f"{request.user.get_full_name()} ended the link.")
    audit(request.user, AuditLog.Action.UPDATE, link, field="active", old=True, new=False)
    messages.info(request, "Link ended. Past visits and advice stay in the records.")
    return redirect("advisory:home" if request.user.is_farmer else "core:dashboard")


@farmer_required
@require_POST
def rec_respond(request, pk):
    rec = get_object_or_404(Recommendation, pk=pk, farmer=request.user, status=Recommendation.Status.OPEN)
    form = ResponseForm(request.POST)
    if form.is_valid():
        rec.status = form.cleaned_data["status"]
        rec.farmer_note = form.cleaned_data["note"]
        rec.done_on = timezone.localdate() if rec.status == Recommendation.Status.DONE else None
        rec.save()
        notify(rec.agronomist, f"{request.user.farm.farm_name}: advice {rec.get_status_display().lower()}",
               f"{rec.advice[:80]}" + (f" — {rec.farmer_note}" if rec.farmer_note else ""),
               link=f"/advice/farms/{request.user.pk}/")
        messages.success(request, "Thank you — your agronomist has been told.")
    return redirect("advisory:home")


# ---------------------------------------------------------------- records (farmer or linked agronomist)
@role_required("farmer", "agronomist")
@require_POST
def soil_add(request, farmer_pk=None):
    farmer = _farm_for(request, farmer_pk)
    form = SoilTestForm(request.POST, farmer=farmer)
    if form.is_valid():
        obj = form.save(commit=False)
        obj.added_by = request.user
        if _save(request, obj, form, "soil test"):
            messages.success(request, f"Soil test for {obj.field.name} saved. {obj.ph_note}")
            if request.user.is_agronomist:
                notify(farmer, f"Soil test results: {obj.field.name}", f"pH {obj.ph_kcl}. {obj.ph_note}",
                       link="/advice/")
    else:
        _form_errors(request, form)
    return _back(request, farmer)


@role_required("farmer", "agronomist")
@require_POST
def spray_add(request, farmer_pk=None):
    farmer = _farm_for(request, farmer_pk)
    planting = None
    if request.POST.get("planting_fixed"):
        planting = get_object_or_404(Planting, pk=request.POST["planting_fixed"], farmer=farmer)
    form = SprayForm(request.POST, farmer=farmer, planting=planting)
    if form.is_valid():
        obj = form.save(commit=False)
        if request.user.is_agronomist:
            obj.recommended_by = request.user
        if _save(request, obj, form, "spray"):
            msg = f"Spray recorded. {obj.planting.commodity} may be harvested from {obj.safe_from:%d %b %Y}."
            messages.success(request, msg)
            if request.user.is_agronomist:
                notify(farmer, f"Spray programme: {obj.product} on {obj.planting.commodity}",
                       f"Rate {obj.rate} against {obj.target}. Do not harvest before {obj.safe_from:%d %b %Y}.",
                       link=f"/farm/crops/{obj.planting.pk}/", sms=True)
    else:
        _form_errors(request, form)
    return _back(request, farmer)


# ---------------------------------------------------------------- agronomist
@verified_agronomist_required
def farm_view(request, farmer_pk):
    farmer = _farm_for(request, farmer_pk)
    today = timezone.localdate()
    fields = list(Field.objects.filter(farmer=farmer).prefetch_related("soil_tests"))
    for f in fields:
        f.latest_soil = f.soil_tests.first()
    current = (Planting.objects.filter(farmer=farmer).exclude(status=Planting.Status.COMPLETED)
               .select_related("commodity", "field").prefetch_related("sprays", "visits", "harvests", "costs"))
    past = (Planting.objects.filter(farmer=farmer, status=Planting.Status.COMPLETED)
            .select_related("commodity", "field").prefetch_related("sprays", "visits", "harvests", "costs")[:12])
    return render(request, "advisory/farm.html", {
        "farmer": farmer, "farm": farmer.farm, "fields": fields,
        "current": [crop_summary(p) for p in current], "past": [crop_summary(p) for p in past],
        "visits": FieldVisit.objects.filter(farmer=farmer).select_related("field", "planting__commodity",
                                                                         "agronomist")[:15],
        "recs": Recommendation.objects.filter(farmer=farmer).select_related("field", "planting__commodity",
                                                                          "agronomist")[:20],
        "sprays": SprayRecord.objects.filter(planting__farmer=farmer).select_related("planting__commodity",
                                                                                    "planting__field")[:15],
        "visit_form": VisitForm(farmer=farmer, initial={"visited_on": today}),
        "rec_form": RecommendationForm(farmer=farmer),
        "soil_form": SoilTestForm(farmer=farmer, initial={"sampled_on": today}),
        "spray_form": SprayForm(farmer=farmer, initial={"applied_on": today}),
        "link": FarmLink.objects.filter(farmer=farmer, agronomist=request.user, active=True).first(),
    })


@verified_agronomist_required
@require_POST
def visit_add(request, farmer_pk):
    farmer = _farm_for(request, farmer_pk)
    form = VisitForm(request.POST, request.FILES, farmer=farmer)
    if form.is_valid():
        visit = form.save(commit=False)
        visit.farmer, visit.agronomist = farmer, request.user
        with transaction.atomic():
            if _save(request, visit, form, "visit"):
                d = form.cleaned_data
                text = f"{visit.field.name}: {visit.get_problem_display()}"
                if d.get("rec_kind"):
                    with clock.as_of(clock.at_date(visit.visited_on)):
                        Recommendation.objects.create(
                            farmer=farmer, agronomist=request.user, field=visit.field, planting=visit.planting,
                            visit=visit, kind=d["rec_kind"], advice=d["rec_advice"].strip(), do_by=d.get("rec_do_by"))
                    text += " — new recommendation"
                notify(farmer, f"Field visit by {request.user.get_full_name()}", text, link="/advice/",
                       sms=visit.severity == FieldVisit.Severity.HIGH)
                messages.success(request, "Visit saved and the farmer has been told.")
    else:
        _form_errors(request, form)
    return redirect("advisory:farm", farmer_pk=farmer.pk)


@verified_agronomist_required
@require_POST
def rec_add(request, farmer_pk):
    farmer = _farm_for(request, farmer_pk)
    form = RecommendationForm(request.POST, farmer=farmer)
    if form.is_valid():
        rec = form.save(commit=False)
        rec.farmer, rec.agronomist = farmer, request.user
        rec.save()
        audit(request.user, AuditLog.Action.CREATE, rec)
        notify(farmer, f"New advice: {rec.get_kind_display()}", rec.advice[:120], link="/advice/", sms=True)
        messages.success(request, "Recommendation sent to the farmer.")
    else:
        _form_errors(request, form)
    return redirect("advisory:farm", farmer_pk=farmer.pk)


@role_required("agronomist", "farmer")
def alerts(request):
    """Pest and disease alerts. Agronomists post them; farmers growing the crop in that province are told."""
    u = request.user
    form = None
    if u.is_agronomist and u.is_verified_agronomist:
        form = PestAlertForm(request.POST or None, initial={"province": u.province})
        if request.method == "POST" and form.is_valid():
            alert = form.save(commit=False)
            alert.agronomist = u
            alert.save()
            send_alert(alert)
            audit(u, AuditLog.Action.CREATE, alert, new=f"{alert.farms_told} farms told")
            messages.success(request, f"Alert sent to {alert.farms_told} farm(s) growing {alert.commodity} "
                                      f"in {alert.get_province_display()}.")
            return redirect(f"{reverse('advisory:alerts')}?new={alert.pk}")
    rows = PestAlert.objects.select_related("commodity", "agronomist")
    if u.is_farmer:
        rows = rows.filter(province=u.province)
    return render(request, "advisory/alerts.html", {"rows": rows[:60], "form": form,
                                                    "today": timezone.localdate()})


def agronomist_dashboard(user):
    """Context for the agronomist's dashboard (used by core.views.dashboard)."""
    today = timezone.localdate()
    links = (FarmLink.objects.filter(agronomist=user, active=True)
             .select_related("farmer__farm"))
    farms = []
    for link in links:
        f = link.farmer
        current = Planting.objects.filter(farmer=f).exclude(status=Planting.Status.COMPLETED)
        last = FieldVisit.objects.filter(farmer=f, agronomist=user).aggregate(d=Max("visited_on"))["d"]
        recs = Recommendation.objects.filter(farmer=f, agronomist=user, status=Recommendation.Status.OPEN)
        farms.append({
            "link": link, "farmer": f, "farm": f.farm,
            "ha": Field.objects.filter(farmer=f).aggregate(t=Sum("size_ha"))["t"] or 0,
            "crops": ", ".join(sorted({p.commodity.name for p in current.select_related("commodity")})) or "—",
            "last_visit": last, "days_since": (today - last).days if last else None,
            "open_recs": recs.count(), "overdue": sum(1 for r in recs if r.overdue),
            "in_phi": SprayRecord.objects.filter(planting__farmer=f, safe_from__gt=today).count(),
        })
    farms.sort(key=lambda r: (r["days_since"] is not None, -(r["days_since"] or 0)))
    return {
        "farms": farms,
        "due": [r for r in farms if r["days_since"] is None or r["days_since"] > 21],
        "recent_visits": FieldVisit.objects.filter(agronomist=user).select_related("farmer__farm", "field")[:8],
        "my_alerts": PestAlert.objects.filter(agronomist=user)[:5],
        "open_recs": Recommendation.objects.filter(agronomist=user, status=Recommendation.Status.OPEN).count(),
        "visits_month": FieldVisit.objects.filter(agronomist=user, visited_on__gte=today.replace(day=1)).count(),
    }


# ---------------------------------------------------------------- verified remedy catalogue (Act 36)
def _notify_admins(title, body, link):
    for admin in User.objects.filter(role=User.Role.ADMIN, is_active=True):
        notify(admin, title, body, link=link)


@admin_required
def remedies(request):
    """Admin: the verified list of agricultural remedies and the label PHI for each crop."""
    status = request.GET.get("status", RegisteredRemedy.Status.PENDING
                             if RegisteredRemedy.objects.filter(status="pending").exists() else "verified")
    rows = (RegisteredRemedy.objects.filter(status=status).select_related("submitted_by", "verified_by")
            .prefetch_related("uses__commodity"))
    form = RemedyForm(prefix="r")
    use_form = RemedyUseForm(prefix="u")
    if request.method == "POST":
        form = RemedyForm(request.POST, request.FILES, prefix="r")
        use_form = RemedyUseForm(request.POST, prefix="u")
        if form.is_valid() and use_form.is_valid():
            with transaction.atomic():
                r = form.save(commit=False)
                r.submitted_by = request.user
                r.save()
                use = use_form.save(commit=False)
                use.remedy = r
                use.save()
            audit(request.user, AuditLog.Action.CREATE, r, field="remedy", new=str(r))
            messages.success(request, f"{r} added. Check it against the label, then click Verify.")
            return redirect(f"{reverse('advisory:remedies')}?status=pending&new={r.pk}")
    counts = dict(RegisteredRemedy.objects.values_list("status").annotate(n=Count("pk")))
    return render(request, "advisory/remedies.html", {
        "rows": rows, "status": status, "form": form, "use_form": use_form,
        "statuses": [(v, l, counts.get(v, 0)) for v, l in RegisteredRemedy.Status.choices],
        "add_use_form": RemedyUseForm(prefix="a")})


@admin_required
@require_POST
def remedy_action(request, pk):
    r = get_object_or_404(RegisteredRemedy, pk=pk)
    action = request.POST.get("action")
    note = request.POST.get("note", "").strip()[:200]
    back = f"{reverse('advisory:remedies')}?status={r.status}"
    old = r.status
    if action == "add_use":
        uf = RemedyUseForm(request.POST, prefix="a")
        if uf.is_valid():
            use = uf.save(commit=False)
            use.remedy = r
            try:
                use.full_clean()
                use.save()
            except ValidationError as e:
                for m in e.messages:
                    messages.error(request, m)
                return redirect(back)
            audit(request.user, AuditLog.Action.CREATE, r, field="label use", new=str(use))
            messages.success(request, f"Added: {use}.")
        else:
            _form_errors(request, uf)
        return redirect(back)
    if action == "remove_use":
        use = get_object_or_404(RemedyUse, pk=request.POST.get("use"), remedy=r)
        audit(request.user, AuditLog.Action.DELETE, r, field="label use", old=str(use))
        use.delete()
        messages.success(request, "Crop removed from this product. Earlier spray records keep their waiting period.")
        return redirect(back)
    if action == "verify":
        if not r.uses.exists():
            messages.error(request, "Add at least one crop with its pre-harvest interval from the label first.")
            return redirect(back)
        r.status, r.verified_by, r.verified_at, r.note = RegisteredRemedy.Status.VERIFIED, request.user, timezone.now(), ""
    elif action in ("reject", "withdraw"):
        if not note:
            messages.error(request, "Give the reason.")
            return redirect(back)
        r.status = RegisteredRemedy.Status.REJECTED if action == "reject" else RegisteredRemedy.Status.WITHDRAWN
        r.note = note
    else:
        return redirect(back)
    r.save()
    audit(request.user, AuditLog.Action.STATUS, r, field="status", old=old, new=r.status)
    if r.submitted_by and r.submitted_by != request.user:
        notify(r.submitted_by, f"{r}: {r.get_status_display().lower()}",
               note or "It can now be chosen when recording a spray.", link="/advice/remedies/request/")
    messages.success(request, f"{r} is now {r.get_status_display().lower()}.")
    return redirect(f"{reverse('advisory:remedies')}?status={r.status}")


@role_required("farmer", "agronomist")
def remedy_request(request):
    """A product that is not in the verified list yet is sent to the administrator for checking."""
    if request.user.is_agronomist and not request.user.is_verified_agronomist:
        raise PermissionDenied("Your registration must be verified first.")
    form = RemedyForm(request.POST or None, request.FILES or None, prefix="r")
    use_form = RemedyUseForm(request.POST or None, prefix="u")
    form.fields["label_photo"].required = True
    form.fields["label_photo"].help_text = "A clear photo of the label showing the L number, crops and waiting periods."
    if request.method == "POST":
        existing = RegisteredRemedy.objects.filter(
            registration_no=request.POST.get("r-registration_no", "").strip().upper()).first()
        if existing and existing.status == RegisteredRemedy.Status.VERIFIED:
            messages.info(request, f"{existing} is already verified. Ask the administrator to add the crop if it "
                                   f"is missing.")
        elif form.is_valid() and use_form.is_valid():
            with transaction.atomic():
                r = form.save(commit=False)
                r.registration_no = r.registration_no.upper()
                r.submitted_by = request.user
                r.status = RegisteredRemedy.Status.PENDING
                r.save()
                use = use_form.save(commit=False)
                use.remedy = r
                use.save()
            audit(request.user, AuditLog.Action.CREATE, r, field="remedy request", new=str(r))
            _notify_admins(f"Product to verify: {r}", f"Sent by {request.user.get_full_name()}. Check the label photo.",
                           "/advice/remedies/?status=pending")
            messages.success(request, f"{r} sent for verification. You can use it once the administrator verifies it.")
            return redirect("advisory:remedy_request")
    mine = (RegisteredRemedy.objects.filter(submitted_by=request.user).prefetch_related("uses__commodity")
            .order_by("-created_at"))
    verified = (RegisteredRemedy.objects.filter(status=RegisteredRemedy.Status.VERIFIED)
                .prefetch_related("uses__commodity"))
    return render(request, "advisory/remedy_request.html", {"form": form, "use_form": use_form, "mine": mine,
                                                            "verified": verified})
