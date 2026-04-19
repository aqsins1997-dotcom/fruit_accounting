from django.urls import path

from .views import supplier_balances, supplier_payment_create, supplier_payment_list

app_name = "payables"

urlpatterns = [
    path("suppliers/", supplier_balances, name="supplier_balances"),
    path("suppliers/payments/", supplier_payment_list, name="supplier_payment_list"),
    path("suppliers/payments/add/", supplier_payment_create, name="supplier_payment_create"),
]
