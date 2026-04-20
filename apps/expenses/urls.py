from django.urls import path

from .views import (
    advance_create,
    category_list,
    employee_report,
    expense_create,
    expense_report,
    salary_payment_create,
    store_expense_create,
)

app_name = "expenses"

urlpatterns = [
    path("expenses/categories/", category_list, name="category_list"),
    path("expenses/advances/add/", advance_create, name="advance_create"),
    path("expenses/records/add/", expense_create, name="expense_create"),
    path("expenses/store/add/", store_expense_create, name="store_expense_create"),
    path("expenses/salary/add/", salary_payment_create, name="salary_payment_create"),
    path("reports/expenses/employees/", employee_report, name="employee_report"),
    path("reports/expenses/summary/", expense_report, name="expense_report"),
]
