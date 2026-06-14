from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError
from django.db.models import Prefetch, Sum
from django.shortcuts import get_object_or_404, redirect, render

from .forms import SaleCashToCreditForm, SaleCreateForm, SaleItemCreateForm, purchase_item_options_data
from .models import CashRegister, Sale, SaleItem, SaleItemBatch
from .services import convert_sale_cash_to_credit, create_sale_from_valid_forms, preview_sale_cash_to_credit

SALE_LIST_PAGE_SIZE = 40


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


def _attach_validation_error(form, exc):
    if hasattr(exc, "message_dict"):
        for field_name, messages_list in exc.message_dict.items():
            target_field = field_name if field_name in form.fields else None
            for message in messages_list:
                form.add_error(target_field, message)
        return

    for message in exc.messages:
        form.add_error(None, message)


@login_required
def sale_create(request):
    if request.method == "POST":
        sale_form = SaleCreateForm(request.POST)
        item_form = SaleItemCreateForm(
            request.POST,
            store_id=request.POST.get("store"),
            product_id=request.POST.get("product"),
        )
        if sale_form.is_valid() and item_form.is_valid():
            try:
                create_sale_from_valid_forms(sale_form=sale_form, item_form=item_form)
            except ValidationError as exc:
                _attach_validation_error(item_form, exc)
            else:
                messages.success(request, "Продажа сохранена.")
                return redirect("sales:sale_list")
    else:
        sale_form = SaleCreateForm()
        item_form = SaleItemCreateForm()

    context = {
        "sale_form": sale_form,
        "item_form": item_form,
        "purchase_item_options": purchase_item_options_data(),
        "selected_purchase_item_id": request.POST.get("purchase_item", "") if request.method == "POST" else "",
    }
    return render(request, "sales/sale_form.html", context)


@login_required
def sale_list(request):
    batch_queryset = (
        SaleItemBatch.objects.select_related("purchase_item__purchase__supplier")
        .only(
            "id",
            "sale_item_id",
            "purchase_item_id",
            "quantity",
            "purchase_item__id",
            "purchase_item__purchase_id",
            "purchase_item__purchase_price_per_kg",
            "purchase_item__purchase__id",
            "purchase_item__purchase__date",
            "purchase_item__purchase__supplier_id",
            "purchase_item__purchase__supplier__id",
            "purchase_item__purchase__supplier__name",
        )
        .order_by("id")
    )
    item_queryset = (
        SaleItem.objects.select_related("product")
        .only(
            "id",
            "sale_id",
            "product_id",
            "quantity_kg",
            "sale_price_per_kg",
            "line_total",
            "product__id",
            "product__name",
        )
        .prefetch_related(Prefetch("batches", queryset=batch_queryset))
        .order_by("id")
    )
    sales_queryset = (
        Sale.objects.select_related("store", "customer")
        .filter(deleted_at__isnull=True)
        .only(
            "id",
            "date",
            "store_id",
            "payment_type",
            "customer_id",
            "total_amount",
            "store__id",
            "store__name",
            "customer__id",
            "customer__name",
        )
        .prefetch_related(Prefetch("items", queryset=item_queryset))
        .order_by("-date", "-id")
    )
    page_number = _positive_page_number(request.GET.get("page"))
    offset = (page_number - 1) * SALE_LIST_PAGE_SIZE
    sales_page = list(sales_queryset[offset : offset + SALE_LIST_PAGE_SIZE + 1])
    page_obj = SimplePage(
        sales_page[:SALE_LIST_PAGE_SIZE],
        number=page_number,
        has_next=len(sales_page) > SALE_LIST_PAGE_SIZE,
    )
    cash_registers = (
        CashRegister.objects.select_related("store")
        .only("id", "store_id", "balance", "store__id", "store__name")
        .order_by("store__name")
    )
    return render(
        request,
        "sales/sale_list.html",
        {
            "sales": page_obj.object_list,
            "page_obj": page_obj,
            "cash_registers": cash_registers,
        },
    )


def _sale_item_rows_for_payment_change(sale):
    rows = []
    for item in sale.items.all():
        batch_rows = []
        for batch in item.batches.all():
            try:
                purchase_item = batch.purchase_item
                purchase = purchase_item.purchase
                supplier_name = purchase.supplier.name if purchase.supplier_id else "-"
                batch_rows.append(
                    {
                        "purchase_id": purchase_item.purchase_id,
                        "purchase_date": purchase.date,
                        "supplier_name": supplier_name,
                        "quantity": batch.quantity,
                    }
                )
            except ObjectDoesNotExist:
                batch_rows.append(
                    {
                        "purchase_id": None,
                        "purchase_date": None,
                        "supplier_name": "-",
                        "quantity": batch.quantity,
                    }
                )

        rows.append(
            {
                "product_name": item.product.name if item.product_id else "-",
                "quantity_kg": item.quantity_kg,
                "sale_price_per_kg": item.sale_price_per_kg,
                "line_total": item.line_total,
                "batches": batch_rows,
            }
        )
    return rows


@login_required
def sale_cash_to_credit(request, pk):
    sale = get_object_or_404(
        Sale.objects.select_related("store", "customer")
        .prefetch_related("items__product", "items__batches__purchase_item__purchase__supplier")
        .filter(deleted_at__isnull=True),
        pk=pk,
    )
    if sale.payment_type != Sale.PAYMENT_TYPE_CASH:
        messages.error(request, "Эта продажа уже не является наличной.")
        return redirect("sales:sale_list")

    if request.method == "POST":
        form = SaleCashToCreditForm(request.POST)
        if form.is_valid():
            customer = form.cleaned_data["customer"]
            try:
                convert_sale_cash_to_credit(
                    sale_id=sale.pk,
                    customer_id=customer.pk,
                    note=f"Переведена из наличной продажи в кредит. Клиент: {customer}.",
                )
            except ValidationError as exc:
                _attach_validation_error(form, exc)
            else:
                messages.success(request, "Продажа переведена в кредит. Касса и долг клиента пересчитаны.")
                return redirect("sales:sale_list")
    else:
        form = SaleCashToCreditForm()

    preview_customer = None
    if form.is_bound and form.is_valid():
        preview_customer = form.cleaned_data["customer"]
    preview = preview_sale_cash_to_credit(sale_id=sale.pk, customer=preview_customer)

    return render(
        request,
        "sales/sale_cash_to_credit.html",
        {
            "sale": sale,
            "sale_item_rows": _sale_item_rows_for_payment_change(sale),
            "form": form,
            "preview": preview,
        },
    )


@login_required
def cash_registers(request):
    registers = CashRegister.objects.select_related("store").order_by("store__name")
    total_cash = registers.aggregate(total=Sum("balance"))["total"] or 0
    return render(
        request,
        "sales/cash_registers.html",
        {
            "cash_registers": registers,
            "total_cash": total_cash,
        },
    )
