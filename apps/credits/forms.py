from django import forms

from .models import CreditPayment


class CreditPaymentCreateForm(forms.ModelForm):
    class Meta:
        model = CreditPayment
        fields = ("date", "amount", "comment")
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }

