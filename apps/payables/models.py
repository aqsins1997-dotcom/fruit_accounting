from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import Store, Supplier
from apps.inventory.models import Purchase


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


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
