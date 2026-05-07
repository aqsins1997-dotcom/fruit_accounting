from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from apps.core.models import Customer, Product, Store, Supplier
from apps.expenses.models import ExpenseCategory, StoreExpense
from apps.inventory.models import Purchase, PurchaseItem, StoreStock
from apps.payables.models import SupplierPayment

from .models import CashRegister, Sale, SaleItem
from .services import recalculate_cash_registers


class SalesNoAdminViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="sales-user", password="secret123")
        self.client = Client()
        self.client.force_login(self.user)
        self.store = Store.objects.create(name="Магазин 1")
        self.supplier = Supplier.objects.create(name="Поставщик 1")
        self.product = Product.objects.create(name="Груша")

        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-18")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("20.000"),
            purchase_price_per_kg=Decimal("10.00"),
        )

    def test_sale_create_page_renders(self):
        response = self.client.get(reverse("sales:sale_create"))
        self.assertEqual(response.status_code, 200)

    def test_cash_sale_can_be_created_without_admin(self):
        response = self.client.post(
            reverse("sales:sale_create"),
            {
                "store": self.store.id,
                "date": "2026-04-19",
                "payment_type": "cash",
                "customer": "",
                "comment": "Продажа у витрины",
                "product": self.product.id,
                "quantity_kg": "5.000",
                "sale_price_per_kg": "30.00",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(SaleItem.objects.count(), 1)
        stock = StoreStock.objects.get(store=self.store, product=self.product)
        self.assertEqual(stock.quantity_kg, Decimal("15.000"))
        register = CashRegister.objects.get(store=self.store)
        self.assertEqual(register.balance, Decimal("150.00"))

    def test_cash_sale_can_be_created_from_quantity_and_total(self):
        response = self.client.post(
            reverse("sales:sale_create"),
            {
                "store": self.store.id,
                "date": "2026-04-19",
                "payment_type": "cash",
                "customer": "",
                "comment": "Total based sale",
                "product": self.product.id,
                "quantity_kg": "4.000",
                "sale_price_per_kg": "",
                "sale_total": "1000.00",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        item = SaleItem.objects.get()
        self.assertEqual(item.sale_price_per_kg, Decimal("250.00"))
        self.assertEqual(item.line_total, Decimal("1000.00"))
        self.assertEqual(item.sale.total_amount, Decimal("1000.00"))

        stock = StoreStock.objects.get(store=self.store, product=self.product)
        self.assertEqual(stock.quantity_kg, Decimal("16.000"))

        register = CashRegister.objects.get(store=self.store)
        self.assertEqual(register.balance, Decimal("1000.00"))

        cash_response = self.client.get(reverse("sales:cash_registers"), HTTP_HOST="localhost")
        self.assertEqual(cash_response.status_code, 200)
        self.assertEqual(cash_response.context["total_cash"], Decimal("1000.00"))

    def test_cash_sale_total_rounds_price_to_two_decimals(self):
        response = self.client.post(
            reverse("sales:sale_create"),
            {
                "store": self.store.id,
                "date": "2026-04-19",
                "payment_type": "cash",
                "customer": "",
                "comment": "",
                "product": self.product.id,
                "quantity_kg": "3.000",
                "sale_price_per_kg": "",
                "sale_total": "1000.00",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        item = SaleItem.objects.get()
        self.assertEqual(item.sale_price_per_kg, Decimal("333.33"))
        self.assertEqual(item.line_total, Decimal("1000.00"))
        self.assertEqual(item.sale.total_amount, Decimal("1000.00"))
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("1000.00"))

    def test_sale_create_shows_form_error_when_stock_is_insufficient(self):
        response = self.client.post(
            reverse("sales:sale_create"),
            {
                "store": self.store.id,
                "date": "2026-04-19",
                "payment_type": "cash",
                "customer": "",
                "comment": "",
                "product": self.product.id,
                "quantity_kg": "25.000",
                "sale_price_per_kg": "30.00",
                "sale_total": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["item_form"],
            "quantity_kg",
            "Недостаточно остатка. Доступно: 20.000 кг.",
        )
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(SaleItem.objects.count(), 0)
        self.assertFalse(CashRegister.objects.filter(store=self.store).exists())
        stock = StoreStock.objects.get(store=self.store, product=self.product)
        self.assertEqual(stock.quantity_kg, Decimal("20.000"))

    def test_changing_cash_sale_to_credit_removes_cash_from_register(self):
        customer = Customer.objects.create(name="Customer 1")
        sale = Sale.objects.create(
            store=self.store,
            date="2026-04-19",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        SaleItem.objects.create(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("5.000"),
            sale_price_per_kg=Decimal("30.00"),
        )
        register = CashRegister.objects.get(store=self.store)
        self.assertEqual(register.balance, Decimal("150.00"))

        sale.payment_type = Sale.PAYMENT_TYPE_CREDIT
        sale.customer = customer
        sale.save()

        register.refresh_from_db()
        sale.refresh_from_db()
        self.assertEqual(register.balance, Decimal("0.00"))
        self.assertEqual(sale.credit.remaining_amount, Decimal("150.00"))

    def test_sale_store_cannot_change_after_items_are_saved(self):
        other_store = Store.objects.create(name="Other store")
        sale = Sale.objects.create(
            store=self.store,
            date="2026-04-19",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        SaleItem.objects.create(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("2.000"),
            sale_price_per_kg=Decimal("30.00"),
        )

        sale.store = other_store
        with self.assertRaises(ValidationError):
            sale.save()

    def test_recalculate_cash_registers_includes_supplier_payments_and_expenses(self):
        category = ExpenseCategory.objects.create(name="Other")
        sale = Sale.objects.create(
            store=self.store,
            date="2026-04-19",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        SaleItem.objects.create(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("5.000"),
            sale_price_per_kg=Decimal("100.00"),
        )

        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-19")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("1.000"),
            purchase_price_per_kg=Decimal("200.00"),
        )
        SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            purchase=purchase,
            date="2026-04-20",
            amount=Decimal("100.00"),
        )
        StoreExpense.objects.create(
            store=self.store,
            category=category,
            date="2026-04-20",
            amount=Decimal("25.00"),
        )

        CashRegister.objects.filter(store=self.store).update(balance=Decimal("9999.00"))

        recalculate_cash_registers(store=self.store)

        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("375.00"))
