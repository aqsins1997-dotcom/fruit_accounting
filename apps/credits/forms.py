from django import forms

from apps.core.models import Customer, Store

from .models import ClientDebtPayment, CreditPayment


class CreditPaymentCreateForm(forms.ModelForm):
    class Meta:
        model = CreditPayment
        fields = ("date", "amount", "comment")
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }


class ClientDebtPaymentCreateForm(forms.ModelForm):
    class Meta:
        model = ClientDebtPayment
        fields = ("store", "client", "amount", "payment_method", "comment", "paid_at")
        widgets = {
            "paid_at": forms.DateInput(attrs={"type": "date"}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["store"].queryset = Store.objects.order_by("name")
        self.fields["client"].queryset = Customer.objects.order_by("name")
        self.fields["amount"].label = "Сумма оплаты"
        self.fields["paid_at"].label = "Дата оплаты"
