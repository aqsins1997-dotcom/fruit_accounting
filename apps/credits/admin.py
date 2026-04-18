from django import forms
from django.contrib import admin

from .models import Credit, CreditPayment


class CreditPaymentAdminForm(forms.ModelForm):
    class Meta:
        model = CreditPayment
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        credit = cleaned_data.get("credit")
        amount = cleaned_data.get("amount")

        if not credit or amount is None:
            return cleaned_data

        if amount <= 0:
            self.add_error("amount", "Сумма оплаты должна быть больше 0.")
            return cleaned_data

        remaining_before = credit.remaining_amount

        if self.instance and self.instance.pk:
            remaining_before += self.instance.amount

        if amount > remaining_before:
            self.add_error(
                "amount",
                f"Сумма оплаты больше остатка долга. Доступно к оплате: {remaining_before}",
            )

        return cleaned_data


class CreditPaymentInline(admin.TabularInline):
    model = CreditPayment
    extra = 0
    fields = ("date", "amount", "comment", "created_at")
    readonly_fields = ("created_at",)


@admin.register(Credit)
class CreditAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "customer",
        "store",
        "original_amount",
        "remaining_amount",
        "status",
        "sale",
        "created_at",
    )
    list_filter = (
        "status",
        "store",
        "created_at",
    )
    search_fields = (
        "customer__full_name",
        "customer__phone",
        "sale__id",
        "store__name",
    )
    readonly_fields = (
        "sale",
        "customer",
        "store",
        "original_amount",
        "remaining_amount",
        "status",
        "comment",
        "created_at",
        "updated_at",
    )
    inlines = [CreditPaymentInline]

    fieldsets = (
        ("Основная информация", {
            "fields": (
                "sale",
                "customer",
                "store",
            )
        }),
        ("Суммы", {
            "fields": (
                "original_amount",
                "remaining_amount",
                "status",
            )
        }),
        ("Комментарий", {
            "fields": ("comment",)
        }),
        ("Служебное", {
            "fields": ("created_at", "updated_at")
        }),
    )

    def has_add_permission(self, request):
        return False


@admin.register(CreditPayment)
class CreditPaymentAdmin(admin.ModelAdmin):
    form = CreditPaymentAdminForm

    list_display = (
        "id",
        "credit",
        "customer_name",
        "store_name",
        "date",
        "amount",
        "created_at",
    )
    list_filter = (
        "date",
        "created_at",
        "credit__store",
    )
    search_fields = (
        "credit__customer__full_name",
        "credit__customer__phone",
        "credit__sale__id",
        "credit__store__name",
    )
    autocomplete_fields = ("credit",)

    fields = (
        "credit",
        "date",
        "amount",
        "comment",
    )

    def customer_name(self, obj):
        return obj.credit.customer
    customer_name.short_description = "Клиент"

    def store_name(self, obj):
        return obj.credit.store
    store_name.short_description = "Магазин"