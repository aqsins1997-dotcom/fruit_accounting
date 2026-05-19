from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from apps.core.models import Customer, Product, Store, Supplier
from apps.credits.models import ClientDebtPayment
from apps.expenses.models import ExpenseCategory, SalaryPayment, StoreExpense
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


class DailyReportAndAuditTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="audit-user", password="secret123")
        self.client = Client()
        self.client.force_login(self.user)

        self.store = Store.objects.create(name="Audit Store")
        self.product = Product.objects.create(name="Audit Product")
        self.customer = Customer.objects.create(name="Audit Customer")
        self.supplier = Supplier.objects.create(name="Audit Supplier")
        self.category = ExpenseCategory.objects.create(name="Audit Expense")

        purchase = Purchase.objects.create(supplier=self.supplier, date="2026-05-02")
        self.purchase_item = PurchaseItem.objects.create(
            purchase=purchase,
            store=self.store,
            product=self.product,
            quantity_kg=Decimal("20.000"),
            purchase_price_per_kg=Decimal("100.00"),
        )

    def test_daily_report_shows_sales_expenses_and_profit(self):
        cash_sale = Sale.objects.create(
            store=self.store,
            date="2026-05-03",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        SaleItem.objects.create(
            sale=cash_sale,
            product=self.product,
            quantity_kg=Decimal("5.000"),
            sale_price_per_kg=Decimal("150.00"),
        )
        credit_sale = Sale.objects.create(
            store=self.store,
            date="2026-05-03",
            payment_type=Sale.PAYMENT_TYPE_CREDIT,
            customer=self.customer,
        )
        SaleItem.objects.create(
            sale=credit_sale,
            product=self.product,
            quantity_kg=Decimal("2.000"),
            sale_price_per_kg=Decimal("200.00"),
        )
        ClientDebtPayment.objects.create(
            store=self.store,
            client=self.customer,
            amount=Decimal("100.00"),
            payment_method=ClientDebtPayment.PAYMENT_METHOD_CASH,
            paid_at="2026-05-03",
        )
        StoreExpense.objects.create(
            store=self.store,
            category=self.category,
            date="2026-05-03",
            amount=Decimal("50.00"),
        )
        SalaryPayment.objects.create(
            store=self.store,
            seller=self.store.sellers.create(name="Audit Seller"),
            date="2026-05-03",
            amount=Decimal("25.00"),
        )

        response = self.client.get(
            reverse("reports:daily_store_report"),
            {"store": self.store.id, "date": "2026-05-03"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cash_sales_total"], Decimal("750.00"))
        self.assertEqual(response.context["credit_sales_total"], Decimal("400.00"))
        self.assertEqual(response.context["total_sales_amount"], Decimal("1150.00"))
        self.assertEqual(response.context["total_cost_amount"], Decimal("700.00"))
        self.assertEqual(response.context["gross_profit_amount"], Decimal("450.00"))
        self.assertEqual(response.context["total_business_expenses"], Decimal("75.00"))
        self.assertEqual(response.context["net_profit_amount"], Decimal("375.00"))
        self.assertEqual(response.context["current_credit_debt"], Decimal("300.00"))

    def test_audit_accounting_integrity_returns_zero_for_consistent_data(self):
        sale = Sale.objects.create(
            store=self.store,
            date="2026-05-03",
            payment_type=Sale.PAYMENT_TYPE_CASH,
        )
        SaleItem.objects.create(
            sale=sale,
            product=self.product,
            quantity_kg=Decimal("5.000"),
            sale_price_per_kg=Decimal("150.00"),
        )

        stdout = StringIO()
        call_command("audit_accounting_integrity", stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("READ ONLY AUDIT", output)
        self.assertIn("SUMMARY", output)
        self.assertIn("CRITICAL: 0", output)

    def test_audit_accounting_integrity_returns_one_for_broken_client_payment(self):
        credit_sale = Sale.objects.create(
            store=self.store,
            date="2026-05-03",
            payment_type=Sale.PAYMENT_TYPE_CREDIT,
            customer=self.customer,
        )
        SaleItem.objects.create(
            sale=credit_sale,
            product=self.product,
            quantity_kg=Decimal("5.000"),
            sale_price_per_kg=Decimal("150.00"),
        )
        payment = ClientDebtPayment.objects.create(
            store=self.store,
            client=self.customer,
            amount=Decimal("200.00"),
            payment_method=ClientDebtPayment.PAYMENT_METHOD_CASH,
            paid_at="2026-05-03",
        )
        for allocation in list(payment.allocations.all()):
            allocation.delete()

        stdout = StringIO()
        with self.assertRaises(SystemExit) as exc:
            call_command("audit_accounting_integrity", stdout=stdout)

        self.assertEqual(exc.exception.code, 1)
        self.assertEqual(payment.allocations.count(), 0)
        self.assertIn("has no allocations", stdout.getvalue())
