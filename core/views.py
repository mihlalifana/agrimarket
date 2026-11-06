import json
import os
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.forms import StyledMixin
from accounts.models import FarmerProfile, Province, User
from core.permissions import admin_required
from finance import services as fin
from insights.trends import platform_trends
from marketplace.models import Category, Commodity, Listing
from orders.models import DeliveryRate, Dispute, Order, Payment, Refund, SubOrder

from .models import AuditLog, MessageLog, Notification, SiteSettings
from .services import audit

S = SubOrder.Status


def _paystack_key():
    from orders import paystack
    return paystack.config().secret_key


def _remedies():
    from advisory.models import RegisteredRemedy
    return RegisteredRemedy.objects.all()


def _card_mode():
    from orders.services import uses_gateway
    return uses_gateway()


@login_required
def dashboard(request):
    u = request.user
    today = timezone.localdate()
    month_start = today.replace(day=1)
    ctx = {"today": today}

    if u.is_admin_role:
        # The administrator manages accounts and platform setup only - no sales or money.
        from advisory.models import AgronomistProfile
        from django.db.models import Count
        people = User.objects.exclude(anonymised_at__isnull=False)
        by_role = (people.values("role").annotate(active=Count("pk", filter=Q(is_active=True)),
                                                   inactive=Count("pk", filter=Q(is_active=False)))
                   .order_by("role"))
        role_names = dict(User.Role.choices)
        ctx.update(
            template="core/dash_admin.html",
            pending_farmers=FarmerProfile.objects.filter(status="pending").count(),
            pending_agronomists=AgronomistProfile.objects.filter(status="pending").count(),
            locked=people.filter(locked_until__gt=timezone.now()),
            new_this_month=people.filter(date_joined__date__gte=month_start).count(),
            remedies_pending=_remedies().filter(status="pending").count(),
            remedies_verified=_remedies().filter(status="verified").count(),
            farms_no_payout=(FarmerProfile.objects.filter(status="approved", paystack_subaccount="",
                                                          user__is_active=True).count()
                             if _card_mode() == "paystack" else 0),
            no_paystack_key=_card_mode() == "paystack" and not _paystack_key(),
            by_role=[(role_names.get(r["role"], r["role"]), r["active"], r["inactive"]) for r in by_role],
            recent_users=people.order_by("-date_joined")[:8],
            recent_audit=AuditLog.objects.select_related("user").filter(
                object_type__in=["User", "FarmerProfile", "AgronomistProfile", "SiteSettings", "Category",
                                 "Commodity", "DeliveryRate"]).order_by("-created_at")[:8],
        )
    elif u.is_farmer:
        farm = getattr(u, "farm", None)
        mine = Listing.objects.filter(farmer=u, status=Listing.Status.ACTIVE)
        near = [l for l in mine.filter(expiry_date__isnull=False, expiry_date__gte=today).order_by("expiry_date")
                if l.is_near_expiry]
        low = [l for l in mine if l.is_low_stock]
        rows, totals = fin.sales_report(month_start, today, farmer=u)
        labels, values = fin.monthly_series(farmer=u)
        ctx.update(
            template="core/dash_farmer.html", farm=farm, totals=totals, near=near, low=low,
            to_pack=SubOrder.objects.filter(farmer=u, status=S.APPROVED).select_related("order")
            .order_by("order__order_date")[:10],
            awaiting_pay=SubOrder.objects.filter(farmer=u, status=S.PENDING).count(),
            open_disputes=Dispute.objects.filter(suborder__farmer=u, status=Dispute.Status.OPEN).select_related("suborder__order"),
            active_count=mine.count(),
            best=fin.best_sellers(month_start, today, limit=5, farmer=u),
            chart=json.dumps({"months": labels, "monthly": values, "line": True}),
        )
    elif u.is_distributor:
        jobs = SubOrder.objects.filter(distributor=u, status__in=[S.APPROVED, S.PACKED, S.IN_TRANSIT]) \
            .select_related("order", "farmer__farm").order_by("scheduled_date")
        ctx.update(template="core/dash_distributor.html", jobs=jobs,
                   today_jobs=[j for j in jobs if j.scheduled_date == today],
                   unscheduled=[j for j in jobs if not j.scheduled_date])
    elif u.is_agronomist:
        from advisory.views import agronomist_dashboard
        ctx.update(template="core/dash_agronomist.html", **agronomist_dashboard(u))
    else:
        orders = Order.objects.filter(buyer=u).prefetch_related("suborders")[:5]
        open_subs = SubOrder.objects.filter(order__buyer=u, status__in=SubOrder.OPEN_STATUSES).count()
        unpaid = Payment.objects.filter(order__buyer=u, method="card", status=Payment.Status.PENDING,
                                        order__suborders__status=S.PENDING).distinct().select_related("order")
        ctx.update(template="core/dash_buyer.html", orders=orders, open_subs=open_subs, unpaid=unpaid)
    return render(request, ctx["template"], ctx)


# ---------------------------------------------------------------- notifications
@login_required
def notifications(request):
    page = Paginator(request.user.notifications.all(), 30).get_page(request.GET.get("page"))
    return render(request, "core/notifications.html", {"page": page})


@login_required
def notification_open(request, pk):
    n = get_object_or_404(Notification, pk=pk, user=request.user)
    if not n.is_read:
        n.is_read = True
        n.save(update_fields=["is_read"])
    return redirect(n.link if n.link.startswith("/") else "core:notifications")


@login_required
@require_POST
def notifications_read_all(request):
    request.user.notifications.filter(is_read=False).update(is_read=True)
    return redirect("core:notifications")


# ---------------------------------------------------------------- admin tools
@admin_required
def audit_log(request):
    logs = AuditLog.objects.select_related("user")
    q, action = request.GET.get("q", "").strip(), request.GET.get("action", "")
    start, end = request.GET.get("start"), request.GET.get("end")
    if q:
        logs = logs.filter(Q(user__email__icontains=q) | Q(object_repr__icontains=q)
                           | Q(object_type__icontains=q))
    if action:
        logs = logs.filter(action=action)
    if start:
        logs = logs.filter(created_at__date__gte=start)
    if end:
        logs = logs.filter(created_at__date__lte=end)
    page = Paginator(logs, 50).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "core/audit_log.html", {"page": page, "actions": AuditLog.Action.choices,
                                                   "params": params.urlencode()})


@admin_required
def message_log(request):
    page = Paginator(MessageLog.objects.all(), 50).get_page(request.GET.get("page"))
    return render(request, "core/message_log.html", {"page": page})


GATEWAY_FIELDS = ["card_gateway", "paystack_secret_key", "card_payment_minutes"]


class SettingsForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = SiteSettings
        exclude = GATEWAY_FIELDS


class GatewayForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = GATEWAY_FIELDS

    def clean(self):
        d = super().clean()
        key = (d.get("paystack_secret_key") or "").strip() or os.environ.get("PAYSTACK_SECRET_KEY", "").strip()
        if d.get("card_gateway") == "paystack_test" and not key.startswith("sk_test_"):
            self.add_error("paystack_secret_key", "Paste your Paystack TEST secret key (it starts with sk_test_).")
        if d.get("card_gateway") == "paystack_live" and not key.startswith("sk_live_"):
            self.add_error("paystack_secret_key", "Live mode needs the live secret key (starts with sk_live_).")
        d["paystack_secret_key"] = key
        return d


class CategoryForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "kind", "is_active"]


class CommodityForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = Commodity
        fields = ["category", "code", "name", "units", "grades", "standard_reference", "is_perishable",
                  "shelf_life_days"]
        widgets = {"grades": forms.Textarea(attrs={"rows": 3, "placeholder": "One grade per line"})}


class RateForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = DeliveryRate
        fields = ["province", "base_fee", "per_ton_fee"]


@admin_required
def settings_page(request):
    obj = SiteSettings.load()
    form = SettingsForm(request.POST or None, instance=obj, prefix="s")
    cat_form = CategoryForm(prefix="c")
    rate_form = RateForm(prefix="r")
    com_form = CommodityForm(prefix="m")
    gw_form = GatewayForm(instance=obj, prefix="g")
    if request.method == "POST":
        which = request.POST.get("form")
        if which == "payout_accounts":
            from orders.services import setup_payout_account
            done = failed = 0
            for farm in FarmerProfile.objects.filter(status="approved").exclude(bank_code="").exclude(bank_account=""):
                ok, note = setup_payout_account(farm)
                if ok:
                    done += 1
                elif ok is False:
                    failed += 1
                    messages.error(request, f"{farm.farm_name}: {note}", extra_tags="stay")
            if not obj.card_gateway.startswith("paystack") or not _paystack_key():
                messages.error(request, "Choose Paystack and save its secret key first.")
            else:
                messages.info(request, f"{done} farm payout account(s) ready, {failed} not set up.", extra_tags="stay")
            return redirect(reverse("core:settings") + "#card-payments")
        if which == "pftest":
            from orders import paystack
            if obj.card_gateway.startswith("paystack"):
                results = paystack.self_test()
            else:
                results = [("Card payments", True, "The simulated card is used; no gateway to test.")]
            for label, ok, msg in results:
                (messages.info if ok else messages.error)(request, f"{'OK' if ok else 'FAILED'} - {label}: {msg}", extra_tags="stay")
            return redirect(reverse("core:settings") + "#card-payments")
        if which == "gateway":
            before = {f: getattr(obj, f) for f in GATEWAY_FIELDS}
            gw_form = GatewayForm(request.POST, instance=obj, prefix="g")
            if gw_form.is_valid():
                gw_form.save()
                for f, v in before.items():
                    if getattr(obj, f) != v:
                        hidden = f == "paystack_secret_key"
                        audit(request.user, AuditLog.Action.UPDATE, obj, field=f,
                              old="•••" if hidden else v, new="•••" if hidden else getattr(obj, f))
                messages.success(request, "Card payment settings saved.")
                if obj.card_gateway.startswith("paystack") and _paystack_key():
                    # Farms that already saved bank details get their payout account now; no re-entry needed.
                    from orders.services import setup_payout_account
                    ready = failed = 0
                    for farm in (FarmerProfile.objects.filter(status="approved", paystack_subaccount="")
                                 .exclude(bank_code="").exclude(bank_account="")):
                        ok, note = setup_payout_account(farm)
                        ready += ok is True
                        failed += ok is False
                        if ok is False:
                            messages.error(request, f"{farm.farm_name}: {note}", extra_tags="stay")
                    if ready or failed:
                        messages.info(request, f"Farm payout accounts: {ready} set up from saved bank details, "
                                               f"{failed} refused by Paystack.", extra_tags="stay")
                return redirect("core:settings")
        if which == "settings":
            before = {f: getattr(obj, f) for f in form.fields}
            if form.is_valid():
                form.save()
                for f, v in before.items():
                    if getattr(obj, f) != v:
                        audit(request.user, AuditLog.Action.UPDATE, obj, field=f, old=v, new=getattr(obj, f))
                messages.success(request, "Settings saved.")
                return redirect("core:settings")
        elif which == "category":
            cat_form = CategoryForm(request.POST, prefix="c")
            if cat_form.is_valid():
                cat = cat_form.save()
                audit(request.user, AuditLog.Action.CREATE, cat)
                messages.success(request, f"Category {cat.name} added.")
                return redirect("core:settings")
        elif which == "commodity":
            com_form = CommodityForm(request.POST, prefix="m")
            if com_form.is_valid():
                com = com_form.save()
                audit(request.user, AuditLog.Action.CREATE, com, new=f"{com.code} {com.units} {com.grades!r}")
                messages.success(request, f"Product {com.name} added to the standard list.")
                return redirect("core:settings")
        elif which == "rate":
            existing = DeliveryRate.objects.filter(province=request.POST.get("r-province")).first()
            rate_form = RateForm(request.POST, prefix="r", instance=existing)
            if rate_form.is_valid():
                rate = rate_form.save()
                audit(request.user, AuditLog.Action.UPDATE, rate,
                      new=f"base R{rate.base_fee} + R{rate.per_ton_fee}/t")
                messages.success(request, f"Delivery rate for {rate} saved.")
                return redirect("core:settings")
    return render(request, "core/settings.html", {
        "form": form, "cat_form": cat_form, "rate_form": rate_form, "com_form": com_form, "gw_form": gw_form,
        "categories": Category.objects.all(), "rates": DeliveryRate.objects.all(),
        "commodities": Commodity.objects.select_related("category"),
        "key_from_file": not obj.paystack_secret_key and bool(os.environ.get("PAYSTACK_SECRET_KEY", "").strip())})


@admin_required
@require_POST
def commodity_toggle(request, pk):
    c = get_object_or_404(Commodity, pk=pk)
    c.is_active = not c.is_active
    c.save(update_fields=["is_active"])
    audit(request.user, AuditLog.Action.UPDATE, c, field="is_active", old=not c.is_active, new=c.is_active)
    return redirect("core:settings")


@admin_required
@require_POST
def category_toggle(request, pk):
    cat = get_object_or_404(Category, pk=pk)
    cat.is_active = not cat.is_active
    cat.save(update_fields=["is_active"])
    audit(request.user, AuditLog.Action.UPDATE, cat, field="is_active", old=not cat.is_active, new=cat.is_active)
    return redirect("core:settings")


# ---------------------------------------------------------------- uploaded files
PRIVATE_FOLDERS = ("disputes/",)


def media_file(request, path):
    """Serve an uploaded file. Dispute photos are private: only the buyer and the farm involved."""
    from django.conf import settings
    from django.core.exceptions import PermissionDenied
    from django.db.models import Q
    from django.views.static import serve
    from orders.models import Dispute
    if path.startswith(PRIVATE_FOLDERS):
        u = request.user
        allowed = u.is_authenticated and (
            Dispute.objects.filter(photo=path).filter(
                Q(suborder__order__buyer=u) | Q(suborder__farmer=u)).exists())
        if not allowed:
            raise PermissionDenied("This file is private.")
    return serve(request, path, document_root=settings.MEDIA_ROOT)
