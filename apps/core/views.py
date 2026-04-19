from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .forms import CustomerForm, ProductForm, SellerForm, StoreForm, SupplierForm
from .models import Customer, Product, Seller, Store, Supplier


@login_required
def dashboard(request):
    context = {
        "sections": [
            {
                "title": "Справочники",
                "items": ["Поставщики", "Товары", "Магазины", "Продавцы", "Клиенты"],
            },
            {
                "title": "Операции",
                "items": ["Закупки", "Продажи", "Оплаты поставщикам", "Погашения долгов клиентов"],
            },
            {
                "title": "Отчеты",
                "items": ["Ежедневный отчет", "Остатки", "Должники", "Долги поставщикам"],
            },
        ]
    }
    return render(request, "core/dashboard.html", context)


def _directory_page(request, *, form_class, queryset, template_name, title, success_message):
    if request.method == "POST":
        form = form_class(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, success_message)
            return redirect(request.path)
    else:
        form = form_class()

    context = {
        "title": title,
        "form": form,
        "objects": queryset,
    }
    return render(request, template_name, context)


@login_required
def stores_page(request):
    return _directory_page(
        request,
        form_class=StoreForm,
        queryset=Store.objects.order_by("name"),
        template_name="core/directory_page.html",
        title="Магазины",
        success_message="Магазин добавлен.",
    )


@login_required
def suppliers_page(request):
    return _directory_page(
        request,
        form_class=SupplierForm,
        queryset=Supplier.objects.order_by("name"),
        template_name="core/directory_page.html",
        title="Поставщики",
        success_message="Поставщик добавлен.",
    )


@login_required
def products_page(request):
    return _directory_page(
        request,
        form_class=ProductForm,
        queryset=Product.objects.order_by("name"),
        template_name="core/directory_page.html",
        title="Товары",
        success_message="Товар добавлен.",
    )


@login_required
def customers_page(request):
    return _directory_page(
        request,
        form_class=CustomerForm,
        queryset=Customer.objects.order_by("name"),
        template_name="core/directory_page.html",
        title="Клиенты",
        success_message="Клиент добавлен.",
    )


@login_required
def sellers_page(request):
    return _directory_page(
        request,
        form_class=SellerForm,
        queryset=Seller.objects.select_related("store").order_by("name"),
        template_name="core/directory_page.html",
        title="Продавцы",
        success_message="Продавец добавлен.",
    )
