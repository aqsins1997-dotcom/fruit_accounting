from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from apps.core.models import Product, Store, Supplier
from apps.sales.models import Sale, SaleItem

from .models import Purchase, PurchaseItem, StoreStock


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
