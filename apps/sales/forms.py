from decimal import Decimal, ROUND_HALF_UP

from django import forms
from django.db.models import Sum
from django.utils import timezone

from apps.core.models import Customer, Product, Store
from apps.inventory.models import PurchaseItem

from .models import Sale, SaleItem, SaleItemBatch, purchase_item_available_quantity


class PurchaseItemChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        available_quantity = getattr(
            obj,
            "_available_quantity",
            getattr(self, "available_quantities", {}).get(obj.id),
        )
        if available_quantity is None:
            available_quantity = purchase_item_available_quantity(obj)
        purchase_date = obj.purchase.date.strftime("%d.%m.%Y") if obj.purchase.date else "-"
        return (
            f"Закупка №{obj.purchase_id} от {purchase_date} | "
            f"{obj.purchase.supplier.name} | закуп {obj.purchase_price_per_kg} | "
            f"остаток {available_quantity} кг"
        )


def available_purchase_item_queryset(*, store_id=None, product_id=None):
    queryset = (
        PurchaseItem.objects.select_related("purchase", "purchase__supplier", "store", "product")
        .filter(purchase__deleted_at__isnull=True)
        .order_by("purchase__date", "id")
    )
    try:
        store_id = int(store_id) if store_id else None
    except (TypeError, ValueError):
        store_id = None
    try:
        product_id = int(product_id) if product_id else None
    except (TypeError, ValueError):
        product_id = None

    if store_id:
        queryset = queryset.filter(store_id=store_id)
    if product_id:
        queryset = queryset.filter(product_id=product_id)

    purchase_items = list(queryset)
    purchase_item_ids = [purchase_item.id for purchase_item in purchase_items]
    allocated_quantities = {
        row["purchase_item_id"]: row["total"] or Decimal("0.000")
        for row in SaleItemBatch.objects.filter(
            purchase_item_id__in=purchase_item_ids,
            purchase_item__purchase__deleted_at__isnull=True,
            sale_item__sale__deleted_at__isnull=True,
        )
        .values("purchase_item_id")
        .annotate(total=Sum("quantity"))
    }

    available_purchase_items = []
    for purchase_item in purchase_items:
        available_quantity = purchase_item.quantity_kg - allocated_quantities.get(
            purchase_item.id,
            Decimal("0.000"),
        )
        if available_quantity < Decimal("0.000"):
            available_quantity = Decimal("0.000")
        purchase_item._available_quantity = available_quantity.quantize(Decimal("0.001"))
        if purchase_item._available_quantity > Decimal("0.000"):
            available_purchase_items.append(purchase_item)

    return available_purchase_items


def purchase_item_options_data():
    label_field = PurchaseItemChoiceField(queryset=PurchaseItem.objects.none())
    return [
        {
            "id": purchase_item.id,
            "store_id": purchase_item.store_id,
            "product_id": purchase_item.product_id,
            "label": label_field.label_from_instance(purchase_item),
            "available": str(purchase_item._available_quantity),
        }
        for purchase_item in available_purchase_item_queryset()
    ]


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
    purchase_item = PurchaseItemChoiceField(
        label="Закупка / партия",
        queryset=PurchaseItem.objects.none(),
        empty_label="Выберите закупку/партию",
        required=True,
    )
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
        fields = ("product", "purchase_item", "quantity_kg", "sale_price_per_kg", "sale_total")

    def __init__(self, *args, store_id=None, product_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.store_id = store_id
        self.fields["product"].queryset = Product.objects.order_by("name")
        self.fields["sale_price_per_kg"].required = False
        purchase_items = []
        if store_id and product_id:
            purchase_items = available_purchase_item_queryset(store_id=store_id, product_id=product_id)
            self.fields["purchase_item"].available_quantities = {
                item.id: item._available_quantity for item in purchase_items
            }
        purchase_item_ids = [item.id for item in purchase_items]
        self.fields["purchase_item"].queryset = (
            PurchaseItem.objects.select_related("purchase", "purchase__supplier", "store", "product")
            .filter(id__in=purchase_item_ids)
            .order_by("purchase__date", "id")
        )

    def clean(self):
        cleaned_data = super().clean()
        product = cleaned_data.get("product")
        purchase_item = cleaned_data.get("purchase_item")
        quantity = cleaned_data.get("quantity_kg")
        sale_total = cleaned_data.get("sale_total")
        sale_price = cleaned_data.get("sale_price_per_kg")

        if purchase_item and product and purchase_item.product_id != product.id:
            self.add_error("purchase_item", "Выбранная закупка относится к другому товару.")

        if purchase_item and self.store_id:
            try:
                store_id = int(self.store_id)
            except (TypeError, ValueError):
                store_id = None
            if store_id and purchase_item.store_id != store_id:
                self.add_error("purchase_item", "Выбранная закупка относится к другому магазину.")

        if purchase_item and quantity:
            available_quantity = purchase_item_available_quantity(purchase_item)
            if quantity > available_quantity:
                self.add_error(
                    "quantity_kg",
                    f"Недостаточно остатка в выбранной закупке. Доступно: {available_quantity} кг.",
                )

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
        instance._selected_purchase_item = self.cleaned_data.get("purchase_item")
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
