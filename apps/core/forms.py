from django import forms

from .models import Customer, Product, Seller, Store, Supplier


class StoreForm(forms.ModelForm):
    class Meta:
        model = Store
        fields = ("name", "is_active")


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ("name", "phone", "comment")
        widgets = {
            "comment": forms.Textarea(attrs={"rows": 3}),
        }


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ("name", "is_active")


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ("name", "phone")


class SellerForm(forms.ModelForm):
    class Meta:
        model = Seller
        fields = ("name", "store", "is_active")
