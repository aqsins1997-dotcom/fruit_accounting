from django.urls import path

from .views import purchase_create, purchase_list, stock_list

app_name = "inventory"

urlpatterns = [
    path("purchases/", purchase_list, name="purchase_list"),
    path("purchases/add/", purchase_create, name="purchase_create"),
    path("stocks/", stock_list, name="stock_list"),
]
