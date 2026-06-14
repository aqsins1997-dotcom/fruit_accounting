from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render

from apps.reports.services import build_product_profitability_rows, build_purchase_item_profitability_map

from .forms import (
    PurchaseCreateForm,
    PurchaseItemCreateForm,
    PurchaseItemPriceUpdateForm,
    PurchaseItemProductUpdateForm,
    PurchaseItemQuantityUpdateForm,
)
from .models import Purchase, PurchaseItem

PURCHASE_LIST_PAGE_SIZE = 40


class SimplePage:
    def __init__(self, object_list, *, number, has_next):
        self.object_list = object_list
        self.number = number
        self._has_next = has_next

    def has_previous(self):
        return self.number > 1

    def has_next(self):
        return self._has_next

    def has_other_pages(self):
        return self.has_previous() or self.has_next()

    def previous_page_number(self):
        return max(1, self.number - 1)

    def next_page_number(self):
        return self.number + 1


def _positive_page_number(value):
    try:
        page_number = int(value)
    except (TypeError, ValueError):
        return 1
    return page_number if page_number > 0 else 1


def _action_from_request(request):
    return request.POST.get("action") or "preview"


def _attach_validation_error(form, exc):
    if hasattr(exc, "message_dict"):
        for field, errors in exc.message_dict.items():
            target = field if field in form.fields else None
            for error in errors:
                form.add_error(target, error)
        return

    for error in getattr(exc, "messages", [str(exc)]):
        form.add_error(None, error)


@login_required
def purchase_create(request):
    if request.method == "POST":
        purchase_form = PurchaseCreateForm(request.POST)
        item_form = PurchaseItemCreateForm(request.POST)
        if purchase_form.is_valid() and item_form.is_valid():
            with transaction.atomic():
                purchase = purchase_form.save()
                item = item_form.save(commit=False)
                item.purchase = purchase
                item.save()
            messages.success(request, "Закупка сохранена.")
            return redirect("inventory:purchase_list")
    else:
        purchase_form = PurchaseCreateForm()
        item_form = PurchaseItemCreateForm()

    return render(
        request,
        "inventory/purchase_form.html",
        {"purchase_form": purchase_form, "item_form": item_form},
    )


@login_required
def purchase_list(request):
    item_queryset = (
        PurchaseItem.objects.select_related("store", "product")
        .only(
            "id",
            "purchase_id",
            "store_id",
            "product_id",
            "quantity_kg",
            "purchase_price_per_kg",
            "store__id",
            "store__name",
            "product__id",
            "product__name",
        )
        .order_by("id")
    )
    purchases_queryset = (
        Purchase.objects.select_related("supplier")
        .filter(deleted_at__isnull=True)
        .only(
            "id",
            "date",
            "supplier_id",
            "supplier__id",
            "supplier__name",
        )
        .prefetch_related(Prefetch("items", queryset=item_queryset))
        .order_by("-date", "-id")
    )
    page_number = _positive_page_number(request.GET.get("page"))
    offset = (page_number - 1) * PURCHASE_LIST_PAGE_SIZE
    purchase_rows = list(purchases_queryset[offset : offset + PURCHASE_LIST_PAGE_SIZE + 1])
    purchases = purchase_rows[:PURCHASE_LIST_PAGE_SIZE]
    page_obj = SimplePage(
        purchases,
        number=page_number,
        has_next=len(purchase_rows) > PURCHASE_LIST_PAGE_SIZE,
    )
    item_ids = [item.id for purchase in purchases for item in purchase.items.all()]
    profitability_map = build_purchase_item_profitability_map(purchase_item_ids=item_ids)
    for purchase in purchases:
        for item in purchase.items.all():
            item.profitability = profitability_map.get(item.id)

    return render(
        request,
        "inventory/purchase_list.html",
        {
            "purchases": purchases,
            "page_obj": page_obj,
        },
    )


@login_required
def purchase_item_product_update(request, pk):
    item = get_object_or_404(
        PurchaseItem.objects.select_related("purchase__supplier", "store", "product").filter(
            purchase__deleted_at__isnull=True
        ),
        pk=pk,
    )

    if request.method == "POST":
        form = PurchaseItemProductUpdateForm(request.POST, instance=item)
        if form.is_valid():
            try:
                result = form.save()
            except ValidationError as exc:
                _attach_validation_error(form, exc)
            else:
                if result["changed"]:
                    messages.success(request, "Товар закупки успешно обновлен.")
                else:
                    messages.success(request, "Товар закупки не изменился.")
                return redirect("inventory:purchase_list")
    else:
        form = PurchaseItemProductUpdateForm(instance=item)

    return render(
        request,
        "inventory/purchase_product_update.html",
        {"form": form, "item": item},
    )


@login_required
def purchase_item_price_update(request, pk):
    item_queryset = PurchaseItem.objects.filter(purchase__deleted_at__isnull=True)
    if request.method == "POST":
        item_queryset = item_queryset.select_related("purchase").only(
            "id",
            "purchase_id",
            "store_id",
            "product_id",
            "quantity_kg",
            "purchase_price_per_kg",
            "purchase__id",
            "purchase__supplier_id",
            "purchase__deleted_at",
        )
    else:
        item_queryset = item_queryset.select_related("purchase__supplier", "store", "product")
    item = get_object_or_404(item_queryset, pk=pk)
    preview = None

    if request.method == "POST":
        form = PurchaseItemPriceUpdateForm(request.POST, instance=item)
        if form.is_valid():
            if _action_from_request(request) == "apply":
                form.save()
                messages.success(request, "Цена закупки успешно обновлена.")
                return redirect("inventory:purchase_list")
            preview = form.build_preview()
    else:
        form = PurchaseItemPriceUpdateForm(instance=item)

    return render(
        request,
        "inventory/purchase_price_update.html",
        {"form": form, "item": item, "preview": preview},
    )


@login_required
def purchase_item_quantity_update(request, pk):
    item_queryset = PurchaseItem.objects.select_related(
        "purchase__supplier",
        "store",
        "product",
    ).filter(purchase__deleted_at__isnull=True)

    if request.method == "POST":
        with transaction.atomic():
            item = get_object_or_404(
                item_queryset.select_for_update(),
                pk=pk,
            )
            preview = None
            form = PurchaseItemQuantityUpdateForm(request.POST, instance=item)
            if form.is_valid():
                if _action_from_request(request) == "apply":
                    form.save()
                    messages.success(request, "Вес закупки успешно обновлен.")
                    return redirect("inventory:purchase_list")
                preview = form.build_preview()
    else:
        item = get_object_or_404(
            item_queryset,
            pk=pk,
        )
        preview = None
        form = PurchaseItemQuantityUpdateForm(instance=item)

    return render(
        request,
        "inventory/purchase_quantity_update.html",
        {
            "form": form,
            "item": item,
            "preview": preview,
            "sold_quantity": form.sold_quantity,
            "current_stock_quantity": form.current_stock_quantity,
            "paid_amount": form.paid_amount,
            "current_purchase_total": form.current_purchase_total_for_store,
            "new_purchase_total": form.new_purchase_total_for_store,
        },
    )


@login_required
def stock_list(request):
    stocks = build_product_profitability_rows(group_by_store=True)
    return render(request, "inventory/stock_list.html", {"stocks": stocks})
