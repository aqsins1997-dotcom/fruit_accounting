from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.core.models import Product, Store, Supplier
from apps.payables.models import SupplierOverpayment, SupplierPayment, SupplierPaymentAllocation
from apps.reports.services import build_product_profitability_rows, build_purchase_item_profitability_map
from apps.sales.models import CashRegister, Sale, SaleItem, SaleItemBatch

from .models import Purchase, PurchaseItem, StockMovement, StoreStock


class InventoryNoAdminViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="inventory-user", password="secret123")
        self.client = Client()
        self.client.force_login(self.user)
        self.store = Store.objects.create(name="Склад 1")
        self.supplier = Supplier.objects.create(name="Поставщик 1")
        self.product = Product.objects.create(name="Яблоко")

    def test_purchase_create_page_renders(self):
        response = self.client.get(reverse("inventory:purchase_create"))
        self.assertEqual(response.status_code, 200)

    def test_stock_and_purchase_lists_show_profitability_columns(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-19")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("25.00"),
        )

        stock_response = self.client.get(reverse("inventory:stock_list"))
        self.assertContains(stock_response, "Средняя цена продажи")
        self.assertContains(stock_response, "Маржа на единицу")

        purchase_response = self.client.get(reverse("inventory:purchase_list"))
        self.assertContains(purchase_response, "средняя продажа")
        self.assertContains(purchase_response, "прибыль")
        self.assertContains(purchase_response, "Изменить цену")
        self.assertContains(purchase_response, "Изменить товар")

    def test_purchase_profitability_is_calculated_per_batch(self):
        first_purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-06")
        first_item = PurchaseItem.objects.create(
            purchase=first_purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("25.00"),
        )
        second_purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-08")
        second_item = PurchaseItem.objects.create(
            purchase=second_purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("40.00"),
        )

        sale = Sale.objects.create(
            store=self.store,
            date="2026-05-09",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        first_sale_item = SaleItem(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            sale_price_per_kg=Decimal("100.00"),
        )
        first_sale_item._selected_purchase_item = first_item
        first_sale_item.save()
        second_sale_item = SaleItem(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("5.000"),
            sale_price_per_kg=Decimal("100.00"),
        )
        second_sale_item._selected_purchase_item = second_item
        second_sale_item.save()

        profitability = build_purchase_item_profitability_map(
            purchase_item_ids=[first_item.id, second_item.id]
        )

        self.assertEqual(profitability[first_item.id]["sold_quantity"], Decimal("10.000"))
        self.assertEqual(profitability[first_item.id]["stock_quantity"], Decimal("0.000"))
        self.assertEqual(profitability[first_item.id]["average_sale_price"], Decimal("100.00"))
        self.assertEqual(profitability[first_item.id]["profit"], Decimal("750.00"))

        self.assertEqual(profitability[second_item.id]["sold_quantity"], Decimal("5.000"))
        self.assertEqual(profitability[second_item.id]["stock_quantity"], Decimal("5.000"))
        self.assertEqual(profitability[second_item.id]["average_sale_price"], Decimal("100.00"))
        self.assertEqual(profitability[second_item.id]["profit"], Decimal("300.00"))

    def test_purchase_can_be_created_without_admin(self):
        response = self.client.post(
            reverse("inventory:purchase_create"),
            {
                "supplier": self.supplier.id,
                "date": "2026-04-19",
                "comment": "Первая закупка",
                "store": self.store.id,
                "product": self.product.id,
                "quantity_kg": "15.500",
                "purchase_price_per_kg": "25.00",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PurchaseItem.objects.count(), 1)
        stock = StoreStock.objects.get(store=self.store, product=self.product)
        self.assertEqual(stock.quantity_kg, Decimal("15.500"))

    def test_purchase_cannot_be_reduced_below_sold_quantity(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-19")
        item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("25.00"),
        )

        sale = Sale.objects.create(
            store=self.store,
            date="2026-04-20",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        SaleItem.objects.create(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("6.000"),
            sale_price_per_kg=Decimal("40.00"),
        )

        item.quantity_kg = Decimal("5.000")
        with self.assertRaises(ValidationError):
            item.save()

        item.refresh_from_db()
        stock = StoreStock.objects.get(store=self.store, product=self.product)
        self.assertEqual(item.quantity_kg, Decimal("10.000"))
        self.assertEqual(stock.quantity_kg, Decimal("4.000"))

    def test_purchase_item_price_update_recalculates_cost_without_changing_cash_or_stock(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-01")
        item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("330.00"),
        )
        sale = Sale.objects.create(
            store=self.store,
            date="2026-05-02",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        sale_item = SaleItem.objects.create(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("4.000"),
            sale_price_per_kg=Decimal("400.00"),
        )
        cash_before = CashRegister.objects.get(store=self.store).balance

        response = self.client.post(
            reverse("inventory:purchase_item_price_update", args=[item.id]),
            {"purchase_price_per_kg": "300.00", "action": "apply"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Цена закупки успешно обновлена")

        item.refresh_from_db()
        sale.refresh_from_db()
        sale_item.refresh_from_db()
        stock = StoreStock.objects.get(store=self.store, product=self.product)
        cash_after = CashRegister.objects.get(store=self.store).balance

        self.assertEqual(item.purchase_price_per_kg, Decimal("300.00"))
        self.assertEqual(stock.quantity_kg, Decimal("6.000"))
        self.assertEqual(cash_after, cash_before)
        self.assertEqual(sale_item.line_total, Decimal("1600.00"))
        self.assertEqual(sale_item.line_cost_total, Decimal("1200.00"))
        self.assertEqual(sale_item.profit, Decimal("400.00"))
        self.assertEqual(sale.total_amount, Decimal("1600.00"))
        self.assertEqual(sale.total_cost, Decimal("1200.00"))
        self.assertEqual(sale.total_profit, Decimal("400.00"))

        profitability = build_purchase_item_profitability_map(purchase_item_ids=[item.id])
        self.assertEqual(profitability[item.id]["sold_cost"], Decimal("1200.00"))
        self.assertEqual(profitability[item.id]["profit"], Decimal("400.00"))
        self.assertEqual(profitability[item.id]["margin_per_unit"], Decimal("100.00"))

    def test_purchase_item_price_update_apply_query_count_stays_bounded(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-01")
        item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("330.00"),
        )
        sale = Sale.objects.create(
            store=self.store,
            date="2026-05-02",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        SaleItem.objects.create(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("4.000"),
            sale_price_per_kg=Decimal("400.00"),
        )

        with CaptureQueriesContext(connection) as ctx:
            response = self.client.post(
                reverse("inventory:purchase_item_price_update", args=[item.id]),
                {"purchase_price_per_kg": "300.00", "action": "apply"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertLessEqual(len(ctx.captured_queries), 35)

    def test_purchase_item_price_update_rejects_negative_price(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-01")
        item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("330.00"),
        )

        response = self.client.post(
            reverse("inventory:purchase_item_price_update", args=[item.id]),
            {"purchase_price_per_kg": "-1.00"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Новая закупочная цена не может быть отрицательной.",
            response.context["form"].errors["purchase_price_per_kg"],
        )
        item.refresh_from_db()
        self.assertEqual(item.purchase_price_per_kg, Decimal("330.00"))

    def test_purchase_item_quantity_update_rejects_below_sold_and_recalculates_stock_and_debt(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-01")
        item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("500.000"),
            purchase_price_per_kg=Decimal("10.00"),
        )
        sale = Sale.objects.create(
            store=self.store,
            date="2026-05-02",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        SaleItem.objects.create(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("300.000"),
            sale_price_per_kg=Decimal("20.00"),
        )
        SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            purchase=purchase,
            date="2026-05-03",
            amount=Decimal("1000.00"),
        )

        response = self.client.post(
            reverse("inventory:purchase_item_quantity_update", args=[item.id]),
            {"quantity_kg": "250.000"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Минимум: 300.000 кг")
        item.refresh_from_db()
        self.assertEqual(item.quantity_kg, Decimal("500.000"))

        response = self.client.post(
            reverse("inventory:purchase_item_quantity_update", args=[item.id]),
            {"quantity_kg": "400.000", "action": "apply"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        stock = StoreStock.objects.get(store=self.store, product=self.product)
        self.assertEqual(item.quantity_kg, Decimal("400.000"))
        self.assertEqual(stock.quantity_kg, Decimal("100.000"))

        response = self.client.get(reverse("payables:supplier_balances"), HTTP_HOST="localhost")
        group = response.context["supplier_groups"][0]
        self.assertEqual(group["purchase_total"], Decimal("4000.00"))
        self.assertEqual(group["paid_amount"], Decimal("1000.00"))
        self.assertEqual(group["remaining_amount"], Decimal("3000.00"))
        self.assertEqual(SupplierPaymentAllocation.objects.get(purchase=purchase).amount, Decimal("1000.00"))

    def test_purchase_item_quantity_update_creates_supplier_overpayment_when_total_drops_below_paid_amount(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-01")
        item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("500.000"),
            purchase_price_per_kg=Decimal("10.00"),
        )
        CashRegister.objects.create(store=self.store, balance=Decimal("10000.00"))
        SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            purchase=purchase,
            date="2026-05-03",
            amount=Decimal("4000.00"),
        )

        response = self.client.post(
            reverse("inventory:purchase_item_quantity_update", args=[item.id]),
            {"quantity_kg": "300.000", "action": "apply"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "успешно обновлен")
        item.refresh_from_db()
        self.assertEqual(item.quantity_kg, Decimal("300.000"))
        allocation = SupplierPaymentAllocation.objects.get(purchase=purchase)
        self.assertEqual(allocation.amount, Decimal("3000.00"))
        overpayment = SupplierOverpayment.objects.get(source_payment__purchase=purchase)
        self.assertEqual(overpayment.amount, Decimal("1000.00"))
        self.assertEqual(overpayment.remaining_amount, Decimal("1000.00"))
        response = self.client.get(reverse("payables:supplier_balances"), HTTP_HOST="localhost")
        group = response.context["supplier_groups"][0]
        self.assertEqual(group["remaining_amount"], Decimal("0.00"))
        self.assertEqual(group["overpayment_amount"], Decimal("1000.00"))

    def test_purchase_item_product_update_moves_stock_to_new_product(self):
        new_product = Product.objects.create(name="Груша")
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-01")
        item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("25.00"),
        )

        response = self.client.post(
            reverse("inventory:purchase_item_product_update", args=[item.id]),
            {"product": new_product.id},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Товар закупки успешно обновлен.")
        item.refresh_from_db()
        self.assertEqual(item.product_id, new_product.id)
        self.assertEqual(
            StoreStock.objects.get(store=self.store, product=self.product).quantity_kg,
            Decimal("0.000"),
        )
        self.assertEqual(
            StoreStock.objects.get(store=self.store, product=new_product).quantity_kg,
            Decimal("10.000"),
        )

    def test_purchase_item_product_update_moves_linked_sales_and_profitability_to_new_product(self):
        new_product = Product.objects.create(name="Груша")
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-01")
        item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("100.000"),
            purchase_price_per_kg=Decimal("10.00"),
        )
        sale = Sale.objects.create(
            store=self.store,
            date="2026-05-02",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        sale_item = SaleItem(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("30.000"),
            sale_price_per_kg=Decimal("50.00"),
        )
        sale_item._selected_purchase_item = item
        sale_item.save()
        batch = SaleItemBatch.objects.get(sale_item=sale_item)

        response = self.client.post(
            reverse("inventory:purchase_item_product_update", args=[item.id]),
            {"product": new_product.id},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        sale_item.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(item.product_id, new_product.id)
        self.assertEqual(sale_item.product_id, new_product.id)
        self.assertEqual(batch.purchase_item_id, item.id)
        self.assertEqual(
            StoreStock.objects.get(store=self.store, product=self.product).quantity_kg,
            Decimal("0.000"),
        )
        self.assertEqual(
            StoreStock.objects.get(store=self.store, product=new_product).quantity_kg,
            Decimal("70.000"),
        )

        old_product_rows = build_product_profitability_rows(store=self.store, product=self.product)
        new_product_rows = build_product_profitability_rows(store=self.store, product=new_product)
        self.assertEqual(old_product_rows, [])
        self.assertEqual(new_product_rows[0]["purchased_quantity"], Decimal("100.000"))
        self.assertEqual(new_product_rows[0]["sold_quantity"], Decimal("30.000"))
        self.assertEqual(new_product_rows[0]["stock_quantity"], Decimal("70.000"))
        self.assertEqual(new_product_rows[0]["revenue"], Decimal("1500.00"))
        self.assertEqual(new_product_rows[0]["sold_cost"], Decimal("300.00"))
        self.assertEqual(new_product_rows[0]["profit"], Decimal("1200.00"))

        purchase_profitability = build_purchase_item_profitability_map(purchase_item_ids=[item.id])
        self.assertEqual(purchase_profitability[item.id]["product_id"], new_product.id)
        self.assertEqual(purchase_profitability[item.id]["sold_quantity"], Decimal("30.000"))
        self.assertEqual(purchase_profitability[item.id]["stock_quantity"], Decimal("70.000"))

    def test_purchase_item_product_update_noops_when_product_is_unchanged(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-01")
        item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("25.00"),
        )

        response = self.client.post(
            reverse("inventory:purchase_item_product_update", args=[item.id]),
            {"product": self.product.id},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Товар закупки не изменился.")
        item.refresh_from_db()
        self.assertEqual(item.product_id, self.product.id)

    def test_purchase_item_product_update_rejects_multi_batch_sale_items(self):
        new_product = Product.objects.create(name="Груша")
        first_purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-01")
        first_item = PurchaseItem.objects.create(
            purchase=first_purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("100.000"),
            purchase_price_per_kg=Decimal("10.00"),
        )
        second_purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-02")
        second_item = PurchaseItem.objects.create(
            purchase=second_purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("100.000"),
            purchase_price_per_kg=Decimal("12.00"),
        )
        sale = Sale.objects.create(
            store=self.store,
            date="2026-05-03",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        sale_item = SaleItem(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("30.000"),
            sale_price_per_kg=Decimal("50.00"),
        )
        sale_item._selected_purchase_item = first_item
        sale_item.save()
        SaleItemBatch.objects.create(
            sale_item=sale_item,
            purchase_item=second_item,
            quantity=Decimal("5.000"),
            sale_price=Decimal("50.00"),
            total_amount=Decimal("250.00"),
        )

        response = self.client.post(
            reverse("inventory:purchase_item_product_update", args=[first_item.id]),
            {"product": new_product.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "распределена по нескольким партиям")
        first_item.refresh_from_db()
        sale_item.refresh_from_db()
        self.assertEqual(first_item.product_id, self.product.id)
        self.assertEqual(sale_item.product_id, self.product.id)

    def test_purchase_delete_is_soft_and_keeps_sales_history(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-01")
        item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("25.00"),
        )
        sale = Sale.objects.create(
            store=self.store,
            date="2026-05-02",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        sale_item = SaleItem.objects.create(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("4.000"),
            sale_price_per_kg=Decimal("40.00"),
        )

        purchase.delete()

        purchase.refresh_from_db()
        item.refresh_from_db()
        sale_item.refresh_from_db()
        stock = StoreStock.objects.get(store=self.store, product=self.product)

        self.assertIsNotNone(purchase.deleted_at)
        self.assertEqual(Purchase.objects.count(), 1)
        self.assertEqual(PurchaseItem.objects.count(), 1)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(SaleItem.objects.count(), 1)
        self.assertEqual(SaleItemBatch.objects.filter(sale_item=sale_item, purchase_item=item).count(), 1)
        self.assertEqual(stock.quantity_kg, Decimal("0.000"))
        self.assertTrue(
            StockMovement.objects.filter(
                movement_type="adjustment_out",
                reference_note=f"Purchase soft delete #{purchase.id} item #{item.id}",
            ).exists()
        )

        purchase_response = self.client.get(reverse("inventory:purchase_list"))
        self.assertNotContains(purchase_response, "Поставщик 1")

        stock_response = self.client.get(reverse("inventory:stock_list"))
        self.assertEqual(stock_response.status_code, 200)
        self.assertNotContains(stock_response, "NaN")
        self.assertNotContains(stock_response, "undefined")

        profitability = build_purchase_item_profitability_map(purchase_item_ids=[item.id])
        self.assertEqual(profitability, {})

    def test_soft_deleted_purchase_is_not_used_for_new_fifo_sales(self):
        deleted_purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-01")
        deleted_item = PurchaseItem.objects.create(
            purchase=deleted_purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("5.000"),
            purchase_price_per_kg=Decimal("10.00"),
        )
        deleted_purchase.delete()

        active_purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-02")
        active_item = PurchaseItem.objects.create(
            purchase=active_purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("5.000"),
            purchase_price_per_kg=Decimal("20.00"),
        )

        sale = Sale.objects.create(
            store=self.store,
            date="2026-05-03",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        sale_item = SaleItem.objects.create(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("2.000"),
            sale_price_per_kg=Decimal("50.00"),
        )

        self.assertFalse(
            SaleItemBatch.objects.filter(sale_item=sale_item, purchase_item=deleted_item).exists()
        )
        self.assertTrue(
            SaleItemBatch.objects.filter(sale_item=sale_item, purchase_item=active_item).exists()
        )

    def test_admin_purchase_delete_soft_deletes_purchase(self):
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save(update_fields=["is_staff", "is_superuser"])
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-01")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("3.000"),
            purchase_price_per_kg=Decimal("10.00"),
        )

        response = self.client.post(
            reverse("admin:inventory_purchase_delete", args=[purchase.id]),
            {"post": "yes"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        purchase.refresh_from_db()
        self.assertIsNotNone(purchase.deleted_at)
        self.assertEqual(Purchase.objects.count(), 1)
        self.assertEqual(PurchaseItem.objects.count(), 1)

    def test_purchase_without_items_can_be_soft_deleted_safely(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-01")

        purchase.delete()

        purchase.refresh_from_db()
        self.assertIsNotNone(purchase.deleted_at)
        response = self.client.get(reverse("inventory:purchase_list"))
        self.assertEqual(response.status_code, 200)
