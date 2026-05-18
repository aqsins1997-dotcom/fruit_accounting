from decimal import Decimal

from django import forms
from django.db.models import Sum
from django.utils import timezone

from apps.core.models import Product, Store, Supplier
from apps.payables.models import SupplierPaymentAllocation
from apps.sales.models import SaleItemBatch

from .models import Purchase, PurchaseItem


class PurchaseCreateForm(forms.ModelForm):
    class Meta:
        model = Purchase
        fields = ("supplier", "date", "comment")
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.order_by("name")
        if not self.initial.get("date"):
            self.initial["date"] = timezone.now().date()


class PurchaseItemCreateForm(forms.ModelForm):
    class Meta:
        model = PurchaseItem
        fields = ("store", "product", "quantity_kg", "purchase_price_per_kg")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["store"].queryset = Store.objects.order_by("name")
        self.fields["product"].queryset = Product.objects.order_by("name")


class PurchaseItemPriceUpdateForm(forms.ModelForm):
    class Meta:
        model = PurchaseItem
        fields = ("purchase_price_per_kg",)
        labels = {
            "purchase_price_per_kg": "Новая цена закупки",
        }
        widgets = {
            "purchase_price_per_kg": forms.NumberInput(
                attrs={
                    "min": "0",
                    "step": "0.01",
                    "inputmode": "decimal",
                }
            ),
        }

    def clean_purchase_price_per_kg(self):
        price = self.cleaned_data["purchase_price_per_kg"]
        if price < 0:
            raise forms.ValidationError("Новая цена закупки не может быть отрицательной.")
        return price


class PurchaseItemQuantityUpdateForm(forms.ModelForm):
    class Meta:
        model = PurchaseItem
        fields = ("quantity_kg",)
        labels = {
            "quantity_kg": "Новый вес закупки",
        }
        widgets = {
            "quantity_kg": forms.NumberInput(
                attrs={
                    "min": "0.001",
                    "step": "0.001",
                    "inputmode": "decimal",
                }
            ),
        }

    @property
    def sold_quantity(self):
        if not self.instance.pk:
            return Decimal("0.000")
        return (
            SaleItemBatch.objects.filter(
                purchase_item=self.instance,
                purchase_item__purchase__deleted_at__isnull=True,
                sale_item__sale__deleted_at__isnull=True,
            ).aggregate(total=Sum("quantity"))["total"]
            or Decimal("0.000")
        )

    @property
    def current_stock_quantity(self):
        stock = self.instance.quantity_kg - self.sold_quantity
        return stock if stock > 0 else 0

    @property
    def paid_amount(self):
        if not self.instance.pk:
            return Decimal("0.00")
        return (
            SupplierPaymentAllocation.objects.filter(
                purchase=self.instance.purchase,
                store=self.instance.store,
                payment__status="active",
            ).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )

    def purchase_total_for_store(self, quantity):
        total = quantity * self.instance.purchase_price_per_kg
        for item in self.instance.purchase.items.filter(store=self.instance.store).exclude(pk=self.instance.pk):
            total += item.quantity_kg * item.purchase_price_per_kg
        return total

    @property
    def current_purchase_total_for_store(self):
        return self.purchase_total_for_store(self.instance.quantity_kg)

    @property
    def new_purchase_total_for_store(self):
        quantity = self.cleaned_data.get("quantity_kg") if hasattr(self, "cleaned_data") else None
        if quantity is None:
            quantity = self.instance.quantity_kg
        return self.purchase_total_for_store(quantity)

    def clean_quantity_kg(self):
        quantity = self.cleaned_data["quantity_kg"]
        if quantity <= 0:
            raise forms.ValidationError("Новый вес закупки должен быть больше 0.")

        sold_quantity = self.sold_quantity
        if quantity < sold_quantity:
            minimum_quantity = sold_quantity.quantize(Decimal("0.001"))
            raise forms.ValidationError(
                f"Нельзя сделать вес меньше уже проданного по этой партии. Минимум: {minimum_quantity} кг."
            )

        new_total = self.purchase_total_for_store(quantity)
        paid_amount = self.paid_amount
        if new_total < paid_amount:
            paid_display = paid_amount.quantize(Decimal("0.01"))
            raise forms.ValidationError(
                f"Нельзя сделать сумму закупки меньше уже оплаченной суммы. Оплачено: {paid_display}."
            )

        return quantity
