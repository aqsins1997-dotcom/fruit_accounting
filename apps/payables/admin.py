from django.contrib import admin

from .models import SupplierPayment


@admin.register(SupplierPayment)
class SupplierPaymentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "store",
        "supplier",
        "date",
        "amount",
        "created_at",
    )
    list_filter = (
        "store",
        "supplier",
        "date",
        "created_at",
    )
    search_fields = (
        "store__name",
        "supplier__name",
        "comment",
    )
    date_hierarchy = "date"