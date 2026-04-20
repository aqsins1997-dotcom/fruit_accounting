from django.urls import path

from .views import cash_registers, sale_create, sale_list

app_name = "sales"

urlpatterns = [
    path("sales/", sale_list, name="sale_list"),
    path("sales/add/", sale_create, name="sale_create"),
    path("sales/cash/", cash_registers, name="cash_registers"),
]
