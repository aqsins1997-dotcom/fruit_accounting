import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.core.models import Store

from .forms import ClientDebtPaymentCreateForm
from .models import ClientDebtPayment, Credit
from .services import build_debtor_rows, get_client_debt


def _attach_validation_error(form, exc):
    if hasattr(exc, "message_dict"):
        for field_name, messages_list in exc.message_dict.items():
            target_field = field_name if field_name in form.fields else None
            for message in messages_list:
                form.add_error(target_field, message)
        return

    for message in exc.messages:
        form.add_error(None, message)


def _payment_create_url(*, store_id=None, client_id=None, amount=None):
    url = reverse("credits:client_debt_payment_create")
    params = []
    if store_id:
        params.append(f"store={store_id}")
    if client_id:
        params.append(f"client={client_id}")
    if amount is not None:
        params.append(f"amount={amount}")
    if params:
        url = f"{url}?{'&'.join(params)}"
    return url


def _get_payment_initial(request):
    initial = {"paid_at": timezone.localdate()}
    store_id = request.GET.get("store")
    client_id = request.GET.get("client")

    if store_id:
        initial["store"] = store_id
    if client_id:
        initial["client"] = client_id

    if store_id and client_id:
        current_debt = get_client_debt(store_id=store_id, client_id=client_id)
        if current_debt > 0:
            initial["amount"] = current_debt
    elif request.GET.get("amount"):
        initial["amount"] = request.GET["amount"]

    return initial


def _selected_payment_context(request, form):
    cleaned_data = getattr(form, "cleaned_data", {}) if form.is_bound else {}
    store = cleaned_data.get("store")
    client = cleaned_data.get("client")

    if not store and request.GET.get("store"):
        try:
            store = form.fields["store"].queryset.get(pk=request.GET["store"])
        except (ValueError, form.fields["store"].queryset.model.DoesNotExist):
            store = None
    if not client and request.GET.get("client"):
        try:
            client = form.fields["client"].queryset.get(pk=request.GET["client"])
        except (ValueError, form.fields["client"].queryset.model.DoesNotExist):
            client = None

    current_debt = None
    if store and client:
        current_debt = get_client_debt(store=store, client=client)

    return {
        "selected_store": store,
        "selected_client": client,
        "current_debt": current_debt,
    }


@login_required
def credit_payment_create(request, credit_id):
    credit = get_object_or_404(
        Credit.objects.select_related("customer", "store").filter(
            sale__deleted_at__isnull=True
        ),
        pk=credit_id,
    )

    return redirect(
        _payment_create_url(
            store_id=credit.store_id,
            client_id=credit.customer_id,
            amount=credit.remaining_amount,
        )
    )


@login_required
def client_debt_payment_create(request):
    if request.method == "POST":
        form = ClientDebtPaymentCreateForm(request.POST)
        if form.is_valid():
            payment = form.save(commit=False)
            payment.employee = request.user
            try:
                payment.save()
            except ValidationError as exc:
                _attach_validation_error(form, exc)
            else:
                messages.success(request, "Оплата долга успешно сохранена")
                return redirect("credits:credit_payment_list")
    else:
        form = ClientDebtPaymentCreateForm(initial=_get_payment_initial(request))

    context = {
        "form": form,
        **_selected_payment_context(request, form),
    }
    return render(request, "credits/payment_form.html", context)


@login_required
def credit_payment_list(request):
    payments = (
        ClientDebtPayment.objects.select_related("store", "client", "employee")
        .order_by("-paid_at", "-id")
    )
    return render(request, "credits/payment_list.html", {"payments": payments})


@login_required
@require_GET
def api_debtors(request):
    store = None
    store_id = request.GET.get("store")
    if store_id:
        try:
            store = Store.objects.get(pk=store_id)
        except (ValueError, Store.DoesNotExist):
            return JsonResponse({"error": "Магазин не найден."}, status=404)

    rows = build_debtor_rows(store=store, search=request.GET.get("q", "").strip())
    return JsonResponse(
        {
            "results": [
                {
                    "store_id": row["store_id"],
                    "store_name": row["store_name"],
                    "client_id": row["customer_id"],
                    "client_name": row["customer_name"],
                    "client_phone": row["customer_phone"],
                    "debt": str(row["total_debt"]),
                    "total_taken": str(row["total_taken"]),
                    "total_paid": str(row["total_paid"]),
                }
                for row in rows
            ]
        }
    )


@login_required
@require_GET
def api_client_debt(request):
    store_id = request.GET.get("store")
    client_id = request.GET.get("client")

    if not store_id or not client_id:
        return JsonResponse({"error": "Нужно указать магазин и клиента."}, status=400)

    return JsonResponse({"debt": str(get_client_debt(store_id=store_id, client_id=client_id))})


def _request_data(request):
    content_type = request.headers.get("Content-Type", "")
    if content_type.startswith("application/json"):
        try:
            return json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
    return request.POST


def _payment_to_dict(payment):
    return {
        "id": payment.id,
        "paid_at": payment.paid_at.isoformat(),
        "store_id": payment.store_id,
        "store_name": payment.store.name,
        "client_id": payment.client_id,
        "client_name": payment.client.name,
        "amount": str(payment.amount),
        "payment_method": payment.payment_method,
        "payment_method_display": payment.get_payment_method_display(),
        "comment": payment.comment,
        "employee": payment.employee.username if payment.employee else "",
    }


@login_required
@require_POST
def api_client_payment_create(request):
    form = ClientDebtPaymentCreateForm(_request_data(request))
    if not form.is_valid():
        return JsonResponse({"errors": form.errors.get_json_data()}, status=400)

    payment = form.save(commit=False)
    payment.employee = request.user
    try:
        payment.save()
    except ValidationError as exc:
        if hasattr(exc, "message_dict"):
            return JsonResponse({"errors": exc.message_dict}, status=400)
        return JsonResponse({"errors": {"__all__": exc.messages}}, status=400)

    return JsonResponse({"payment": _payment_to_dict(payment)}, status=201)


@login_required
@require_GET
def api_client_payment_history(request):
    payments = ClientDebtPayment.objects.select_related("store", "client", "employee").order_by("-paid_at", "-id")

    if request.GET.get("store"):
        payments = payments.filter(store_id=request.GET["store"])
    if request.GET.get("client"):
        payments = payments.filter(client_id=request.GET["client"])

    return JsonResponse({"results": [_payment_to_dict(payment) for payment in payments]})
