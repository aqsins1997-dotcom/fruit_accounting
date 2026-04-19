from django.urls import path

from .views import credit_payment_create, credit_payment_list

app_name = "credits"

urlpatterns = [
    path("payments/", credit_payment_list, name="credit_payment_list"),
    path("pay/<int:credit_id>/", credit_payment_create, name="credit_payment_create"),
]
