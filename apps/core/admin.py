from django.contrib import admin
from .models import Store, Supplier, Product, Customer, Seller


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "is_active", "created_at")
    search_fields = ("name",)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "phone")
    search_fields = ("name",)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "is_active")
    search_fields = ("name",)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "phone")
    search_fields = ("name",)


@admin.register(Seller)
class SellerAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "store", "is_active")
    list_filter = ("store",)
    search_fields = ("name",)