"""Small helpers to build test data."""
from decimal import Decimal

from django.utils import timezone

from accounts.models import FarmerProfile, User
from marketplace.models import Cart, CartItem, Category, Commodity, Listing, LivestockDetail
from orders.models import DeliveryRate

PW = "Farm2026!"
_n = [0]


def _next():
    _n[0] += 1
    return _n[0]


def user(role="buyer", **kw):
    n = _next()
    kw.setdefault("province", "FS")
    return User.objects.create_user(email=f"{role}{n}@test.za", password=PW, role=role,
                                     first_name=role.title(), last_name=str(n), phone=f"08210{n:05d}",
                                     popia_consent_at=timezone.now(), **kw)


def farmer(vat=False, status="approved", payout_account=True, **kw):
    """A farm; by default it has bank details and a Paystack subaccount, so buyers can pay it by card."""
    u = user("farmer", **kw)
    FarmerProfile.objects.create(user=u, farm_name=f"Farm {u.pk}", town="Bloemfontein", province="FS",
                                 brand_mark="ABC", is_vat_registered=vat, vat_number="4123456789" if vat else "",
                                 status=status,
                                 bank_code="250655" if payout_account else "",
                                 bank_account=f"62{u.pk:08d}" if payout_account else "",
                                 paystack_subaccount=f"ACCT_test{u.pk}" if payout_account else "")
    return u


def category(kind="produce"):
    name = f"{kind}-{_next()}"
    return Category.objects.create(name=name, kind=kind)


def commodity(kind="produce", unit="ton", grades=""):
    n = _next()
    return Commodity.objects.create(category=category(kind), code=f"T{n}", name=f"Test product {n}",
                                    units="head" if kind == "livestock" else f"{unit},kg", grades=grades)


def listing(farmer_user, qty="10", price="100.00", unit="ton", kind="produce", **kw):
    if kind == "livestock":
        unit = "head"
    com = kw.pop("commodity", None) or commodity(kind, unit)
    kw.pop("name", None)
    l = Listing.objects.create(farmer=farmer_user, commodity=com, unit=unit, price=Decimal(price),
                               quantity_available=Decimal(qty),
                               min_order_qty=Decimal(kw.pop("min_order_qty", "1")), **kw)
    if kind == "livestock":
        LivestockDetail.objects.create(listing=l, species="Cattle", breed="Bonsmara", sex="mixed",
                                       age_months=8, avg_weight_kg=Decimal("230"), tag_or_brand="ABC 1",
                                       vaccination_status="up_to_date")
    return l


def cart_with(buyer, *lines):
    cart, _ = Cart.objects.get_or_create(buyer=buyer)
    cart.items.all().delete()
    for l, q in lines:
        CartItem.objects.create(cart=cart, listing=l, quantity=Decimal(q))
    return cart


def rates():
    for p in ("FS", "GP", "KZN"):
        DeliveryRate.objects.get_or_create(province=p, defaults={"base_fee": 300, "per_ton_fee": 100})


def use_paystack():
    """Switch card payments to Paystack test mode (no network calls are made unless a test mocks them)."""
    from core.models import SiteSettings
    s = SiteSettings.load()
    s.card_gateway = "paystack_test"
    s.paystack_secret_key = "sk_test_abc123"
    s.save()


def remedy(commodity, phi_days=7, reg="L5021", name="Mancozeb 800 WP", status="verified"):
    """A catalogue entry registered for one crop, with that crop's label pre-harvest interval."""
    from advisory.models import RegisteredRemedy, RemedyUse
    r, _ = RegisteredRemedy.objects.get_or_create(registration_no=reg, defaults={
        "trade_name": name, "active_ingredient": "mancozeb", "source": "Test label", "status": status,
        "verified_at": timezone.now() if status == "verified" else None,
        "note": "" if status in ("verified", "pending") else "Test reason"})
    RemedyUse.objects.get_or_create(remedy=r, commodity=commodity, defaults={"phi_days": phi_days})
    return r


def spray(planting, applied_on, phi_days=7, **kw):
    """A spray made with a verified product whose label gives `phi_days` for this crop."""
    from advisory.models import SprayRecord
    r = remedy(planting.commodity, phi_days, **kw)
    return SprayRecord.objects.create(planting=planting, remedy=r, target="Early blight", rate="2 kg/ha",
                                      applied_on=applied_on)
