from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import Customer, Store
from apps.sales.models import Sale, CashRegister


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Credit(TimeStampedModel):
    STATUS_UNPAID = "unpaid"
    STATUS_PARTIAL = "partial"
    STATUS_PAID = "paid"

    STATUS_CHOICES = (
        (STATUS_UNPAID, "Не оплачен"),
        (STATUS_PARTIAL, "Оплачен частично"),
        (STATUS_PAID, "Оплачен полностью"),
    )

    sale = models.OneToOneField(
        Sale,
        on_delete=models.CASCADE,
        related_name="credit",
        verbose_name="Продажа",
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name="credits",
        verbose_name="Клиент",
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        related_name="credits",
        verbose_name="Магазин",
    )
    original_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Изначальная сумма долга",
    )
    remaining_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Остаток долга",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_UNPAID,
        verbose_name="Статус долга",
    )
    comment = models.TextField(
        blank=True,
        verbose_name="Комментарий",
    )

    class Meta:
        verbose_name = "Кредит"
        verbose_name_plural = "Кредиты"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["store", "status"]),
            models.Index(fields=["customer", "store"]),
            models.Index(fields=["remaining_amount"]),
        ]

    def __str__(self):
        return f"Кредит #{self.id} | {self.customer} | Остаток: {self.remaining_amount}"

    def clean(self):
        if self.original_amount < 0:
            raise ValidationError({"original_amount": "Сумма долга не может быть отрицательной."})

        if self.remaining_amount < 0:
            raise ValidationError({"remaining_amount": "Остаток долга не может быть отрицательным."})

        if self.remaining_amount > self.original_amount:
            raise ValidationError({"remaining_amount": "Остаток долга не может быть больше исходной суммы."})

    def recalculate(self):
        paid_amount = sum((payment.amount for payment in self.payments.all()), Decimal("0.00"))
        remaining = self.original_amount - paid_amount

        if remaining < 0:
            remaining = Decimal("0.00")

        self.remaining_amount = remaining

        if remaining == self.original_amount:
            self.status = self.STATUS_UNPAID
        elif remaining == Decimal("0.00"):
            self.status = self.STATUS_PAID
        else:
            self.status = self.STATUS_PARTIAL

        self.save(update_fields=["remaining_amount", "status", "updated_at"])


class CreditPayment(TimeStampedModel):
    credit = models.ForeignKey(
        Credit,
        on_delete=models.CASCADE,
        related_name="payments",
        verbose_name="Кредит",
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
        verbose_name = "Оплата по кредиту"
        verbose_name_plural = "Оплаты по кредитам"
        ordering = ["-date", "-id"]
        indexes = [
            models.Index(fields=["credit", "date"]),
        ]

    def __str__(self):
        return f"Оплата #{self.id} | {self.credit.customer} | {self.amount}"

    def clean(self):
        if self.amount <= 0:
            raise ValidationError({"amount": "Сумма оплаты должна быть больше 0."})

        remaining_before = self.credit.remaining_amount
        if self.pk:
            old = CreditPayment.objects.get(pk=self.pk)
            remaining_before += old.amount

        if self.amount > remaining_before:
            raise ValidationError(
                {"amount": f"Сумма оплаты больше остатка долга. Доступно к оплате: {remaining_before}"}
            )

    def save(self, *args, **kwargs):
        self.full_clean()

        is_new = self.pk is None
        old_amount = Decimal("0.00")

        if not is_new:
            old = CreditPayment.objects.get(pk=self.pk)
            old_amount = old.amount

        super().save(*args, **kwargs)

        cash_register, _ = CashRegister.objects.get_or_create(
            store=self.credit.store,
            defaults={"balance": Decimal("0.00")},
        )

        if is_new:
            cash_register.balance += self.amount
        else:
            cash_register.balance = cash_register.balance - old_amount + self.amount

        if cash_register.balance < 0:
            cash_register.balance = Decimal("0.00")

        cash_register.save(update_fields=["balance", "updated_at"])
        self.credit.recalculate()

    def delete(self, *args, **kwargs):
        cash_register, _ = CashRegister.objects.get_or_create(
            store=self.credit.store,
            defaults={"balance": Decimal("0.00")},
        )

        cash_register.balance -= self.amount
        if cash_register.balance < 0:
            cash_register.balance = Decimal("0.00")
        cash_register.save(update_fields=["balance", "updated_at"])

        credit = self.credit
        super().delete(*args, **kwargs)
        credit.recalculate()
