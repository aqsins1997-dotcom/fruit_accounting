from django import forms
from django.contrib import admin

from .models import CashRegister, Sale, SaleItem


class SaleItemInlineForm(forms.ModelForm):
    class Meta:
        model = SaleItem
        fields = "__all__"


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    form = SaleItemInlineForm
    extra = 1
    min_num = 1
    fields = (
        "product",
        "quantity_kg",
        "sale_price_per_kg",
        "cost_price_per_kg",
        "line_total",
        "line_cost_total",
        "profit",
    )
    readonly_fields = (
        "cost_price_per_kg",
        "line_total",
        "line_cost_total",
        "profit",
    )


class SaleAdminForm(forms.ModelForm):
    class Meta:
        model = Sale
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        payment_type = cleaned_data.get("payment_type")
        customer = cleaned_data.get("customer")

        if payment_type == Sale.PAYMENT_TYPE_CREDIT and not customer:
            self.add_error("customer", "Для продажи в кредит нужно указать клиента.")

        if payment_type == Sale.PAYMENT_TYPE_CASH:
            cleaned_data["customer"] = None

        return cleaned_data


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    form = SaleAdminForm

    list_display = (
        "id",
        "store",
        "date",
        "payment_type",
        "customer",
        "total_amount",
        "total_cost",
        "total_profit",
        "created_at",
    )
    list_filter = (
        "payment_type",
        "store",
        "date",
        "created_at",
    )
    search_fields = (
        "id",
        "customer__full_name",
        "customer__phone",
        "store__name",
        "comment",
    )
    date_hierarchy = "date"
    inlines = [SaleItemInline]

    fields = (
        "store",
        "date",
        "payment_type",
        "customer",
        "comment",
        "total_amount",
        "total_cost",
        "total_profit",
    )

    readonly_fields = (
        "total_amount",
        "total_cost",
        "total_profit",
    )

    def save_model(self, request, obj, form, change):
        if obj.payment_type == Sale.PAYMENT_TYPE_CASH:
            obj.customer = None
        super().save_model(request, obj, form, change)


@admin.register(SaleItem)
class SaleItemAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "sale",
        "product",
        "quantity_kg",
        "sale_price_per_kg",
        "cost_price_per_kg",
        "line_total",
        "profit",
        "created_at",
    )
    list_filter = (
        "sale__store",
        "sale__payment_type",
        "created_at",
    )
    search_fields = (
        "sale__id",
        "product__name",
        "sale__customer__full_name",
    )
    readonly_fields = (
        "cost_price_per_kg",
        "line_total",
        "line_cost_total",
        "profit",
    )


@admin.register(CashRegister)
class CashRegisterAdmin(admin.ModelAdmin):
    list_display = (
        "store",
        "balance",
        "updated_at",
    )
    search_fields = ("store__name",)