from django.contrib import admin

from .models import Purchase, PurchaseItem, StoreStock, StockMovement


class PurchaseItemInline(admin.TabularInline):
    model = PurchaseItem
    extra = 1


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ("id", "supplier", "date")
    list_filter = ("supplier", "date")
    search_fields = ("supplier__name",)
    inlines = [PurchaseItemInline]


@admin.register(PurchaseItem)
class PurchaseItemAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "purchase",
        "store",
        "product",
        "quantity_kg",
        "purchase_price_per_kg",
        "total_cost",
    )
    list_filter = ("store", "product", "purchase__supplier")
    search_fields = ("product__name", "store__name", "purchase__supplier__name")


@admin.register(StoreStock)
class StoreStockAdmin(admin.ModelAdmin):
    list_display = ("id", "store", "product", "quantity_kg", "average_purchase_price")
    list_filter = ("store", "product")
    search_fields = ("store__name", "product__name")


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "date",
        "store",
        "product",
        "movement_type",
        "quantity_kg_delta",
        "unit_cost",
        "total_cost",
    )
    list_filter = ("movement_type", "store", "date")
    search_fields = ("store__name", "product__name", "reference_note")