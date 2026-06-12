from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from apps.core.models import Product, Store, Supplier


def _rebuild_supplier_payment_allocations(groups):
    groups = {(supplier_id, store_id) for supplier_id, store_id in groups if supplier_id and store_id}
    if not groups:
        return

    from apps.payables.models import rebuild_supplier_payment_allocations

    for supplier_id, store_id in groups:
        rebuild_supplier_payment_allocations(supplier_id=supplier_id, store_id=store_id)


def _get_or_create_locked_stock(*, store_id, product_id):
    stock, _ = StoreStock.objects.select_for_update().get_or_create(
        store_id=store_id,
        product_id=product_id,
        defaults={
            "quantity_kg": Decimal("0.000"),
            "average_purchase_price": Decimal("0.00"),
        },
    )
    return stock


def _validate_non_negative_stock(quantity):
    if quantity < Decimal("0.000"):
        raise ValidationError(
            {"quantity_kg": "Cannot reduce or delete a purchase below the quantity already sold."}
        )


def _weighted_average_purchase_price(*, store_id, product_id):
    total_quantity = Decimal("0.000")
    total_cost = Decimal("0.00")

    for item in PurchaseItem.objects.filter(
        store_id=store_id,
        product_id=product_id,
        purchase__deleted_at__isnull=True,
    ):
        total_quantity += item.quantity_kg
        total_cost += item.quantity_kg * item.purchase_price_per_kg

    if total_quantity <= 0:
        return Decimal("0.00")

    return (total_cost / total_quantity).quantize(Decimal("0.01"))


def _save_stock(stock):
    stock.average_purchase_price = _weighted_average_purchase_price(
        store_id=stock.store_id,
        product_id=stock.product_id,
    )
    stock.full_clean()
    stock.save(update_fields=["quantity_kg", "average_purchase_price", "updated_at"])


def _get_purchase_item_allocated_quantity(purchase_item_id):
    if not purchase_item_id:
        return Decimal("0.000")

    from apps.sales.models import SaleItemBatch

    return SaleItemBatch.objects.filter(
        purchase_item_id=purchase_item_id,
        purchase_item__purchase__deleted_at__isnull=True,
        sale_item__sale__deleted_at__isnull=True,
    ).aggregate(
        total=models.Sum("quantity")
    )["total"] or Decimal("0.000")


def calculate_active_stock_quantity(*, store_id, product_id, exclude_sale_item_id=None):
    if not store_id or not product_id:
        return Decimal("0.000")

    from apps.sales.models import SaleItem

    purchased_quantity = PurchaseItem.objects.filter(
        store_id=store_id,
        product_id=product_id,
        purchase__deleted_at__isnull=True,
    ).aggregate(total=models.Sum("quantity_kg"))["total"] or Decimal("0.000")

    sale_items = SaleItem.objects.filter(
        sale__store_id=store_id,
        product_id=product_id,
        sale__deleted_at__isnull=True,
    )
    if exclude_sale_item_id:
        sale_items = sale_items.exclude(id=exclude_sale_item_id)

    sold_quantity = sale_items.aggregate(total=models.Sum("quantity_kg"))["total"] or Decimal("0.000")
    quantity = purchased_quantity - sold_quantity
    if quantity < Decimal("0.000"):
        return Decimal("0.000")
    return quantity.quantize(Decimal("0.001"))


def sync_store_stock_from_active_inventory(*, store_id, product_id):
    stock = _get_or_create_locked_stock(store_id=store_id, product_id=product_id)
    stock.quantity_kg = calculate_active_stock_quantity(store_id=store_id, product_id=product_id)
    _save_stock(stock)
    return stock


def change_purchase_item_product(*, purchase_item, product):
    if not purchase_item or not purchase_item.pk:
        raise ValidationError({"product": "Строка закупки не найдена."})
    if not product or not product.pk:
        raise ValidationError({"product": "Выберите товар."})

    with transaction.atomic():
        item = (
            PurchaseItem.objects.select_for_update()
            .select_related("purchase", "store", "product")
            .get(pk=purchase_item.pk)
        )
        if item.purchase.deleted_at:
            raise ValidationError({"product": "Нельзя менять товар удаленной закупки."})

        try:
            new_product = Product.objects.get(pk=product.pk)
        except Product.DoesNotExist as exc:
            raise ValidationError({"product": "Выберите существующий товар."}) from exc
        old_product = item.product
        old_product_id = item.product_id
        new_product_id = new_product.pk

        if old_product_id == new_product_id:
            return {
                "changed": False,
                "purchase_item": item,
                "old_product": old_product,
                "new_product": new_product,
                "updated_sale_items": 0,
            }

        from apps.sales.models import SaleItem, SaleItemBatch

        linked_sale_item_ids = sorted(
            set(
                SaleItemBatch.objects.select_for_update()
                .filter(purchase_item_id=item.pk)
                .values_list("sale_item_id", flat=True)
            )
        )

        if linked_sale_item_ids:
            other_batch = (
                SaleItemBatch.objects.select_for_update()
                .select_related("sale_item__sale")
                .filter(sale_item_id__in=linked_sale_item_ids)
                .exclude(purchase_item_id=item.pk)
                .order_by("sale_item__sale_id", "sale_item_id", "id")
                .first()
            )
            if other_batch:
                raise ValidationError(
                    {
                        "product": (
                            "Нельзя изменить товар: продажа "
                            f"№{other_batch.sale_item.sale_id} распределена по нескольким партиям. "
                            "Сначала исправьте распределение этой продажи."
                        )
                    }
                )

            list(
                SaleItem.objects.select_for_update()
                .filter(id__in=linked_sale_item_ids)
                .values_list("id", flat=True)
            )

        now = timezone.now()
        PurchaseItem.objects.filter(pk=item.pk).update(product_id=new_product_id, updated_at=now)
        if linked_sale_item_ids:
            SaleItem.objects.filter(id__in=linked_sale_item_ids).update(
                product_id=new_product_id,
                updated_at=now,
            )

        sync_store_stock_from_active_inventory(store_id=item.store_id, product_id=old_product_id)
        sync_store_stock_from_active_inventory(store_id=item.store_id, product_id=new_product_id)

        item.product = new_product
        item.product_id = new_product_id
        item.updated_at = now

        return {
            "changed": True,
            "purchase_item": item,
            "old_product": old_product,
            "new_product": new_product,
            "updated_sale_items": len(linked_sale_item_ids),
        }


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Purchase(TimeStampedModel):
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="purchases",
        verbose_name="Поставщик",
    )
    date = models.DateField(verbose_name="Дата закупки")
    comment = models.TextField(blank=True, verbose_name="Комментарий")
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Удалена",
    )

    class Meta:
        verbose_name = "Закупка"
        verbose_name_plural = "Закупки"
        ordering = ["-date", "-id"]
        indexes = [
            models.Index(fields=["date", "supplier"]),
        ]

    def __str__(self):
        return f"Закупка #{self.id} от {self.date}"

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    def save(self, *args, **kwargs):
        previous_supplier_id = None
        previous_date = None

        with transaction.atomic():
            if self.pk:
                previous = Purchase.objects.select_for_update().get(pk=self.pk)
                previous_supplier_id = previous.supplier_id
                previous_date = previous.date

            super().save(*args, **kwargs)

            if previous_supplier_id and (
                previous_supplier_id != self.supplier_id or previous_date != self.date
            ):
                store_ids = set(self.items.values_list("store_id", flat=True))
                _rebuild_supplier_payment_allocations(
                    {(previous_supplier_id, store_id) for store_id in store_ids}
                    | {(self.supplier_id, store_id) for store_id in store_ids}
                )

    def delete(self, *args, **kwargs):
        changed = self.soft_delete()
        return (
            1 if changed else 0,
            {self._meta.label: 1 if changed else 0},
        )

    def soft_delete(self):
        with transaction.atomic():
            purchase = (
                Purchase.objects.select_for_update()
                .prefetch_related("items")
                .get(pk=self.pk)
            )
            if purchase.deleted_at:
                self.deleted_at = purchase.deleted_at
                return False

            affected_groups = set()
            now = timezone.now()
            items = (
                purchase.items.select_for_update()
                .select_related("store", "product")
                .order_by("id")
            )

            for item in items:
                allocated_quantity = _get_purchase_item_allocated_quantity(item.pk)
                remaining_quantity = item.quantity_kg - allocated_quantity
                if remaining_quantity <= Decimal("0.000"):
                    remaining_quantity = Decimal("0.000")

                affected_groups.add((purchase.supplier_id, item.store_id))

                if remaining_quantity <= Decimal("0.000"):
                    continue

                stock = _get_or_create_locked_stock(
                    store_id=item.store_id,
                    product_id=item.product_id,
                )
                stock.quantity_kg -= remaining_quantity
                if stock.quantity_kg < Decimal("0.000"):
                    stock.quantity_kg = Decimal("0.000")
                _save_stock(stock)

                StockMovement.objects.create(
                    store=item.store,
                    product=item.product,
                    movement_type="adjustment_out",
                    quantity_kg_delta=remaining_quantity,
                    unit_cost=item.purchase_price_per_kg,
                    total_cost=remaining_quantity * item.purchase_price_per_kg,
                    reference_note=f"Purchase soft delete #{purchase.id} item #{item.id}",
                    date=timezone.localdate(),
                )

            Purchase.objects.filter(pk=purchase.pk).update(deleted_at=now, updated_at=now)
            purchase.deleted_at = now
            self.deleted_at = now
            _rebuild_supplier_payment_allocations(affected_groups)
            return True


class PurchaseItem(TimeStampedModel):
    purchase = models.ForeignKey(
        Purchase,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="Закупка",
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        related_name="purchase_items",
        verbose_name="Магазин",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="purchase_items",
        verbose_name="Товар",
    )
    quantity_kg = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        verbose_name="Количество (кг)",
    )
    purchase_price_per_kg = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Закупочная цена за кг",
    )

    class Meta:
        verbose_name = "Строка закупки"
        verbose_name_plural = "Строки закупки"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["purchase", "store"]),
            models.Index(fields=["store", "product"]),
        ]

    def __str__(self):
        return f"{self.store} | {self.product} | {self.quantity_kg} кг"

    @property
    def total_cost(self):
        if self.quantity_kg is None or self.purchase_price_per_kg is None:
            return Decimal("0.00")
        return self.quantity_kg * self.purchase_price_per_kg

    def clean(self):
        if self.quantity_kg is not None and self.quantity_kg <= 0:
            raise ValidationError({"quantity_kg": "Количество должно быть больше 0."})

        if self.purchase_price_per_kg is not None and self.purchase_price_per_kg < 0:
            raise ValidationError({"purchase_price_per_kg": "Цена не может быть отрицательной."})

        if self.purchase_id and self.purchase.deleted_at:
            raise ValidationError({"purchase": "Нельзя изменять строки удаленной закупки."})

    def save(self, *args, **kwargs):
        is_new = self.pk is None

        old_quantity = Decimal("0.000")
        old_price = Decimal("0.00")
        old_store_id = None
        old_product_id = None

        if not is_new:
            old = PurchaseItem.objects.get(pk=self.pk)
            old_quantity = old.quantity_kg
            old_price = old.purchase_price_per_kg
            old_store_id = old.store_id
            old_product_id = old.product_id

        super().save(*args, **kwargs)

        if is_new:
            stock, _ = StoreStock.objects.get_or_create(
                store=self.store,
                product=self.product,
                defaults={
                    "quantity_kg": Decimal("0.000"),
                    "average_purchase_price": Decimal("0.00"),
                },
            )

            new_total_qty = stock.quantity_kg + self.quantity_kg

            if new_total_qty > 0:
                stock.average_purchase_price = (
                    (stock.quantity_kg * stock.average_purchase_price)
                    + (self.quantity_kg * self.purchase_price_per_kg)
                ) / new_total_qty

            stock.quantity_kg = new_total_qty
            stock.save()

            StockMovement.objects.create(
                store=self.store,
                product=self.product,
                movement_type="purchase_in",
                quantity_kg_delta=self.quantity_kg,
                unit_cost=self.purchase_price_per_kg,
                total_cost=self.total_cost,
                reference_note=f"Закупка #{self.purchase_id}",
                date=self.purchase.date,
            )

        else:
            if old_store_id == self.store_id and old_product_id == self.product_id:
                stock, _ = StoreStock.objects.get_or_create(
                    store=self.store,
                    product=self.product,
                    defaults={
                        "quantity_kg": Decimal("0.000"),
                        "average_purchase_price": Decimal("0.00"),
                    },
                )

                stock.quantity_kg = stock.quantity_kg - old_quantity + self.quantity_kg
                stock.average_purchase_price = self.purchase_price_per_kg
                stock.save()
            else:
                old_stock, _ = StoreStock.objects.get_or_create(
                    store_id=old_store_id,
                    product_id=old_product_id,
                    defaults={
                        "quantity_kg": Decimal("0.000"),
                        "average_purchase_price": Decimal("0.00"),
                    },
                )
                old_stock.quantity_kg -= old_quantity
                if old_stock.quantity_kg < 0:
                    old_stock.quantity_kg = Decimal("0.000")
                old_stock.save()

                new_stock, _ = StoreStock.objects.get_or_create(
                    store=self.store,
                    product=self.product,
                    defaults={
                        "quantity_kg": Decimal("0.000"),
                        "average_purchase_price": Decimal("0.00"),
                    },
                )
                new_stock.quantity_kg += self.quantity_kg
                new_stock.average_purchase_price = self.purchase_price_per_kg
                new_stock.save()

    def delete(self, *args, **kwargs):
        stock, _ = StoreStock.objects.get_or_create(
            store=self.store,
            product=self.product,
            defaults={
                "quantity_kg": Decimal("0.000"),
                "average_purchase_price": Decimal("0.00"),
            },
        )

        stock.quantity_kg -= self.quantity_kg

        if stock.quantity_kg < 0:
            stock.quantity_kg = Decimal("0.000")

        stock.save()

        StockMovement.objects.create(
            store=self.store,
            product=self.product,
            movement_type="adjustment_out",
            quantity_kg_delta=self.quantity_kg,
            unit_cost=self.purchase_price_per_kg,
            total_cost=self.total_cost,
            reference_note=f"Удаление строки закупки #{self.id}",
            date=self.purchase.date,
        )

        super().delete(*args, **kwargs)


    def save(self, *args, **kwargs):
        with transaction.atomic():
            self.full_clean()
            is_new = self.pk is None

            old_quantity = Decimal("0.000")
            old_price = None
            old_store_id = None
            old_product_id = None
            old_supplier_id = None

            if not is_new:
                old = PurchaseItem.objects.select_for_update().select_related("purchase").get(pk=self.pk)
                old_quantity = old.quantity_kg
                old_price = old.purchase_price_per_kg
                old_store_id = old.store_id
                old_product_id = old.product_id
                old_supplier_id = old.purchase.supplier_id
                allocated_quantity = _get_purchase_item_allocated_quantity(self.pk)
                if allocated_quantity > self.quantity_kg:
                    raise ValidationError(
                        {
                            "quantity_kg": (
                                "Нельзя уменьшить закупку ниже уже проданного по этой партии. "
                                f"Продано: {allocated_quantity} кг."
                            )
                        }
                    )
                if allocated_quantity > 0 and (
                    old_store_id != self.store_id or old_product_id != self.product_id
                ):
                    raise ValidationError(
                        {
                            "product": (
                                "Нельзя менять магазин или товар закупки, по которой уже были продажи."
                            )
                        }
                    )

            if is_new:
                super().save(*args, **kwargs)
                stock = _get_or_create_locked_stock(store_id=self.store_id, product_id=self.product_id)
                stock.quantity_kg += self.quantity_kg
                _save_stock(stock)

                StockMovement.objects.create(
                    store=self.store,
                    product=self.product,
                    movement_type="purchase_in",
                    quantity_kg_delta=self.quantity_kg,
                    unit_cost=self.purchase_price_per_kg,
                    total_cost=self.total_cost,
                    reference_note=f"Purchase #{self.purchase_id}",
                    date=self.purchase.date,
                )
            else:
                if old_store_id == self.store_id and old_product_id == self.product_id:
                    stock = _get_or_create_locked_stock(store_id=self.store_id, product_id=self.product_id)
                    new_quantity = stock.quantity_kg - old_quantity + self.quantity_kg
                    _validate_non_negative_stock(new_quantity)

                    super().save(*args, **kwargs)
                    stock.quantity_kg = new_quantity
                    _save_stock(stock)
                else:
                    old_stock = _get_or_create_locked_stock(store_id=old_store_id, product_id=old_product_id)
                    new_old_stock_quantity = old_stock.quantity_kg - old_quantity
                    _validate_non_negative_stock(new_old_stock_quantity)

                    new_stock = _get_or_create_locked_stock(store_id=self.store_id, product_id=self.product_id)

                    super().save(*args, **kwargs)

                    old_stock.quantity_kg = new_old_stock_quantity
                    _save_stock(old_stock)

                    new_stock.quantity_kg += self.quantity_kg
                    _save_stock(new_stock)

            _rebuild_supplier_payment_allocations(
                {
                    (old_supplier_id, old_store_id),
                    (self.purchase.supplier_id, self.store_id),
                }
            )

            if not is_new and old_price != self.purchase_price_per_kg:
                from apps.sales.services import recalculate_sale_costs_for_purchase_item

                recalculate_sale_costs_for_purchase_item(self)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            allocated_quantity = _get_purchase_item_allocated_quantity(self.pk)
            if allocated_quantity > 0:
                raise ValidationError(
                    {
                        "quantity_kg": (
                            "Нельзя удалить закупку, по которой уже были продажи. "
                            f"Продано: {allocated_quantity} кг."
                        )
                    }
                )

            stock = _get_or_create_locked_stock(store_id=self.store_id, product_id=self.product_id)
            new_quantity = stock.quantity_kg - self.quantity_kg
            _validate_non_negative_stock(new_quantity)

            supplier_id = self.purchase.supplier_id
            store_id = self.store_id
            reference_id = self.id

            StockMovement.objects.create(
                store=self.store,
                product=self.product,
                movement_type="adjustment_out",
                quantity_kg_delta=self.quantity_kg,
                unit_cost=self.purchase_price_per_kg,
                total_cost=self.total_cost,
                reference_note=f"Purchase item delete #{reference_id}",
                date=self.purchase.date,
            )

            super().delete(*args, **kwargs)

            stock.quantity_kg = new_quantity
            _save_stock(stock)
            _rebuild_supplier_payment_allocations({(supplier_id, store_id)})


class StoreStock(TimeStampedModel):
    store = models.ForeignKey(
        Store,
        on_delete=models.CASCADE,
        related_name="stocks",
        verbose_name="Магазин",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="store_stocks",
        verbose_name="Товар",
    )
    quantity_kg = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        default=Decimal("0.000"),
        verbose_name="Остаток (кг)",
    )
    average_purchase_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Средняя закупочная цена",
    )

    class Meta:
        verbose_name = "Остаток магазина"
        verbose_name_plural = "Остатки магазинов"
        unique_together = ("store", "product")
        ordering = ["store__name", "product__name"]

    def __str__(self):
        return f"{self.store} | {self.product} | {self.quantity_kg} кг"

    def clean(self):
        if self.quantity_kg is not None and self.quantity_kg < 0:
            raise ValidationError({"quantity_kg": "Остаток не может быть отрицательным."})
        if self.average_purchase_price is not None and self.average_purchase_price < 0:
            raise ValidationError({"average_purchase_price": "Цена не может быть отрицательной."})


class StockMovement(TimeStampedModel):
    MOVEMENT_TYPE_CHOICES = [
        ("purchase_in", "Поступление"),
        ("sale_out", "Продажа"),
        ("adjustment_in", "Корректировка +"),
        ("adjustment_out", "Корректировка -"),
    ]

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        related_name="stock_movements",
        verbose_name="Магазин",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="stock_movements",
        verbose_name="Товар",
    )
    movement_type = models.CharField(
        max_length=30,
        choices=MOVEMENT_TYPE_CHOICES,
        verbose_name="Тип движения",
    )
    quantity_kg_delta = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        verbose_name="Изменение (кг)",
    )
    unit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Себестоимость за кг",
    )
    total_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Общая себестоимость",
    )
    reference_note = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Источник / примечание",
    )
    date = models.DateField(verbose_name="Дата движения")

    class Meta:
        verbose_name = "Движение товара"
        verbose_name_plural = "Движения товара"
        ordering = ["-date", "-id"]
        indexes = [
            models.Index(fields=["store", "date"]),
            models.Index(fields=["product", "date"]),
        ]

    def __str__(self):
        return f"{self.store} | {self.product} | {self.movement_type} | {self.quantity_kg_delta} кг"
