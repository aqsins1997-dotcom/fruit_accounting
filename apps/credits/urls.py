from django.urls import path

from .views import credit_payment_create

app_name = "credits"

urlpatterns = [
    path("pay/<int:credit_id>/", credit_payment_create, name="credit_payment_create"),
]
