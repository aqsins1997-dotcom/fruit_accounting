from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include
from apps.reports.views import (
    mobile_credit_payment_add,
    mobile_debtor_detail,
    mobile_debtors,
    mobile_home,
    mobile_purchase_add,
    mobile_sale_add,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("apps.dashboard.urls")),
    path("mobile/", mobile_home, name="mobile_home_root"),
    path("mobile/purchases/add/", mobile_purchase_add, name="mobile_purchase_add_root"),
    path("mobile/debtors/", mobile_debtors, name="mobile_debtors_root"),
    path("mobile/debtors/<int:credit_id>/", mobile_debtor_detail, name="mobile_debtor_detail_root"),
    path("mobile/debtors/<int:credit_id>/pay/", mobile_credit_payment_add, name="mobile_credit_payment_add_root"),
    path("mobile/sales/add/", mobile_sale_add, name="mobile_sale_add_root"),
    path("credits/", include("apps.credits.urls")),
    path("", include("apps.core.urls")),
    path("", include("apps.inventory.urls")),
    path("", include("apps.sales.urls")),
    path("", include("apps.expenses.urls")),
    path("reports/", include("apps.reports.urls")),
    path("reports/", include("apps.payables.urls")),

    path(
        "accounts/login/",
        auth_views.LoginView.as_view(template_name="accounts/login.html"),
        name="login",
    ),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
]
