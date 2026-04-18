from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Store(TimeStampedModel):
    name = models.CharField(max_length=255, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Магазин"
        verbose_name_plural = "Магазины"

    def __str__(self):
        return self.name


class Supplier(TimeStampedModel):
    name = models.CharField(max_length=255, unique=True)
    phone = models.CharField(max_length=50, blank=True)
    comment = models.TextField(blank=True)

    class Meta:
        verbose_name = "Поставщик"
        verbose_name_plural = "Поставщики"

    def __str__(self):
        return self.name


class Product(TimeStampedModel):
    name = models.CharField(max_length=255, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Товар"
        verbose_name_plural = "Товары"

    def __str__(self):
        return self.name


class Customer(TimeStampedModel):
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=50, blank=True)

    class Meta:
        verbose_name = "Клиент"
        verbose_name_plural = "Клиенты"

    def __str__(self):
        return self.name


class Seller(TimeStampedModel):
    name = models.CharField(max_length=255)
    store = models.ForeignKey(
        Store,
        on_delete=models.CASCADE,
        related_name="sellers"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Продавец"
        verbose_name_plural = "Продавцы"

    def __str__(self):
        return f"{self.name} ({self.store.name})"