from django.urls import path

from .views import (
    supplier_balances,
    supplier_payment_cancel,
    supplier_payment_create,
    supplier_payment_list,
    supplier_payment_update,
)

app_name = "payables"

urlpatterns = [
    path("suppliers/", supplier_balances, name="supplier_balances"),
    path("suppliers/payments/", supplier_payment_list, name="supplier_payment_list"),
    path("suppliers/payments/add/", supplier_payment_create, name="supplier_payment_create"),
    path("suppliers/payments/<int:pk>/edit/", supplier_payment_update, name="supplier_payment_update"),
    path("suppliers/payments/<int:pk>/cancel/", supplier_payment_cancel, name="supplier_payment_cancel"),
]
