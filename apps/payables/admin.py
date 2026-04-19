from django.contrib import admin

from .models import SupplierPayment, SupplierPaymentAllocation


class SupplierPaymentAllocationInline(admin.TabularInline):
    model = SupplierPaymentAllocation
    extra = 0
    fields = ("purchase", "store", "amount", "created_at")
    readonly_fields = ("purchase", "store", "amount", "created_at")
    can_delete = False


@admin.register(SupplierPayment)
class SupplierPaymentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "store",
        "supplier",
        "purchase",
        "date",
        "amount",
        "created_at",
    )
    list_filter = (
        "store",
        "supplier",
        "purchase",
        "date",
        "created_at",
    )
    search_fields = (
        "store__name",
        "supplier__name",
        "purchase__id",
        "comment",
    )
    date_hierarchy = "date"
    inlines = [SupplierPaymentAllocationInline]


@admin.register(SupplierPaymentAllocation)
class SupplierPaymentAllocationAdmin(admin.ModelAdmin):
    list_display = ("id", "payment", "purchase", "store", "amount", "created_at")
    list_filter = ("store", "purchase", "created_at")
    search_fields = ("payment__supplier__name", "purchase__id", "store__name")
