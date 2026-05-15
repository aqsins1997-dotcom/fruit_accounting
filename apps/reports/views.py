from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Prefetch, Q, Sum
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET

from apps.core.models import Customer, Product, Store
from apps.credits.models import ClientDebtPayment, Credit, CreditPayment
from apps.credits.services import build_debtor_rows, summarize_debt_by_store
from apps.expenses.models import Expense, SalaryPayment, StoreExpense
from apps.expenses.services import build_employee_balance_report
from apps.sales.models import Sale
from .services import build_product_profitability_rows, summarize_product_profitability

from .forms import (
    MobileCreditPaymentForm,
    MobilePurchaseForm,
    MobilePurchaseItemForm,
    MobileSaleForm,
    MobileSaleItemForm,
)


def _product_profitability_filters(request):
    selected_store = None
    selected_product = None
    store_id = request.GET.get("store")
    product_id = request.GET.get("product")

    if store_id:
        selected_store = Store.objects.filter(pk=store_id).first()
    if product_id:
        selected_product = Product.objects.filter(pk=product_id).first()

    date_from_raw = request.GET.get("date_from", "").strip()
    date_to_raw = request.GET.get("date_to", "").strip()

    return {
        "selected_store": selected_store,
        "selected_product": selected_product,
        "date_from": parse_date(date_from_raw) if date_from_raw else None,
        "date_to": parse_date(date_to_raw) if date_to_raw else None,
        "date_from_raw": date_from_raw,
        "date_to_raw": date_to_raw,
    }


def _product_profitability_row_to_dict(row):
    return {
        "store_id": row["store_id"],
        "store": row["store_name"],
        "product_id": row["product_id"],
        "product": row["product_name"],
        "purchased": str(row["purchased_quantity"]),
        "sold": str(row["sold_quantity"]),
        "stock": str(row["stock_quantity"]),
        "average_purchase_price": str(row["average_purchase_price"]),
        "average_sale_price": str(row["average_sale_price"]),
        "revenue": str(row["revenue"]),
        "sold_cost": str(row["sold_cost"]),
        "profit": str(row["profit"]),
        "margin_per_unit": str(row["margin_per_unit"]),
    }


@login_required
def daily_store_report(request):
    report_date = request.GET.get("date")
    store_id = request.GET.get("store")

    today = timezone.now().date()

    if report_date:
        try:
            from datetime import datetime
            report_date_obj = datetime.strptime(report_date, "%Y-%m-%d").date()
        except ValueError:
            report_date_obj = today
    else:
        report_date_obj = today

    stores = Store.objects.all().order_by("name")
    selected_store = None

    if store_id:
        selected_store = Store.objects.filter(id=store_id).first()

    cash_sales_total = Decimal("0.00")
    credit_sales_total = Decimal("0.00")
    total_sales_amount = Decimal("0.00")
    total_cost_amount = Decimal("0.00")
    gross_profit_amount = Decimal("0.00")
    employee_expenses_total = Decimal("0.00")
    store_expenses_total = Decimal("0.00")
    salary_payments_total = Decimal("0.00")
    total_business_expenses = Decimal("0.00")
    total_expense_operations = 0
    net_profit_amount = Decimal("0.00")
    outstanding_advance_total = Decimal("0.00")
    current_credit_debt = Decimal("0.00")
    today_credit_clients = []
    stock_rows = []
    sale_rows = []
    expense_rows = []
    store_expense_rows = []
    salary_rows = []
    expense_by_category = []

    if selected_store:
        sales_qs = Sale.objects.filter(
            store=selected_store,
            date=report_date_obj,
            deleted_at__isnull=True,
        )

        total_sales_amount = sales_qs.aggregate(
            total=Sum("total_amount")
        )["total"] or Decimal("0.00")

        total_cost_amount = sales_qs.aggregate(
            total=Sum("total_cost")
        )["total"] or Decimal("0.00")

        cash_sales_total = sales_qs.filter(
            payment_type=Sale.PAYMENT_TYPE_CASH
        ).aggregate(
            total=Sum("total_amount")
        )["total"] or Decimal("0.00")

        credit_sales_total = sales_qs.filter(
            payment_type=Sale.PAYMENT_TYPE_CREDIT
        ).aggregate(
            total=Sum("total_amount")
        )["total"] or Decimal("0.00")

        today_credit_clients = Credit.objects.filter(
            store=selected_store,
            sale__date=report_date_obj,
            sale__deleted_at__isnull=True,
        ).select_related("customer", "sale").order_by("-id")

        current_credit_debt = Credit.objects.filter(
            store=selected_store,
            sale__deleted_at__isnull=True,
        ).exclude(
            status=Credit.STATUS_PAID
        ).aggregate(
            total=Sum("remaining_amount")
        )["total"] or Decimal("0.00")

        stock_rows = [
            row
            for row in build_product_profitability_rows(store=selected_store, group_by_store=True)
            if row["stock_quantity"] > Decimal("0.000")
        ]

        sale_rows = sales_qs.select_related("customer").order_by("-id")

        employee_expense_queryset = Expense.objects.filter(
            store=selected_store,
            date=report_date_obj,
        ).select_related("seller", "category", "advance").order_by("-id")

        store_expense_queryset = StoreExpense.objects.filter(
            store=selected_store,
            date=report_date_obj,
            deleted_at__isnull=True,
        ).select_related("category").order_by("-id")

        salary_queryset = SalaryPayment.objects.filter(
            store=selected_store,
            date=report_date_obj,
        ).select_related("seller").order_by("-id")

        employee_expenses_total = employee_expense_queryset.aggregate(
            total=Sum("amount")
        )["total"] or Decimal("0.00")

        store_expenses_total = store_expense_queryset.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        salary_payments_total = salary_queryset.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        total_business_expenses = employee_expenses_total + store_expenses_total + salary_payments_total
        total_expense_operations = (
            employee_expense_queryset.count()
            + store_expense_queryset.count()
            + salary_queryset.count()
        )

        expense_rows = employee_expense_queryset
        store_expense_rows = store_expense_queryset
        salary_rows = salary_queryset

        expense_category_map = {}
        for row in employee_expense_queryset.values("category__name").annotate(total=Sum("amount")).order_by("category__name"):
            expense_category_map[row["category__name"]] = row["total"] or Decimal("0.00")
        for row in store_expense_queryset.values("category__name").annotate(total=Sum("amount")).order_by("category__name"):
            expense_category_map[row["category__name"]] = expense_category_map.get(row["category__name"], Decimal("0.00")) + (row["total"] or Decimal("0.00"))
        if salary_payments_total:
            expense_category_map["Зарплата"] = expense_category_map.get("Зарплата", Decimal("0.00")) + salary_payments_total
        expense_by_category = [
            {"category__name": category_name, "total": total}
            for category_name, total in sorted(expense_category_map.items(), key=lambda item: item[0])
        ]

        _, advance_summary = build_employee_balance_report(store=selected_store, date_to=report_date_obj)
        outstanding_advance_total = advance_summary["remaining_amount"]

        gross_profit_amount = total_sales_amount - total_cost_amount
        net_profit_amount = gross_profit_amount - total_business_expenses

    context = {
        "stores": stores,
        "selected_store": selected_store,
        "report_date": report_date_obj,
        "cash_sales_total": cash_sales_total,
        "credit_sales_total": credit_sales_total,
        "total_sales_amount": total_sales_amount,
        "total_cost_amount": total_cost_amount,
        "gross_profit_amount": gross_profit_amount,
        "employee_expenses_total": employee_expenses_total,
        "store_expenses_total": store_expenses_total,
        "salary_payments_total": salary_payments_total,
        "total_business_expenses": total_business_expenses,
        "total_expense_operations": total_expense_operations,
        "net_profit_amount": net_profit_amount,
        "outstanding_advance_total": outstanding_advance_total,
        "current_credit_debt": current_credit_debt,
        "today_credit_clients": today_credit_clients,
        "stock_rows": stock_rows,
        "sale_rows": sale_rows,
        "expense_rows": expense_rows,
        "store_expense_rows": store_expense_rows,
        "salary_rows": salary_rows,
        "expense_by_category": expense_by_category,
    }

    return render(request, "reports/daily_store_report.html", context)


@login_required
def product_profitability_report(request):
    filters = _product_profitability_filters(request)
    rows = build_product_profitability_rows(
        store=filters["selected_store"],
        product=filters["selected_product"],
        date_from=filters["date_from"],
        date_to=filters["date_to"],
        group_by_store=True,
    )
    summary = summarize_product_profitability(rows)

    context = {
        "rows": rows,
        "summary": summary,
        "stores": Store.objects.order_by("name"),
        "products": Product.objects.order_by("name"),
        "selected_store": filters["selected_store"],
        "selected_product": filters["selected_product"],
        "date_from": filters["date_from_raw"],
        "date_to": filters["date_to_raw"],
    }
    return render(request, "reports/product_profitability.html", context)


@login_required
@require_GET
def product_profitability_data(request):
    filters = _product_profitability_filters(request)
    rows = build_product_profitability_rows(
        store=filters["selected_store"],
        product=filters["selected_product"],
        date_from=filters["date_from"],
        date_to=filters["date_to"],
        group_by_store=True,
    )
    summary = summarize_product_profitability(rows)

    return JsonResponse(
        {
            "results": [_product_profitability_row_to_dict(row) for row in rows],
            "summary": _product_profitability_row_to_dict(
                {
                    "store_id": None,
                    "store_name": "Итого",
                    "product_id": None,
                    "product_name": "Итого",
                    "purchased_quantity": summary["purchased_quantity"],
                    "sold_quantity": summary["sold_quantity"],
                    "stock_quantity": summary["stock_quantity"],
                    "average_purchase_price": summary["average_purchase_price"],
                    "average_sale_price": summary["average_sale_price"],
                    "revenue": summary["revenue"],
                    "sold_cost": summary["sold_cost"],
                    "profit": summary["profit"],
                    "margin_per_unit": summary["margin_per_unit"],
                }
            ),
        }
    )


@login_required
def debtors_report(request):
    store_id = request.GET.get("store")
    search = request.GET.get("q", "").strip()

    selected_store = None
    if store_id:
        selected_store = Store.objects.filter(pk=store_id).first()

    debtor_rows = build_debtor_rows(store=selected_store, search=search)
    total_debt = sum((row["total_debt"] for row in debtor_rows), Decimal("0.00"))
    debt_by_store = summarize_debt_by_store(debtor_rows)

    stores = Store.objects.all().order_by("name")

    context = {
        "stores": stores,
        "selected_store": selected_store,
        "debtor_rows": debtor_rows,
        "total_debt": total_debt,
        "debt_by_store": debt_by_store,
        "search": search,
    }

    return render(request, "reports/debtors.html", context)


@login_required
def debtors_print_report(request):
    store_id = request.GET.get("store")
    search = request.GET.get("q", "").strip()

    selected_store = None
    if store_id:
        selected_store = Store.objects.filter(pk=store_id).first()

    debtor_rows = build_debtor_rows(store=selected_store, search=search)
    total_debt = sum((row["total_debt"] for row in debtor_rows), Decimal("0.00"))

    context = {
        "debtor_rows": debtor_rows,
        "selected_store": selected_store,
        "search": search,
        "total_debt": total_debt,
    }

    return render(request, "reports/debtors_print.html", context)


@login_required
def debtor_detail(request, customer_id):
    customer = get_object_or_404(Customer, pk=customer_id)

    credits = (
        Credit.objects.filter(customer_id=customer_id)
        .filter(sale__deleted_at__isnull=True)
        .select_related("customer", "store", "sale")
        .prefetch_related(
            Prefetch(
                "payments",
                queryset=CreditPayment.objects.select_related("credit").order_by("-date", "-id"),
            )
        )
        .order_by("-created_at", "-id")
    )

    payments = (
        ClientDebtPayment.objects.filter(client_id=customer_id)
        .select_related("store", "client", "employee")
        .order_by("-paid_at", "-id")
    )
    legacy_payments = CreditPayment.objects.filter(
        credit__customer_id=customer_id,
        credit__sale__deleted_at__isnull=True,
        client_debt_payment__isnull=True,
    )

    total_taken = credits.aggregate(
        total=Coalesce(Sum("original_amount"), Decimal("0.00"))
    )["total"]
    total_paid = payments.aggregate(
        total=Coalesce(Sum("amount"), Decimal("0.00"))
    )["total"]
    total_paid += legacy_payments.aggregate(
        total=Coalesce(Sum("amount"), Decimal("0.00"))
    )["total"]
    total_remaining = credits.aggregate(
        total=Coalesce(Sum("remaining_amount"), Decimal("0.00"))
    )["total"]

    current_debts = [credit for credit in credits if credit.remaining_amount > 0]
    stores_summary = (
        credits.values("store_id", "store__name")
        .annotate(
            total_taken=Coalesce(Sum("original_amount"), Decimal("0.00")),
            total_remaining=Coalesce(Sum("remaining_amount"), Decimal("0.00")),
        )
        .order_by("store__name")
    )

    context = {
        "customer": customer,
        "credits": credits,
        "payments": payments,
        "stores_summary": stores_summary,
        "current_debts": current_debts,
        "total_taken": total_taken,
        "total_paid": total_paid,
        "total_remaining": total_remaining,
    }
    return render(request, "reports/debtor_detail.html", context)


@login_required
def debtor_detail_print(request, customer_id):
    customer = get_object_or_404(Customer, pk=customer_id)

    credits = (
        Credit.objects.filter(customer_id=customer_id)
        .filter(sale__deleted_at__isnull=True)
        .select_related("customer", "store", "sale")
        .order_by("-created_at", "-id")
    )
    payments = (
        ClientDebtPayment.objects.filter(client_id=customer_id)
        .select_related("store", "client", "employee")
        .order_by("-paid_at", "-id")
    )
    legacy_payments = (
        CreditPayment.objects.filter(
            credit__customer_id=customer_id,
            credit__sale__deleted_at__isnull=True,
            client_debt_payment__isnull=True,
        )
        .select_related("credit", "credit__store")
        .order_by("-date", "-id")
    )

    total_taken = credits.aggregate(
        total=Coalesce(Sum("original_amount"), Decimal("0.00"))
    )["total"]
    total_paid = payments.aggregate(
        total=Coalesce(Sum("amount"), Decimal("0.00"))
    )["total"]
    total_paid += legacy_payments.aggregate(
        total=Coalesce(Sum("amount"), Decimal("0.00"))
    )["total"]
    total_remaining = credits.aggregate(
        total=Coalesce(Sum("remaining_amount"), Decimal("0.00"))
    )["total"]

    context = {
        "customer": customer,
        "credits": credits,
        "payments": payments,
        "total_taken": total_taken,
        "total_paid": total_paid,
        "total_remaining": total_remaining,
    }
    return render(request, "reports/debtor_detail_print.html", context)


@login_required
def mobile_home(request):
    return render(request, "reports/mobile_home.html")


@login_required
def mobile_purchase_add(request):
    if request.method == "POST":
        purchase_form = MobilePurchaseForm(request.POST)
        item_form = MobilePurchaseItemForm(request.POST)

        if purchase_form.is_valid() and item_form.is_valid():
            with transaction.atomic():
                purchase = purchase_form.save()
                item = item_form.save(commit=False)
                item.purchase = purchase
                item.save()

            messages.success(request, "Закупка успешно добавлена.")
            return redirect("reports:mobile_purchase_add")
    else:
        purchase_form = MobilePurchaseForm(initial={"date": timezone.now().date()})
        item_form = MobilePurchaseItemForm()

    context = {
        "purchase_form": purchase_form,
        "item_form": item_form,
    }
    return render(request, "reports/mobile_purchase_form.html", context)


@login_required
def mobile_debtors(request):
    store_id = request.GET.get("store")
    search = request.GET.get("q", "").strip()

    credits = (
        Credit.objects.filter(remaining_amount__gt=0, sale__deleted_at__isnull=True)
        .select_related("customer", "store", "sale")
        .order_by("store__name", "-remaining_amount", "-sale__date", "-id")
    )

    selected_store = None
    if store_id:
        selected_store = Store.objects.filter(pk=store_id).first()
        if selected_store:
            credits = credits.filter(store_id=selected_store.id)

    if search:
        credits = credits.filter(
            Q(customer__name__icontains=search) |
            Q(customer__phone__icontains=search)
        )

    context = {
        "credits": credits,
        "stores": Store.objects.order_by("name"),
        "selected_store": selected_store,
        "search": search,
    }
    return render(request, "reports/mobile_debtors.html", context)


@login_required
def mobile_debtor_detail(request, credit_id):
    credit = get_object_or_404(
        Credit.objects.select_related("customer", "store", "sale").prefetch_related(
            Prefetch("payments", queryset=CreditPayment.objects.order_by("-date", "-id"))
        ).filter(sale__deleted_at__isnull=True),
        pk=credit_id,
    )

    context = {
        "credit": credit,
        "payments": credit.payments.all(),
    }
    return render(request, "reports/mobile_debtor_detail.html", context)


@login_required
def mobile_credit_payment_add(request, credit_id):
    credit = get_object_or_404(
        Credit.objects.select_related("customer", "store", "sale").filter(
            sale__deleted_at__isnull=True
        ),
        pk=credit_id,
    )

    if request.method == "POST":
        form = MobileCreditPaymentForm(request.POST)
        form.instance.credit = credit
        if form.is_valid():
            payment = ClientDebtPayment(
                store=credit.store,
                client=credit.customer,
                amount=form.cleaned_data["amount"],
                payment_method=ClientDebtPayment.PAYMENT_METHOD_CASH,
                paid_at=form.cleaned_data["date"],
                comment=form.cleaned_data["comment"],
                employee=request.user,
            )
            payment.save()
            messages.success(request, "Оплата успешно внесена.")
            return redirect("reports:mobile_debtor_detail", credit_id=credit.id)
    else:
        form = MobileCreditPaymentForm(initial={"date": timezone.now().date()})

    context = {
        "credit": credit,
        "form": form,
    }
    return render(request, "reports/mobile_credit_payment_form.html", context)


@login_required
def mobile_sale_add(request):
    if request.method == "POST":
        sale_form = MobileSaleForm(request.POST)
        item_form = MobileSaleItemForm(request.POST)

        if sale_form.is_valid() and item_form.is_valid():
            with transaction.atomic():
                sale = sale_form.save()
                item = item_form.save(commit=False)
                item.sale = sale
                item.save()

            messages.success(request, "Продажа успешно добавлена.")
            return redirect("reports:mobile_sale_add")
    else:
        sale_form = MobileSaleForm(initial={"date": timezone.now().date()})
        item_form = MobileSaleItemForm()

    context = {
        "sale_form": sale_form,
        "item_form": item_form,
    }
    return render(request, "reports/mobile_sale_form.html", context)
