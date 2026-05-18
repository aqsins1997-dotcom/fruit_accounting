from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

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
            purchase__deleted_at__isnull=True,
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
            status=SupplierPayment.STATUS_ACTIVE,
        ).order_by("date", "id")
    )

    SupplierPaymentAllocation.objects.filter(
        payment__supplier_id=supplier_id,
        payment__store_id=store_id,
        payment__status=SupplierPayment.STATUS_ACTIVE,
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


def get_supplier_remaining_debt(*, supplier_id, store_id, exclude_payment_id=None):
    if not supplier_id or not store_id:
        return Decimal("0.00")

    money_field = DecimalField(max_digits=14, decimal_places=2)
    line_total = ExpressionWrapper(
        F("quantity_kg") * F("purchase_price_per_kg"),
        output_field=money_field,
    )
    purchase_total = (
        PurchaseItem.objects.filter(
            purchase__supplier_id=supplier_id,
            purchase__deleted_at__isnull=True,
            store_id=store_id,
        )
        .annotate(line_total=line_total)
        .aggregate(
            total=Coalesce(
                Sum("line_total"),
                Value(Decimal("0.00"), output_field=money_field),
            )
        )["total"]
        or Decimal("0.00")
    )

    allocations = SupplierPaymentAllocation.objects.filter(
        payment__supplier_id=supplier_id,
        payment__store_id=store_id,
        payment__status=SupplierPayment.STATUS_ACTIVE,
        purchase__deleted_at__isnull=True,
        store_id=store_id,
    )
    if exclude_payment_id:
        allocations = allocations.exclude(payment_id=exclude_payment_id)

    paid_total = allocations.aggregate(
        total=Coalesce(
            Sum("amount"),
            Value(Decimal("0.00"), output_field=money_field),
        )
    )["total"] or Decimal("0.00")

    remaining = purchase_total - paid_total
    if remaining < Decimal("0.00"):
        return Decimal("0.00")
    return remaining.quantize(Decimal("0.01"))


class SupplierPayment(TimeStampedModel):
    STATUS_ACTIVE = "active"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = (
        (STATUS_ACTIVE, "Активна"),
        (STATUS_CANCELLED, "Отменена"),
    )

    PAYMENT_METHOD_CASH = "cash"
    PAYMENT_METHOD_CARD = "card"
    PAYMENT_METHOD_TRANSFER = "transfer"

    PAYMENT_METHOD_CHOICES = (
        (PAYMENT_METHOD_CASH, "Наличные"),
        (PAYMENT_METHOD_CARD, "Карта"),
        (PAYMENT_METHOD_TRANSFER, "Перевод"),
    )

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
    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHOD_CHOICES,
        default=PAYMENT_METHOD_CASH,
        verbose_name="Способ оплаты",
    )
    comment = models.TextField(
        blank=True,
        verbose_name="Комментарий",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
        verbose_name="Статус",
    )
    cancelled_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Дата отмены",
    )
    cancel_reason = models.TextField(
        blank=True,
        verbose_name="Причина отмены",
    )

    class Meta:
        verbose_name = "Оплата поставщику"
        verbose_name_plural = "Оплаты поставщикам"
        ordering = ["-date", "-id"]
        indexes = [
            models.Index(fields=["supplier", "store", "date"]),
            models.Index(fields=["purchase", "store"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        purchase_part = f" | закупка #{self.purchase_id}" if self.purchase_id else ""
        return f"{self.store} | {self.supplier}{purchase_part} | {self.amount}"

    def clean(self):
        if self.amount is None or self.amount <= Decimal("0.00"):
            raise ValidationError({"amount": "Сумма оплаты должна быть больше 0."})

        if self.status == self.STATUS_ACTIVE:
            remaining_debt = get_supplier_remaining_debt(
                supplier_id=self.supplier_id,
                store_id=self.store_id,
                exclude_payment_id=self.pk,
            )
            if self.amount and self.amount > remaining_debt:
                raise ValidationError(
                    {
                        "amount": (
                            "Сумма оплаты не может быть больше текущего долга поставщика. "
                            f"Текущий долг: {remaining_debt}."
                        )
                    }
                )

        if self.purchase_id:
            errors = {}
            if self.purchase.deleted_at:
                errors["purchase"] = "Нельзя привязать оплату к удаленной закупке."
            if self.purchase.supplier_id != self.supplier_id:
                errors["purchase"] = "Закупка должна относиться к выбранному поставщику."

            purchase_has_store = self.purchase.items.filter(store_id=self.store_id).exists()
            if not purchase_has_store:
                errors["store"] = "У выбранной закупки нет позиций для этого магазина."

            if errors:
                raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            self.full_clean()

            previous = None
            previous_supplier_id = None
            previous_store_id = None
            if self.pk:
                previous = SupplierPayment.objects.select_for_update().select_related("store").get(pk=self.pk)
                previous_supplier_id = previous.supplier_id
                previous_store_id = previous.store_id

            from apps.expenses.services import _get_cash_register, _save_cash_register, _validate_cash_outflow

            if previous and previous.status == self.STATUS_ACTIVE:
                previous_register = _get_cash_register(previous.store)
                previous_register.balance += previous.amount
                _save_cash_register(previous_register)

            if self.status == self.STATUS_ACTIVE:
                register = _get_cash_register(self.store)
                _validate_cash_outflow(
                    store=self.store,
                    amount=self.amount,
                    available_amount=register.balance,
                )
                register.balance -= self.amount
                _save_cash_register(register)

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
        self.cancel(reason="Удаление заменено безопасной отменой оплаты.")

    def cancel(self, *, reason=""):
        with transaction.atomic():
            payment = SupplierPayment.objects.select_for_update().get(pk=self.pk)
            if payment.status == self.STATUS_CANCELLED:
                return payment
            supplier_id = payment.supplier_id
            store_id = payment.store_id
            payment.status = self.STATUS_CANCELLED
            payment.cancelled_at = timezone.now()
            payment.cancel_reason = reason or payment.cancel_reason
            payment.save()
            rebuild_supplier_payment_allocations(
                supplier_id=supplier_id,
                store_id=store_id,
            )
            return payment


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
        indexes = [
            models.Index(fields=["payment", "purchase"]),
            models.Index(fields=["store", "purchase"]),
        ]

    def __str__(self):
        return f"Оплата #{self.payment_id} -> закупка #{self.purchase_id} | {self.amount}"
