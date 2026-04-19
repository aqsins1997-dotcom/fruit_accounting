from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.core.models import Product, Store, Supplier
from apps.inventory.models import Purchase, PurchaseItem, StoreStock

from .models import CashRegister, Sale, SaleItem


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
