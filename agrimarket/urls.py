from django.conf import settings
from django.contrib import admin

# The database screen is for account and platform setup only, like the administrator's role:
# orders, payments, payouts, stock and farm records are not shown there.
_SETUP = {"Category", "Commodity", "DeliveryRate", "SiteSettings"}
for _model in list(admin.site._registry):
    if _model._meta.app_label in {"orders", "finance", "farm", "advisory", "marketplace", "insights"} \
            and _model.__name__ not in _SETUP:
        admin.site.unregister(_model)
from django.urls import include, path, re_path

from core.views import media_file

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("orders/", include("orders.urls")),
    path("finance/", include("finance.urls")),
    path("farm/", include("farm.urls")),
    path("insights/", include("insights.urls")),
    path("advice/", include("advisory.urls")),
    path("", include("core.urls")),
    path("", include("marketplace.urls")),
    # Uploaded files go through a view so private ones (dispute photos) are checked before they are sent.
    re_path(r"^media/(?P<path>.+)$", media_file, name="media"),
]
