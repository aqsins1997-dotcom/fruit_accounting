from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import render
from django.utils import timezone

from apps.core.models import Product, Store
from apps.expenses.models import EmployeeAdvance, Expense, SalaryPayment, StoreExpense
from apps.inventory.models import StoreStock
from apps.sales.models import Sale


@login_required
def index(request):
    today = timezone.localdate()

    stores_count = Store.objects.count()
    products_count = Product.objects.count()

    total_stock = StoreStock.objects.aggregate(total=Sum("quantity_kg"))["total"] or 0
    today_sales = Sale.objects.filter(date=today).aggregate(total=Sum("total_amount"))["total"] or 0

    today_employee_expenses = Expense.objects.filter(date=today).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    today_store_expenses = StoreExpense.objects.filter(date=today).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    today_salary_payments = SalaryPayment.objects.filter(date=today).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    today_advances = EmployeeAdvance.objects.filter(date=today).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    today_total_expenses = today_employee_expenses + today_store_expenses + today_salary_payments
    today_expense_count = (
        Expense.objects.filter(date=today).count()
        + StoreExpense.objects.filter(date=today).count()
        + SalaryPayment.objects.filter(date=today).count()
    )

    context = {
        "stats": {
            "stores_count": stores_count,
            "products_count": products_count,
            "total_stock": total_stock,
            "today_sales": today_sales,
            "today_total_expenses": today_total_expenses,
            "today_expense_count": today_expense_count,
            "today_salary_payments": today_salary_payments,
            "today_advances": today_advances,
            "active_credits": 0,
        },
        "today": today,
    }

    return render(request, "dashboard/index.html", context)
