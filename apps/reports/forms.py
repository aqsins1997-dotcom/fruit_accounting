from django import forms

from apps.core.models import Customer, Product, Store, Supplier
from apps.credits.models import CreditPayment
from apps.inventory.models import Purchase, PurchaseItem
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
    class Meta:
        model = SaleItem
        fields = ("product", "quantity_kg", "sale_price_per_kg")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.order_by("name")
