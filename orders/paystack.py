"""Paystack card payments (test or live).

Flow
  1. Checkout creates the order as "Awaiting payment" (stock held).
  2. AgriMarket asks Paystack to start a transaction (initialize) and sends the
     buyer to Paystack's checkout page. Card details are typed there only.
  3. Paystack sends the buyer back to /orders/<id>/paid/?reference=...
  4. AgriMarket asks Paystack directly whether that reference was paid (verify)
     and checks the amount. Only then is the order confirmed.

Because AgriMarket asks Paystack (step 4) instead of waiting for Paystack to
call in, this works on a laptop at 127.0.0.1 with no public address.

Split payments (money straight to the farms)
  Every farm with bank details has a Paystack subaccount. When a buyer pays,
  the transaction carries a flat split: each farm's share (goods + VAT minus
  the 5% commission) is settled by Paystack straight into that farm's bank
  account. What is left - the commission and the delivery fee - stays in
  AgriMarket's own Paystack account. AgriMarket bears Paystack's fee
  (bearer_type "account"). A farm without a subaccount is still paid, by
  AgriMarket on its monthly statement.

Test mode uses a secret key starting with sk_test_ and moves no real money.
Test card: 4084 0840 8408 4081, CVV 408, any future expiry date.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.paystack.co"
FAKE_TLDS = (".test", ".local", ".localhost", ".example", ".invalid", ".lan")


class PaystackError(Exception):
    pass


class Config:
    def __init__(self, settings):
        self.mode = settings.card_gateway
        # The key saved in Settings; if none is saved, PAYSTACK_SECRET_KEY from email_settings.bat (run.bat).
        self.secret_key = (settings.paystack_secret_key.strip()
                           or os.environ.get("PAYSTACK_SECRET_KEY", "").strip())

    @property
    def enabled(self):
        return self.mode in ("paystack_test", "paystack_live")

    @property
    def test(self):
        return self.mode == "paystack_test"


def config():
    from core.models import SiteSettings
    return Config(SiteSettings.load())


def _api(method, path, cfg, payload=None):
    """Call the Paystack API; returns the decoded JSON or raises PaystackError with Paystack's message."""
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=body, method=method, headers={
        "Authorization": f"Bearer {cfg.secret_key}", "Content-Type": "application/json",
        "User-Agent": "AgriMarket"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("message", "")
        except ValueError:
            msg = ""
        raise PaystackError(f"Paystack said: {msg or e.reason} (HTTP {e.code})") from e
    except OSError as e:
        raise PaystackError(f"Could not reach Paystack: {e}") from e
    if not data.get("status"):
        raise PaystackError(f"Paystack said: {data.get('message', 'request refused')}")
    return data


def _email(user):
    e = (user.email or "").strip()
    if "@" in e and "." in e.split("@")[-1] and not e.lower().endswith(FAKE_TLDS):
        return e
    return f"buyer{user.pk}@example.com"  # Paystack needs an email; sample accounts use made-up ones


# Used when Paystack's bank list can't be fetched (South African universal branch codes).
ZA_BANKS = [("632005", "Absa Bank"), ("470010", "Capitec Bank"), ("250655", "First National Bank (FNB)"),
            ("198765", "Nedbank"), ("051001", "Standard Bank")]
_bank_cache = {"at": 0, "banks": None}


def banks(cfg=None):
    """[(code, name)] of South African banks Paystack can pay into; cached for a day."""
    if _bank_cache["banks"] and time.time() - _bank_cache["at"] < 86400:
        return _bank_cache["banks"]
    cfg = cfg or config()
    found = []
    if cfg.enabled and cfg.secret_key:
        try:
            data = _api("GET", "/bank?currency=ZAR&perPage=100", cfg)
            found = sorted({(str(b["code"]), b["name"]) for b in data.get("data", [])
                            if b.get("code") and b.get("active", True)}, key=lambda b: b[1].lower())
        except (PaystackError, KeyError, TypeError):
            found = []
    if found:
        _bank_cache.update(at=time.time(), banks=found)
    return found or ZA_BANKS


def save_subaccount(farm, commission_rate, cfg=None):
    """Create (or update) the farm's Paystack subaccount. Returns the subaccount code."""
    cfg = cfg or config()
    user = farm.user
    payload = {"business_name": farm.farm_name[:100], "settlement_bank": farm.bank_code, "bank_code": farm.bank_code,
               "account_number": farm.bank_account, "percentage_charge": float(commission_rate * 100),
               "description": f"AgriMarket farm, {farm.town}",
               "primary_contact_name": user.get_full_name()[:100], "primary_contact_email": _email(user),
               "primary_contact_phone": user.phone}
    if farm.paystack_subaccount:
        data = _api("PUT", f"/subaccount/{urllib.parse.quote(farm.paystack_subaccount)}", cfg, payload)
    else:
        data = _api("POST", "/subaccount", cfg, payload)
    code = (data.get("data") or {}).get("subaccount_code") or farm.paystack_subaccount
    if not code:
        raise PaystackError("Paystack did not return a subaccount code.")
    return code


def split_for(order):
    """{sub-order id: (subaccount, share paid to the farm, amount kept back)} for this order.

    share = goods + VAT - commission, less anything the farm still owes AgriMarket for its part of
    earlier refunds (kept back so the debt is repaid automatically).
    """
    from decimal import Decimal
    from core.models import SiteSettings
    from .services import farm_debt
    rate = SiteSettings.load().commission_rate
    shares, debts = {}, {}
    for sub in order.suborders.select_related("farmer__farm").order_by("pk"):
        farm = getattr(sub.farmer, "farm", None)
        if not (farm and farm.paystack_subaccount):
            continue
        commission = (sub.goods_subtotal * rate).quantize(Decimal("0.01"))
        entitled = sub.goods_subtotal + sub.vat_amount - commission
        if sub.farmer_id not in debts:
            debts[sub.farmer_id] = farm_debt(sub.farmer)
        kept = min(debts[sub.farmer_id], entitled)
        debts[sub.farmer_id] -= kept
        shares[sub.pk] = (farm.paystack_subaccount, entitled - kept, kept)
    return shares


def refund(transaction_id, amount, note="", cfg=None):
    """Refund `amount` (rand) of a Paystack transaction to the buyer's card. Returns Paystack's refund id."""
    cfg = cfg or config()
    data = _api("POST", "/refund", cfg, {"transaction": str(transaction_id), "amount": int(round(amount * 100)),
                                         "merchant_note": note[:100]})
    d = data.get("data") or {}
    return str(d.get("id") or (d.get("transaction") or {}).get("id") or "")


def start(order, callback_url, cfg=None, split=None):
    """Start a Paystack transaction. Returns (checkout_url, reference).

    `split` is split_for(order): each farm's share is paid by Paystack straight into its bank account.
    """
    cfg = cfg or config()
    reference = f"{order.number}-{int(time.time())}"  # a new one for every attempt
    payload = {"email": _email(order.buyer), "amount": int(round(order.grand_total * 100)),
               "currency": "ZAR", "reference": reference, "callback_url": callback_url,
               "channels": ["card"],
               "metadata": {"order": order.number, "payment_id": order.payment.pk,
                            "cancel_action": callback_url}}
    subaccounts = [{"subaccount": code, "share": int(round(share * 100))}
                   for code, share, _kept in (split or {}).values() if share > 0]
    if subaccounts:
        payload["split"] = {"type": "flat", "bearer_type": "account", "subaccounts": subaccounts}
    try:
        data = _api("POST", "/transaction/initialize", cfg, payload)
    except PaystackError as e:
        if "currency" not in str(e).lower() and "channel" not in str(e).lower():
            raise
        # account not set up for ZAR or card-only: let Paystack use the account's defaults
        payload.pop("currency"); payload.pop("channels")
        data = _api("POST", "/transaction/initialize", cfg, payload)
    return data["data"]["authorization_url"], data["data"].get("reference", reference)


def verify(reference, cfg=None):
    """(status, amount in rand, Paystack id, raw) for a reference. status is 'success' when paid."""
    cfg = cfg or config()
    data = _api("GET", f"/transaction/verify/{urllib.parse.quote(reference)}", cfg)
    d = data.get("data") or {}
    from decimal import Decimal
    amount = Decimal(d.get("amount") or 0) / 100
    return (d.get("status") or ""), amount, str(d.get("id") or ""), d


def self_test(cfg=None):
    """Check the secret key with a harmless read-only call."""
    cfg = cfg or config()
    if not cfg.secret_key:
        return [("Paystack key", False, "Enter your Paystack secret key and save first.")]
    if cfg.test and not cfg.secret_key.startswith("sk_test_"):
        return [("Paystack key", False, "Test mode needs the TEST secret key (starts with sk_test_).")]
    try:
        _api("GET", "/transaction?perPage=1", cfg)
    except PaystackError as e:
        return [("Paystack key", False, str(e))]
    return [("Paystack key", True, "Paystack accepted the secret key. Card payments are ready.")]
