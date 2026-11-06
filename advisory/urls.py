from django.urls import path

from . import views

app_name = "advisory"

urlpatterns = [
    path("", views.home, name="home"),
    path("register/", views.register_agronomist, name="register"),
    path("link/", views.link_add, name="link_add"),
    path("link/<int:pk>/end/", views.link_end, name="link_end"),
    path("advice/<int:pk>/respond/", views.rec_respond, name="rec_respond"),
    path("soil/", views.soil_add, name="soil_add"),
    path("sprays/", views.spray_add, name="spray_add"),
    path("alerts/", views.alerts, name="alerts"),
    path("farms/<int:farmer_pk>/", views.farm_view, name="farm"),
    path("farms/<int:farmer_pk>/visit/", views.visit_add, name="visit_add"),
    path("farms/<int:farmer_pk>/advice/", views.rec_add, name="rec_add"),
    path("farms/<int:farmer_pk>/soil/", views.soil_add, name="soil_add_for"),
    path("farms/<int:farmer_pk>/sprays/", views.spray_add, name="spray_add_for"),
    path("remedies/", views.remedies, name="remedies"),
    path("remedies/<int:pk>/", views.remedy_action, name="remedy_action"),
    path("remedies/request/", views.remedy_request, name="remedy_request"),
    path("admin/agronomists/", views.agronomist_queue, name="agronomist_queue"),
    path("admin/agronomists/<int:pk>/", views.agronomist_review, name="agronomist_review"),
]
