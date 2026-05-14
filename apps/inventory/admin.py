from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse

from .models import Purchase, PurchaseItem, StoreStock, StockMovement


class PurchaseItemInline(admin.TabularInline):
    model = PurchaseItem
    extra = 1


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ("id", "supplier", "date", "deleted_status", "deleted_at")
    list_filter = ("supplier", "date", "deleted_at")
    search_fields = ("supplier__name",)
    readonly_fields = ("deleted_at",)
    inlines = [PurchaseItemInline]

    @admin.display(description="Статус")
    def deleted_status(self, obj):
        return "Удалена" if obj.deleted_at else "Активна"

    def delete_model(self, request, obj):
        changed = obj.soft_delete()
        if changed:
            self.message_user(
                request,
                "Закупка помечена удаленной. Продажи и старые записи сохранены.",
            )
        else:
            self.message_user(request, "Закупка уже была помечена удаленной.")

    def delete_queryset(self, request, queryset):
        changed_count = 0
        for purchase in queryset:
            if purchase.soft_delete():
                changed_count += 1
        self.message_user(
            request,
            f"Закупок помечено удаленными: {changed_count}. Продажи и старые записи сохранены.",
        )

    def response_delete(self, request, obj_display, obj_id):
        opts = self.model._meta
        return HttpResponseRedirect(
            reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist", current_app=self.admin_site.name)
        )


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
    list_filter = ("store", "product", "purchase__supplier", "purchase__deleted_at")
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
