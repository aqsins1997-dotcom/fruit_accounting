from django.urls import path

from .views import supplier_balances

app_name = "payables"

urlpatterns = [
    path("suppliers/", supplier_balances, name="supplier_balances"),
]