from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

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
        paid_amount = sum(
            (
                payment.amount
                for payment in self.payments.filter(
                    Q(client_debt_payment__isnull=True)
                    | Q(client_debt_payment__status=ClientDebtPayment.STATUS_ACTIVE)
                )
            ),
            Decimal("0.00"),
        )
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


class ClientDebtPayment(TimeStampedModel):
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

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        related_name="client_debt_payments",
        verbose_name="Магазин",
    )
    client = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name="debt_payments",
        db_column="client_id",
        verbose_name="Клиент",
    )
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
    paid_at = models.DateField(verbose_name="Дата оплаты")
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
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="client_debt_payments",
        db_column="employee_id",
        blank=True,
        null=True,
        verbose_name="Сотрудник",
    )

    class Meta:
        db_table = "client_debt_payments"
        verbose_name = "Оплата долга клиента"
        verbose_name_plural = "Оплаты долгов клиентов"
        ordering = ["-paid_at", "-id"]
        indexes = [
            models.Index(fields=["store", "client"]),
            models.Index(fields=["paid_at"]),
            models.Index(fields=["payment_method"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"Оплата клиента #{self.id} | {self.client} | {self.amount}"

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "Сумма оплаты должна быть больше 0."})

        if self.status == self.STATUS_CANCELLED:
            return

        if self.amount and self.store_id and self.client_id:
            from .services import get_client_debt

            current_debt = get_client_debt(
                store_id=self.store_id,
                client_id=self.client_id,
                exclude_payment_id=self.pk,
            )
            if self.amount > current_debt:
                raise ValidationError(
                    {"amount": "Сумма оплаты не может быть больше текущего долга клиента"}
                )

    @staticmethod
    def _apply_cash_delta(*, store_id, amount):
        if not store_id or amount == Decimal("0.00"):
            return

        cash_register, _ = CashRegister.objects.select_for_update().get_or_create(
            store_id=store_id,
            defaults={"balance": Decimal("0.00")},
        )
        cash_register.balance += amount
        cash_register.save(update_fields=["balance", "updated_at"])

    def _delete_allocations(self):
        for allocation in list(self.allocations.select_related("credit")):
            allocation.delete()

    def _recalculate_allocated_credits(self, credit_ids):
        for credit in Credit.objects.filter(id__in=set(credit_ids)):
            credit.recalculate()

    def _create_allocations(self):
        remaining_to_allocate = self.amount
        credits = (
            Credit.objects.select_for_update()
            .filter(
                store_id=self.store_id,
                customer_id=self.client_id,
                sale__deleted_at__isnull=True,
            )
            .exclude(status=Credit.STATUS_PAID)
            .order_by("sale__date", "id")
        )

        for credit in credits:
            if remaining_to_allocate <= Decimal("0.00"):
                break
            if credit.remaining_amount <= Decimal("0.00"):
                continue

            allocation_amount = min(credit.remaining_amount, remaining_to_allocate)
            CreditPayment.objects.create(
                credit=credit,
                client_debt_payment=self,
                date=self.paid_at,
                amount=allocation_amount,
                comment=self.comment,
            )
            remaining_to_allocate -= allocation_amount

        if remaining_to_allocate > Decimal("0.00"):
            raise ValidationError(
                {"amount": "Сумма оплаты не может быть больше текущего долга клиента"}
            )

    def save(self, *args, **kwargs):
        with transaction.atomic():
            previous = None
            previous_credit_ids = []
            if self.pk:
                previous = ClientDebtPayment.objects.select_for_update().get(pk=self.pk)
                previous_credit_ids = list(previous.allocations.values_list("credit_id", flat=True))

            if self.store_id and self.client_id:
                list(
                    Credit.objects.select_for_update()
                    .filter(
                        store_id=self.store_id,
                        customer_id=self.client_id,
                        sale__deleted_at__isnull=True,
                    )
                    .values_list("id", flat=True)
                )
                list(
                    ClientDebtPayment.objects.select_for_update()
                    .filter(store_id=self.store_id, client_id=self.client_id)
                    .exclude(pk=self.pk)
                    .values_list("id", flat=True)
                )

            self.full_clean()

            if previous:
                if previous.status == self.STATUS_ACTIVE:
                    self._apply_cash_delta(
                        store_id=previous.store_id,
                        amount=-previous.amount,
                    )

                if self.status == self.STATUS_ACTIVE:
                    previous._delete_allocations()
                elif previous.status == self.STATUS_ACTIVE:
                    self._recalculate_allocated_credits(previous_credit_ids)

            super().save(*args, **kwargs)
            if self.status == self.STATUS_ACTIVE:
                self._apply_cash_delta(store_id=self.store_id, amount=self.amount)
                self._create_allocations()
            else:
                self._recalculate_allocated_credits(previous_credit_ids)

    def delete(self, *args, **kwargs):
        self.cancel(reason="Удаление заменено безопасной отменой оплаты.")

    def cancel(self, *, reason=""):
        with transaction.atomic():
            payment = ClientDebtPayment.objects.select_for_update().get(pk=self.pk)
            if payment.status == self.STATUS_CANCELLED:
                return payment
            payment.status = self.STATUS_CANCELLED
            payment.cancelled_at = timezone.now()
            payment.cancel_reason = reason or payment.cancel_reason
            payment.save()
            return payment


class CreditPayment(TimeStampedModel):
    credit = models.ForeignKey(
        Credit,
        on_delete=models.CASCADE,
        related_name="payments",
        verbose_name="Кредит",
    )
    client_debt_payment = models.ForeignKey(
        ClientDebtPayment,
        on_delete=models.CASCADE,
        related_name="allocations",
        blank=True,
        null=True,
        verbose_name="Оплата долга клиента",
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

        if self.credit_id and self.credit.sale.deleted_at:
            raise ValidationError({"credit": "Нельзя внести оплату по удаленной продаже."})

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

        if not self.client_debt_payment_id:
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
        if not self.client_debt_payment_id:
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
