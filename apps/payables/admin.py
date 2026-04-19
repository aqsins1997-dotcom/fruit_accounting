from django.contrib import admin

from .models import SupplierPayment


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
