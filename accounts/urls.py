from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import views

app_name = "accounts"

urlpatterns = [
    path("register/", views.register_choice, name="register"),
    path("register/buyer/", views.register_buyer, name="register_buyer"),
    path("register/farmer/", views.register_farmer, name="register_farmer"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("profile/", views.profile, name="profile"),
    path("password/", views.change_password, name="change_password"),
    path("delete-me/", views.request_deletion, name="request_deletion"),
    # Password reset by e-mailed link, valid 30 minutes and usable once (FR-01).
    path("password-reset/", auth_views.PasswordResetView.as_view(
        template_name="accounts/password_reset.html",
        email_template_name="accounts/password_reset_email.txt",
        subject_template_name="accounts/password_reset_subject.txt",
        success_url=reverse_lazy("accounts:password_reset_done")), name="password_reset"),
    path("password-reset/sent/", auth_views.PasswordResetDoneView.as_view(
        template_name="accounts/password_reset_done.html"), name="password_reset_done"),
    path("reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(
        template_name="accounts/form_page.html",
        extra_context={"title": "Choose a new password", "button": "Set password"},
        success_url=reverse_lazy("accounts:login")), name="password_reset_confirm"),
    # Admin
    path("admin/users/", views.user_list, name="user_list"),
    path("admin/users/new/", views.create_staff, name="create_staff"),
    path("admin/users/<int:pk>/toggle/", views.toggle_active, name="toggle_active"),
    path("admin/users/<int:pk>/unlock/", views.unlock, name="unlock"),
    path("admin/farmers/", views.farmer_queue, name="farmer_queue"),
    path("admin/farmers/<int:pk>/", views.farmer_review, name="farmer_review"),
]
