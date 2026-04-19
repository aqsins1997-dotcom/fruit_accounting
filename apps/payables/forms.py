from django import forms
from django.utils import timezone

from apps.core.models import Store, Supplier
from apps.inventory.models import Purchase

from .models import SupplierPayment


class SupplierPaymentCreateForm(forms.ModelForm):
    class Meta:
        model = SupplierPayment
        fields = ("supplier", "store", "purchase", "date", "amount", "comment")
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.order_by("name")
        self.fields["store"].queryset = Store.objects.order_by("name")
        self.fields["purchase"].queryset = Purchase.objects.select_related("supplier").order_by("-date", "-id")
        self.fields["purchase"].required = False
        self.fields["purchase"].help_text = "Необязательно. Если указать закупку, оплата сначала закроет именно ее."

        if not self.initial.get("date"):
            self.initial["date"] = timezone.now().date()
