from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import FarmerProfile, User


@admin.register(User)
class AgriUserAdmin(UserAdmin):
    list_display = ("email", "first_name", "last_name", "role", "province", "is_active")
    list_filter = ("role", "is_active", "province")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("email",)
    fieldsets = UserAdmin.fieldsets + (
        ("AgriMarket", {"fields": ("role", "phone", "province", "address", "popia_consent_at",
                                   "failed_login_count", "locked_until")}),
    )


@admin.register(FarmerProfile)
class FarmerProfileAdmin(admin.ModelAdmin):
    list_display = ("farm_name", "user", "province", "status", "is_vat_registered")
    list_filter = ("status", "province")
