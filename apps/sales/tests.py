from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.core.models import Customer, Product, Store, Supplier
from apps.credits.models import ClientDebtPayment, Credit
from apps.credits.services import build_debtor_rows
from apps.expenses.models import ExpenseCategory, StoreExpense
from apps.inventory.models import Purchase, PurchaseItem, StoreStock, calculate_active_stock_quantity
from apps.payables.models import SupplierPayment
from apps.reports.services import build_product_profitability_rows, build_purchase_item_profitability_map

from .models import CashRegister, Sale, SaleItem, SaleItemBatch, purchase_item_available_quantity
from .services import (
    build_cash_breakdown,
    convert_sale_cash_to_credit,
    preview_sale_cash_to_credit,
    recalculate_cash_registers,
)


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

    def _create_cash_sale_from_batch(
        self,
        *,
        purchase_item=None,
        product=None,
        quantity=Decimal("5.000"),
        price=Decimal("30.00"),
        total=None,
    ):
        purchase_item = purchase_item or self.purchase_item
        product = product or purchase_item.product
        sale = Sale.objects.create(
            store=self.store,
            date="2026-04-19",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        item = SaleItem(
            sale=sale,
            product=product,
            quantity_kg=quantity,
            sale_price_per_kg=price,
        )
        item._selected_purchase_item = purchase_item
        if total is not None:
            item._sale_total_override = total
        item.save()
        return sale, item

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

    def test_cash_sale_post_query_count_stays_bounded(self):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.post(
                reverse("sales:sale_create"),
                {
                    "store": self.store.id,
                    "date": "2026-04-19",
                    "payment_type": "cash",
                    "customer": "",
                    "comment": "profile",
                    "product": self.product.id,
                    "purchase_item": self.purchase_item.id,
                    "quantity_kg": "5.000",
                    "sale_price_per_kg": "30.00",
                    "sale_total": "",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertLessEqual(len(ctx.captured_queries), 30)
        self.assertEqual(SaleItemBatch.objects.get().purchase_item_id, self.purchase_item.id)
        self.assertEqual(StoreStock.objects.get(store=self.store, product=self.product).quantity_kg, Decimal("15.000"))
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("150.00"))

    def test_credit_sale_post_query_count_stays_bounded(self):
        customer = Customer.objects.create(name="Credit customer")

        with CaptureQueriesContext(connection) as ctx:
            response = self.client.post(
                reverse("sales:sale_create"),
                {
                    "store": self.store.id,
                    "date": "2026-04-19",
                    "payment_type": "credit",
                    "customer": customer.id,
                    "comment": "profile",
                    "product": self.product.id,
                    "purchase_item": self.purchase_item.id,
                    "quantity_kg": "5.000",
                    "sale_price_per_kg": "30.00",
                    "sale_total": "",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertLessEqual(len(ctx.captured_queries), 30)
        self.assertEqual(SaleItemBatch.objects.get().purchase_item_id, self.purchase_item.id)
        self.assertEqual(StoreStock.objects.get(store=self.store, product=self.product).quantity_kg, Decimal("15.000"))
        self.assertFalse(CashRegister.objects.filter(store=self.store).exists())
        debt = build_debtor_rows()[0]
        self.assertEqual(debt["total_debt"], Decimal("150.00"))

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

    def test_sale_list_shows_cash_to_credit_action_for_cash_sales(self):
        sale, _ = self._create_cash_sale_from_batch()

        response = self.client.get(reverse("sales:sale_list"), HTTP_HOST="localhost")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("sales:sale_cash_to_credit", args=[sale.pk]))
        self.assertContains(response, "Перевести в кредит")

    def test_cash_sale_can_be_converted_to_credit_safely(self):
        customer = Customer.objects.create(name="Арых")
        product = Product.objects.create(name="Черешня")
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-06-06")
        purchase_item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=product,
            quantity_kg=Decimal("500.000"),
            purchase_price_per_kg=Decimal("500.00"),
        )
        sale, sale_item = self._create_cash_sale_from_batch(
            purchase_item=purchase_item,
            product=product,
            quantity=Decimal("185.500"),
            price=Decimal("450.13"),
            total=Decimal("83500.00"),
        )
        batch_before = list(
            SaleItemBatch.objects.filter(sale_item=sale_item).values(
                "purchase_item_id",
                "quantity",
                "sale_price",
                "total_amount",
            )
        )
        available_before = purchase_item_available_quantity(purchase_item)
        stock_before = StoreStock.objects.get(store=self.store, product=product).quantity_kg
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("83500.00"))

        converted_sale = convert_sale_cash_to_credit(
            sale_id=sale.pk,
            customer_id=customer.pk,
            note="Test conversion",
        )

        converted_sale.refresh_from_db()
        sale_item.refresh_from_db()
        self.assertEqual(converted_sale.payment_type, Sale.PAYMENT_TYPE_CREDIT)
        self.assertEqual(converted_sale.customer_id, customer.pk)
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("0.00"))
        credit = Credit.objects.get(sale=converted_sale)
        self.assertEqual(credit.customer_id, customer.pk)
        self.assertEqual(credit.original_amount, Decimal("83500.00"))
        self.assertEqual(credit.remaining_amount, Decimal("83500.00"))
        self.assertEqual(build_debtor_rows()[0]["total_debt"], Decimal("83500.00"))
        self.assertEqual(
            list(
                SaleItemBatch.objects.filter(sale_item=sale_item).values(
                    "purchase_item_id",
                    "quantity",
                    "sale_price",
                    "total_amount",
                )
            ),
            batch_before,
        )
        self.assertEqual(purchase_item_available_quantity(purchase_item), available_before)
        self.assertEqual(StoreStock.objects.get(store=self.store, product=product).quantity_kg, stock_before)
        self.assertEqual(sale_item.line_total, Decimal("83500.00"))
        self.assertEqual(sale_item.line_cost_total, Decimal("92750.00"))

        audit_output = StringIO()
        call_command("audit_accounting_integrity", stdout=audit_output)
        self.assertIn("CRITICAL: 0", audit_output.getvalue())

    def test_cash_sale_to_credit_requires_customer(self):
        sale, _ = self._create_cash_sale_from_batch()

        preview = preview_sale_cash_to_credit(sale_id=sale.pk, customer=None)

        self.assertFalse(preview["can_apply"])
        self.assertIn("клиента", preview["error"])
        self.assertEqual(Sale.objects.get(pk=sale.pk).payment_type, Sale.PAYMENT_TYPE_CASH)
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("150.00"))

    def test_cash_sale_to_credit_is_idempotent(self):
        customer = Customer.objects.create(name="Credit customer")
        sale, _ = self._create_cash_sale_from_batch(total=Decimal("150.00"))

        convert_sale_cash_to_credit(sale_id=sale.pk, customer_id=customer.pk)
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("0.00"))

        with self.assertRaises(ValidationError):
            convert_sale_cash_to_credit(sale_id=sale.pk, customer_id=customer.pk)

        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("0.00"))
        self.assertEqual(Credit.objects.filter(sale=sale).count(), 1)

    def test_cash_to_credit_ui_post_converts_sale(self):
        customer = Customer.objects.create(name="UI customer")
        sale, _ = self._create_cash_sale_from_batch(total=Decimal("150.00"))

        get_response = self.client.get(reverse("sales:sale_cash_to_credit", args=[sale.pk]), HTTP_HOST="localhost")
        self.assertEqual(get_response.status_code, 200)
        self.assertContains(get_response, "Касса магазина уменьшится")

        response = self.client.post(
            reverse("sales:sale_cash_to_credit", args=[sale.pk]),
            {"customer": customer.pk},
            follow=True,
            HTTP_HOST="localhost",
        )

        self.assertEqual(response.status_code, 200)
        sale.refresh_from_db()
        self.assertEqual(sale.payment_type, Sale.PAYMENT_TYPE_CREDIT)
        self.assertEqual(sale.customer_id, customer.pk)
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("0.00"))

    def test_convert_sale_cash_to_credit_command_dry_run_and_apply(self):
        customer = Customer.objects.create(name="Command customer")
        sale, _ = self._create_cash_sale_from_batch(total=Decimal("83500.00"))

        dry_run_output = StringIO()
        call_command(
            "convert_sale_cash_to_credit",
            "--sale-id",
            str(sale.pk),
            "--client-id",
            str(customer.pk),
            stdout=dry_run_output,
        )
        self.assertIn("DRY RUN", dry_run_output.getvalue())
        self.assertIn("cash impact: -83500.00", dry_run_output.getvalue())
        self.assertIn("client debt impact: +83500.00", dry_run_output.getvalue())
        self.assertIn("can_apply: YES", dry_run_output.getvalue())
        sale.refresh_from_db()
        self.assertEqual(sale.payment_type, Sale.PAYMENT_TYPE_CASH)
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("83500.00"))

        apply_output = StringIO()
        call_command(
            "convert_sale_cash_to_credit",
            "--sale-id",
            str(sale.pk),
            "--client-id",
            str(customer.pk),
            "--apply",
            stdout=apply_output,
        )
        self.assertIn(f"converted sale #{sale.pk} to credit", apply_output.getvalue())
        sale.refresh_from_db()
        self.assertEqual(sale.payment_type, Sale.PAYMENT_TYPE_CREDIT)
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("0.00"))

    def test_verify_sale_payment_type_command_is_read_only(self):
        customer = Customer.objects.create(name="Verify customer")
        sale, _ = self._create_cash_sale_from_batch(total=Decimal("83500.00"))
        convert_sale_cash_to_credit(sale_id=sale.pk, customer_id=customer.pk)

        output = StringIO()
        call_command("verify_sale_payment_type", "--sale-id", str(sale.pk), stdout=output)

        self.assertIn("READ ONLY SALE PAYMENT TYPE VERIFICATION", output.getvalue())
        self.assertIn("payment type: credit", output.getvalue())
        self.assertIn("client debt effect expected: 83500.00", output.getvalue())
        self.assertIn("represented correctly in cash/client debt: YES", output.getvalue())
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("0.00"))

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

    def test_repair_saleitem_allocations_fixes_safe_one_batch_mismatch(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-22")
        purchase_item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("40.000"),
            purchase_price_per_kg=Decimal("10.00"),
        )
        sale = Sale.objects.create(
            store=self.store,
            date="2026-04-23",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        sale_item = SaleItem(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("34.000"),
            sale_price_per_kg=Decimal("20.00"),
        )
        sale_item._selected_purchase_item = purchase_item
        sale_item.save()

        batch = SaleItemBatch.objects.get(sale_item=sale_item)
        SaleItemBatch.objects.filter(pk=batch.pk).update(quantity=Decimal("25.800"))
        SaleItem.objects.filter(pk=sale_item.pk).update(
            cost_price_per_kg=Decimal("7.59"),
            line_cost_total=Decimal("258.00"),
            profit=Decimal("422.00"),
        )
        sale_item.refresh_from_db()
        self.assertEqual(SaleItemBatch.objects.get(pk=batch.pk).quantity, Decimal("25.800"))

        audit_stdout = StringIO()
        with self.assertRaises(SystemExit) as exc:
            call_command("audit_accounting_integrity", stdout=audit_stdout)
        self.assertEqual(exc.exception.code, 1)
        self.assertIn(
            f"SaleItem #{sale_item.id} batch quantity 25.800 does not match sale quantity 34.000.",
            audit_stdout.getvalue(),
        )

        dry_run_stdout = StringIO()
        call_command(
            "repair_saleitem_allocations",
            "--sale-item-id",
            str(sale_item.id),
            stdout=dry_run_stdout,
        )
        self.assertIn("WOULD REPAIR", dry_run_stdout.getvalue())
        self.assertEqual(SaleItemBatch.objects.get(pk=batch.pk).quantity, Decimal("25.800"))

        apply_stdout = StringIO()
        call_command(
            "repair_saleitem_allocations",
            "--sale-item-id",
            str(sale_item.id),
            "--apply",
            stdout=apply_stdout,
        )
        self.assertIn("REPAIRED", apply_stdout.getvalue())

        sale_item.refresh_from_db()
        batch = SaleItemBatch.objects.get(sale_item=sale_item)
        sale.refresh_from_db()
        self.assertEqual(batch.quantity, Decimal("34.000"))
        self.assertEqual(batch.total_amount, Decimal("680.00"))
        self.assertEqual(sale_item.line_cost_total, Decimal("340.00"))
        self.assertEqual(sale_item.profit, Decimal("340.00"))
        self.assertEqual(sale.total_cost, Decimal("340.00"))
        self.assertEqual(sale.total_profit, Decimal("340.00"))

        clean_audit_stdout = StringIO()
        call_command("audit_accounting_integrity", stdout=clean_audit_stdout)
        self.assertIn("CRITICAL: 0", clean_audit_stdout.getvalue())

    def test_repair_saleitem_allocations_skips_ambiguous_multi_batch_case(self):
        first_purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-22")
        first_item = PurchaseItem.objects.create(
            purchase=first_purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("40.000"),
            purchase_price_per_kg=Decimal("10.00"),
        )
        second_purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-23")
        second_item = PurchaseItem.objects.create(
            purchase=second_purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("40.000"),
            purchase_price_per_kg=Decimal("12.00"),
        )
        sale = Sale.objects.create(
            store=self.store,
            date="2026-04-24",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        sale_item = SaleItem(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("34.000"),
            sale_price_per_kg=Decimal("20.00"),
        )
        sale_item._selected_purchase_item = first_item
        sale_item.save()

        original_batch = SaleItemBatch.objects.get(sale_item=sale_item)
        SaleItemBatch.objects.filter(pk=original_batch.pk).update(quantity=Decimal("20.000"))
        SaleItemBatch.objects.create(
            sale_item=sale_item,
            purchase_item=second_item,
            quantity=Decimal("5.800"),
            sale_price=Decimal("20.00"),
            total_amount=Decimal("116.00"),
        )

        output = StringIO()
        call_command(
            "repair_saleitem_allocations",
            "--sale-item-id",
            str(sale_item.id),
            "--apply",
            stdout=output,
        )

        self.assertIn("SKIP: Ambiguous: more than one active batch is linked to this sale item.", output.getvalue())
        quantities = list(
            SaleItemBatch.objects.filter(sale_item=sale_item)
            .order_by("id")
            .values_list("quantity", flat=True)
        )
        self.assertEqual(quantities, [Decimal("20.000"), Decimal("5.800")])

    def test_repair_saleitem_14_batch_mismatch_moves_later_sale_to_safe_batch(self):
        product = Product.objects.create(name="Клубника")
        source_purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-13")
        source_item = PurchaseItem.objects.create(
            purchase=source_purchase,
            store=self.store,
            product=product,
            quantity_kg=Decimal("60.000"),
            purchase_price_per_kg=Decimal("200.00"),
        )
        alternative_purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-13")
        alternative_item = PurchaseItem.objects.create(
            purchase=alternative_purchase,
            store=self.store,
            product=product,
            quantity_kg=Decimal("20.000"),
            purchase_price_per_kg=Decimal("210.00"),
        )

        target_sale = Sale.objects.create(
            store=self.store,
            date="2026-05-13",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        target_item = SaleItem(
            sale=target_sale,
            product=product,
            quantity_kg=Decimal("34.000"),
            sale_price_per_kg=Decimal("291.18"),
        )
        target_item._selected_purchase_item = source_item
        target_item.save()
        target_batch = SaleItemBatch.objects.get(sale_item=target_item)
        SaleItemBatch.objects.filter(pk=target_batch.pk).update(
            quantity=Decimal("25.800"),
            total_amount=Decimal("7512.44"),
        )
        SaleItem.objects.filter(pk=target_item.pk).update(
            line_total=Decimal("9900.00"),
            line_cost_total=Decimal("5160.00"),
            profit=Decimal("4740.00"),
            sale_price_per_kg=Decimal("291.18"),
        )
        Sale.objects.filter(pk=target_sale.pk).update(
            total_amount=Decimal("9900.00"),
            total_cost=Decimal("5160.00"),
            total_profit=Decimal("4740.00"),
        )
        target_item.refresh_from_db()

        later_sale = Sale.objects.create(
            store=self.store,
            date="2026-05-14",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        later_item = SaleItem(
            sale=later_sale,
            product=product,
            quantity_kg=Decimal("10.000"),
            sale_price_per_kg=Decimal("300.00"),
        )
        later_item._selected_purchase_item = source_item
        later_item.save()
        recalculate_cash_registers(store=self.store)

        audit_before = StringIO()
        with self.assertRaises(SystemExit):
            call_command("audit_accounting_integrity", stdout=audit_before)
        self.assertIn(
            f"SaleItem #{target_item.id} batch quantity 25.800 does not match sale quantity 34.000.",
            audit_before.getvalue(),
        )

        dry_run_output = StringIO()
        call_command(
            "repair_saleitem_14_batch_mismatch",
            "--sale-item-id",
            str(target_item.id),
            "--purchase-item-id",
            str(source_item.id),
            stdout=dry_run_output,
        )
        self.assertIn("SAFE APPLY AVAILABLE", dry_run_output.getvalue())
        self.assertIn(f"purchase_item #{alternative_item.id}", dry_run_output.getvalue())
        self.assertIn("simple shift without replacement would leave it mismatched: True", dry_run_output.getvalue())

        apply_output = StringIO()
        call_command(
            "repair_saleitem_14_batch_mismatch",
            "--sale-item-id",
            str(target_item.id),
            "--purchase-item-id",
            str(source_item.id),
            "--apply",
            stdout=apply_output,
        )
        self.assertIn("audit_accounting_integrity summary: CRITICAL=0", apply_output.getvalue())

        target_item.refresh_from_db()
        later_item.refresh_from_db()
        target_batches = list(SaleItemBatch.objects.filter(sale_item=target_item).order_by("id"))
        later_batches = list(SaleItemBatch.objects.filter(sale_item=later_item).order_by("purchase_item_id", "id"))
        self.assertEqual(len(target_batches), 1)
        self.assertEqual(target_batches[0].purchase_item_id, source_item.id)
        self.assertEqual(target_batches[0].quantity, Decimal("34.000"))

        self.assertEqual(len(later_batches), 2)
        self.assertEqual(later_batches[0].purchase_item_id, source_item.id)
        self.assertEqual(later_batches[0].quantity, Decimal("1.800"))
        self.assertEqual(later_batches[1].purchase_item_id, alternative_item.id)
        self.assertEqual(later_batches[1].quantity, Decimal("8.200"))

        self.assertEqual(target_item.line_cost_total, Decimal("6800.00"))
        self.assertEqual(target_item.profit, Decimal("3100.00"))
        self.assertEqual(later_item.line_cost_total, Decimal("2082.00"))
        self.assertEqual(later_item.profit, Decimal("918.00"))

        clean_audit = StringIO()
        call_command("audit_accounting_integrity", stdout=clean_audit)
        self.assertIn("CRITICAL: 0", clean_audit.getvalue())

    def test_repair_saleitem_14_batch_mismatch_skips_when_no_safe_alternative_batch_exists(self):
        product = Product.objects.create(name="Клубника")
        source_purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-13")
        source_item = PurchaseItem.objects.create(
            purchase=source_purchase,
            store=self.store,
            product=product,
            quantity_kg=Decimal("60.000"),
            purchase_price_per_kg=Decimal("200.00"),
        )
        target_sale = Sale.objects.create(
            store=self.store,
            date="2026-05-13",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        target_item = SaleItem(
            sale=target_sale,
            product=product,
            quantity_kg=Decimal("34.000"),
            sale_price_per_kg=Decimal("291.18"),
        )
        target_item._selected_purchase_item = source_item
        target_item.save()
        target_batch = SaleItemBatch.objects.get(sale_item=target_item)
        SaleItemBatch.objects.filter(pk=target_batch.pk).update(quantity=Decimal("25.800"))

        later_sale = Sale.objects.create(
            store=self.store,
            date="2026-05-14",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        later_item = SaleItem(
            sale=later_sale,
            product=product,
            quantity_kg=Decimal("10.000"),
            sale_price_per_kg=Decimal("300.00"),
        )
        later_item._selected_purchase_item = source_item
        later_item.save()

        output = StringIO()
        call_command(
            "repair_saleitem_14_batch_mismatch",
            "--sale-item-id",
            str(target_item.id),
            "--purchase-item-id",
            str(source_item.id),
            "--apply",
            stdout=output,
        )
        self.assertIn("UNSAFE NO APPLY:", output.getvalue())
        self.assertIn("simple shift without replacement would leave it mismatched: True", output.getvalue())
        self.assertEqual(SaleItemBatch.objects.get(sale_item=target_item).quantity, Decimal("25.800"))
        self.assertEqual(
            list(
                SaleItemBatch.objects.filter(sale_item=later_item)
                .values_list("purchase_item_id", "quantity")
            ),
            [(source_item.id, Decimal("10.000"))],
        )
        self.assertEqual(purchase_item_available_quantity(source_item), Decimal("24.200"))

    def test_repair_saleitem_14_batch_mismatch_skips_ambiguous_target_multi_batch_case(self):
        product = Product.objects.create(name="Клубника")
        first_purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-13")
        first_item = PurchaseItem.objects.create(
            purchase=first_purchase,
            store=self.store,
            product=product,
            quantity_kg=Decimal("50.000"),
            purchase_price_per_kg=Decimal("200.00"),
        )
        second_purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-13")
        second_item = PurchaseItem.objects.create(
            purchase=second_purchase,
            store=self.store,
            product=product,
            quantity_kg=Decimal("50.000"),
            purchase_price_per_kg=Decimal("210.00"),
        )
        target_sale = Sale.objects.create(
            store=self.store,
            date="2026-05-13",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        target_item = SaleItem(
            sale=target_sale,
            product=product,
            quantity_kg=Decimal("34.000"),
            sale_price_per_kg=Decimal("291.18"),
        )
        target_item._selected_purchase_item = first_item
        target_item.save()
        original_batch = SaleItemBatch.objects.get(sale_item=target_item)
        SaleItemBatch.objects.filter(pk=original_batch.pk).update(quantity=Decimal("20.000"))
        SaleItemBatch.objects.create(
            sale_item=target_item,
            purchase_item=second_item,
            quantity=Decimal("5.800"),
            sale_price=Decimal("291.18"),
            total_amount=Decimal("1688.84"),
        )

        output = StringIO()
        call_command(
            "repair_saleitem_14_batch_mismatch",
            "--sale-item-id",
            str(target_item.id),
            "--apply",
            stdout=output,
        )
        self.assertIn("UNSAFE NO APPLY: Ambiguous target sale item", output.getvalue())
