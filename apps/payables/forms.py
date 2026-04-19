from django import forms
from django.utils import timezone

from apps.core.models import Store, Supplier
from apps.inventory.models import Purchase

from .models import SupplierPayment


class SupplierPaymentCreateForm(forms.ModelForm):
    def __init__(self, *args, supplier_id=None, store_id=None, purchase_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.order_by("name")
        self.fields["store"].queryset = Store.objects.order_by("name")

        purchases = Purchase.objects.select_related("supplier").order_by("-date", "-id")
        if supplier_id:
            purchases = purchases.filter(supplier_id=supplier_id)
            self.initial.setdefault("supplier", supplier_id)
        if store_id:
            purchases = purchases.filter(items__store_id=store_id).distinct()
            self.initial.setdefault("store", store_id)
        if purchase_id:
            self.initial.setdefault("purchase", purchase_id)

        self.fields["purchase"].queryset = purchases
        self.fields["purchase"].required = False
        self.fields["purchase"].help_text = "Необязательно. Если указать закупку, оплата сначала закроет именно ее."

        if not self.initial.get("date"):
            self.initial["date"] = timezone.now().date()

    class Meta:
        model = SupplierPayment
        fields = ("supplier", "store", "purchase", "date", "amount", "comment")
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }


class SupplierBalanceFilterForm(forms.Form):
    STATUS_ALL = "all"
    STATUS_UNPAID = "unpaid"
    STATUS_PARTIAL = "partial"
    STATUS_PAID = "paid"

    STATUS_CHOICES = (
        (STATUS_ALL, "Все"),
        (STATUS_UNPAID, "Не оплачено"),
        (STATUS_PARTIAL, "Частично оплачено"),
        (STATUS_PAID, "Оплачено"),
    )

    supplier = forms.ModelChoiceField(
        queryset=Supplier.objects.order_by("name"),
        required=False,
        empty_label="Все поставщики",
    )
    store = forms.ModelChoiceField(
        queryset=Store.objects.order_by("name"),
        required=False,
        empty_label="Все магазины",
    )
    status = forms.ChoiceField(
        choices=STATUS_CHOICES,
        required=False,
        initial=STATUS_ALL,
    )
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
