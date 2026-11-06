
# Most tests use the built-in simulated card (no internet); Paystack tests switch Paystack on.
from core.models import SiteSettings as _S  # noqa: E402
_f = _S._meta.get_field("card_gateway")
_f.default = "simulated"
_f.__dict__.pop("_get_default", None)
