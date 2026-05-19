from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from apps.core.models import Customer, Product, Store, Supplier
from apps.credits.models import ClientDebtPayment
from apps.credits.services import build_debtor_rows
from apps.expenses.models import ExpenseCategory, StoreExpense
from apps.inventory.models import Purchase, PurchaseItem, StoreStock, calculate_active_stock_quantity
from apps.payables.models import SupplierPayment
from apps.reports.services import build_product_profitability_rows, build_purchase_item_profitability_map

from .models import CashRegister, Sale, SaleItem, SaleItemBatch
from .services import build_cash_breakdown, recalculate_cash_registers


class SalesNoAdminViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="sales-user", password="secret123")
        self.client = Client()
        self.client.force_login(self.user)
        self.store = Store.objects.create(name="Магазин 1")
        self.supplier = Supplier.objects.create(name="Поставщик 1")
        self.product = Product.objects.create(name="Груша")

        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-18")
        self.purchase_item = PurchaseItem.objects.create(
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
                "purchase_item": self.purchase_item.id,
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

    def test_sale_create_requires_purchase_batch(self):
        response = self.client.post(
            reverse("sales:sale_create"),
            {
                "store": self.store.id,
                "date": "2026-04-19",
                "payment_type": "cash",
                "customer": "",
                "comment": "",
                "product": self.product.id,
                "purchase_item": "",
                "quantity_kg": "5.000",
                "sale_price_per_kg": "30.00",
                "sale_total": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("purchase_item", response.context["item_form"].errors)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(SaleItem.objects.count(), 0)

    def test_sale_is_allocated_only_to_selected_purchase_batch(self):
        second_purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-20")
        second_item = PurchaseItem.objects.create(
            purchase=second_purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("500.000"),
            purchase_price_per_kg=Decimal("20.00"),
        )

        sale = Sale.objects.create(
            store=self.store,
            date="2026-04-21",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        item = SaleItem(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("22.000"),
            sale_price_per_kg=Decimal("30.00"),
        )
        item._selected_purchase_item = second_item
        item.save()

        batches = list(SaleItemBatch.objects.order_by("id"))
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].purchase_item_id, second_item.id)
        self.assertEqual(batches[0].quantity, Decimal("22.000"))

        item.refresh_from_db()
        self.assertEqual(item.line_cost_total, Decimal("440.00"))
        self.assertEqual(item.profit, Decimal("220.00"))
        profitability = build_purchase_item_profitability_map(
            purchase_item_ids=[self.purchase_item.id, second_item.id]
        )
        self.assertEqual(profitability[self.purchase_item.id]["sold_quantity"], Decimal("0.000"))
        self.assertEqual(profitability[self.purchase_item.id]["stock_quantity"], Decimal("20.000"))
        self.assertEqual(profitability[second_item.id]["sold_quantity"], Decimal("22.000"))
        self.assertEqual(profitability[second_item.id]["stock_quantity"], Decimal("478.000"))
        self.assertEqual(StoreStock.objects.get(store=self.store, product=self.product).quantity_kg, Decimal("498.000"))

    def test_sale_form_sells_from_selected_purchase_not_older_batch(self):
        second_purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-20")
        second_item = PurchaseItem.objects.create(
            purchase=second_purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("500.000"),
            purchase_price_per_kg=Decimal("350.00"),
        )

        response = self.client.post(
            reverse("sales:sale_create"),
            {
                "store": self.store.id,
                "date": "2026-04-21",
                "payment_type": "cash",
                "customer": "",
                "comment": "",
                "product": self.product.id,
                "purchase_item": second_item.id,
                "quantity_kg": "100.000",
                "sale_price_per_kg": "500.00",
                "sale_total": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        sale_item = SaleItem.objects.get()
        batch = SaleItemBatch.objects.get(sale_item=sale_item)
        self.assertEqual(batch.purchase_item_id, second_item.id)
        self.assertEqual(sale_item.line_cost_total, Decimal("35000.00"))
        self.assertEqual(sale_item.profit, Decimal("15000.00"))

        profitability = build_purchase_item_profitability_map(
            purchase_item_ids=[self.purchase_item.id, second_item.id]
        )
        self.assertEqual(profitability[self.purchase_item.id]["sold_quantity"], Decimal("0.000"))
        self.assertEqual(profitability[self.purchase_item.id]["stock_quantity"], Decimal("20.000"))
        self.assertEqual(profitability[second_item.id]["sold_quantity"], Decimal("100.000"))
        self.assertEqual(profitability[second_item.id]["stock_quantity"], Decimal("400.000"))

    def test_sale_list_shows_items_with_weight_price_and_total(self):
        sale = Sale.objects.create(
            store=self.store,
            date="2026-04-21",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        SaleItem.objects.create(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("5.000"),
            sale_price_per_kg=Decimal("30.00"),
        )

        response = self.client.get(reverse("sales:sale_list"), HTTP_HOST="localhost")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.product.name)
        self.assertContains(response, "5,000")
        self.assertContains(response, "30,00")
        self.assertContains(response, "150,00")

    def test_sale_delete_is_soft_and_restores_active_stock_cash_and_purchase_metrics(self):
        product = Product.objects.create(name="Apple")
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-20")
        purchase_item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=product,
            quantity_kg=Decimal("100.000"),
            purchase_price_per_kg=Decimal("10.00"),
        )
        sale = Sale.objects.create(
            store=self.store,
            date="2026-04-21",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        sale_item = SaleItem.objects.create(
            sale=sale,
            product=product,
            quantity_kg=Decimal("30.000"),
            sale_price_per_kg=Decimal("20.00"),
        )

        self.assertEqual(StoreStock.objects.get(store=self.store, product=product).quantity_kg, Decimal("70.000"))
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("600.00"))

        sale.delete()

        sale.refresh_from_db()
        sale_item.refresh_from_db()
        stock = StoreStock.objects.get(store=self.store, product=product)
        register = CashRegister.objects.get(store=self.store)

        self.assertIsNotNone(sale.deleted_at)
        self.assertEqual(Sale.objects.filter(pk=sale.pk).count(), 1)
        self.assertEqual(SaleItem.objects.filter(pk=sale_item.pk).count(), 1)
        self.assertEqual(SaleItemBatch.objects.filter(sale_item=sale_item, purchase_item=purchase_item).count(), 1)
        self.assertEqual(stock.quantity_kg, Decimal("100.000"))
        self.assertEqual(register.balance, Decimal("0.00"))

        sales_response = self.client.get(reverse("sales:sale_list"), HTTP_HOST="localhost")
        self.assertEqual(sales_response.status_code, 200)
        self.assertNotContains(sales_response, "Apple")

        purchases_response = self.client.get(reverse("inventory:purchase_list"), HTTP_HOST="localhost")
        self.assertEqual(purchases_response.status_code, 200)
        self.assertNotContains(purchases_response, "NaN")
        self.assertNotContains(purchases_response, "undefined")

        profitability = build_purchase_item_profitability_map(purchase_item_ids=[purchase_item.id])
        self.assertEqual(profitability[purchase_item.id]["sold_quantity"], Decimal("0.000"))
        self.assertEqual(profitability[purchase_item.id]["stock_quantity"], Decimal("100.000"))
        self.assertEqual(profitability[purchase_item.id]["revenue"], Decimal("0.00"))
        self.assertEqual(profitability[purchase_item.id]["sold_cost"], Decimal("0.00"))
        self.assertEqual(profitability[purchase_item.id]["profit"], Decimal("0.00"))

        product_rows = build_product_profitability_rows(store=self.store, product=product)
        self.assertEqual(product_rows[0]["sold_quantity"], Decimal("0.000"))
        self.assertEqual(product_rows[0]["sold_cost"], Decimal("0.00"))
        self.assertEqual(product_rows[0]["profit"], Decimal("0.00"))

        stock_response = self.client.get(reverse("inventory:stock_list"), HTTP_HOST="localhost")
        self.assertEqual(stock_response.status_code, 200)
        self.assertNotContains(stock_response, "NaN")
        self.assertNotContains(stock_response, "undefined")

    def test_sale_with_total_amount_but_no_items_does_not_break_pages(self):
        Sale.objects.create(
            store=self.store,
            date="2026-04-21",
            payment_type=Sale.PAYMENT_TYPE_CASH,
            total_amount=Decimal("15900.00"),
        )

        sales_response = self.client.get(reverse("sales:sale_list"), HTTP_HOST="localhost")
        self.assertEqual(sales_response.status_code, 200)
        self.assertContains(sales_response, "Нет строк продажи")
        self.assertContains(sales_response, "15900,00")

        stock_response = self.client.get(reverse("inventory:stock_list"), HTTP_HOST="localhost")
        self.assertEqual(stock_response.status_code, 200)
        self.assertNotContains(stock_response, "NaN")
        self.assertNotContains(stock_response, "undefined")

        report_response = self.client.get(reverse("reports:product_profitability_report"), HTTP_HOST="localhost")
        self.assertEqual(report_response.status_code, 200)
        self.assertNotContains(report_response, "NaN")
        self.assertNotContains(report_response, "undefined")

    def test_stock_uses_active_sale_items_when_fifo_allocation_is_incomplete(self):
        product = Product.objects.create(name="Batch mismatch product")
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-20")
        purchase_item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=product,
            quantity_kg=Decimal("100.000"),
            purchase_price_per_kg=Decimal("10.00"),
        )
        sale = Sale.objects.create(
            store=self.store,
            date="2026-04-21",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        sale_item = SaleItem.objects.create(
            sale=sale,
            product=product,
            quantity_kg=Decimal("30.000"),
            sale_price_per_kg=Decimal("20.00"),
        )
        SaleItemBatch.objects.filter(sale_item=sale_item, purchase_item=purchase_item).update(
            quantity=Decimal("20.000"),
            total_amount=Decimal("400.00"),
        )

        self.assertEqual(
            calculate_active_stock_quantity(store_id=self.store.id, product_id=product.id),
            Decimal("70.000"),
        )
        product_row = build_product_profitability_rows(store=self.store, product=product)[0]
        self.assertEqual(product_row["sold_quantity"], Decimal("30.000"))
        self.assertEqual(product_row["stock_quantity"], Decimal("70.000"))
        self.assertEqual(product_row["sold_cost"], Decimal("200.00"))
        self.assertEqual(product_row["profit"], Decimal("400.00"))

    def test_soft_deleted_credit_sale_is_excluded_from_client_debt(self):
        product = Product.objects.create(name="Credit Apple")
        customer = Customer.objects.create(name="Customer A")
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-20")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=product,
            quantity_kg=Decimal("100.000"),
            purchase_price_per_kg=Decimal("10.00"),
        )
        sale = Sale.objects.create(
            store=self.store,
            date="2026-04-21",
            payment_type=Sale.PAYMENT_TYPE_CREDIT,
            customer=customer,
        )
        SaleItem.objects.create(
            sale=sale,
            product=product,
            quantity_kg=Decimal("30.000"),
            sale_price_per_kg=Decimal("20.00"),
        )

        self.assertEqual(build_debtor_rows()[0]["total_debt"], Decimal("600.00"))

        sale.delete()

        self.assertEqual(build_debtor_rows(), [])

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
                "purchase_item": self.purchase_item.id,
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
                "purchase_item": self.purchase_item.id,
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
                "purchase_item": self.purchase_item.id,
                "quantity_kg": "25.000",
                "sale_price_per_kg": "30.00",
                "sale_total": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["item_form"],
            "quantity_kg",
            "Недостаточно остатка в выбранной закупке. Доступно: 20.000 кг.",
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

    def test_cash_breakdown_excludes_credit_sales_from_current_cash(self):
        customer = Customer.objects.create(name="Customer 1")
        cash_sale = Sale.objects.create(
            store=self.store,
            date="2026-04-19",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        SaleItem.objects.create(
            sale=cash_sale,
            product=self.product,
            quantity_kg=Decimal("2.000"),
            sale_price_per_kg=Decimal("50.00"),
        )
        credit_sale = Sale.objects.create(
            store=self.store,
            date="2026-04-19",
            payment_type=Sale.PAYMENT_TYPE_CREDIT,
            customer=customer,
        )
        SaleItem.objects.create(
            sale=credit_sale,
            product=self.product,
            quantity_kg=Decimal("3.000"),
            sale_price_per_kg=Decimal("70.00"),
        )

        breakdown = build_cash_breakdown(self.store)

        self.assertEqual(breakdown["cash_sales"], Decimal("100.00"))
        self.assertEqual(breakdown["credit_sales"], Decimal("210.00"))
        self.assertEqual(breakdown["formula_balance"], Decimal("100.00"))
        self.assertEqual(breakdown["stored_balance"], Decimal("100.00"))

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

    def test_reconcile_cash_apply_restores_balance_to_formula(self):
        category = ExpenseCategory.objects.create(name="Other")
        customer = Customer.objects.create(name="Debt customer")

        cash_sale = Sale.objects.create(
            store=self.store,
            date="2026-04-19",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        SaleItem.objects.create(
            sale=cash_sale,
            product=self.product,
            quantity_kg=Decimal("2.000"),
            sale_price_per_kg=Decimal("100.00"),
        )
        credit_sale = Sale.objects.create(
            store=self.store,
            date="2026-04-19",
            payment_type=Sale.PAYMENT_TYPE_CREDIT,
            customer=customer,
        )
        SaleItem.objects.create(
            sale=credit_sale,
            product=self.product,
            quantity_kg=Decimal("3.000"),
            sale_price_per_kg=Decimal("50.00"),
        )
        ClientDebtPayment.objects.create(
            store=self.store,
            client=customer,
            amount=Decimal("50.00"),
            payment_method=ClientDebtPayment.PAYMENT_METHOD_CASH,
            paid_at="2026-04-20",
        )

        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-19")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("1.000"),
            purchase_price_per_kg=Decimal("40.00"),
        )
        SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            purchase=purchase,
            date="2026-04-20",
            amount=Decimal("40.00"),
        )
        StoreExpense.objects.create(
            store=self.store,
            category=category,
            date="2026-04-20",
            amount=Decimal("10.00"),
        )
        CashRegister.objects.filter(store=self.store).update(balance=Decimal("9999.00"))

        dry_run_output = StringIO()
        call_command("reconcile_cash", "--store", self.store.name, "--dry-run", stdout=dry_run_output)
        self.assertIn("DRY RUN", dry_run_output.getvalue())
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("9999.00"))

        apply_output = StringIO()
        call_command("reconcile_cash", "--store", self.store.name, "--apply", stdout=apply_output)

        self.assertIn("Applied cash correction", apply_output.getvalue())
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("200.00"))

    def test_cash_breakdown_ignores_non_cash_client_and_supplier_payments(self):
        customer = Customer.objects.create(name="Debt customer")
        credit_sale = Sale.objects.create(
            store=self.store,
            date="2026-04-19",
            payment_type=Sale.PAYMENT_TYPE_CREDIT,
            customer=customer,
        )
        SaleItem.objects.create(
            sale=credit_sale,
            product=self.product,
            quantity_kg=Decimal("5.000"),
            sale_price_per_kg=Decimal("100.00"),
        )
        ClientDebtPayment.objects.create(
            store=self.store,
            client=customer,
            amount=Decimal("200.00"),
            payment_method=ClientDebtPayment.PAYMENT_METHOD_TRANSFER,
            paid_at="2026-04-20",
        )

        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-19")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("40.00"),
        )
        SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            purchase=purchase,
            date="2026-04-20",
            payment_method=SupplierPayment.PAYMENT_METHOD_TRANSFER,
            amount=Decimal("100.00"),
        )

        breakdown = build_cash_breakdown(self.store)

        self.assertEqual(breakdown["client_debt_payments"], Decimal("0.00"))
        self.assertEqual(breakdown["supplier_payments"], Decimal("0.00"))
        self.assertEqual(breakdown["formula_balance"], Decimal("0.00"))
        self.assertEqual(breakdown["stored_balance"], Decimal("0.00"))
