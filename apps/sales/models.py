from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import Product, Store, Customer
from apps.inventory.models import StoreStock, StockMovement


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class CashRegister(TimeStampedModel):
    store = models.OneToOneField(
        Store,
        on_delete=models.CASCADE,
        related_name="cash_register",
        verbose_name="Магазин",
    )
    balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Баланс кассы",
    )

    class Meta:
        verbose_name = "Касса магазина"
        verbose_name_plural = "Кассы магазинов"
        ordering = ["store__name"]
        indexes = [
            models.Index(fields=["balance"]),
        ]

    def __str__(self):
        return f"{self.store} | Касса | {self.balance}"


class Sale(TimeStampedModel):
    PAYMENT_TYPE_CASH = "cash"
    PAYMENT_TYPE_CREDIT = "credit"

    PAYMENT_TYPE_CHOICES = (
        (PAYMENT_TYPE_CASH, "Наличные"),
        (PAYMENT_TYPE_CREDIT, "Кредит"),
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        related_name="sales",
        verbose_name="Магазин",
    )
    date = models.DateField(verbose_name="Дата продажи")
    payment_type = models.CharField(
        max_length=20,
        choices=PAYMENT_TYPE_CHOICES,
        default=PAYMENT_TYPE_CASH,
        verbose_name="Тип оплаты",
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name="sales",
        blank=True,
        null=True,
        verbose_name="Клиент",
    )
    comment = models.TextField(blank=True, verbose_name="Комментарий")
    total_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Сумма продажи",
    )
    total_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Себестоимость",
    )
    total_profit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Прибыль",
    )

    class Meta:
        verbose_name = "Продажа"
        verbose_name_plural = "Продажи"
        ordering = ["-date", "-id"]
        indexes = [
            models.Index(fields=["store", "date"]),
            models.Index(fields=["payment_type", "date"]),
        ]

    def __str__(self):
        return f"Продажа #{self.id} | {self.store} | {self.date}"

    def clean(self):
        if self.payment_type == self.PAYMENT_TYPE_CREDIT and not self.customer:
            raise ValidationError({"customer": "Для продажи в кредит нужно указать клиента."})

        if self.payment_type == self.PAYMENT_TYPE_CASH and self.customer:
            raise ValidationError({"customer": "Для продажи за наличные клиент не нужен."})

    def sync_credit(self):
        if self.payment_type != self.PAYMENT_TYPE_CREDIT:
            if hasattr(self, "credit"):
                self.credit.delete()
            return

        from apps.credits.models import Credit

        credit, _ = Credit.objects.get_or_create(
            sale=self,
            defaults={
                "customer": self.customer,
                "store": self.store,
                "original_amount": self.total_amount,
                "remaining_amount": self.total_amount,
                "comment": self.comment,
            },
        )

        paid_amount = Decimal("0.00")
        if credit.pk:
            paid_amount = sum((payment.amount for payment in credit.payments.all()), Decimal("0.00"))

        credit.customer = self.customer
        credit.store = self.store
        credit.original_amount = self.total_amount
        credit.remaining_amount = self.total_amount - paid_amount
        if credit.remaining_amount < 0:
            credit.remaining_amount = Decimal("0.00")
        credit.comment = self.comment
        credit.save()

        credit.recalculate()

    def recalculate_totals(self):
        items = self.items.all()
        total_amount = sum((item.line_total for item in items), Decimal("0.00"))
        total_cost = sum((item.line_cost_total for item in items), Decimal("0.00"))
        total_profit = sum((item.profit for item in items), Decimal("0.00"))

        self.total_amount = total_amount
        self.total_cost = total_cost
        self.total_profit = total_profit
        self.save(update_fields=["total_amount", "total_cost", "total_profit", "updated_at"])

        self.sync_credit()


class SaleItem(TimeStampedModel):
    sale = models.ForeignKey(
        Sale,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="Продажа",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="sale_items",
        verbose_name="Товар",
    )
    quantity_kg = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        verbose_name="Количество (кг)",
    )
    sale_price_per_kg = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Цена продажи за кг",
    )
    cost_price_per_kg = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Себестоимость за кг",
    )
    line_total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Сумма строки",
    )
    line_cost_total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Себестоимость строки",
    )
    profit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Прибыль",
    )

    class Meta:
        verbose_name = "Строка продажи"
        verbose_name_plural = "Строки продаж"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["sale", "product"]),
        ]

    def __str__(self):
        return f"{self.sale} | {self.product} | {self.quantity_kg} кг"

    def clean(self):
        if self.quantity_kg is not None and self.quantity_kg <= 0:
            raise ValidationError({"quantity_kg": "Количество должно быть больше 0."})

        if self.sale_price_per_kg is not None and self.sale_price_per_kg < 0:
            raise ValidationError({"sale_price_per_kg": "Цена продажи не может быть отрицательной."})

        if self.sale_id and self.product_id:
            stock = StoreStock.objects.filter(
                store=self.sale.store,
                product=self.product,
            ).first()

            available_qty = stock.quantity_kg if stock else Decimal("0.000")

            if self.pk:
                old = SaleItem.objects.get(pk=self.pk)
                available_qty += old.quantity_kg

            if self.quantity_kg is not None and self.quantity_kg > available_qty:
                raise ValidationError(
                    {"quantity_kg": f"Недостаточно остатка. Доступно: {available_qty} кг."}
                )

    def save(self, *args, **kwargs):
        self.full_clean()

        is_new = self.pk is None

        old_quantity = Decimal("0.000")
        old_line_total = Decimal("0.00")
        old_sale_store_id = None
        old_product_id = None

        if not is_new:
            old = SaleItem.objects.get(pk=self.pk)
            old_quantity = old.quantity_kg
            old_line_total = old.line_total
            old_sale_store_id = old.sale.store_id
            old_product_id = old.product_id

        stock = StoreStock.objects.get(
            store=self.sale.store,
            product=self.product,
        )

        self.cost_price_per_kg = stock.average_purchase_price
        self.line_total = self.quantity_kg * self.sale_price_per_kg
        self.line_cost_total = self.quantity_kg * self.cost_price_per_kg
        self.profit = self.line_total - self.line_cost_total

        super().save(*args, **kwargs)

        cash_register, _ = CashRegister.objects.get_or_create(
            store=self.sale.store,
            defaults={"balance": Decimal("0.00")},
        )

        if is_new:
            stock.quantity_kg -= self.quantity_kg
            if stock.quantity_kg < 0:
                stock.quantity_kg = Decimal("0.000")
            stock.save()

            if self.sale.payment_type == Sale.PAYMENT_TYPE_CASH:
                cash_register.balance += self.line_total
                cash_register.save()

            StockMovement.objects.create(
                store=self.sale.store,
                product=self.product,
                movement_type="sale_out",
                quantity_kg_delta=self.quantity_kg,
                unit_cost=self.cost_price_per_kg,
                total_cost=self.line_cost_total,
                reference_note=f"Продажа #{self.sale_id}",
                date=self.sale.date,
            )

        else:
            if old_sale_store_id == self.sale.store_id and old_product_id == self.product_id:
                stock.quantity_kg += old_quantity
                stock.quantity_kg -= self.quantity_kg
                if stock.quantity_kg < 0:
                    stock.quantity_kg = Decimal("0.000")
                stock.save()
            else:
                old_stock = StoreStock.objects.get(
                    store_id=old_sale_store_id,
                    product_id=old_product_id,
                )
                old_stock.quantity_kg += old_quantity
                old_stock.save()

                new_stock = StoreStock.objects.get(
                    store=self.sale.store,
                    product=self.product,
                )
                new_stock.quantity_kg -= self.quantity_kg
                if new_stock.quantity_kg < 0:
                    new_stock.quantity_kg = Decimal("0.000")
                new_stock.save()

            if self.sale.payment_type == Sale.PAYMENT_TYPE_CASH:
                cash_register.balance = cash_register.balance - old_line_total + self.line_total
                if cash_register.balance < 0:
                    cash_register.balance = Decimal("0.00")
                cash_register.save()

        self.sale.recalculate_totals()

    def delete(self, *args, **kwargs):
        stock, _ = StoreStock.objects.get_or_create(
            store=self.sale.store,
            product=self.product,
            defaults={
                "quantity_kg": Decimal("0.000"),
                "average_purchase_price": Decimal("0.00"),
            },
        )

        stock.quantity_kg += self.quantity_kg
        stock.save()

        if self.sale.payment_type == Sale.PAYMENT_TYPE_CASH:
            cash_register, _ = CashRegister.objects.get_or_create(
                store=self.sale.store,
                defaults={"balance": Decimal("0.00")},
            )
            cash_register.balance -= self.line_total
            if cash_register.balance < 0:
                cash_register.balance = Decimal("0.00")
            cash_register.save()

        StockMovement.objects.create(
            store=self.sale.store,
            product=self.product,
            movement_type="adjustment_in",
            quantity_kg_delta=self.quantity_kg,
            unit_cost=self.cost_price_per_kg,
            total_cost=self.line_cost_total,
            reference_note=f"Удаление строки продажи #{self.id}",
            date=self.sale.date,
        )

        sale = self.sale
        super().delete(*args, **kwargs)
        sale.recalculate_totals()
