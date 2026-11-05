import csv
import io
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .i18n import LANGUAGES, t
from .models import (
    ApiToken, Conversation, DiscountCode, Product, SallaConnection, ShopifyConnection, SizeChart, ZidConnection,
    ZidSubscription,
)
from .services import catalog as catalog_service
from .services import salla as salla_service
from .services import sizing
from .services import shopify as shopify_service
from .services import zid as zid_service

REQUIRED_COLUMNS = {"sku", "title", "price", "image_url"}


def set_language(request):
    lang = request.POST.get("lang") or request.GET.get("lang")
    if lang in LANGUAGES:
        request.session["lang"] = lang
    next_url = request.POST.get("next") or request.GET.get("next") or "/"
    if not next_url.startswith("/"):
        next_url = "/"
    return HttpResponseRedirect(next_url)


@staff_member_required
def dashboard(request):
    new_token = None
    lang = request.session.get("lang", "ar")

    if request.method == "POST":
        label = request.POST.get("label", "").strip()
        quota_raw = request.POST.get("image_quota", "").strip()
        period = request.POST.get("period", ApiToken.Period.MONTHLY)

        error = None
        if not label:
            error = t("err_label_required", lang)
        elif not quota_raw.isdigit() or int(quota_raw) <= 0:
            error = t("err_quota_invalid", lang)
        elif period not in ApiToken.Period.values:
            error = t("err_period_invalid", lang)

        if error:
            messages.error(request, error)
        else:
            new_token = ApiToken.objects.create(
                label=label,
                image_quota=int(quota_raw),
                period=period,
                created_by=request.user,
            )
            messages.success(request, t("msg_token_created", lang))

    tokens = ApiToken.objects.annotate(product_count=Count("products")).select_related(
        "shopify_connection"
    )
    stats = {
        "total": len(tokens),
        "active": sum(1 for tok in tokens if tok.is_active),
        "images_used": sum(tok.images_used for tok in tokens),
    }
    periods = [(value, t("period_" + value, lang)) for value in ApiToken.Period.values]
    return render(
        request,
        "apitokens/dashboard.html",
        {
            "tokens": tokens,
            "new_token": new_token,
            "periods": periods,
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
    lang = request.session.get("lang", "ar")

    if request.method == "POST" and request.POST.get("action") in ("size_chart", "size_chart_delete"):
        category = request.POST.get("category", "").strip()[:120]
        if request.POST.get("action") == "size_chart_delete":
            SizeChart.objects.filter(merchant=merchant, category=category).delete()
            messages.success(request, t("msg_size_chart_deleted", lang))
            return redirect("apitokens:catalog", pk=pk)
        try:
            chart = sizing.parse_chart(request.POST.get("chart", ""))
        except sizing.SizeChartError as exc:
            messages.error(request, t("err_size_chart_invalid", lang) % {"where": exc})
            return redirect("apitokens:catalog", pk=pk)
        if not chart:
            messages.error(request, t("err_size_chart_empty", lang))
            return redirect("apitokens:catalog", pk=pk)
        SizeChart.objects.update_or_create(merchant=merchant, category=category, defaults={"chart": chart})
        messages.success(request, t("msg_size_chart_saved", lang))
        return redirect("apitokens:catalog", pk=pk)

    if request.method == "POST" and request.POST.get("action") == "store_info":
        merchant.store_info = request.POST.get("store_info", "").strip()[:8000]
        merchant.currency = request.POST.get("currency", "").strip().upper()[:8]
        merchant.save(update_fields=["store_info", "currency"])
        messages.success(request, t("msg_store_info_saved", lang))
        return redirect("apitokens:catalog", pk=pk)

    if request.method == "POST":
        upload = request.FILES.get("feed_file")
        if not upload:
            messages.error(request, t("err_csv_required", lang))
        else:
            error = _replace_catalog(merchant, upload, lang)
            if error:
                messages.error(request, error)
            else:
                messages.success(request, t("msg_catalog_replaced", lang))
        return redirect("apitokens:catalog", pk=pk)

    products = merchant.products.all()
    return render(
        request,
        "apitokens/catalog.html",
        {
            "merchant": merchant,
            "products": products,
            "size_charts": [
                {"category": c.category, "text": sizing.chart_to_text(c.chart)}
                for c in merchant.size_charts.order_by("category")
            ],
        },
    )


def _replace_catalog(merchant, upload, lang="ar"):
    try:
        raw = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return t("err_file_utf8", lang)

    reader = csv.DictReader(io.StringIO(raw))
    if reader.fieldnames is None or not REQUIRED_COLUMNS.issubset(set(reader.fieldnames)):
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        return t("err_missing_columns", lang) % {"missing": ", ".join(missing)}

    new_products = []
    for i, row in enumerate(reader, start=2):
        sku = (row.get("sku") or "").strip()
        title = (row.get("title") or "").strip()
        image_url = (row.get("image_url") or "").strip()
        price_raw = (row.get("price") or "").strip()
        if not sku or not title or not image_url:
            return t("err_row_missing_field", lang) % {"row": i}
        try:
            price = Decimal(price_raw)
        except InvalidOperation:
            return t("err_row_bad_price", lang) % {"row": i, "price": price_raw}

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
        return t("err_empty_csv", lang)

    with transaction.atomic():
        merchant.products.all().delete()
        Product.objects.bulk_create(new_products)

    return None


@staff_member_required
def shopify_connect(request, pk):
    merchant = get_object_or_404(ApiToken, pk=pk)
    connection = getattr(merchant, "shopify_connection", None)
    lang = request.session.get("lang", "ar")

    if request.method == "POST":
        action = request.POST.get("action", "connect")

        if action == "disconnect":
            if connection:
                connection.delete()
                messages.success(request, t("msg_shopify_disconnected", lang))
            return redirect("apitokens:shopify-connect", pk=pk)

        if action == "resync":
            if not connection:
                messages.error(request, t("err_no_connection_to_resync", lang))
            else:
                try:
                    catalog_service.sync_shopify(merchant, connection)
                    messages.success(request, t("msg_resync_success", lang))
                except shopify_service.ShopifyError as exc:
                    messages.error(request, str(exc))
            return redirect("apitokens:shopify-connect", pk=pk)

        shop_domain = _normalize_shop_domain(request.POST.get("shop_domain", ""))
        client_id = request.POST.get("client_id", "").strip()
        access_token = request.POST.get("access_token", "").strip()
        api_secret = request.POST.get("api_secret", "").strip()

        if not shop_domain or not api_secret or not (client_id or access_token):
            messages.error(request, t("err_missing_connect_fields", lang))
            return redirect("apitokens:shopify-connect", pk=pk)

        # تطبيق Dev Dashboard (client_id) بيتقدّم على التوكن الثابت القديم لو الاتنين موجودين.
        candidate = ShopifyConnection(
            merchant=merchant,
            shop_domain=shop_domain,
            client_id=client_id,
            access_token="" if client_id else access_token,
            api_secret=api_secret,
        )
        try:
            token = shopify_service.get_access_token(candidate)
            shopify_service.get_shop_info(shop_domain, token)
        except shopify_service.ShopifyError as exc:
            messages.error(request, str(exc))
            return redirect("apitokens:shopify-connect", pk=pk)

        connection, _ = ShopifyConnection.objects.update_or_create(
            merchant=merchant,
            defaults={
                "shop_domain": shop_domain,
                "client_id": client_id,
                "access_token": token,
                "token_expires_at": candidate.token_expires_at,
                "api_secret": api_secret,
                "is_active": True,
            },
        )

        callback_url = f"{settings.SITE_BASE_URL}/webhooks/shopify/{merchant.token}/"
        try:
            orders_ok = shopify_service.register_webhooks(shop_domain, token, callback_url)
            catalog_service.sync_shopify(merchant, connection)
            messages.success(request, t("msg_connect_sync_success", lang))
            if not orders_ok:
                messages.warning(request, t("warn_no_orders_scope", lang))
        except shopify_service.ShopifyError as exc:
            messages.error(request, t("err_connect_sync_partial", lang) % {"exc": exc})

        return redirect("apitokens:shopify-connect", pk=pk)

    return render(
        request,
        "apitokens/shopify_connect.html",
        {"merchant": merchant, "connection": connection},
    )


def _normalize_shop_domain(value):
    """بيقبل الدومين بأي شكل الزبون بينسخه (مع https:// أو / بالآخر) وبيرجّع
    my-store.myshopify.com. لو ما في نقطة منفترض إنه اسم المتجر بس."""
    domain = value.strip().lower()
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    domain = domain.split("/")[0]
    if domain and "." not in domain:
        domain += ".myshopify.com"
    return domain


@staff_member_required
def discount_codes(request, pk):
    merchant = get_object_or_404(ApiToken, pk=pk)
    lang = request.session.get("lang", "ar")

    if request.method == "POST":
        action = request.POST.get("action", "add")

        if action == "toggle":
            code_obj = get_object_or_404(
                DiscountCode, pk=request.POST.get("code_id"), merchant=merchant
            )
            code_obj.is_active = not code_obj.is_active
            code_obj.save(update_fields=["is_active"])
            return redirect("apitokens:discount-codes", pk=pk)

        code = request.POST.get("code", "").strip()
        description = request.POST.get("description", "").strip()
        if not code:
            messages.error(request, t("err_discount_code_required", lang))
        else:
            DiscountCode.objects.create(merchant=merchant, code=code, description=description)
            messages.success(request, t("msg_discount_code_added", lang))
        return redirect("apitokens:discount-codes", pk=pk)

    return render(
        request,
        "apitokens/discount_codes.html",
        {"merchant": merchant, "codes": merchant.discount_codes.all()},
    )


@staff_member_required
def conversations(request, pk):
    merchant = get_object_or_404(ApiToken, pk=pk)
    since = timezone.now() - timedelta(days=30)
    recent = Conversation.objects.filter(merchant=merchant, started_at__gte=since)
    agg = recent.aggregate(
        total=Count("id"),
        tryons=Sum("tryons"),
        intent=Count("id", filter=Q(buy_clicks__gt=0) | Q(add_to_carts__gt=0)),
        orders=Count("id", filter=~Q(order_id="")),
        revenue=Sum("order_total"),
    )
    with_recommendation = sum(1 for skus in recent.values_list("recommended_skus", flat=True) if skus)
    stats = {
        "total": agg["total"],
        "with_recommendation": with_recommendation,
        "tryons": agg["tryons"] or 0,
        "intent": agg["intent"],
        "orders": agg["orders"],
        "revenue": f"{agg['revenue'] or 0:.2f} {merchant.currency}".strip(),
    }
    return render(
        request,
        "apitokens/conversations.html",
        {
            "merchant": merchant,
            "stats": stats,
            "conversations": Conversation.objects.filter(merchant=merchant)[:50],
        },
    )


SALLA_SNIPPET_PATH = settings.BASE_DIR / "apitokens" / "static" / "apitokens" / "salla_snippet.js"


@staff_member_required
def salla_integration(request):
    """تطبيق سلة: الإعدادات المطلوبة، رابط الإشعارات والسكربت للصقهم ببوابة
    الشركاء، والمتاجر المربوطة (بتنضاف لحالها لما التاجر يركّب التطبيق)."""
    lang = request.session.get("lang", "ar")
    if request.method == "POST" and request.POST.get("action") == "resync":
        connection = get_object_or_404(SallaConnection, pk=request.POST.get("connection_id"))
        try:
            count = catalog_service.sync_salla(connection.merchant, connection)
            messages.success(request, t("msg_salla_resync_success", lang) % {"count": count})
        except salla_service.SallaError as exc:
            messages.error(request, t("err_salla_resync_failed", lang) % {"exc": exc})
        return redirect("apitokens:salla")

    config = [
        ("SALLA_APP_ID", bool(settings.SALLA_APP_ID)),
        ("SALLA_CLIENT_ID", bool(settings.SALLA_CLIENT_ID)),
        ("SALLA_CLIENT_SECRET", bool(settings.SALLA_CLIENT_SECRET)),
        ("SALLA_WEBHOOK_SECRET", bool(settings.SALLA_WEBHOOK_SECRET)),
    ]
    connections = (
        SallaConnection.objects.select_related("merchant")
        .annotate(product_count=Count("merchant__products"))
        .order_by("-created_at")
    )
    return render(
        request,
        "apitokens/salla.html",
        {
            "config": config,
            "config_ready": all(ok for _, ok in config),
            "webhook_url": f"{settings.SITE_BASE_URL}/webhooks/salla/",
            "snippet": SALLA_SNIPPET_PATH.read_text(encoding="utf-8"),
            "connections": connections,
        },
    )


ZID_SNIPPET_PATH = settings.BASE_DIR / "apitokens" / "static" / "apitokens" / "zid_snippet.js"


@staff_member_required
def zid_integration(request):
    """تطبيق زد: المتاجر المفعّلة وباقاتها، الإعدادات المطلوبة، والقيم اللي
    بتنحط ببوابة شركاء زد."""
    lang = request.session.get("lang", "ar")
    if request.method == "POST" and request.POST.get("action") == "resync":
        connection = get_object_or_404(ZidConnection, pk=request.POST.get("connection_id"))
        try:
            count = catalog_service.sync_zid(connection.merchant, connection)
            messages.success(request, t("msg_zid_resync_success", lang) % {"count": count})
        except zid_service.ZidError as exc:
            messages.error(request, t("err_zid_resync_failed", lang) % {"exc": exc})
        return redirect("apitokens:zid")

    config = [
        ("ZID_CLIENT_ID", bool(settings.ZID_CLIENT_ID)),
        ("ZID_CLIENT_SECRET", bool(settings.ZID_CLIENT_SECRET)),
        ("ZID_WEBHOOK_SECRET", bool(settings.ZID_WEBHOOK_SECRET)),
    ]
    connections = list(
        ZidConnection.objects.select_related("merchant")
        .annotate(product_count=Count("merchant__products"))
        .order_by("-created_at")
    )
    subscriptions = {
        s.store_id: s for s in ZidSubscription.objects.filter(store_id__in=[c.store_id for c in connections])
    }
    for connection in connections:
        connection.subscription = subscriptions.get(connection.store_id)
    base = settings.SITE_BASE_URL
    return render(
        request,
        "apitokens/zid.html",
        {
            "config": config,
            "connections": connections,
            "portal_urls": [
                ("Application URL", f"{base}/zid/app/"),
                ("Redirection URL", f"{base}/zid/install/"),
                ("Callback URL", f"{base}/zid/callback/"),
                ("Privacy Policy URL", f"{base}/privacy/"),
                ("Webhook URL", f"{base}/webhooks/zid/app/"),
            ],
            "header_note": t("zid_webhook_header_note", lang) % {"header": "X-Control-Secret"},
            "snippet": "<script>\n" + ZID_SNIPPET_PATH.read_text(encoding="utf-8") + "</script>",
        },
    )
