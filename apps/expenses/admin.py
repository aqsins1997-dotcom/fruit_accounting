from django.contrib import admin

from .models import EmployeeAdvance, Expense, ExpenseCategory, SalaryPayment, StoreExpense
from .services import save_employee_advance, save_expense, save_salary_payment, save_store_expense


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "created_at")
    list_filter = ("is_active", "created_at")
    search_fields = ("name",)


@admin.register(EmployeeAdvance)
class EmployeeAdvanceAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "date",
        "store",
        "seller",
        "amount",
        "created_by",
        "created_at",
    )
    list_filter = ("store", "seller", "date", "created_at")
    search_fields = ("store__name", "seller__name", "comment")
    date_hierarchy = "date"

    def save_model(self, request, obj, form, change):
        if request.user.is_authenticated and not obj.created_by_id:
            obj.created_by = request.user
        save_employee_advance(obj)


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "date",
        "store",
        "seller",
        "category",
        "amount",
        "advance",
        "created_by",
        "created_at",
    )
    list_filter = ("store", "seller", "category", "date", "created_at")
    search_fields = ("store__name", "seller__name", "category__name", "comment")
    date_hierarchy = "date"

    def save_model(self, request, obj, form, change):
        if request.user.is_authenticated and not obj.created_by_id:
            obj.created_by = request.user
        save_expense(obj)


@admin.register(StoreExpense)
class StoreExpenseAdmin(admin.ModelAdmin):
    list_display = ("id", "date", "store", "category", "amount", "created_by", "created_at")
    list_filter = ("store", "category", "date", "created_at")
    search_fields = ("store__name", "category__name", "comment")
    date_hierarchy = "date"

    def save_model(self, request, obj, form, change):
        if request.user.is_authenticated and not obj.created_by_id:
            obj.created_by = request.user
        save_store_expense(obj)


@admin.register(SalaryPayment)
class SalaryPaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "date", "store", "seller", "amount", "created_by", "created_at")
    list_filter = ("store", "seller", "date", "created_at")
    search_fields = ("store__name", "seller__name", "comment")
    date_hierarchy = "date"

    def save_model(self, request, obj, form, change):
        if request.user.is_authenticated and not obj.created_by_id:
            obj.created_by = request.user
        save_salary_payment(obj)
