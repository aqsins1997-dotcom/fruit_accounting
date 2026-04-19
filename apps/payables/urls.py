from django.urls import path

from .views import supplier_balances, supplier_payment_create

app_name = "payables"

urlpatterns = [
    path("suppliers/", supplier_balances, name="supplier_balances"),
    path("suppliers/payments/add/", supplier_payment_create, name="supplier_payment_create"),
]
