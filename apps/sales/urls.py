from django.urls import path

from .views import cash_registers, sale_cash_to_credit, sale_create, sale_list

app_name = "sales"

urlpatterns = [
    path("sales/", sale_list, name="sale_list"),
    path("sales/add/", sale_create, name="sale_create"),
    path("sales/<int:pk>/cash-to-credit/", sale_cash_to_credit, name="sale_cash_to_credit"),
    path("sales/cash/", cash_registers, name="cash_registers"),
]
