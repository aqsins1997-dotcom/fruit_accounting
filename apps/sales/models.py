from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from apps.core.models import Product, Store, Customer
from apps.inventory.models import (
    PurchaseItem,
    StoreStock,
    StockMovement,
    calculate_active_stock_quantity,
    sync_store_stock_from_active_inventory,
)


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


def _money(value):
    return value.quantize(Decimal("0.01"))


def _sale_line_total(item):
    override = getattr(item, "_sale_total_override", None)
    if override is not None:
        return _money(override)
    return _money(item.quantity_kg * item.sale_price_per_kg)


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


def _apply_cash_register_delta(*, store_id, amount):
    if not store_id or amount == Decimal("0.00"):
        return

    now = timezone.now()
    updated = CashRegister.objects.filter(store_id=store_id).update(
        balance=models.F("balance") + amount,
        updated_at=now,
    )
    if updated:
        return

    try:
        CashRegister.objects.create(store_id=store_id, balance=amount)
    except IntegrityError:
        CashRegister.objects.filter(store_id=store_id).update(
            balance=models.F("balance") + amount,
            updated_at=now,
        )


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
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Удалена",
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

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    def clean(self):
        if self.payment_type == self.PAYMENT_TYPE_CREDIT and not self.customer:
            raise ValidationError({"customer": "Для продажи в кредит нужно указать клиента."})

        if self.payment_type == self.PAYMENT_TYPE_CASH and self.customer:
            raise ValidationError({"customer": "Для продажи за наличные клиент не нужен."})

    def sync_credit(self):
        if self.deleted_at:
            return

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


    def _cash_contribution(self):
        if self.deleted_at:
            return None, Decimal("0.00")
        if self.payment_type == self.PAYMENT_TYPE_CASH:
            return self.store_id, self.total_amount or Decimal("0.00")
        return None, Decimal("0.00")

    def save(self, *args, **kwargs):
        with transaction.atomic():
            is_new = self.pk is None
            old_store_id = None
            old_cash_amount = Decimal("0.00")

            if self.pk:
                old = Sale.objects.select_for_update().get(pk=self.pk)
                if old.store_id != self.store_id and old.items.exists():
                    raise ValidationError(
                        {"store": "Cannot change the store after sale items have been saved."}
                    )
                update_fields = kwargs.get("update_fields")
                total_fields = {"total_amount", "total_cost", "total_profit"}
                if update_fields is None or total_fields.isdisjoint(set(update_fields)):
                    self.total_amount = old.total_amount
                    self.total_cost = old.total_cost
                    self.total_profit = old.total_profit
                old_store_id, old_cash_amount = old._cash_contribution()

            self.full_clean()
            super().save(*args, **kwargs)

            new_store_id, new_cash_amount = self._cash_contribution()

            if old_store_id == new_store_id:
                _apply_cash_register_delta(
                    store_id=new_store_id,
                    amount=new_cash_amount - old_cash_amount,
                )
            else:
                _apply_cash_register_delta(store_id=old_store_id, amount=-old_cash_amount)
                _apply_cash_register_delta(store_id=new_store_id, amount=new_cash_amount)

            should_sync_credit = not is_new
            if is_new and self.payment_type == self.PAYMENT_TYPE_CREDIT and self.total_amount != Decimal("0.00"):
                should_sync_credit = True
            if should_sync_credit:
                self.sync_credit()

    def delete(self, *args, **kwargs):
        changed = self.soft_delete()
        return (
            1 if changed else 0,
            {self._meta.label: 1 if changed else 0},
        )

    def soft_delete(self):
        with transaction.atomic():
            sale = (
                Sale.objects.select_for_update()
                .prefetch_related("items")
                .get(pk=self.pk)
            )
            if sale.deleted_at:
                self.deleted_at = sale.deleted_at
                return False

            items = list(
                sale.items.select_for_update()
                .select_related("product")
                .order_by("id")
            )
            affected_pairs = {(sale.store_id, item.product_id) for item in items}
            old_store_id, old_cash_amount = sale._cash_contribution()
            now = timezone.now()

            Sale.objects.filter(pk=sale.pk).update(deleted_at=now, updated_at=now)
            sale.deleted_at = now
            self.deleted_at = now

            _apply_cash_register_delta(store_id=old_store_id, amount=-old_cash_amount)

            for item in items:
                StockMovement.objects.create(
                    store=sale.store,
                    product=item.product,
                    movement_type="adjustment_in",
                    quantity_kg_delta=item.quantity_kg,
                    unit_cost=item.cost_price_per_kg,
                    total_cost=item.line_cost_total,
                    reference_note=f"Sale soft delete #{sale.id} item #{item.id}",
                    date=sale.date,
                )

            for store_id, product_id in affected_pairs:
                sync_store_stock_from_active_inventory(store_id=store_id, product_id=product_id)

            return True


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

        if self.sale_id and self.sale.deleted_at:
            raise ValidationError({"sale": "Нельзя изменять строки удаленной продажи."})

        if self.sale_id and self.product_id:
            available_qty = calculate_active_stock_quantity(
                store_id=self.sale.store_id,
                product_id=self.product_id,
                exclude_sale_item_id=self.pk,
            )

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

    def save(self, *args, **kwargs):
        with transaction.atomic():
            self.full_clean()
            is_new = self.pk is None

            old_quantity = Decimal("0.000")
            old_sale_store_id = None
            old_product_id = None

            if not is_new:
                old = SaleItem.objects.select_for_update().select_related("sale").get(pk=self.pk)
                old_quantity = old.quantity_kg
                old_sale_store_id = old.sale.store_id
                old_product_id = old.product_id

            sale = Sale.objects.select_for_update().get(pk=self.sale_id)

            if is_new:
                available_quantity = calculate_active_stock_quantity(
                    store_id=sale.store_id,
                    product_id=self.product_id,
                )
                _validate_available_stock(available_quantity, self.quantity_kg)

                self.line_total = _sale_line_total(self)
                self.cost_price_per_kg = Decimal("0.00")
                self.line_cost_total = Decimal("0.00")
                self.profit = self.line_total

                super().save(*args, **kwargs)

                _apply_batch_costs(self, _replace_sale_item_batches(self, sale=sale))
                super().save(update_fields=[
                    "cost_price_per_kg",
                    "line_total",
                    "line_cost_total",
                    "profit",
                    "updated_at",
                ])

                StockMovement.objects.create(
                    store=sale.store,
                    product=self.product,
                    movement_type="sale_out",
                    quantity_kg_delta=self.quantity_kg,
                    unit_cost=self.cost_price_per_kg,
                    total_cost=self.line_cost_total,
                    reference_note=f"Sale #{self.sale_id}",
                    date=sale.date,
                )
                sync_store_stock_from_active_inventory(
                    store_id=sale.store_id,
                    product_id=self.product_id,
                )
            else:
                if old_sale_store_id == sale.store_id and old_product_id == self.product_id:
                    available_quantity = calculate_active_stock_quantity(
                        store_id=sale.store_id,
                        product_id=self.product_id,
                        exclude_sale_item_id=self.pk,
                    )
                    _validate_available_stock(available_quantity, self.quantity_kg)

                    self.line_total = _sale_line_total(self)
                    _apply_batch_costs(self, _replace_sale_item_batches(self, sale=sale))

                    super().save(*args, **kwargs)
                    sync_store_stock_from_active_inventory(
                        store_id=sale.store_id,
                        product_id=self.product_id,
                    )
                else:
                    available_quantity = calculate_active_stock_quantity(
                        store_id=sale.store_id,
                        product_id=self.product_id,
                    )
                    _validate_available_stock(available_quantity, self.quantity_kg)

                    self.line_total = _sale_line_total(self)
                    _apply_batch_costs(self, _replace_sale_item_batches(self, sale=sale))

                    super().save(*args, **kwargs)
                    sync_store_stock_from_active_inventory(
                        store_id=old_sale_store_id,
                        product_id=old_product_id,
                    )
                    sync_store_stock_from_active_inventory(
                        store_id=sale.store_id,
                        product_id=self.product_id,
                    )

            sale.recalculate_totals()

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            sale = Sale.objects.select_for_update().get(pk=self.sale_id)
            if sale.deleted_at:
                return (0, {})

            changed = sale.soft_delete()
            return (
                1 if changed else 0,
                {sale._meta.label: 1 if changed else 0},
            )


class SaleItemBatch(TimeStampedModel):
    sale_item = models.ForeignKey(
        SaleItem,
        on_delete=models.CASCADE,
        related_name="batches",
        verbose_name="Строка продажи",
    )
    purchase_item = models.ForeignKey(
        PurchaseItem,
        on_delete=models.PROTECT,
        related_name="sale_batches",
        verbose_name="Партия закупки",
    )
    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        verbose_name="Количество",
    )
    sale_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Цена продажи",
    )
    total_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Сумма продажи",
    )

    class Meta:
        db_table = "sale_item_batches"
        verbose_name = "Распределение продажи по партии"
        verbose_name_plural = "Распределения продаж по партиям"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["sale_item"]),
            models.Index(fields=["purchase_item"]),
        ]

    def __str__(self):
        return f"{self.sale_item_id} -> {self.purchase_item_id}: {self.quantity}"

    def clean(self):
        if self.quantity is not None and self.quantity <= 0:
            raise ValidationError({"quantity": "Количество партии должно быть больше 0."})
        if self.sale_price is not None and self.sale_price < 0:
            raise ValidationError({"sale_price": "Цена продажи не может быть отрицательной."})
        if self.total_amount is not None and self.total_amount < 0:
            raise ValidationError({"total_amount": "Сумма продажи не может быть отрицательной."})

        if self.sale_item_id and self.purchase_item_id:
            sale = self.sale_item.sale
            if sale.deleted_at:
                raise ValidationError({"sale_item": "Нельзя менять FIFO-списание удаленной продажи."})
            if self.purchase_item.purchase.deleted_at:
                raise ValidationError({"purchase_item": "Нельзя списывать товар с удаленной закупки."})
            if self.purchase_item.store_id != sale.store_id:
                raise ValidationError({"purchase_item": "Партия должна относиться к магазину продажи."})
            if self.purchase_item.product_id != self.sale_item.product_id:
                raise ValidationError({"purchase_item": "Партия должна относиться к товару продажи."})

    def save(self, *args, **kwargs):
        if self.total_amount is None:
            self.total_amount = _money(self.quantity * self.sale_price)
        self.full_clean()
        super().save(*args, **kwargs)


def _validate_available_stock(available_quantity, requested_quantity):
    if requested_quantity > available_quantity:
        raise ValidationError(
            {"quantity_kg": f"Недостаточно остатка. Доступно: {available_quantity} кг."}
        )


def purchase_item_allocated_quantity(purchase_item, *, exclude_sale_item_id=None):
    allocations = SaleItemBatch.objects.filter(
        purchase_item=purchase_item,
        purchase_item__purchase__deleted_at__isnull=True,
        sale_item__sale__deleted_at__isnull=True,
    )
    if exclude_sale_item_id:
        allocations = allocations.exclude(sale_item_id=exclude_sale_item_id)
    return allocations.aggregate(total=models.Sum("quantity"))["total"] or Decimal("0.000")


def purchase_item_available_quantity(purchase_item, *, exclude_sale_item_id=None):
    quantity = purchase_item.quantity_kg - purchase_item_allocated_quantity(
        purchase_item,
        exclude_sale_item_id=exclude_sale_item_id,
    )
    if quantity < Decimal("0.000"):
        return Decimal("0.000")
    return quantity.quantize(Decimal("0.001"))


def _purchase_item_allocated_quantity(purchase_item):
    return purchase_item_allocated_quantity(purchase_item)


def _selected_purchase_item_for_sale_item(sale_item, *, sale):
    selected_purchase_item = getattr(sale_item, "_selected_purchase_item", None)
    selected_purchase_item_id = getattr(sale_item, "_selected_purchase_item_id", None)

    if selected_purchase_item is None and selected_purchase_item_id:
        selected_purchase_item = (
            PurchaseItem.objects.select_for_update()
            .select_related("purchase", "purchase__supplier")
            .get(pk=selected_purchase_item_id)
        )

    if selected_purchase_item is None and sale_item.pk:
        existing_batches = list(
            SaleItemBatch.objects.select_related("purchase_item", "purchase_item__purchase")
            .filter(sale_item=sale_item)
            .order_by("id")[:2]
        )
        if len(existing_batches) == 1:
            selected_purchase_item = existing_batches[0].purchase_item

    if selected_purchase_item is None:
        candidates = []
        purchase_items = (
            PurchaseItem.objects.select_for_update()
            .select_related("purchase", "purchase__supplier")
            .filter(
                store_id=sale.store_id,
                product_id=sale_item.product_id,
                purchase__deleted_at__isnull=True,
            )
            .order_by("purchase__date", "id")
        )
        for purchase_item in purchase_items:
            if purchase_item_available_quantity(
                purchase_item,
                exclude_sale_item_id=sale_item.pk,
            ) >= sale_item.quantity_kg:
                candidates.append(purchase_item)
                if len(candidates) > 1:
                    break

        if len(candidates) == 1:
            selected_purchase_item = candidates[0]

    if selected_purchase_item is None:
        raise ValidationError({"purchase_item": "Выберите закупку/партию, с которой списывается товар."})

    return (
        PurchaseItem.objects.select_for_update()
        .select_related("purchase", "purchase__supplier")
        .get(pk=selected_purchase_item.pk)
    )


def _replace_sale_item_batches(sale_item, *, sale):
    purchase_item = _selected_purchase_item_for_sale_item(sale_item, sale=sale)

    if purchase_item.purchase.deleted_at:
        raise ValidationError({"purchase_item": "Нельзя списывать товар с удаленной закупки."})
    if purchase_item.store_id != sale.store_id:
        raise ValidationError({"purchase_item": "Выбранная закупка относится к другому магазину."})
    if purchase_item.product_id != sale_item.product_id:
        raise ValidationError({"purchase_item": "Выбранная закупка относится к другому товару."})

    available_quantity = purchase_item_available_quantity(
        purchase_item,
        exclude_sale_item_id=sale_item.pk,
    )
    if sale_item.quantity_kg > available_quantity:
        raise ValidationError(
            {"quantity_kg": f"Недостаточно остатка в выбранной закупке. Доступно: {available_quantity} кг."}
        )

    SaleItemBatch.objects.filter(sale_item=sale_item).delete()
    SaleItemBatch.objects.create(
        sale_item=sale_item,
        purchase_item=purchase_item,
        quantity=sale_item.quantity_kg,
        sale_price=sale_item.sale_price_per_kg,
        total_amount=sale_item.line_total,
    )

    return sale_item.quantity_kg * purchase_item.purchase_price_per_kg


def _apply_batch_costs(sale_item, total_cost):
    sale_item.line_cost_total = _money(total_cost)
    if sale_item.quantity_kg > Decimal("0.000"):
        sale_item.cost_price_per_kg = _money(total_cost / sale_item.quantity_kg)
    else:
        sale_item.cost_price_per_kg = Decimal("0.00")
    sale_item.profit = _money(sale_item.line_total - sale_item.line_cost_total)
