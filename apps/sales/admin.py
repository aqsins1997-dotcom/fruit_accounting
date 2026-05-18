from django import forms
from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse

from apps.inventory.models import PurchaseItem

from .forms import PurchaseItemChoiceField, available_purchase_item_queryset
from .models import CashRegister, Sale, SaleItem, SaleItemBatch


class SaleItemInlineForm(forms.ModelForm):
    purchase_item = PurchaseItemChoiceField(
        label="Закупка / партия",
        queryset=PurchaseItem.objects.none(),
        required=False,
    )

    class Meta:
        model = SaleItem
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        purchase_items = available_purchase_item_queryset()
        if self.instance and self.instance.pk:
            batch = self.instance.batches.select_related("purchase_item").first()
            if batch and batch.purchase_item not in purchase_items:
                purchase_items.append(batch.purchase_item)
            if batch:
                self.initial["purchase_item"] = batch.purchase_item_id
        self.fields["purchase_item"].queryset = (
            PurchaseItem.objects.select_related("purchase", "purchase__supplier", "store", "product")
            .filter(id__in=[item.id for item in purchase_items])
            .order_by("purchase__date", "id")
        )

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance._selected_purchase_item = self.cleaned_data.get("purchase_item")
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    form = SaleItemInlineForm
    extra = 1
    min_num = 1
    fields = (
        "product",
        "purchase_item",
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
        "deleted_status",
        "deleted_at",
        "created_at",
    )
    list_filter = (
        "payment_type",
        "store",
        "date",
        "deleted_at",
        "created_at",
    )
    search_fields = (
        "id",
        "customer__name",
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
        "deleted_at",
    )

    readonly_fields = (
        "total_amount",
        "total_cost",
        "total_profit",
        "deleted_at",
    )

    @admin.display(description="Статус")
    def deleted_status(self, obj):
        return "Удалена" if obj.deleted_at else "Активна"

    def save_model(self, request, obj, form, change):
        if obj.payment_type == Sale.PAYMENT_TYPE_CASH:
            obj.customer = None
        super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        changed = obj.soft_delete()
        if changed:
            self.message_user(
                request,
                "Продажа помечена удаленной. Строки продажи и FIFO-история сохранены.",
            )
        else:
            self.message_user(request, "Продажа уже была помечена удаленной.")

    def delete_queryset(self, request, queryset):
        changed_count = 0
        for sale in queryset:
            if sale.soft_delete():
                changed_count += 1
        self.message_user(
            request,
            f"Продаж помечено удаленными: {changed_count}. Строки продажи и FIFO-история сохранены.",
        )

    def response_delete(self, request, obj_display, obj_id):
        opts = self.model._meta
        return HttpResponseRedirect(
            reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist", current_app=self.admin_site.name)
        )


@admin.register(SaleItem)
class SaleItemAdmin(admin.ModelAdmin):
    form = SaleItemInlineForm
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
        "sale__deleted_at",
        "created_at",
    )
    search_fields = (
        "sale__id",
        "product__name",
        "sale__customer__name",
    )
    readonly_fields = (
        "cost_price_per_kg",
        "line_total",
        "line_cost_total",
        "profit",
    )

    def delete_model(self, request, obj):
        obj.sale.soft_delete()
        self.message_user(
            request,
            "Продажа помечена удаленной. Строка продажи и FIFO-история сохранены.",
        )

    def delete_queryset(self, request, queryset):
        sale_ids = set(queryset.values_list("sale_id", flat=True))
        changed_count = 0
        for sale in Sale.objects.filter(id__in=sale_ids):
            if sale.soft_delete():
                changed_count += 1
        self.message_user(
            request,
            f"Продаж помечено удаленными: {changed_count}. Строки продажи и FIFO-история сохранены.",
        )


@admin.register(SaleItemBatch)
class SaleItemBatchAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "sale_item",
        "purchase_item",
        "quantity",
        "sale_price",
        "total_amount",
        "created_at",
    )
    list_filter = (
        "purchase_item__store",
        "purchase_item__product",
        "sale_item__sale__deleted_at",
        "created_at",
    )
    search_fields = (
        "sale_item__sale__id",
        "sale_item__product__name",
        "purchase_item__purchase__id",
        "purchase_item__store__name",
    )
    readonly_fields = ("created_at", "updated_at")


@admin.register(CashRegister)
class CashRegisterAdmin(admin.ModelAdmin):
    list_display = (
        "store",
        "balance",
        "updated_at",
    )
    search_fields = ("store__name",)
