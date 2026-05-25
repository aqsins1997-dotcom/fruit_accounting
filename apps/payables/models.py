from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from apps.core.models import Store, Supplier
from apps.inventory.models import Purchase


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


def rebuild_supplier_payment_allocations(*, supplier_id, store_id):
    if not supplier_id or not store_id:
        return

    from .services import rebuild_supplier_settlement_state

    rebuild_supplier_settlement_state(supplier_id=supplier_id, store_id=store_id)


def get_supplier_remaining_debt(*, supplier_id, store_id, exclude_payment_id=None):
    if not supplier_id or not store_id:
        return Decimal("0.00")

    from .services import calculate_supplier_remaining_debt

    return calculate_supplier_remaining_debt(
        supplier_id=supplier_id,
        store_id=store_id,
        exclude_payment_id=exclude_payment_id,
    )


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
    comment = models.TextField(blank=True, verbose_name="Комментарий")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
        verbose_name="Статус",
    )
    cancelled_at = models.DateTimeField(blank=True, null=True, verbose_name="Дата отмены")
    cancel_reason = models.TextField(blank=True, verbose_name="Причина отмены")

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

    def _cash_contribution(self):
        if self.status != self.STATUS_ACTIVE:
            return None, Decimal("0.00")
        if self.payment_method != self.PAYMENT_METHOD_CASH:
            return None, Decimal("0.00")
        return self.store_id, self.amount or Decimal("0.00")

    def clean(self):
        if self.amount is None or self.amount <= Decimal("0.00"):
            raise ValidationError({"amount": "Сумма оплаты должна быть больше 0."})

        if self.status == self.STATUS_ACTIVE:
            current_amount = Decimal("0.00")
            if self.pk:
                current_amount = (
                    SupplierPayment.objects.filter(pk=self.pk).values_list("amount", flat=True).first()
                    or Decimal("0.00")
                )
            remaining_debt = get_supplier_remaining_debt(
                supplier_id=self.supplier_id,
                store_id=self.store_id,
                exclude_payment_id=self.pk,
            )
            allowed_amount = remaining_debt if current_amount <= remaining_debt else current_amount
            if self.amount and self.amount > allowed_amount:
                raise ValidationError(
                    {
                        "amount": (
                            "Сумма оплаты не может быть больше текущего долга поставщику. "
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
            previous_cash_store_id = None
            previous_cash_amount = Decimal("0.00")
            if self.pk:
                previous = SupplierPayment.objects.select_for_update().select_related("store").get(pk=self.pk)
                previous_supplier_id = previous.supplier_id
                previous_store_id = previous.store_id
                previous_cash_store_id, previous_cash_amount = previous._cash_contribution()

            from apps.expenses.services import _get_cash_register, _save_cash_register, _validate_cash_outflow

            current_cash_store_id, current_cash_amount = self._cash_contribution()

            if previous_cash_store_id == current_cash_store_id and current_cash_store_id:
                register = _get_cash_register(self.store)
                available_amount = register.balance + previous_cash_amount
                _validate_cash_outflow(
                    store=self.store,
                    amount=current_cash_amount,
                    available_amount=available_amount,
                )
                register.balance = available_amount - current_cash_amount
                _save_cash_register(register)
            else:
                if previous_cash_store_id and previous_cash_amount > Decimal("0.00"):
                    previous_register = _get_cash_register(previous.store)
                    previous_register.balance += previous_cash_amount
                    _save_cash_register(previous_register)

                if current_cash_store_id and current_cash_amount > Decimal("0.00"):
                    register = _get_cash_register(self.store)
                    _validate_cash_outflow(
                        store=self.store,
                        amount=current_cash_amount,
                        available_amount=register.balance,
                    )
                    register.balance -= current_cash_amount
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
            payment = SupplierPayment.objects.select_for_update().select_related("store").get(pk=self.pk)
            if payment.status == self.STATUS_CANCELLED:
                self.status = payment.status
                self.cancelled_at = payment.cancelled_at
                self.cancel_reason = payment.cancel_reason
                return payment
            supplier_id = payment.supplier_id
            store_id = payment.store_id
            cash_store_id, cash_amount = payment._cash_contribution()

            if cash_store_id and cash_amount > Decimal("0.00"):
                from apps.expenses.services import _get_cash_register, _save_cash_register

                register = _get_cash_register(payment.store)
                register.balance += cash_amount
                _save_cash_register(register)

            now = timezone.now()
            cancel_reason = reason or payment.cancel_reason
            SupplierPayment.objects.filter(pk=payment.pk).update(
                status=self.STATUS_CANCELLED,
                cancelled_at=now,
                cancel_reason=cancel_reason,
                updated_at=now,
            )
            payment.status = self.STATUS_CANCELLED
            payment.cancelled_at = now
            payment.cancel_reason = cancel_reason
            self.status = payment.status
            self.cancelled_at = payment.cancelled_at
            self.cancel_reason = payment.cancel_reason
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


class SupplierOverpayment(TimeStampedModel):
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="overpayments",
        verbose_name="Поставщик",
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        related_name="supplier_overpayments",
        verbose_name="Магазин",
    )
    source_payment = models.OneToOneField(
        SupplierPayment,
        on_delete=models.CASCADE,
        related_name="overpayment",
        verbose_name="Исходная оплата",
        null=True,
        blank=True,
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Сумма переплаты",
    )
    remaining_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Остаток переплаты",
    )
    comment = models.TextField(blank=True, verbose_name="Комментарий")

    class Meta:
        verbose_name = "Переплата поставщику"
        verbose_name_plural = "Переплаты поставщикам"
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["supplier", "store", "created_at"]),
        ]

    def __str__(self):
        return f"{self.store} | {self.supplier} | переплата {self.remaining_amount}"
