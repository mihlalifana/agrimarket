from django.contrib import admin

from .models import (DeliveryRate, Dispute, Order, OrderItem, Payment, Refund, Review,
                     StatusChange, SubOrder)


class NoDelete(admin.ModelAdmin):
    """Financial records are kept, never deleted (NFR-06)."""

    def has_delete_permission(self, request, obj=None):
        return False


class ItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    can_delete = False
    readonly_fields = [f.name for f in OrderItem._meta.fields]


@admin.register(Order)
class OrderAdmin(NoDelete):
    list_display = ("number", "buyer", "grand_total", "payment_method", "created_at")
    search_fields = ("number", "buyer__email")
    readonly_fields = ("number", "goods_total", "vat_total", "delivery_total", "grand_total")


@admin.register(SubOrder)
class SubOrderAdmin(NoDelete):
    list_display = ("order", "farmer", "status", "goods_subtotal", "distributor")
    list_filter = ("status",)
    readonly_fields = ("status", "goods_subtotal", "vat_amount", "commission_amount", "invoice_number")
    inlines = [ItemInline]


for m in (Payment, Refund, Dispute):
    admin.site.register(m, NoDelete)
admin.site.register(DeliveryRate)
admin.site.register(Review)
admin.site.register(StatusChange, NoDelete)

from .models import Quote, QuoteRequest  # noqa: E402

admin.site.register(QuoteRequest)
admin.site.register(Quote, NoDelete)
