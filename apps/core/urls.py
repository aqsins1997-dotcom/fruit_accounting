from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("stores/", views.stores_page, name="stores_page"),
    path("suppliers/", views.suppliers_page, name="suppliers_page"),
    path("products/", views.products_page, name="products_page"),
    path("customers/", views.customers_page, name="customers_page"),
    path("sellers/", views.sellers_page, name="sellers_page"),
]
