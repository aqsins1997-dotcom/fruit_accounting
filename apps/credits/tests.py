from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.core.models import Customer, Product, Store, Supplier
from apps.inventory.models import Purchase, PurchaseItem
from apps.sales.models import Sale, SaleItem

from .models import CreditPayment


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

    def test_credit_payment_can_be_created_without_admin(self):
        response = self.client.post(
            reverse("credits:credit_payment_create", args=[self.credit.id]),
            {"date": "2026-04-19", "amount": "20.00", "comment": "Частичная оплата"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CreditPayment.objects.count(), 1)
