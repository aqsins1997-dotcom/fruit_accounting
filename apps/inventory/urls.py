from django.urls import path

from .views import (
    purchase_create,
    purchase_item_price_update,
    purchase_item_quantity_update,
    purchase_list,
    stock_list,
)

app_name = "inventory"

urlpatterns = [
    path("purchases/", purchase_list, name="purchase_list"),
    path("purchases/add/", purchase_create, name="purchase_create"),
    path("purchases/items/<int:pk>/price/", purchase_item_price_update, name="purchase_item_price_update"),
    path("purchases/items/<int:pk>/quantity/", purchase_item_quantity_update, name="purchase_item_quantity_update"),
    path("stocks/", stock_list, name="stock_list"),
]
