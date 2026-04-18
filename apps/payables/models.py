from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import Store, Supplier


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
        return f"{self.store} | {self.supplier} | {self.amount}"

    def clean(self):
        if self.amount is None or self.amount <= Decimal("0.00"):
            raise ValidationError({"amount": "Сумма оплаты должна быть больше 0."})