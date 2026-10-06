import csv
import io

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .models import ApiToken, Product

REQUIRED_COLUMNS = {"sku", "title", "price", "image_url"}


@staff_member_required
def dashboard(request):
    new_token = None

    if request.method == "POST":
        label = request.POST.get("label", "").strip()
        quota_raw = request.POST.get("image_quota", "").strip()
        period = request.POST.get("period", ApiToken.Period.MONTHLY)

        error = None
        if not label:
            error = "لازم تكتب اسم للتعريف"
        elif not quota_raw.isdigit() or int(quota_raw) <= 0:
            error = "عدد الصور لازم يكون رقم أكبر من صفر"
        elif period not in ApiToken.Period.values:
            error = "فترة غير صحيحة"

        if error:
            messages.error(request, error)
        else:
            new_token = ApiToken.objects.create(
                label=label,
                image_quota=int(quota_raw),
                period=period,
                created_by=request.user,
            )
            messages.success(request, "تم إنشاء التوكن — انسخه وابعتو للمستخدم")

    tokens = ApiToken.objects.annotate(product_count=Count("products"))
    stats = {
        "total": len(tokens),
        "active": sum(1 for t in tokens if t.is_active),
        "images_used": sum(t.images_used for t in tokens),
    }
    return render(
        request,
        "apitokens/dashboard.html",
        {
            "tokens": tokens,
            "new_token": new_token,
            "periods": ApiToken.Period.choices,
            "stats": stats,
        },
    )


@staff_member_required
@require_http_methods(["POST"])
def toggle_active(request, pk):
    token = get_object_or_404(ApiToken, pk=pk)
    token.is_active = not token.is_active
    token.save(update_fields=["is_active"])
    return redirect("apitokens:dashboard")


@staff_member_required
def catalog(request, pk):
    merchant = get_object_or_404(ApiToken, pk=pk)

    if request.method == "POST":
        upload = request.FILES.get("feed_file")
        if not upload:
            messages.error(request, "لازم تختار ملف CSV")
        else:
            error = _replace_catalog(merchant, upload)
            if error:
                messages.error(request, error)
            else:
                messages.success(request, "تم استبدال الكتالوج بنجاح")
        return redirect("apitokens:catalog", pk=pk)

    products = merchant.products.all()
    return render(
        request,
        "apitokens/catalog.html",
        {"merchant": merchant, "products": products},
    )


def _replace_catalog(merchant, upload):
    try:
        raw = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return "الملف لازم يكون UTF-8"

    reader = csv.DictReader(io.StringIO(raw))
    if reader.fieldnames is None or not REQUIRED_COLUMNS.issubset(set(reader.fieldnames)):
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        return f"أعمدة ناقصة بالملف: {', '.join(missing)}"

    new_products = []
    for i, row in enumerate(reader, start=2):
        sku = (row.get("sku") or "").strip()
        title = (row.get("title") or "").strip()
        image_url = (row.get("image_url") or "").strip()
        price_raw = (row.get("price") or "").strip()
        if not sku or not title or not image_url:
            return f"سطر {i}: sku أو title أو image_url ناقص"
        try:
            price = Decimal(price_raw)
        except InvalidOperation:
            return f"سطر {i}: السعر '{price_raw}' مش رقم صحيح"

        new_products.append(
            Product(
                merchant=merchant,
                sku=sku,
                title=title,
                description=(row.get("description") or "").strip(),
                price=price,
                image_url=image_url,
                product_url=(row.get("product_url") or "").strip(),
                category=(row.get("category") or "").strip(),
                tags=(row.get("tags") or "").strip(),
            )
        )

    if not new_products:
        return "الملف ما فيه ولا صف منتج"

    with transaction.atomic():
        merchant.products.all().delete()
        Product.objects.bulk_create(new_products)

    return None
