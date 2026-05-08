from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.core.models import Product, Store, Supplier
from apps.inventory.models import Purchase, PurchaseItem
from apps.sales.models import Sale, SaleItem


class ProductProfitabilityReportTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="report-user", password="secret123")
        self.client = Client()
        self.client.force_login(self.user)

        self.store = Store.objects.create(name="Алик")
        self.product = Product.objects.create(name="клубника")
        supplier = Supplier.objects.create(name="Поставщик")

        purchase = Purchase.objects.create(supplier=supplier, date="2026-05-01")
        PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("584.000"),
            purchase_price_per_kg=Decimal("270.00"),
        )

        for quantity, price in (
            (Decimal("100.000"), Decimal("350.00")),
            (Decimal("50.000"), Decimal("400.00")),
            (Decimal("30.000"), Decimal("300.00")),
        ):
            sale = Sale.objects.create(
                store=self.store,
                date="2026-05-02",
                payment_type=Sale.PAYMENT_TYPE_CASH,
            )
            SaleItem.objects.create(
                sale=sale,
                product=self.product,
                quantity_kg=quantity,
                sale_price_per_kg=price,
            )

    def test_product_profitability_page_renders(self):
        response = self.client.get(reverse("reports:product_profitability_report"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Аналитика продаж товаров")
        self.assertContains(response, "Средняя цена продажи")

    def test_product_profitability_api_calculates_average_sale_price(self):
        response = self.client.get(
            reverse("reports:product_profitability_data"),
            {"store": self.store.id, "product": self.product.id},
        )
        self.assertEqual(response.status_code, 200)

        row = response.json()["results"][0]
        self.assertEqual(row["purchased"], "584.000")
        self.assertEqual(row["sold"], "180.000")
        self.assertEqual(row["stock"], "404.000")
        self.assertEqual(row["average_purchase_price"], "270.00")
        self.assertEqual(row["average_sale_price"], "355.56")
        self.assertEqual(row["revenue"], "64000.00")
        self.assertEqual(row["sold_cost"], "48600.00")
        self.assertEqual(row["profit"], "15400.00")
        self.assertEqual(row["margin_per_unit"], "85.56")
