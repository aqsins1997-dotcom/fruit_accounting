import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.core.models import Customer, Product, Store, Supplier
from apps.inventory.models import Purchase, PurchaseItem
from apps.sales.models import CashRegister, Sale, SaleItem

from .models import ClientDebtPayment, CreditPayment


class CreditNoAdminViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="credits-user", password="secret123")
        self.client = Client()
        self.client.force_login(self.user)
        self.store = Store.objects.create(name="Магазин 1")
        self.customer = Customer.objects.create(name="Покупатель 1")
        self.supplier = Supplier.objects.create(name="Поставщик 1")
        self.product = Product.objects.create(name="Апельсин")

        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-04-18")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("10.000"),
            purchase_price_per_kg=Decimal("12.00"),
        )

        self.sale = Sale.objects.create(
            store=self.store,
            date="2026-04-19",
            payment_type=Sale.PAYMENT_TYPE_CREDIT,
            customer=self.customer,
            comment="Продажа в долг",
        )
        SaleItem.objects.create(
            sale=self.sale,
            product=self.product,
            quantity_kg=Decimal("2.000"),
            sale_price_per_kg=Decimal("30.00"),
        )
        self.credit = self.sale.credit

    def test_credit_payment_list_renders(self):
        response = self.client.get(reverse("credits:credit_payment_list"))
        self.assertEqual(response.status_code, 200)

    def test_client_debt_payment_can_be_created_without_admin(self):
        response = self.client.post(
            reverse("credits:client_debt_payment_create"),
            {
                "store": self.store.id,
                "client": self.customer.id,
                "amount": "20.00",
                "payment_method": ClientDebtPayment.PAYMENT_METHOD_CASH,
                "paid_at": "2026-04-19",
                "comment": "Частичная оплата",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ClientDebtPayment.objects.count(), 1)
        self.assertEqual(CreditPayment.objects.count(), 1)

        self.credit.refresh_from_db()
        self.assertEqual(self.credit.remaining_amount, Decimal("40.00"))
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("20.00"))

    def test_debt_payment_history_shows_method_and_employee(self):
        self.client.post(
            reverse("credits:client_debt_payment_create"),
            {
                "store": self.store.id,
                "client": self.customer.id,
                "amount": "20.00",
                "payment_method": ClientDebtPayment.PAYMENT_METHOD_CARD,
                "paid_at": "2026-04-19",
                "comment": "Карта",
            },
        )

        response = self.client.get(reverse("credits:credit_payment_list"))
        self.assertContains(response, "Покупатель 1")
        self.assertContains(response, "Карта")
        self.assertContains(response, "credits-user")

    def test_debtors_page_has_accept_payment_button(self):
        response = self.client.get(reverse("reports:debtors_report"))
        self.assertContains(response, "Принять оплату")
        self.assertContains(response, reverse("credits:client_debt_payment_create"))

    def test_overpayment_is_rejected(self):
        response = self.client.post(
            reverse("credits:client_debt_payment_create"),
            {
                "store": self.store.id,
                "client": self.customer.id,
                "amount": "70.00",
                "payment_method": ClientDebtPayment.PAYMENT_METHOD_TRANSFER,
                "paid_at": "2026-04-19",
                "comment": "Переплата",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Сумма оплаты не может быть больше текущего долга клиента")
        self.assertEqual(ClientDebtPayment.objects.count(), 0)
        self.assertEqual(CreditPayment.objects.count(), 0)
        self.assertEqual(CashRegister.objects.count(), 0)

    def test_full_payment_removes_customer_from_debtors(self):
        self.client.post(
            reverse("credits:client_debt_payment_create"),
            {
                "store": self.store.id,
                "client": self.customer.id,
                "amount": "60.00",
                "payment_method": ClientDebtPayment.PAYMENT_METHOD_CASH,
                "paid_at": "2026-04-19",
                "comment": "Полная оплата",
            },
        )

        response = self.client.get(reverse("reports:debtors_report"))
        self.assertContains(response, "Активных долгов нет.")
        self.assertContains(self.client.get(reverse("credits:credit_payment_list")), "Полная оплата")

    def test_client_payment_cancel_restores_debt_and_cash_without_deleting_history(self):
        payment = ClientDebtPayment.objects.create(
            store=self.store,
            client=self.customer,
            amount=Decimal("60.00"),
            payment_method=ClientDebtPayment.PAYMENT_METHOD_CASH,
            paid_at="2026-04-19",
            employee=self.user,
        )
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("60.00"))
        self.credit.refresh_from_db()
        self.assertEqual(self.credit.remaining_amount, Decimal("0.00"))

        response = self.client.post(
            reverse("credits:client_debt_payment_cancel", args=[payment.id]),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.credit.refresh_from_db()
        self.assertEqual(payment.status, ClientDebtPayment.STATUS_CANCELLED)
        self.assertEqual(ClientDebtPayment.objects.count(), 1)
        self.assertEqual(CreditPayment.objects.count(), 1)
        self.assertEqual(self.credit.remaining_amount, Decimal("60.00"))
        self.assertEqual(self.credit.status, self.credit.STATUS_UNPAID)
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("0.00"))
        self.assertContains(response, "Отменена")

    def test_client_payment_update_reallocates_debt_and_cash(self):
        payment = ClientDebtPayment.objects.create(
            store=self.store,
            client=self.customer,
            amount=Decimal("60.00"),
            payment_method=ClientDebtPayment.PAYMENT_METHOD_CASH,
            paid_at="2026-04-19",
            employee=self.user,
        )

        response = self.client.post(
            reverse("credits:client_debt_payment_update", args=[payment.id]),
            {
                "amount": "20.00",
                "payment_method": ClientDebtPayment.PAYMENT_METHOD_TRANSFER,
                "paid_at": "2026-04-20",
                "comment": "Исправленная сумма",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.credit.refresh_from_db()
        self.assertEqual(payment.amount, Decimal("20.00"))
        self.assertEqual(payment.payment_method, ClientDebtPayment.PAYMENT_METHOD_TRANSFER)
        self.assertEqual(payment.allocations.count(), 1)
        self.assertEqual(payment.allocations.get().amount, Decimal("20.00"))
        self.assertEqual(self.credit.remaining_amount, Decimal("40.00"))
        self.assertEqual(self.credit.status, self.credit.STATUS_PARTIAL)
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("20.00"))

    def test_client_payment_update_cannot_exceed_debt_with_current_payment(self):
        payment = ClientDebtPayment.objects.create(
            store=self.store,
            client=self.customer,
            amount=Decimal("20.00"),
            payment_method=ClientDebtPayment.PAYMENT_METHOD_CASH,
            paid_at="2026-04-19",
            employee=self.user,
        )

        response = self.client.post(
            reverse("credits:client_debt_payment_update", args=[payment.id]),
            {
                "amount": "70.00",
                "payment_method": ClientDebtPayment.PAYMENT_METHOD_CASH,
                "paid_at": "2026-04-20",
                "comment": "Слишком много",
            },
        )

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.credit.refresh_from_db()
        self.assertEqual(payment.amount, Decimal("20.00"))
        self.assertEqual(self.credit.remaining_amount, Decimal("40.00"))
        self.assertEqual(CashRegister.objects.get(store=self.store).balance, Decimal("20.00"))

    def test_client_debt_api_routes(self):
        debt_response = self.client.get(
            reverse("credits:api_client_debt"),
            {"store": self.store.id, "client": self.customer.id},
        )
        self.assertEqual(debt_response.status_code, 200)
        self.assertEqual(debt_response.json()["debt"], "60.00")

        debtors_response = self.client.get(reverse("credits:api_debtors"))
        self.assertEqual(debtors_response.status_code, 200)
        self.assertEqual(debtors_response.json()["results"][0]["debt"], "60.00")

        create_response = self.client.post(
            reverse("credits:api_client_payment_create"),
            data=json.dumps(
                {
                    "store": self.store.id,
                    "client": self.customer.id,
                    "amount": "20.00",
                    "payment_method": ClientDebtPayment.PAYMENT_METHOD_TRANSFER,
                    "paid_at": "2026-04-19",
                    "comment": "API",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(create_response.status_code, 201)

        history_response = self.client.get(reverse("credits:api_client_payment_history"))
        self.assertEqual(history_response.status_code, 200)
        self.assertEqual(history_response.json()["results"][0]["comment"], "API")
