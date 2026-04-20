from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import Seller, Store


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ExpenseCategory(TimeStampedModel):
    name = models.CharField(max_length=255, unique=True, verbose_name="Категория")
    is_active = models.BooleanField(default=True, verbose_name="Активна")

    class Meta:
        verbose_name = "Категория расхода"
        verbose_name_plural = "Категории расходов"
        ordering = ["name"]

    def __str__(self):
        return self.name


class CashDocumentBase(TimeStampedModel):
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        verbose_name="Магазин",
    )
    date = models.DateField(verbose_name="Дата")
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Сумма",
    )
    comment = models.TextField(blank=True, verbose_name="Комментарий")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
        blank=True,
        verbose_name="Создал",
    )

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if self.amount is None or self.amount <= Decimal("0.00"):
            raise ValidationError({"amount": "Сумма должна быть больше 0."})


class EmployeeAdvance(CashDocumentBase):
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        related_name="employee_advances",
        verbose_name="Магазин",
    )
    seller = models.ForeignKey(
        Seller,
        on_delete=models.PROTECT,
        related_name="employee_advances",
        verbose_name="Сотрудник",
    )
    date = models.DateField(verbose_name="Дата выдачи")
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Сумма аванса",
    )
    comment = models.TextField(blank=True, verbose_name="Комментарий")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="created_employee_advances",
        null=True,
        blank=True,
        verbose_name="Создал",
    )

    class Meta:
        verbose_name = "Подотчетный аванс"
        verbose_name_plural = "Подотчетные авансы"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.seller} | {self.store} | {self.amount}"

    @property
    def confirmed_expenses_amount(self):
        total = self.expenses.aggregate(total=models.Sum("amount"))["total"]
        return total or Decimal("0.00")

    @property
    def remaining_amount(self):
        remaining = self.amount - self.confirmed_expenses_amount
        return remaining if remaining > Decimal("0.00") else Decimal("0.00")

    def clean(self):
        super().clean()
        errors = {}

        if self.seller_id and self.store_id and self.seller.store_id != self.store_id:
            errors["seller"] = "Сотрудник должен относиться к выбранному магазину."

        if errors:
            raise ValidationError(errors)


class Expense(CashDocumentBase):
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        related_name="employee_expenses",
        verbose_name="Магазин",
    )
    seller = models.ForeignKey(
        Seller,
        on_delete=models.PROTECT,
        related_name="expenses",
        verbose_name="Сотрудник",
    )
    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.PROTECT,
        related_name="expenses",
        verbose_name="Категория",
    )
    date = models.DateField(verbose_name="Дата расхода")
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Сумма расхода",
    )
    comment = models.TextField(blank=True, verbose_name="Комментарий")
    advance = models.ForeignKey(
        EmployeeAdvance,
        on_delete=models.PROTECT,
        related_name="expenses",
        null=True,
        blank=True,
        verbose_name="Аванс",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="created_employee_expenses",
        null=True,
        blank=True,
        verbose_name="Создал",
    )

    class Meta:
        verbose_name = "Расход сотрудника"
        verbose_name_plural = "Расходы сотрудников"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.seller} | {self.category} | {self.amount}"

    def clean(self):
        super().clean()
        errors = {}

        if self.seller_id and self.store_id and self.seller.store_id != self.store_id:
            errors["seller"] = "Сотрудник должен относиться к выбранному магазину."

        if self.advance_id:
            if self.advance.store_id != self.store_id:
                errors["advance"] = "Аванс должен относиться к выбранному магазину."
            if self.advance.seller_id != self.seller_id:
                errors["advance"] = "Аванс должен относиться к выбранному сотруднику."
            if self.date and self.advance.date and self.date < self.advance.date:
                errors["date"] = "Дата расхода не может быть раньше даты выбранного аванса."

        if errors:
            raise ValidationError(errors)

        if self.store_id and self.seller_id and self.date and self.amount:
            from .services import validate_expense_balance

            validate_expense_balance(
                expense=self,
                allow_overrun=getattr(self, "_allow_overrun", False),
            )


class StoreExpense(CashDocumentBase):
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        related_name="store_expenses",
        verbose_name="Магазин",
    )
    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.PROTECT,
        related_name="store_expenses",
        verbose_name="Категория",
    )
    date = models.DateField(verbose_name="Дата расхода")
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Сумма расхода",
    )
    comment = models.TextField(blank=True, verbose_name="Комментарий")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="created_store_expenses",
        null=True,
        blank=True,
        verbose_name="Создал",
    )

    class Meta:
        verbose_name = "Расход магазина"
        verbose_name_plural = "Расходы магазина"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.store} | {self.category} | {self.amount}"


class SalaryPayment(CashDocumentBase):
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        related_name="salary_payments",
        verbose_name="Магазин",
    )
    seller = models.ForeignKey(
        Seller,
        on_delete=models.PROTECT,
        related_name="salary_payments",
        verbose_name="Сотрудник",
    )
    date = models.DateField(verbose_name="Дата выплаты")
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Сумма зарплаты",
    )
    comment = models.TextField(blank=True, verbose_name="Комментарий")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="created_salary_payments",
        null=True,
        blank=True,
        verbose_name="Создал",
    )

    class Meta:
        verbose_name = "Выплата зарплаты"
        verbose_name_plural = "Выплаты зарплаты"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.seller} | {self.store} | {self.amount}"

    def clean(self):
        super().clean()
        if self.seller_id and self.store_id and self.seller.store_id != self.store_id:
            raise ValidationError({"seller": "Сотрудник должен относиться к выбранному магазину."})
