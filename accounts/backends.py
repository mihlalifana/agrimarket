from datetime import timedelta

from django.contrib.auth.backends import ModelBackend
from django.utils import timezone

from core.models import SiteSettings

from .models import User


class EmailBackend(ModelBackend):
    """Email + password login with lock-out after repeated failures (FR-01)."""

    def authenticate(self, request, username=None, password=None, email=None, **kwargs):
        email = (email or username or "").strip().lower()
        if not email or password is None:
            return None
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            User().set_password(password)  # equalise timing
            return None

        if user.is_locked:
            if request is not None:
                request.login_locked = True
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            if user.failed_login_count or user.locked_until:
                user.failed_login_count = 0
                user.locked_until = None
                user.save(update_fields=["failed_login_count", "locked_until"])
            return user

        rules = SiteSettings.load()
        user.failed_login_count += 1
        if user.failed_login_count >= rules.max_failed_logins:
            user.locked_until = timezone.now() + timedelta(minutes=rules.lockout_minutes)
            user.failed_login_count = 0
        user.save(update_fields=["failed_login_count", "locked_until"])
        return None
