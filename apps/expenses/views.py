from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.shortcuts import redirect, render
from django.utils import timezone

from .forms import (
    EmployeeAdvanceForm,
    EmployeeBalanceFilterForm,
    ExpenseCategoryForm,
    ExpenseForm,
    ExpenseReportFilterForm,
    SalaryPaymentForm,
    StoreExpenseForm,
)
from .models import EmployeeAdvance, Expense, ExpenseCategory, SalaryPayment, StoreExpense
from .services import (
    build_employee_balance_report,
    build_expense_report,
    save_employee_advance,
    save_expense,
    save_salary_payment,
    save_store_expense,
)


def _attach_validation_error(form, exc):
    if hasattr(exc, "message_dict"):
        for field_name, messages_list in exc.message_dict.items():
            target_field = field_name if field_name in form.fields else None
            for message in messages_list:
                form.add_error(target_field, message)
        return
    for message in exc.messages:
        form.add_error(None, message)


@login_required
def category_list(request):
    if request.method == "POST":
        form = ExpenseCategoryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Категория расхода добавлена.")
            return redirect("expenses:category_list")
    else:
        form = ExpenseCategoryForm()

    categories = ExpenseCategory.objects.order_by("name")
    return render(
        request,
        "expenses/category_list.html",
        {
            "form": form,
            "categories": categories,
        },
    )


@login_required
def advance_create(request):
    if request.method == "POST":
        form = EmployeeAdvanceForm(request.POST)
        if form.is_valid():
            advance = form.save(commit=False)
            if request.user.is_authenticated:
                advance.created_by = request.user
            try:
                save_employee_advance(advance)
            except ValidationError as exc:
                _attach_validation_error(form, exc)
            else:
                messages.success(request, "Подотчетные деньги сотруднику выданы.")
                return redirect("expenses:employee_report")
    else:
        form = EmployeeAdvanceForm()

    recent_advances = EmployeeAdvance.objects.select_related("store", "seller").order_by("-date", "-id")[:10]
    return render(
        request,
        "expenses/advance_form.html",
        {
            "form": form,
            "recent_advances": recent_advances,
        },
    )


@login_required
def expense_create(request):
    if request.method == "POST":
        form = ExpenseForm(request.POST)
        if form.is_valid():
            expense = form.save(commit=False)
            if request.user.is_authenticated:
                expense.created_by = request.user
            try:
                save_expense(expense)
            except ValidationError as exc:
                _attach_validation_error(form, exc)
            else:
                messages.success(request, "Расход сотрудника сохранен.")
                return redirect("expenses:expense_report")
    else:
        form = ExpenseForm()

    recent_expenses = Expense.objects.select_related("store", "seller", "category", "advance").order_by("-date", "-id")[:10]
    return render(
        request,
        "expenses/expense_form.html",
        {
            "form": form,
            "recent_expenses": recent_expenses,
        },
    )


@login_required
def store_expense_create(request):
    if request.method == "POST":
        form = StoreExpenseForm(request.POST)
        if form.is_valid():
            store_expense = form.save(commit=False)
            if request.user.is_authenticated:
                store_expense.created_by = request.user
            try:
                save_store_expense(store_expense)
            except ValidationError as exc:
                _attach_validation_error(form, exc)
            else:
                messages.success(request, "Расход магазина проведен и списан из кассы.")
                return redirect("expenses:expense_report")
    else:
        form = StoreExpenseForm()

    recent_store_expenses = StoreExpense.objects.select_related("store", "category").order_by("-date", "-id")[:10]
    return render(
        request,
        "expenses/store_expense_form.html",
        {
            "form": form,
            "recent_store_expenses": recent_store_expenses,
        },
    )


@login_required
def salary_payment_create(request):
    if request.method == "POST":
        form = SalaryPaymentForm(request.POST)
        if form.is_valid():
            salary_payment = form.save(commit=False)
            if request.user.is_authenticated:
                salary_payment.created_by = request.user
            try:
                save_salary_payment(salary_payment)
            except ValidationError as exc:
                _attach_validation_error(form, exc)
            else:
                messages.success(request, "Выплата зарплаты сохранена и списана из кассы.")
                return redirect("expenses:employee_report")
    else:
        form = SalaryPaymentForm()

    today = timezone.localdate()
    recent_salary_payments = SalaryPayment.objects.select_related("store", "seller").order_by("-date", "-id")[:10]
    return render(
        request,
        "expenses/salary_payment_form.html",
        {
            "form": form,
            "today": today,
            "recent_salary_payments": recent_salary_payments,
            "today_salary_total": SalaryPayment.objects.filter(date=today).aggregate(total=Sum("amount"))["total"] or 0,
        },
    )


@login_required
def employee_report(request):
    filter_form = EmployeeBalanceFilterForm(request.GET if request.GET is not None else {})
    filter_form.is_valid()

    store = filter_form.cleaned_data.get("store")
    seller = filter_form.cleaned_data.get("seller")
    today = timezone.localdate()

    rows, summary = build_employee_balance_report(store=store, seller=seller, today=today)
    return render(
        request,
        "expenses/employee_report.html",
        {
            "filter_form": filter_form,
            "rows": rows,
            "summary": summary,
            "today": today,
        },
    )


@login_required
def expense_report(request):
    filter_form = ExpenseReportFilterForm(request.GET if request.GET is not None else {})
    filter_form.is_valid()

    store = filter_form.cleaned_data.get("store")
    seller = filter_form.cleaned_data.get("seller")
    category = filter_form.cleaned_data.get("category")
    date_from = filter_form.cleaned_data.get("date_from")
    date_to = filter_form.cleaned_data.get("date_to")

    report = build_expense_report(
        store=store,
        seller=seller,
        category=category,
        date_from=date_from,
        date_to=date_to,
    )

    return render(
        request,
        "expenses/expense_report.html",
        {
            "filter_form": filter_form,
            **report,
        },
    )
