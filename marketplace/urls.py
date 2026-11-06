from django.urls import path

from . import views

app_name = "marketplace"

urlpatterns = [
    path("", views.catalog, name="catalog"),
    path("listing/<int:pk>/", views.listing_detail, name="listing"),
    path("farms/<int:pk>/", views.farmer_page, name="farmer_page"),
    path("cart/", views.cart_view, name="cart"),
    path("favourites/", views.favourites, name="favourites"),
    path("favourites/<int:farmer_pk>/toggle/", views.toggle_favourite, name="toggle_favourite"),
    path("alerts/", views.alerts, name="alerts"),
    path("alerts/<int:pk>/delete/", views.alert_delete, name="alert_delete"),
    path("cart/add/<int:pk>/", views.cart_add, name="cart_add"),
    path("cart/item/<int:item_id>/", views.cart_update, name="cart_update"),
    path("farmer/listings/", views.my_listings, name="my_listings"),
    path("farmer/listings/new/", views.listing_create, name="listing_create"),
    path("farmer/listings/<int:pk>/edit/", views.listing_edit, name="listing_edit"),
    path("farmer/listings/<int:pk>/adjust/", views.listing_adjust, name="listing_adjust"),
    path("farmer/listings/<int:pk>/remove/", views.listing_remove, name="listing_remove"),
    path("farmer/listings/<int:pk>/restore/", views.listing_restore, name="listing_restore"),
    path("farmer/photos/<int:pk>/delete/", views.photo_delete, name="photo_delete"),
]
