from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from apps.core.models import Product, Store, Supplier
from apps.inventory.models import Purchase, PurchaseItem
from apps.sales.models import CashRegister

from .models import SupplierPayment, SupplierPaymentAllocation


class SupplierBalancesViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="tester",
            password="secret123",
        )
        self.client = Client()
        self.client.force_login(self.user)

        self.store = Store.objects.create(name="Основной")
        self.other_store = Store.objects.create(name="Резерв")
        self.supplier = Supplier.objects.create(name="Поставщик 1")
        self.other_supplier = Supplier.objects.create(name="Поставщик 2")
        self.product = Product.objects.create(name="Товар 1")
        CashRegister.objects.create(store=self.store, balance=Decimal("5000.00"))
        CashRegister.objects.create(store=self.other_store, balance=Decimal("5000.00"))

    def test_supplier_balances_renders_and_allocates_general_payment_fifo(self):
        purchase_one = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        purchase_two = Purchase.objects.create(supplier=self.supplier, date="2026-04-12")

        PurchaseItem.objects.create(
            purchase=purchase_one,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("20.00"),
        )
        PurchaseItem.objects.create(
            purchase=purchase_two,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("5.000"),
            purchase_price_per_kg=Decimal("30.00"),
        )

        payment = SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            date="2026-04-13",
            amount=Decimal("250.00"),
        )

        response = self.client.get(reverse("payables:supplier_balances"), HTTP_HOST="localhost")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Поставщик 1")
        self.assertContains(response, "Закупка №")

        group = response.context["supplier_groups"][0]
        self.assertEqual(group["purchase_total"], Decimal("350.00"))
        self.assertEqual(group["paid_amount"], Decimal("250.00"))
        self.assertEqual(group["remaining_amount"], Decimal("100.00"))

        first_row, second_row = group["rows"]
        self.assertEqual(first_row["paid_amount"], Decimal("200.00"))
        self.assertEqual(second_row["paid_amount"], Decimal("50.00"))
        self.assertIn("supplier=1", first_row["payment_url"])

        allocations = list(
            SupplierPaymentAllocation.objects.filter(payment=payment).values_list("purchase_id", "amount")
        )
        self.assertEqual(allocations, [(purchase_one.id, Decimal("200.00")), (purchase_two.id, Decimal("50.00"))])

    def test_supplier_payment_can_be_bound_to_purchase(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("3.000"),
            purchase_price_per_kg=Decimal("40.00"),
        )

        payment = SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            purchase=purchase,
            date="2026-04-11",
            amount=Decimal("60.00"),
        )

        allocation = SupplierPaymentAllocation.objects.get(payment=payment)
        self.assertEqual(allocation.purchase_id, purchase.id)
        self.assertEqual(allocation.amount, Decimal("60.00"))

    def test_supplier_payment_decreases_cash_and_supplier_debt(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("20.000"),
            purchase_price_per_kg=Decimal("100.00"),
        )

        payment = SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            purchase=purchase,
            date="2026-04-11",
            amount=Decimal("1000.00"),
        )

        cash_register = CashRegister.objects.get(store=self.store)
        self.assertEqual(cash_register.balance, Decimal("4000.00"))

        response = self.client.get(reverse("payables:supplier_balances"), HTTP_HOST="localhost")
        self.assertEqual(response.status_code, 200)

        group = response.context["supplier_groups"][0]
        self.assertEqual(group["purchase_total"], Decimal("2000.00"))
        self.assertEqual(group["paid_amount"], Decimal("1000.00"))
        self.assertEqual(group["remaining_amount"], Decimal("1000.00"))

        payment.refresh_from_db()
        cash_register.refresh_from_db()
        self.assertEqual(payment.amount, Decimal("1000.00"))
        self.assertEqual(cash_register.balance, Decimal("4000.00"))

    def test_supplier_payment_cannot_exceed_cash_balance_and_rolls_back(self):
        CashRegister.objects.filter(store=self.store).update(balance=Decimal("500.00"))
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("20.000"),
            purchase_price_per_kg=Decimal("100.00"),
        )

        with self.assertRaises(ValidationError):
            SupplierPayment.objects.create(
                supplier=self.supplier,
                store=self.store,
                purchase=purchase,
                date="2026-04-11",
                amount=Decimal("1000.00"),
            )

        self.assertFalse(SupplierPayment.objects.exists())
        self.assertFalse(SupplierPaymentAllocation.objects.exists())
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("500.00"))

    def test_supplier_payment_delete_restores_cash_and_debt(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("100.00"),
        )
        payment = SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            purchase=purchase,
            date="2026-04-11",
            amount=Decimal("400.00"),
        )

        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("4600.00"))

        payment.delete()

        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("5000.00"))
        self.assertFalse(SupplierPaymentAllocation.objects.exists())

        response = self.client.get(reverse("payables:supplier_balances"), HTTP_HOST="localhost")
        group = response.context["supplier_groups"][0]
        self.assertEqual(group["paid_amount"], Decimal("0.00"))
        self.assertEqual(group["remaining_amount"], Decimal("1000.00"))

    def test_allocations_rebuild_when_purchase_item_changes(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("20.00"),
        )
        payment = SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            purchase=purchase,
            date="2026-04-11",
            amount=Decimal("150.00"),
        )

        item.quantity_kg = Decimal("5.000")
        item.save()

        allocation = SupplierPaymentAllocation.objects.get(payment=payment)
        self.assertEqual(allocation.amount, Decimal("100.00"))

    def test_allocations_rebuild_when_purchase_item_price_changes(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("20.00"),
        )
        payment = SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            purchase=purchase,
            date="2026-04-11",
            amount=Decimal("150.00"),
        )

        item.purchase_price_per_kg = Decimal("10.00")
        item.save()

        allocation = SupplierPaymentAllocation.objects.get(payment=payment)
        self.assertEqual(allocation.amount, Decimal("100.00"))

    def test_purchase_specific_payment_is_not_double_counted_with_general_payment(self):
        purchase_one = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        purchase_two = Purchase.objects.create(supplier=self.supplier, date="2026-04-12")

        PurchaseItem.objects.create(
            purchase=purchase_one,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("4.000"),
            purchase_price_per_kg=Decimal("30.00"),
        )
        PurchaseItem.objects.create(
            purchase=purchase_two,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("4.000"),
            purchase_price_per_kg=Decimal("20.00"),
        )

        specific_payment = SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            purchase=purchase_one,
            date="2026-04-13",
            amount=Decimal("50.00"),
        )
        general_payment = SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            date="2026-04-14",
            amount=Decimal("100.00"),
        )

        response = self.client.get(reverse("payables:supplier_balances"), HTTP_HOST="localhost")

        self.assertEqual(response.status_code, 200)
        group = response.context["supplier_groups"][0]
        first_row, second_row = group["rows"]

        self.assertEqual(first_row["purchase_total"], Decimal("120.00"))
        self.assertEqual(first_row["paid_amount"], Decimal("120.00"))
        self.assertEqual(first_row["remaining_amount"], Decimal("0.00"))

        self.assertEqual(second_row["purchase_total"], Decimal("80.00"))
        self.assertEqual(second_row["paid_amount"], Decimal("30.00"))
        self.assertEqual(second_row["remaining_amount"], Decimal("50.00"))

        self.assertEqual(group["paid_amount"], Decimal("150.00"))
        self.assertEqual(group["remaining_amount"], Decimal("50.00"))

        specific_allocations = list(
            SupplierPaymentAllocation.objects.filter(payment=specific_payment).values_list("purchase_id", "amount")
        )
        general_allocations = list(
            SupplierPaymentAllocation.objects.filter(payment=general_payment).values_list("purchase_id", "amount")
        )
        self.assertEqual(specific_allocations, [(purchase_one.id, Decimal("50.00"))])
        self.assertEqual(general_allocations, [(purchase_one.id, Decimal("70.00")), (purchase_two.id, Decimal("30.00"))])

    def test_supplier_payment_form_page_opens_with_prefill(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("1.000"),
            purchase_price_per_kg=Decimal("10.00"),
        )

        response = self.client.get(
            reverse("payables:supplier_payment_create"),
            {"supplier": self.supplier.id, "store": self.store.id, "purchase": purchase.id},
            HTTP_HOST="localhost",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(response.context["form"].initial["supplier"]), str(self.supplier.id))
        self.assertEqual(str(response.context["form"].initial["store"]), str(self.store.id))
        self.assertEqual(str(response.context["form"].initial["purchase"]), str(purchase.id))

    def test_supplier_balance_filter_by_supplier_and_status(self):
        unpaid_purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        paid_purchase = Purchase.objects.create(supplier=self.other_supplier, date="2026-04-11")

        PurchaseItem.objects.create(
            purchase=unpaid_purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("2.000"),
            purchase_price_per_kg=Decimal("50.00"),
        )
        PurchaseItem.objects.create(
            purchase=paid_purchase,
            store=self.other_store,
            product=self.product,
            quantity_kg=Decimal("1.000"),
            purchase_price_per_kg=Decimal("70.00"),
        )

        SupplierPayment.objects.create(
            supplier=self.other_supplier,
            store=self.other_store,
            purchase=paid_purchase,
            date="2026-04-12",
            amount=Decimal("70.00"),
        )

        response = self.client.get(
            reverse("payables:supplier_balances"),
            {"supplier": self.supplier.id, "status": "unpaid"},
            HTTP_HOST="localhost",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["supplier_groups"]), 1)
        group = response.context["supplier_groups"][0]
        self.assertEqual(group["supplier_name"], self.supplier.name)
        self.assertEqual(len(group["rows"]), 1)
        self.assertEqual(group["rows"][0]["status"], "Не оплачено")

    def test_supplier_payment_history_page_renders(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("2.000"),
            purchase_price_per_kg=Decimal("50.00"),
        )
        SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            purchase=purchase,
            date="2026-04-11",
            amount=Decimal("40.00"),
        )

        response = self.client.get(reverse("payables:supplier_payment_list"), HTTP_HOST="localhost")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.supplier.name)
