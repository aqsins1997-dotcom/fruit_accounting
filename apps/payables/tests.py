from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.core.models import Product, Store, Supplier
from apps.inventory.models import Purchase, PurchaseItem

from .models import SupplierPayment


class SupplierBalancesViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="tester",
            password="secret123",
        )
        self.client = Client()
        self.client.force_login(self.user)

        self.store = Store.objects.create(name="Основной")
        self.supplier = Supplier.objects.create(name="Поставщик 1")
        self.product = Product.objects.create(name="Товар 1")

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

        SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            date="2026-04-13",
            amount=Decimal("250.00"),
        )

        response = self.client.get(reverse("payables:supplier_balances"), HTTP_HOST="localhost")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Поставщик 1")
        self.assertContains(response, "Закупка №")

        groups = response.context["supplier_groups"]
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["purchase_total"], Decimal("350.00"))
        self.assertEqual(group["paid_amount"], Decimal("250.00"))
        self.assertEqual(group["remaining_amount"], Decimal("100.00"))

        first_row, second_row = group["rows"]
        self.assertEqual(first_row["purchase_total"], Decimal("200.00"))
        self.assertEqual(first_row["paid_amount"], Decimal("200.00"))
        self.assertEqual(first_row["remaining_amount"], Decimal("0.00"))
        self.assertEqual(first_row["status"], "Оплачено")

        self.assertEqual(second_row["purchase_total"], Decimal("150.00"))
        self.assertEqual(second_row["paid_amount"], Decimal("50.00"))
        self.assertEqual(second_row["remaining_amount"], Decimal("100.00"))
        self.assertEqual(second_row["status"], "Частично оплачено")

    def test_supplier_payment_can_be_bound_to_purchase(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("3.000"),
            purchase_price_per_kg=Decimal("40.00"),
        )

        payment = SupplierPayment(
            supplier=self.supplier,
            store=self.store,
            purchase=purchase,
            date="2026-04-11",
            amount=Decimal("60.00"),
        )

        payment.full_clean()

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

        SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            purchase=purchase_one,
            date="2026-04-13",
            amount=Decimal("50.00"),
        )
        SupplierPayment.objects.create(
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
