from django.contrib import admin

from .models import Category, Listing, LivestockDetail, ListingPhoto, PriceTier, StockAdjustment


class PhotoInline(admin.TabularInline):
    model = ListingPhoto
    extra = 0


class TierInline(admin.TabularInline):
    model = PriceTier
    extra = 0


@admin.register(Listing)
class ListingAdmin(admin.ModelAdmin):
    list_display = ("batch_number", "name", "farmer", "category", "quantity_available", "unit", "price", "status")
    list_filter = ("status", "category", "kind")
    search_fields = ("name", "batch_number")
    readonly_fields = ("quantity_available",)  # change stock through adjustments only
    inlines = [TierInline, PhotoInline]


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(admin.ModelAdmin):
    list_display = ("created_at", "listing", "quantity_change", "reason", "created_by")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(Category)
admin.site.register(LivestockDetail)

from .models import Commodity, FavouriteFarm, SearchLog, StockAlert  # noqa: E402


@admin.register(Commodity)
class CommodityAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "category", "units", "standard_reference", "is_active")
    list_filter = ("category", "is_active")


for m in (FavouriteFarm, StockAlert, SearchLog):
    admin.site.register(m)
