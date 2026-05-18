from django import forms

from apps.core.models import Customer, Product, Store, Supplier
from apps.credits.models import CreditPayment
from apps.inventory.models import Purchase, PurchaseItem
from apps.sales.forms import PurchaseItemChoiceField, available_purchase_item_queryset
from apps.sales.models import Sale, SaleItem


class MobilePurchaseForm(forms.ModelForm):
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


class MobilePurchaseItemForm(forms.ModelForm):
    class Meta:
        model = PurchaseItem
        fields = ("store", "product", "quantity_kg", "purchase_price_per_kg")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["store"].queryset = Store.objects.order_by("name")
        self.fields["product"].queryset = Product.objects.order_by("name")


class MobileCreditPaymentForm(forms.ModelForm):
    class Meta:
        model = CreditPayment
        fields = ("date", "amount", "comment")
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }


class MobileSaleForm(forms.ModelForm):
    class Meta:
        model = Sale
        fields = ("store", "date", "payment_type", "customer", "comment")
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["store"].queryset = Store.objects.order_by("name")
        self.fields["customer"].queryset = Customer.objects.order_by("name")
        self.fields["customer"].required = False


class MobileSaleItemForm(forms.ModelForm):
    purchase_item = PurchaseItemChoiceField(
        label="Закупка / партия",
        queryset=PurchaseItem.objects.none(),
        empty_label="Выберите закупку/партию",
        required=True,
    )

    class Meta:
        model = SaleItem
        fields = ("product", "purchase_item", "quantity_kg", "sale_price_per_kg")

    def __init__(self, *args, store_id=None, product_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.store_id = store_id
        self.fields["product"].queryset = Product.objects.order_by("name")
        purchase_item_ids = [
            item.id for item in available_purchase_item_queryset(store_id=store_id, product_id=product_id)
        ]
        self.fields["purchase_item"].queryset = (
            PurchaseItem.objects.select_related("purchase", "purchase__supplier", "store", "product")
            .filter(id__in=purchase_item_ids)
            .order_by("purchase__date", "id")
        )

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance._selected_purchase_item = self.cleaned_data.get("purchase_item")
        if commit:
            instance.save()
            self.save_m2m()
        return instance
