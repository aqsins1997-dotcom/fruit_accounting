from decimal import Decimal, ROUND_HALF_UP

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
    sale_total = forms.DecimalField(
        label="Сумма продажи",
        max_digits=12,
        decimal_places=2,
        required=False,
        min_value=0,
        widget=forms.NumberInput(
            attrs={
                "step": "0.01",
                "inputmode": "decimal",
            }
        ),
    )

    class Meta:
        model = SaleItem
        fields = ("product", "quantity_kg", "sale_price_per_kg", "sale_total")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.order_by("name")
        self.fields["sale_price_per_kg"].required = False

    def clean(self):
        cleaned_data = super().clean()
        quantity = cleaned_data.get("quantity_kg")
        sale_total = cleaned_data.get("sale_total")
        sale_price = cleaned_data.get("sale_price_per_kg")

        if quantity and quantity > 0 and sale_total is not None:
            cleaned_data["sale_price_per_kg"] = (sale_total / quantity).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
        elif sale_price is None:
            self.add_error("sale_price_per_kg", "Укажите цену за кг или сумму продажи.")

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        sale_total = self.cleaned_data.get("sale_total")
        if sale_total is not None:
            instance._sale_total_override = sale_total.quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )

        if commit:
            instance.save()
            self.save_m2m()

        return instance
