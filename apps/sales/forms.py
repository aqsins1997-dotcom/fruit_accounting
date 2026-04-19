from django import forms
from django.utils import timezone

from apps.core.models import Customer, Product, Store

from .models import Sale, SaleItem


class SaleCreateForm(forms.ModelForm):
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
        if not self.initial.get("date"):
            self.initial["date"] = timezone.now().date()


class SaleItemCreateForm(forms.ModelForm):
    class Meta:
        model = SaleItem
        fields = ("product", "quantity_kg", "sale_price_per_kg")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.order_by("name")
