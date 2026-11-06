from django.urls import path

from . import views

app_name = "finance"

urlpatterns = [
    path("reports/", views.reports, name="reports"),
    path("earnings/", views.earnings, name="earnings"),
    path("income/", views.income, name="income"),
    path("income/refunds/<int:pk>/retry/", views.retry_refund, name="retry_refund"),
]
