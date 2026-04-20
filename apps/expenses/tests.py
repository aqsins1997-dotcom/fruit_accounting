from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Product, Seller, Store, Supplier
from apps.inventory.models import Purchase, PurchaseItem
from apps.sales.models import CashRegister, Sale, SaleItem

from .models import EmployeeAdvance, Expense, ExpenseCategory, SalaryPayment, StoreExpense
from .services import save_employee_advance, save_expense, save_salary_payment, save_store_expense


class ExpensesTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="expenses-user", password="secret123")
        self.client = Client()
        self.client.force_login(self.user)

        self.store = Store.objects.create(name="Основной магазин")
        self.seller = Seller.objects.create(name="Айбек", store=self.store, is_active=True)
        self.supplier = Supplier.objects.create(name="Поставщик 1")
        self.product = Product.objects.create(name="Яблоко")
        self.category = ExpenseCategory.objects.create(name="Транспорт")
        self.today = timezone.localdate()
        CashRegister.objects.create(store=self.store, balance=Decimal("500.00"))

    def _create_sale(self, total_quantity="10.000", sale_quantity="2.000", purchase_price="10.00", sale_price="25.00"):
        purchase = Purchase.objects.create(supplier=self.supplier, date=self.today)
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal(total_quantity),
            purchase_price_per_kg=Decimal(purchase_price),
        )

        sale = Sale.objects.create(
            store=self.store,
            date=self.today,
            payment_type=Sale.PAYMENT_TYPE_CASH,
            comment="Продажа",
        )
        SaleItem.objects.create(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal(sale_quantity),
            sale_price_per_kg=Decimal(sale_price),
        )
        return sale

    def test_expense_cannot_exceed_available_advance(self):
        advance = EmployeeAdvance(
            store=self.store,
            seller=self.seller,
            date=self.today,
            amount=Decimal("100.00"),
            created_by=self.user,
        )
        save_employee_advance(advance)

        expense = Expense(
            store=self.store,
            seller=self.seller,
            category=self.category,
            date="2026-04-20",
            amount=Decimal("120.00"),
            advance=advance,
            created_by=self.user,
        )

        with self.assertRaises(ValidationError):
            save_expense(expense)

    def test_store_expense_cannot_exceed_cash_balance(self):
        CashRegister.objects.filter(store=self.store).update(balance=Decimal("15.00"))
        store_expense = StoreExpense(
            store=self.store,
            category=self.category,
            date=self.today,
            amount=Decimal("20.00"),
            created_by=self.user,
        )

        with self.assertRaises(ValidationError):
            save_store_expense(store_expense)

    def test_employee_report_shows_balance_and_today_taken(self):
        advance = EmployeeAdvance(
            store=self.store,
            seller=self.seller,
            date=self.today,
            amount=Decimal("150.00"),
            created_by=self.user,
        )
        save_employee_advance(advance)

        salary_payment = SalaryPayment(
            store=self.store,
            seller=self.seller,
            date=self.today,
            amount=Decimal("60.00"),
            created_by=self.user,
        )
        save_salary_payment(salary_payment)

        expense = Expense(
            store=self.store,
            seller=self.seller,
            category=self.category,
            date=self.today,
            amount=Decimal("40.00"),
            advance=advance,
            created_by=self.user,
        )
        save_expense(expense)

        response = self.client.get(reverse("expenses:employee_report"), HTTP_HOST="localhost")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Айбек")
        self.assertEqual(response.context["summary"]["remaining_amount"], Decimal("110.00"))
        self.assertEqual(response.context["summary"]["today_salary_amount"], Decimal("60.00"))
        self.assertEqual(response.context["summary"]["today_taken_amount"], Decimal("210.00"))

    def test_expense_report_includes_store_expenses_and_salary_in_profit(self):
        self._create_sale()

        advance = EmployeeAdvance(
            store=self.store,
            seller=self.seller,
            date=self.today,
            amount=Decimal("80.00"),
            created_by=self.user,
        )
        save_employee_advance(advance)

        expense = Expense(
            store=self.store,
            seller=self.seller,
            category=self.category,
            date=self.today,
            amount=Decimal("30.00"),
            advance=advance,
            created_by=self.user,
        )
        save_expense(expense)

        store_expense = StoreExpense(
            store=self.store,
            category=self.category,
            date=self.today,
            amount=Decimal("5.00"),
            created_by=self.user,
        )
        save_store_expense(store_expense)

        salary_payment = SalaryPayment(
            store=self.store,
            seller=self.seller,
            date=self.today,
            amount=Decimal("10.00"),
            created_by=self.user,
        )
        save_salary_payment(salary_payment)

        response = self.client.get(reverse("expenses:expense_report"), HTTP_HOST="localhost")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["total_revenue"], Decimal("50.00"))
        self.assertEqual(response.context["summary"]["total_cost"], Decimal("20.00"))
        self.assertEqual(response.context["summary"]["employee_expenses"], Decimal("30.00"))
        self.assertEqual(response.context["summary"]["store_expenses"], Decimal("5.00"))
        self.assertEqual(response.context["summary"]["salary_payments"], Decimal("10.00"))
        self.assertEqual(response.context["summary"]["net_profit"], Decimal("-15.00"))

    def test_cash_balance_changes_after_advance_store_expense_and_salary(self):
        advance = EmployeeAdvance(
            store=self.store,
            seller=self.seller,
            date=self.today,
            amount=Decimal("100.00"),
            created_by=self.user,
        )
        save_employee_advance(advance)

        store_expense = StoreExpense(
            store=self.store,
            category=self.category,
            date=self.today,
            amount=Decimal("50.00"),
            created_by=self.user,
        )
        save_store_expense(store_expense)

        salary_payment = SalaryPayment(
            store=self.store,
            seller=self.seller,
            date=self.today,
            amount=Decimal("20.00"),
            created_by=self.user,
        )
        save_salary_payment(salary_payment)

        cash_register = CashRegister.objects.get(store=self.store)
        self.assertEqual(cash_register.balance, Decimal("330.00"))

    def test_new_expense_pages_open(self):
        self.assertEqual(self.client.get(reverse("expenses:advance_create"), HTTP_HOST="localhost").status_code, 200)
        self.assertEqual(self.client.get(reverse("expenses:expense_create"), HTTP_HOST="localhost").status_code, 200)
        self.assertEqual(self.client.get(reverse("expenses:store_expense_create"), HTTP_HOST="localhost").status_code, 200)
        self.assertEqual(self.client.get(reverse("expenses:salary_payment_create"), HTTP_HOST="localhost").status_code, 200)
