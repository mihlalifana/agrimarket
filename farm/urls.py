from django.urls import path

from . import views

app_name = "farm"

urlpatterns = [
    path("", views.home, name="home"),
    path("fields/", views.fields, name="fields"),
    path("fields/<int:pk>/", views.field_edit, name="field_edit"),
    path("crops/", views.plantings, name="plantings"),
    path("crops/<int:pk>/", views.planting_detail, name="planting"),
    path("harvest/", views.harvests, name="harvests"),
    path("livestock/", views.herds, name="herds"),
    path("livestock/<int:pk>/", views.herd_detail, name="herd"),
    path("treatments/", views.treatments, name="treatments"),
    path("costs/", views.costs, name="costs"),
    path("costs/<int:pk>/delete/", views.cost_delete, name="cost_delete"),
]
