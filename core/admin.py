from django.contrib import admin

from .models import AuditLog, MessageLog, Notification, SiteSettings


class ReadOnlyAdmin(admin.ModelAdmin):
    """Records that must never be edited or deleted (FR-03, NFR-06)."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "user", "action", "object_type", "object_repr", "field", "old_value", "new_value")
    list_filter = ("action", "object_type")
    search_fields = ("object_repr", "user__email")


@admin.register(MessageLog)
class MessageLogAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "channel", "recipient", "text")


admin.site.register(Notification)
admin.site.register(SiteSettings)
