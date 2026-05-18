from django import forms
from django.utils import timezone

from apps.core.models import Store, Supplier

from .models import SupplierPayment


class SupplierPaymentCreateForm(forms.ModelForm):
    def __init__(self, *args, supplier_id=None, store_id=None, purchase_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.order_by("name")
        self.fields["store"].queryset = Store.objects.order_by("name")

        if supplier_id:
            self.initial.setdefault("supplier", supplier_id)
        if store_id:
            self.initial.setdefault("store", store_id)
        if not self.initial.get("date"):
            self.initial["date"] = timezone.now().date()

    class Meta:
        model = SupplierPayment
        fields = ("supplier", "store", "date", "payment_method", "amount", "comment")
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }


class SupplierPaymentUpdateForm(forms.ModelForm):
    class Meta:
        model = SupplierPayment
        fields = ("date", "payment_method", "amount", "comment")
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
