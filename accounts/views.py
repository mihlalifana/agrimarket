from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import AuditLog
from core.permissions import admin_required
from core.services import audit, notify
from marketplace.models import Listing

from .forms import (BuyerRegistrationForm, FarmerRegistrationForm, FarmerReviewForm,
                    FarmProfileForm, LoginForm, ProfileForm, StaffCreateForm)
from .models import FarmerProfile, User


def register_choice(request):
    return render(request, "accounts/register_choice.html")


def register_buyer(request):
    form = BuyerRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save_user(User.Role.BUYER)
        audit(user, AuditLog.Action.CREATE, user, new="buyer registered")
        login(request, user)
        messages.success(request, "Welcome to AgriMarket! Your buyer account is ready.")
        return redirect("marketplace:catalog")
    return render(request, "accounts/register.html", {"form": form, "kind": "Buyer"})


def register_farmer(request):
    form = FarmerRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save_farmer()
        audit(user, AuditLog.Action.CREATE, user, new="farmer registered")
        for admin in User.objects.filter(role=User.Role.ADMIN, is_active=True):
            notify(admin, "Farmer awaiting verification", user.farm.farm_name,
                   link=f"/accounts/admin/farmers/{user.farm.pk}/")
        login(request, user)
        messages.success(request, "Registration received. You can list stock once an "
                                  "administrator has verified your farm.")
        return redirect("core:dashboard")
    return render(request, "accounts/register.html", {"form": form, "kind": "Farmer"})


def login_view(request):
    if request.user.is_authenticated:
        return redirect("core:dashboard")
    form = LoginForm(request, request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.user)
        audit(form.user, AuditLog.Action.LOGIN, form.user)
        nxt = request.GET.get("next", "")
        if nxt.startswith("/") and not nxt.startswith("//"):
            return redirect(nxt)
        return redirect("core:dashboard")
    return render(request, "accounts/login.html", {"form": form})


@require_POST
def logout_view(request):
    logout(request)
    messages.info(request, "You have been logged out.")
    return redirect("marketplace:catalog")


def _pw_form(user, data=None):
    form = PasswordChangeForm(user, data)
    for f in form.fields.values():
        f.widget.attrs["class"] = "input"
    return form


@login_required
def profile(request, pform=None):
    user = request.user
    form = ProfileForm(request.POST or None, instance=user, prefix="u")
    farm_form = (FarmProfileForm(request.POST or None, instance=user.farm, prefix="f")
                 if user.is_farmer and hasattr(user, "farm") else None)
    if request.method == "POST":
        old = {f: getattr(user, f) for f in form.fields}
        old_bank = (user.farm.bank_code, user.farm.bank_account, user.farm.farm_name) if farm_form else None
        if form.is_valid() and (farm_form is None or farm_form.is_valid()):
            form.save()
            if farm_form:
                farm = farm_form.save()
                if (farm.bank_code, farm.bank_account, farm.farm_name) != old_bank:
                    audit(user, AuditLog.Action.UPDATE, farm, field="bank account",
                          old=f"{old_bank[0]} {old_bank[1][-4:]}", new=f"{farm.bank_code} {farm.bank_account[-4:]}")
                    from orders.services import setup_payout_account
                    ok, note = setup_payout_account(farm)
                    if ok is False:
                        messages.warning(request, f"Card payout account: {note}")
                    elif ok:
                        messages.success(request, note)
            for f in form.fields:
                if old[f] != getattr(user, f):
                    audit(user, AuditLog.Action.UPDATE, user, field=f, old=old[f], new=getattr(user, f))
            messages.success(request, "Profile saved.")
            return redirect("accounts:profile")
    return render(request, "accounts/profile.html", {"form": form, "farm_form": farm_form,
                                                     "pform": pform or _pw_form(user)})


@login_required
def change_password(request):
    if request.method != "POST":
        return redirect(reverse("accounts:profile") + "#password")
    form = _pw_form(request.user, request.POST)
    if form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        audit(user, AuditLog.Action.UPDATE, user, field="password", new="changed")
        messages.success(request, "Password changed.")
        return redirect("accounts:profile")
    return profile(request, pform=form)


@login_required
@require_POST
def request_deletion(request):
    """POPIA: anonymise personal data but keep financial records (NFR-06)."""
    user = request.user
    with transaction.atomic():
        Listing.objects.filter(farmer=user).update(status=Listing.Status.ARCHIVED)
        audit(user, AuditLog.Action.DELETE, user, new="personal data anonymised at user request")
        user.first_name, user.last_name = "Deleted", "user"
        user.email = f"deleted-{user.pk}@deleted.invalid"
        user.phone, user.address = "", ""
        user.is_active = False
        user.anonymised_at = timezone.now()
        user.set_unusable_password()
        user.save()
    logout(request)
    messages.info(request, "Your personal information has been removed.")
    return redirect("marketplace:catalog")


# ---------------------------------------------------------------- admin
@admin_required
def user_list(request, staff_form=None):
    users = User.objects.select_related("farm").order_by("role", "email")
    role = request.GET.get("role", "")
    q = request.GET.get("q", "").strip()
    if role:
        users = users.filter(role=role)
    if q:
        users = users.filter(Q(email__icontains=q) | Q(first_name__icontains=q)
                             | Q(last_name__icontains=q) | Q(farm__farm_name__icontains=q))
    page = Paginator(users, 25).get_page(request.GET.get("page"))
    return render(request, "accounts/user_list.html",
                  {"page": page, "roles": User.Role.choices, "role": role, "q": q,
                   "staff_form": staff_form or StaffCreateForm(), "open_staff": staff_form is not None})


@admin_required
@require_POST
def toggle_active(request, pk):
    user = get_object_or_404(User, pk=pk)
    if user == request.user:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect("accounts:user_list")
    user.is_active = not user.is_active
    user.save(update_fields=["is_active"])
    audit(request.user, AuditLog.Action.UPDATE, user, field="is_active",
          old=not user.is_active, new=user.is_active)
    messages.success(request, f"{user} {'reactivated' if user.is_active else 'deactivated'}.")
    return redirect(request.POST.get("next") or "accounts:user_list")


@admin_required
@require_POST
def unlock(request, pk):
    """Clear a lock caused by too many wrong passwords (the user asked for help)."""
    user = get_object_or_404(User, pk=pk)
    user.failed_login_count, user.locked_until = 0, None
    user.save(update_fields=["failed_login_count", "locked_until"])
    audit(request.user, AuditLog.Action.UPDATE, user, field="locked_until", old="locked", new="unlocked")
    messages.success(request, f"{user} can log in again.")
    return redirect(request.POST.get("next") or "accounts:user_list")


@admin_required
def create_staff(request):
    if request.method != "POST":
        return redirect(reverse("accounts:user_list") + "#add-staff")
    form = StaffCreateForm(request.POST)
    if form.is_valid():
        user = form.save_user(form.cleaned_data["role"])
        if user.role == User.Role.ADMIN:
            user.is_staff = user.is_superuser = True
            user.save(update_fields=["is_staff", "is_superuser"])
        audit(request.user, AuditLog.Action.CREATE, user, new=user.role)
        messages.success(request, f"{user.get_role_display()} account created for {user.email}.")
        return redirect(f"{reverse('accounts:user_list')}?new={user.pk}")
    return user_list(request, staff_form=form)


@admin_required
def farmer_queue(request, review=None):
    """Farmers to verify, with approve / reject on each row (no separate page)."""
    status = request.GET.get("status", FarmerProfile.Status.PENDING)
    farms = list(FarmerProfile.objects.select_related("user", "reviewed_by").filter(status=status)
                 .order_by("user__date_joined"))
    for f in farms:
        f.review_form = review if (review is not None and review.farm_pk == f.pk) else FarmerReviewForm(prefix=f"r{f.pk}")
    return render(request, "accounts/farmer_queue.html",
                  {"farms": farms, "status": status, "statuses": FarmerProfile.Status.choices})


@admin_required
def farmer_review(request, pk):
    farm = get_object_or_404(FarmerProfile.objects.select_related("user"), pk=pk)
    if request.method != "POST":
        return redirect(f"{reverse('accounts:farmer_queue')}?status={farm.status}")
    form = FarmerReviewForm(request.POST, prefix=f"r{farm.pk}")
    form.farm_pk = farm.pk
    if form.is_valid():
        old = farm.status
        approve = form.cleaned_data["decision"] == "approve"
        farm.status = FarmerProfile.Status.APPROVED if approve else FarmerProfile.Status.REJECTED
        farm.rejection_reason = "" if approve else form.cleaned_data["reason"].strip()
        farm.reviewed_at, farm.reviewed_by = timezone.now(), request.user
        farm.save()
        audit(request.user, AuditLog.Action.STATUS, farm, field="status", old=old, new=farm.status)
        if approve:
            notify(farm.user, "Your farm is verified", "You can now list stock for sale.",
                   link="/farmer/listings/new/", sms=True, email=True)
            from orders.services import setup_payout_account
            ok, note = setup_payout_account(farm)
            if ok is False and farm.bank_code:
                messages.warning(request, f"{farm.farm_name}: card payout account not set up. {note}")
        else:
            notify(farm.user, "Farm verification rejected", farm.rejection_reason,
                   link="/accounts/profile/", sms=True, email=True)
        messages.success(request, f"{farm.farm_name} {farm.get_status_display().lower()}.")
        return redirect("accounts:farmer_queue")
    return farmer_queue(request, review=form)
