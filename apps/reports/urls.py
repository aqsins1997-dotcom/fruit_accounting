from django.urls import path

from .views import (
    daily_store_report,
    debtor_detail,
    debtor_detail_print,
    debtors_print_report,
    debtors_report,
    mobile_credit_payment_add,
    mobile_debtor_detail,
    mobile_debtors,
    mobile_home,
    mobile_purchase_add,
    mobile_sale_add,
)

app_name = "reports"

urlpatterns = [
    path("mobile/", mobile_home, name="mobile_home"),
    path("mobile/purchases/add/", mobile_purchase_add, name="mobile_purchase_add"),
    path("mobile/debtors/", mobile_debtors, name="mobile_debtors"),
    path("mobile/debtors/<int:credit_id>/", mobile_debtor_detail, name="mobile_debtor_detail"),
    path("mobile/debtors/<int:credit_id>/pay/", mobile_credit_payment_add, name="mobile_credit_payment_add"),
    path("mobile/sales/add/", mobile_sale_add, name="mobile_sale_add"),
    path("daily-store/", daily_store_report, name="daily_store_report"),
    path("debtors/", debtors_report, name="debtors_report"),
    path("debtors/print/", debtors_print_report, name="debtors_print_report"),
    path("debtors/<int:customer_id>/", debtor_detail, name="debtor_detail"),
    path("debtors/<int:customer_id>/print/", debtor_detail_print, name="debtor_detail_print"),
]
