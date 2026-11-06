from django.contrib import admin

from .models import CostEntry, Field, Harvest, Herd, Planting, Treatment


@admin.register(Treatment)
class TreatmentAdmin(admin.ModelAdmin):
    list_display = ("treated_on", "herd", "remedy", "registration_no", "withdrawal_days", "withdrawal_ends")
    list_filter = ("kind",)

    def has_delete_permission(self, request, obj=None):
        return False  # the treatment register is a legal record


for m in (Field, Planting, Harvest, Herd, CostEntry):
    admin.site.register(m)
