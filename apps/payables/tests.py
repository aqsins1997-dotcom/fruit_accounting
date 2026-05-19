from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
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

    def test_supplier_payment_auto_allocates_oldest_debts_and_stops_on_partial(self):
        purchase_old = Purchase.objects.create(supplier=self.supplier, date="2026-04-09")
        purchase_middle = Purchase.objects.create(supplier=self.supplier, date="2026-04-12")
        purchase_new = Purchase.objects.create(supplier=self.supplier, date="2026-04-13")

        PurchaseItem.objects.create(
            purchase=purchase_old,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("1.000"),
            purchase_price_per_kg=Decimal("48980.00"),
        )
        PurchaseItem.objects.create(
            purchase=purchase_middle,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("1.000"),
            purchase_price_per_kg=Decimal("131520.00"),
        )
        PurchaseItem.objects.create(
            purchase=purchase_new,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("1.000"),
            purchase_price_per_kg=Decimal("208600.00"),
        )
        CashRegister.objects.filter(store=self.store).update(balance=Decimal("500000.00"))

        payment = SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            date="2026-04-14",
            payment_method=SupplierPayment.PAYMENT_METHOD_TRANSFER,
            amount=Decimal("150000.00"),
        )

        allocations = list(
            SupplierPaymentAllocation.objects.filter(payment=payment)
            .order_by("purchase__date", "purchase_id")
            .values_list("purchase_id", "amount")
        )
        self.assertEqual(
            allocations,
            [
                (purchase_old.id, Decimal("48980.00")),
                (purchase_middle.id, Decimal("101020.00")),
            ],
        )

        response = self.client.get(reverse("payables:supplier_balances"), HTTP_HOST="localhost")
        group = response.context["supplier_groups"][0]
        self.assertEqual(group["purchase_total"], Decimal("389100.00"))
        self.assertEqual(group["paid_amount"], Decimal("150000.00"))
        self.assertEqual(group["remaining_amount"], Decimal("239100.00"))
        first_row, second_row, third_row = group["rows"]
        self.assertEqual(first_row["status"], "Оплачено")
        self.assertEqual(first_row["remaining_amount"], Decimal("0.00"))
        self.assertEqual(second_row["status"], "Частично оплачено")
        self.assertEqual(second_row["remaining_amount"], Decimal("30500.00"))
        self.assertEqual(third_row["status"], "Не оплачено")
        self.assertEqual(third_row["remaining_amount"], Decimal("208600.00"))

    def test_supplier_payment_cannot_exceed_remaining_debt(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("1.000"),
            purchase_price_per_kg=Decimal("100.00"),
        )

        with self.assertRaises(ValidationError):
            SupplierPayment.objects.create(
                supplier=self.supplier,
                store=self.store,
                date="2026-04-11",
                amount=Decimal("150.00"),
            )

        self.assertFalse(SupplierPayment.objects.exists())
        self.assertFalse(SupplierPaymentAllocation.objects.exists())

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

    def test_soft_deleted_purchase_is_excluded_from_supplier_debt(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("50.00"),
        )

        purchase.delete()

        response = self.client.get(reverse("payables:supplier_balances"), HTTP_HOST="localhost")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["supplier_groups"], [])
        self.assertEqual(response.context["summary"]["total_purchases"], Decimal("0.00"))
        self.assertEqual(response.context["summary"]["total_due"], Decimal("0.00"))
        self.assertEqual(Purchase.objects.count(), 1)
        self.assertEqual(PurchaseItem.objects.count(), 1)

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

    def test_supplier_payment_delete_is_safe_cancel_and_restores_cash_and_debt(self):
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

        payment.refresh_from_db()
        self.assertEqual(payment.status, SupplierPayment.STATUS_CANCELLED)
        self.assertEqual(SupplierPayment.objects.count(), 1)
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("5000.00"))
        self.assertTrue(SupplierPaymentAllocation.objects.exists())

        response = self.client.get(reverse("payables:supplier_balances"), HTTP_HOST="localhost")
        group = response.context["supplier_groups"][0]
        self.assertEqual(group["paid_amount"], Decimal("0.00"))
        self.assertEqual(group["remaining_amount"], Decimal("1000.00"))

    def test_supplier_payment_cancel_restores_debt_cash_and_keeps_history(self):
        purchase_old = Purchase.objects.create(supplier=self.supplier, date="2026-04-09")
        purchase_middle = Purchase.objects.create(supplier=self.supplier, date="2026-04-12")
        purchase_new = Purchase.objects.create(supplier=self.supplier, date="2026-04-13")
        for purchase, amount in (
            (purchase_old, Decimal("48980.00")),
            (purchase_middle, Decimal("131520.00")),
            (purchase_new, Decimal("208600.00")),
        ):
            PurchaseItem.objects.create(
                purchase=purchase,
                store=self.store,
                product=self.product,
                quantity_kg=Decimal("1.000"),
                purchase_price_per_kg=amount,
            )
        CashRegister.objects.filter(store=self.store).update(balance=Decimal("500000.00"))
        payment = SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            date="2026-04-14",
            amount=Decimal("150000.00"),
        )
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("350000.00"))

        response = self.client.post(
            reverse("payables:supplier_payment_cancel", args=[payment.id]),
            follow=True,
            HTTP_HOST="localhost",
        )

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SupplierPayment.STATUS_CANCELLED)
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("500000.00"))
        self.assertTrue(SupplierPaymentAllocation.objects.filter(payment=payment).exists())
        response = self.client.get(reverse("payables:supplier_balances"), HTTP_HOST="localhost")
        group = response.context["supplier_groups"][0]
        self.assertEqual(group["purchase_total"], Decimal("389100.00"))
        self.assertEqual(group["paid_amount"], Decimal("0.00"))
        self.assertEqual(group["remaining_amount"], Decimal("389100.00"))
        self.assertContains(response, self.supplier.name)

    def test_supplier_payment_update_rebuilds_allocations_and_cash(self):
        purchase_old = Purchase.objects.create(supplier=self.supplier, date="2026-04-09")
        purchase_middle = Purchase.objects.create(supplier=self.supplier, date="2026-04-12")
        purchase_new = Purchase.objects.create(supplier=self.supplier, date="2026-04-13")
        for purchase, amount in (
            (purchase_old, Decimal("48980.00")),
            (purchase_middle, Decimal("131520.00")),
            (purchase_new, Decimal("208600.00")),
        ):
            PurchaseItem.objects.create(
                purchase=purchase,
                store=self.store,
                product=self.product,
                quantity_kg=Decimal("1.000"),
                purchase_price_per_kg=amount,
            )
        CashRegister.objects.filter(store=self.store).update(balance=Decimal("500000.00"))
        payment = SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            date="2026-04-14",
            amount=Decimal("150000.00"),
        )

        response = self.client.post(
            reverse("payables:supplier_payment_update", args=[payment.id]),
            {
                "date": "2026-04-15",
                "payment_method": SupplierPayment.PAYMENT_METHOD_TRANSFER,
                "amount": "100000.00",
                "comment": "Исправленная сумма",
            },
            follow=True,
            HTTP_HOST="localhost",
        )

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal("100000.00"))
        self.assertEqual(payment.payment_method, SupplierPayment.PAYMENT_METHOD_TRANSFER)
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("500000.00"))
        allocations = list(
            SupplierPaymentAllocation.objects.filter(payment=payment)
            .order_by("purchase__date", "purchase_id")
            .values_list("purchase_id", "amount")
        )
        self.assertEqual(
            allocations,
            [
                (purchase_old.id, Decimal("48980.00")),
                (purchase_middle.id, Decimal("51020.00")),
            ],
        )
        response = self.client.get(reverse("payables:supplier_balances"), HTTP_HOST="localhost")
        group = response.context["supplier_groups"][0]
        self.assertEqual(group["paid_amount"], Decimal("100000.00"))
        self.assertEqual(group["remaining_amount"], Decimal("289100.00"))

    def test_supplier_payment_update_cannot_exceed_debt_with_current_payment(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("1.000"),
            purchase_price_per_kg=Decimal("100.00"),
        )
        payment = SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            date="2026-04-11",
            amount=Decimal("40.00"),
        )

        response = self.client.post(
            reverse("payables:supplier_payment_update", args=[payment.id]),
            {
                "date": "2026-04-12",
                "payment_method": SupplierPayment.PAYMENT_METHOD_CASH,
                "amount": "120.00",
                "comment": "Слишком много",
            },
            HTTP_HOST="localhost",
        )

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal("40.00"))
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("4960.00"))

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
        self.assertNotIn("purchase", response.context["form"].fields)

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

    def test_repair_supplier_payment_allocations_restores_missing_distribution(self):
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("50.00"),
        )
        payment = SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            date="2026-04-11",
            amount=Decimal("200.00"),
        )
        SupplierPaymentAllocation.objects.filter(payment=payment).delete()
        self.assertEqual(payment.allocations.count(), 0)

        stdout = StringIO()
        call_command("repair_supplier_payment_allocations", stdout=stdout)

        payment.refresh_from_db()
        allocations = list(payment.allocations.values_list("purchase_id", "amount"))
        self.assertEqual(allocations, [(purchase.id, Decimal("200.00"))])

        response = self.client.get(reverse("payables:supplier_balances"), HTTP_HOST="localhost")
        group = response.context["supplier_groups"][0]
        self.assertEqual(group["paid_amount"], Decimal("200.00"))
        self.assertEqual(group["remaining_amount"], Decimal("300.00"))
        self.assertIn("Found payments without allocations: 1", stdout.getvalue())

    def test_non_cash_supplier_payment_does_not_reduce_cash_and_works_without_balance(self):
        CashRegister.objects.filter(store=self.store).update(balance=Decimal("0.00"))
        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-10")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("50.00"),
        )

        payment = SupplierPayment.objects.create(
            supplier=self.supplier,
            store=self.store,
            date="2026-04-11",
            payment_method=SupplierPayment.PAYMENT_METHOD_TRANSFER,
            amount=Decimal("200.00"),
        )

        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("0.00"))
        allocations = list(payment.allocations.values_list("purchase_id", "amount"))
        self.assertEqual(allocations, [(purchase.id, Decimal("200.00"))])
