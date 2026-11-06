from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("dashboard/", views.dashboard, name="dashboard"),
    path("notifications/", views.notifications, name="notifications"),
    path("notifications/<int:pk>/", views.notification_open, name="notification_open"),
    path("notifications/read-all/", views.notifications_read_all, name="notifications_read_all"),
    path("admin-tools/audit/", views.audit_log, name="audit_log"),
    path("admin-tools/messages/", views.message_log, name="message_log"),
    path("admin-tools/settings/", views.settings_page, name="settings"),
    path("admin-tools/categories/<int:pk>/toggle/", views.category_toggle, name="category_toggle"),
    path("admin-tools/products/<int:pk>/toggle/", views.commodity_toggle, name="commodity_toggle"),
]
