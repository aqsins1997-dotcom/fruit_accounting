from django import forms
from django.utils import timezone

from apps.core.models import Seller, Store

from .models import EmployeeAdvance, Expense, ExpenseCategory, SalaryPayment, StoreExpense


class ExpenseCategoryForm(forms.ModelForm):
    class Meta:
        model = ExpenseCategory
        fields = ("name", "is_active")


class EmployeeAdvanceForm(forms.ModelForm):
    class Meta:
        model = EmployeeAdvance
        fields = ("store", "seller", "date", "amount", "comment")
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["store"].queryset = Store.objects.order_by("name")

        sellers = Seller.objects.select_related("store").filter(is_active=True).order_by("name")
        store_id = self.data.get("store") or self.initial.get("store") or getattr(self.instance, "store_id", None)
        if store_id:
            sellers = sellers.filter(store_id=store_id)
        self.fields["seller"].queryset = sellers

        if not self.initial.get("date"):
            self.initial["date"] = timezone.now().date()


class ExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = ("store", "seller", "category", "date", "amount", "comment", "advance")
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["store"].queryset = Store.objects.order_by("name")
        self.fields["category"].queryset = ExpenseCategory.objects.filter(is_active=True).order_by("name")

        sellers = Seller.objects.select_related("store").filter(is_active=True).order_by("name")
        advances = EmployeeAdvance.objects.select_related("store", "seller").order_by("-date", "-id")

        store_id = self.data.get("store") or self.initial.get("store") or getattr(self.instance, "store_id", None)
        seller_id = self.data.get("seller") or self.initial.get("seller") or getattr(self.instance, "seller_id", None)

        if store_id:
            sellers = sellers.filter(store_id=store_id)
            advances = advances.filter(store_id=store_id)
        if seller_id:
            advances = advances.filter(seller_id=seller_id)

        self.fields["seller"].queryset = sellers
        self.fields["advance"].queryset = advances
        self.fields["advance"].required = False
        self.fields["advance"].help_text = "Необязательно. Можно привязать расход к конкретному подотчетному авансу."

        if not self.initial.get("date"):
            self.initial["date"] = timezone.now().date()


class StoreExpenseForm(forms.ModelForm):
    class Meta:
        model = StoreExpense
        fields = ("store", "category", "date", "amount", "comment")
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["store"].queryset = Store.objects.order_by("name")
        self.fields["category"].queryset = ExpenseCategory.objects.filter(is_active=True).order_by("name")
        if not self.initial.get("date"):
            self.initial["date"] = timezone.now().date()


class SalaryPaymentForm(forms.ModelForm):
    class Meta:
        model = SalaryPayment
        fields = ("store", "seller", "date", "amount", "comment")
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["store"].queryset = Store.objects.order_by("name")

        sellers = Seller.objects.select_related("store").filter(is_active=True).order_by("name")
        store_id = self.data.get("store") or self.initial.get("store") or getattr(self.instance, "store_id", None)
        if store_id:
            sellers = sellers.filter(store_id=store_id)
        self.fields["seller"].queryset = sellers

        if not self.initial.get("date"):
            self.initial["date"] = timezone.now().date()


class EmployeeBalanceFilterForm(forms.Form):
    store = forms.ModelChoiceField(
        queryset=Store.objects.order_by("name"),
        required=False,
        empty_label="Все магазины",
    )
    seller = forms.ModelChoiceField(
        queryset=Seller.objects.select_related("store").filter(is_active=True).order_by("name"),
        required=False,
        empty_label="Все сотрудники",
    )


class ExpenseReportFilterForm(forms.Form):
    store = forms.ModelChoiceField(
        queryset=Store.objects.order_by("name"),
        required=False,
        empty_label="Все магазины",
    )
    seller = forms.ModelChoiceField(
        queryset=Seller.objects.select_related("store").filter(is_active=True).order_by("name"),
        required=False,
        empty_label="Все сотрудники",
    )
    category = forms.ModelChoiceField(
        queryset=ExpenseCategory.objects.filter(is_active=True).order_by("name"),
        required=False,
        empty_label="Все категории",
    )
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    def clean(self):
        cleaned_data = super().clean()
        date_from = cleaned_data.get("date_from")
        date_to = cleaned_data.get("date_to")
        if date_from and date_to and date_from > date_to:
            raise forms.ValidationError("Дата начала не может быть позже даты окончания.")
        return cleaned_data
