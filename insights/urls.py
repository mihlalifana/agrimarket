from django.urls import path

from . import views

app_name = "insights"

urlpatterns = [
    path("planner/", views.planner, name="planner"),
    path("my-farm/", views.farm_analytics, name="farm"),
    path("price-guide/", views.price_guide, name="price_guide"),
]
