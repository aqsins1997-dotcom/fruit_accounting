from django import forms
from django.utils import timezone

from apps.core.models import Product, Store, Supplier

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
