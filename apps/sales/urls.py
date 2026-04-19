from django.urls import path

from .views import sale_create, sale_list

app_name = "sales"

urlpatterns = [
    path("sales/", sale_list, name="sale_list"),
    path("sales/add/", sale_create, name="sale_create"),
]
