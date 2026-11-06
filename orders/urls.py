from django.urls import path

from . import views

app_name = "orders"

urlpatterns = [
    path("checkout/", views.checkout, name="checkout"),
    path("mine/", views.my_orders, name="my_orders"),
    path("<int:pk>/", views.order_detail, name="order_detail"),
    path("sub/<int:sub_pk>/dispute/", views.raise_dispute, name="raise_dispute"),
    path("sub/<int:sub_pk>/review/<int:listing_pk>/", views.add_review, name="add_review"),
    path("sub/<int:pk>/", views.suborder_detail, name="suborder"),
    path("sub/<int:pk>/action/<str:action>/", views.suborder_action, name="suborder_action"),
    path("sub/<int:pk>/schedule/", views.schedule, name="schedule"),
    path("sub/<int:pk>/invoice.pdf", views.invoice_pdf, name="invoice"),
    path("incoming/", views.farmer_orders, name="farmer_orders"),
    path("deliveries/", views.distributor_jobs, name="distributor_jobs"),
    path("register/", views.order_register, name="register"),
    path("disputes/", views.dispute_list, name="dispute_list"),
    path("disputes/<int:pk>/", views.dispute_detail, name="dispute_detail"),
    path("reviews/<int:pk>/hide/", views.hide_review, name="hide_review"),
    path("<int:pk>/reorder/", views.reorder, name="reorder"),
    path("<int:pk>/pay/", views.card_pay, name="card_pay"),
    path("<int:pk>/paid/", views.card_return, name="card_return"),
    path("<int:pk>/pay-cancelled/", views.card_cancel, name="card_cancel"),
    path("<int:pk>/pay-check/", views.card_check, name="card_check"),
    path("quotes/", views.rfq_list, name="rfq_list"),
    path("quotes/new/", views.rfq_create, name="rfq_create"),
    path("quotes/<int:pk>/", views.rfq_detail, name="rfq_detail"),
    path("quotes/<int:pk>/close/", views.rfq_close, name="rfq_close"),
    path("quotes/accept/<int:pk>/", views.quote_accept, name="quote_accept"),
]
