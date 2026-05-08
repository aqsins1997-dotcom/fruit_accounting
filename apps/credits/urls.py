from django.urls import path

from .views import (
    api_client_debt,
    api_client_payment_create,
    api_client_payment_history,
    api_debtors,
    client_debt_payment_create,
    credit_payment_create,
    credit_payment_list,
)

app_name = "credits"

urlpatterns = [
    path("payments/add/", client_debt_payment_create, name="client_debt_payment_create"),
    path("payments/", credit_payment_list, name="credit_payment_list"),
    path("pay/<int:credit_id>/", credit_payment_create, name="credit_payment_create"),
    path("api/debtors/", api_debtors, name="api_debtors"),
    path("api/debt/current/", api_client_debt, name="api_client_debt"),
    path("api/payments/", api_client_payment_create, name="api_client_payment_create"),
    path("api/payments/history/", api_client_payment_history, name="api_client_payment_history"),
]
