from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import DecimalField, ExpressionWrapper, F, Sum, Value
from django.db.models.functions import Coalesce

from apps.core.models import Store, Supplier
from apps.inventory.models import Purchase, PurchaseItem


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


def rebuild_supplier_payment_allocations(*, supplier_id, store_id):
    if not supplier_id or not store_id:
        return

    money_field = DecimalField(max_digits=14, decimal_places=2)
    line_total = ExpressionWrapper(
        F("quantity_kg") * F("purchase_price_per_kg"),
        output_field=money_field,
    )

    purchase_rows = list(
        PurchaseItem.objects.filter(
            purchase__supplier_id=supplier_id,
            store_id=store_id,
        )
        .annotate(line_total=line_total)
        .values("purchase_id", "purchase__date")
        .annotate(
            purchase_total=Coalesce(
                Sum("line_total"),
                Value(Decimal("0.00"), output_field=money_field),
            )
        )
        .order_by("purchase__date", "purchase_id")
    )

    purchases = [
        {
            "purchase_id": row["purchase_id"],
            "remaining_amount": row["purchase_total"] or Decimal("0.00"),
        }
        for row in purchase_rows
    ]
    purchases_by_id = {row["purchase_id"]: row for row in purchases}

    payments = list(
        SupplierPayment.objects.filter(
            supplier_id=supplier_id,
            store_id=store_id,
        ).order_by("date", "id")
    )

    SupplierPaymentAllocation.objects.filter(
        payment__supplier_id=supplier_id,
        payment__store_id=store_id,
    ).delete()

    allocations_to_create = []
    for payment in payments:
        remaining_payment = payment.amount or Decimal("0.00")
        if remaining_payment <= 0:
            continue

        if payment.purchase_id:
            bound_purchase = purchases_by_id.get(payment.purchase_id)
            if bound_purchase and bound_purchase["remaining_amount"] > 0:
                applied = min(bound_purchase["remaining_amount"], remaining_payment)
                if applied > 0:
                    allocations_to_create.append(
                        SupplierPaymentAllocation(
                            payment=payment,
                            purchase_id=payment.purchase_id,
                            store_id=store_id,
                            amount=applied,
                        )
                    )
                    bound_purchase["remaining_amount"] -= applied
                    remaining_payment -= applied

        if remaining_payment > 0:
            for purchase in purchases:
                if remaining_payment <= 0:
                    break
                if purchase["remaining_amount"] <= 0:
                    continue

                applied = min(purchase["remaining_amount"], remaining_payment)
                if applied <= 0:
                    continue

                allocations_to_create.append(
                    SupplierPaymentAllocation(
                        payment=payment,
                        purchase_id=purchase["purchase_id"],
                        store_id=store_id,
                        amount=applied,
                    )
                )
                purchase["remaining_amount"] -= applied
                remaining_payment -= applied

    if allocations_to_create:
        SupplierPaymentAllocation.objects.bulk_create(allocations_to_create)


class SupplierPayment(TimeStampedModel):
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="payments",
        verbose_name="Поставщик",
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        related_name="supplier_payments",
        verbose_name="Магазин",
    )
    purchase = models.ForeignKey(
        Purchase,
        on_delete=models.PROTECT,
        related_name="supplier_payments",
        verbose_name="Закупка",
        null=True,
        blank=True,
    )
    date = models.DateField(verbose_name="Дата оплаты")
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Сумма оплаты",
    )
    comment = models.TextField(
        blank=True,
        verbose_name="Комментарий",
    )

    class Meta:
        verbose_name = "Оплата поставщику"
        verbose_name_plural = "Оплаты поставщикам"
        ordering = ["-date", "-id"]

    def __str__(self):
        purchase_part = f" | закупка #{self.purchase_id}" if self.purchase_id else ""
        return f"{self.store} | {self.supplier}{purchase_part} | {self.amount}"

    def clean(self):
        if self.amount is None or self.amount <= Decimal("0.00"):
            raise ValidationError({"amount": "Сумма оплаты должна быть больше 0."})

        if self.purchase_id:
            errors = {}
            if self.purchase.supplier_id != self.supplier_id:
                errors["purchase"] = "Закупка должна относиться к выбранному поставщику."

            purchase_has_store = self.purchase.items.filter(store_id=self.store_id).exists()
            if not purchase_has_store:
                errors["store"] = "У выбранной закупки нет позиций для этого магазина."

            if errors:
                raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()

        previous_supplier_id = None
        previous_store_id = None
        if self.pk:
            previous = SupplierPayment.objects.get(pk=self.pk)
            previous_supplier_id = previous.supplier_id
            previous_store_id = previous.store_id

        super().save(*args, **kwargs)

        affected_groups = {(self.supplier_id, self.store_id)}
        if previous_supplier_id and previous_store_id:
            affected_groups.add((previous_supplier_id, previous_store_id))

        for supplier_id, store_id in affected_groups:
            rebuild_supplier_payment_allocations(
                supplier_id=supplier_id,
                store_id=store_id,
            )

    def delete(self, *args, **kwargs):
        supplier_id = self.supplier_id
        store_id = self.store_id
        super().delete(*args, **kwargs)
        rebuild_supplier_payment_allocations(
            supplier_id=supplier_id,
            store_id=store_id,
        )


class SupplierPaymentAllocation(TimeStampedModel):
    payment = models.ForeignKey(
        SupplierPayment,
        on_delete=models.CASCADE,
        related_name="allocations",
        verbose_name="Оплата поставщику",
    )
    purchase = models.ForeignKey(
        Purchase,
        on_delete=models.CASCADE,
        related_name="payment_allocations",
        verbose_name="Закупка",
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.CASCADE,
        related_name="supplier_payment_allocations",
        verbose_name="Магазин",
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Распределенная сумма",
    )

    class Meta:
        verbose_name = "Распределение оплаты поставщику"
        verbose_name_plural = "Распределения оплат поставщикам"
        ordering = ["payment__date", "payment_id", "purchase_id", "id"]

    def __str__(self):
        return f"Оплата #{self.payment_id} -> закупка #{self.purchase_id} | {self.amount}"
