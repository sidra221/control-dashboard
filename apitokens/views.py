import csv
import io
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponseBadRequest, HttpResponseRedirect
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

from . import portal_auth
from .i18n import LANGUAGES, t
from .models import (
    ApiToken, Conversation, DiscountCode, LlmUsage, MerchantAccount, PhoneOtp, Product, RateBucket, SallaConnection, ShopifyConnection, SizeChart, WooCommerceConnection, ZidConnection,
    ZidSubscription,
)
from .services import catalog as catalog_service
from .services import salla as salla_service
from .services import sizing
from .services import tryon_stats as tryon_stats_service
from .services import shopify as shopify_service
from .services import woocommerce as woocommerce_service
from .services import zid as zid_service
from .woocommerce_webhooks import start_initial_sync as start_woocommerce_sync

REQUIRED_COLUMNS = {"sku", "title", "price", "image_url"}


def set_language(request):
    lang = request.POST.get("lang") or request.GET.get("lang")
    if lang in LANGUAGES:
        request.session["lang"] = lang
    next_url = request.POST.get("next") or request.GET.get("next") or "/"
    # بس مسار داخلي: `//evil.com` و`/\evil.com` بيعدّوا startswith("/") وبيطلعوا لموقع تاني.
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}) or not next_url.startswith("/"):
        next_url = "/"
    return HttpResponseRedirect(next_url)


@staff_member_required
def dashboard(request):
    new_token = None
    lang = request.session.get("lang", "ar")
    # رابط البوابة بيظهر مرة وحدة: منسحبو من الجلسة سواء انعرض أو لا
    new_portal_link = _pop_portal_link(request)

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
        "shopify_connection", "woocommerce_connection", "salla_connection", "zid_connection", "portal_account"
    )
    tokens, store_filter = _apply_store_filter(request, tokens)
    now = timezone.now()
    chat = _chat_usage_by_store(now)
    capped = _chat_cap_hit_store_ids(now)
    for tok in tokens:
        tok.portal = portal_auth.portal_status(tok, now)
        tok.quota = _staff_quota(tok, chat.get(tok.pk), now, lang)
        tok.quota["chat_cap_hit"] = tok.pk in capped
    pending_accounts = list(MerchantAccount.objects.filter(merchant__isnull=True).order_by("created_at"))
    # آخر كود تحقق لكل حساب معلّق (للستاف: حالة التسليم بس، ما في كود ولا hash)
    last_otp = {}
    for otp in PhoneOtp.objects.filter(account__in=pending_accounts).order_by("created_at", "pk"):
        last_otp[otp.account_id] = otp
    for acc in pending_accounts:
        acc.last_otp = last_otp.get(acc.pk)
    # المتاجر المتاحة للربط: فعّالة وما إلها حساب (بس الحسابات الموثّقة بتنربط)
    free_stores = (
        ApiToken.objects.filter(is_active=True, portal_account__isnull=True).order_by("label", "pk")
        if any(acc.phone_verified for acc in pending_accounts) else []
    )
    stats = _store_stats()
    periods = [(value, t("period_" + value, lang)) for value in ApiToken.Period.values]
    return render(
        request,
        "apitokens/dashboard.html",
        {
            "tokens": tokens,
            "new_token": new_token,
            "new_portal_link": new_portal_link,
            "pending_accounts": pending_accounts,
            "free_stores": free_stores,
            "periods": periods,
            "stats": stats,
            "store_filter": store_filter,
        },
    )


CHAT_USAGE_DAYS = 30


def _fill(text, **values):
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def _chat_usage_by_store(now):
    """استهلاك الشات آخر 30 يوم لكل المتاجر بنداء واحد: {merchant_id: {calls, tokens}}."""
    since = timezone.localdate(now) - timedelta(days=CHAT_USAGE_DAYS - 1)
    rows = (
        LlmUsage.objects.filter(day__gte=since).values("merchant_id")
        .annotate(calls=Sum("calls"), input_tokens=Sum("input_tokens"), output_tokens=Sum("output_tokens"))
    )
    return {
        r["merchant_id"]: {"calls": r["calls"] or 0, "tokens": (r["input_tokens"] or 0) + (r["output_tokens"] or 0)}
        for r in rows
    }


def _chat_cap_hit_store_ids(now):
    """المتاجر اللي وصلت سقف الشات اليومي (CHAT_MAX_MESSAGES_PER_STORE_DAY) خلال آخر 24 ساعة:
    علامة الوصول بالشات بتنسجّل كـ RateBucket مرة وحدة بنافذة اليوم."""
    from .widget_views import CHAT_DAY_SECONDS, CHAT_STORE_CAP_LOGGED_KEY

    prefix = CHAT_STORE_CAP_LOGGED_KEY + ":"
    keys = RateBucket.objects.filter(
        key__startswith=prefix, window_start__gt=now - timedelta(seconds=CHAT_DAY_SECONDS),
    ).values_list("key", flat=True)
    return {int(k[len(prefix):]) for k in keys if k[len(prefix):].isdigit()}


def _staff_quota(tok, chat, now, lang):
    """رصيد المتجر للستاف من حقول التوكن مباشرة (بدون استعلام لكل متجر). الفترة بتتجدّد بكسل عند أول
    استهلاك بعد انتهائها، فإذا انتهت بنحسب المستهلك 0. المتبقي هون = الرصيد − المستهلك (بدون الحجوزات
    اللحظية قيد التوليد، عكس بوابة التاجر)."""
    resets_at = tok.period_start + timedelta(days=tok.period_days)
    expired = now >= resets_at
    used = 0 if expired else tok.images_used
    remaining = max(tok.image_quota - used, 0)
    limit = tok.tryons_per_shopper
    chat = chat or {"calls": 0, "tokens": 0}
    return {
        "used_line": _fill(t("portal_admin_quota_used_of", lang), used=used, total=tok.image_quota),
        "remaining_line": _fill(t("portal_admin_quota_remaining", lang), n=remaining),
        "resets_at": resets_at,
        "renews_next_use": expired,
        "per_shopper_line": (
            t("portal_admin_quota_unlimited", lang) if limit == 0
            else _fill(t("portal_admin_quota_per_shopper_value", lang), n=limit)
        ),
        "has_chat": bool(chat["calls"] or chat["tokens"]),
        "chat_calls_line": _fill(t("portal_admin_quota_chat_calls", lang), n=chat["calls"]),
        "chat_tokens_line": _fill(t("portal_admin_quota_chat_tokens", lang), n=chat["tokens"]) if chat["tokens"] else "",
    }


def _pop_portal_link(request):
    stash = request.session.pop("new_portal_link", None)
    if not isinstance(stash, dict) or not stash.get("url"):
        return None
    label = ApiToken.objects.filter(pk=stash.get("merchant_id")).values_list("label", flat=True).first()
    if label is None:
        return None
    return {"label": label, "url": stash["url"]}


@staff_member_required
@require_http_methods(["POST"])
def portal_link(request, pk):
    merchant = get_object_or_404(ApiToken, pk=pk)
    lang = request.session.get("lang", "ar")
    action = request.POST.get("action")
    if action == "generate":
        key = portal_auth.generate_portal_key(merchant)
        url = request.build_absolute_uri(reverse("portal:enter", args=[key]))
        request.session["new_portal_link"] = {"merchant_id": merchant.pk, "url": url}
        messages.success(request, t("portal_admin_msg_generated", lang))
    elif action == "revoke":
        portal_auth.revoke_portal_key(merchant)
        request.session.pop("new_portal_link", None)  # ما نترك رابط ملغي معلّق بالجلسة
        messages.success(request, t("portal_admin_msg_revoked", lang))
    else:
        return HttpResponseBadRequest("invalid action")
    return redirect("apitokens:dashboard")


@staff_member_required
@require_http_methods(["POST"])
def portal_account_link(request, pk):
    """بيربط حساب معلّق (سجّل بنفسه) بمتجر. الـ unique على merchant هو الحماية الأخيرة من السباق."""
    lang = request.session.get("lang", "ar")
    raw = request.POST.get("merchant", "")
    if not (raw.isascii() and raw.isdigit()):
        return HttpResponseBadRequest("invalid merchant")
    try:
        with transaction.atomic():
            account = MerchantAccount.objects.select_for_update().filter(pk=pk).first()
            merchant = ApiToken.objects.select_for_update().filter(pk=int(raw)).first()
            if account is None or merchant is None:
                raise Http404
            if account.merchant_id is not None:
                error = "portal_admin_pending_err_not_pending"
            elif not account.phone_verified:
                error = "portal_admin_pending_err_not_verified"
            elif not merchant.is_active:
                error = "portal_admin_pending_err_store_inactive"
            elif MerchantAccount.objects.filter(merchant=merchant).exists():
                error = "portal_admin_pending_err_store_taken"
            else:
                error = None
                account.merchant = merchant
                account.save(update_fields=["merchant"])
    except IntegrityError:  # سباق: متجر انربط بحساب تاني بنفس اللحظة
        error = "portal_admin_pending_err_store_taken"
    if error:
        messages.error(request, t(error, lang))
    else:
        messages.success(request, t("portal_admin_pending_msg_linked", lang))
    return redirect("apitokens:dashboard")


@staff_member_required
@require_http_methods(["POST"])
def portal_account_delete(request, pk):
    """حذف نهائي لحساب معلّق بس. الحذف مشروط (merchant null) بنفس الاستعلام فما في سباق مع الربط."""
    lang = request.session.get("lang", "ar")
    deleted, _ = MerchantAccount.objects.filter(pk=pk, merchant__isnull=True).delete()
    if deleted:
        messages.success(request, t("portal_admin_pending_msg_deleted", lang))
    elif MerchantAccount.objects.filter(pk=pk).exists():
        messages.error(request, t("portal_admin_pending_err_not_pending", lang))
    else:
        raise Http404
    return redirect("apitokens:dashboard")


@staff_member_required
@require_http_methods(["POST"])
def toggle_active(request, pk):
    token = get_object_or_404(ApiToken, pk=pk)
    token.is_active = not token.is_active
    # إيقاف من الإدارة لازم يصمد قدام إعادة تركيب التطبيق أو تجديد اشتراك زد.
    token.suspended_by_staff = not token.is_active
    token.save(update_fields=["is_active", "suspended_by_staff"])
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


@staff_member_required
def woocommerce_connect(request, pk):
    merchant = get_object_or_404(ApiToken, pk=pk)
    connection = getattr(merchant, "woocommerce_connection", None)
    lang = request.session.get("lang", "ar")

    if request.method == "POST":
        action = request.POST.get("action", "connect")

        if action == "disconnect":
            if connection:
                # أفضل محاولة: نحذف الـ webhooks التي سجّلناها بالمتجر (مهلة قصيرة، الأخطاء تُتجاهل)،
                # ثم المنتجات المتزامنة من WooCommerce (الكتالوج اليدوي external_id="" يبقى)، ثم الربط.
                woocommerce_service.delete_webhooks(connection, _woocommerce_callback_url(merchant))
                with transaction.atomic():
                    Product.objects.filter(merchant=merchant).exclude(external_id="").delete()
                    connection.delete()
                messages.success(request, t("msg_woo_disconnected", lang))
                messages.warning(request, t("warn_woo_revoke_keys", lang))
            return redirect("apitokens:woocommerce-connect", pk=pk)

        if action == "resync":
            if not connection:
                messages.error(request, t("err_no_connection_to_resync", lang))
            else:
                _start_woocommerce_sync_after_commit(connection, merchant)
                messages.success(request, t("msg_woo_sync_started", lang))
            return redirect("apitokens:woocommerce-connect", pk=pk)

        raw_url = request.POST.get("site_url", "")
        consumer_key = request.POST.get("consumer_key", "").strip()
        consumer_secret = request.POST.get("consumer_secret", "").strip()
        if not raw_url.strip() or not consumer_key or not consumer_secret:
            messages.error(request, t("err_woo_missing_fields", lang))
            return redirect("apitokens:woocommerce-connect", pk=pk)
        site_url = woocommerce_service.normalize_site_url(raw_url)
        if not site_url:
            messages.error(request, t("err_woo_https", lang))
            return redirect("apitokens:woocommerce-connect", pk=pk)

        candidate = WooCommerceConnection(
            merchant=merchant, site_url=site_url,
            consumer_key=consumer_key, consumer_secret=consumer_secret,
        )
        try:
            woocommerce_service.assert_public_host(site_url)
            woocommerce_service.get_store_info(candidate)
        except woocommerce_service.WooCommerceError as exc:
            messages.error(request, str(exc))
            return redirect("apitokens:woocommerce-connect", pk=pk)

        # webhook_secret ما بنمرّره: لو في ربط قديم بيضل سرّه، وإلا بينولّد جديد.
        connection, _ = WooCommerceConnection.objects.update_or_create(
            merchant=merchant,
            defaults={
                "site_url": site_url,
                "consumer_key": consumer_key,
                "consumer_secret": consumer_secret,
                "is_active": True,
            },
        )

        # المزامنة الكاملة وتسجيل الـ webhooks بالخلفية بعد حفظ الربط (الكتالوجات الكبيرة
        # تتجاوز مهلة طلب الموظف). النتيجة تظهر في "آخر مزامنة" وعدد المنتجات.
        _start_woocommerce_sync_after_commit(connection, merchant)
        messages.success(request, t("msg_woo_sync_started", lang))
        return redirect("apitokens:woocommerce-connect", pk=pk)

    return render(
        request,
        "apitokens/woocommerce_connect.html",
        {"merchant": merchant, "connection": connection},
    )


def _woocommerce_callback_url(merchant):
    return f"{settings.SITE_BASE_URL}/webhooks/woocommerce/{merchant.token}/"


def _start_woocommerce_sync_after_commit(connection, merchant):
    # الخيط يستخدم اتصال قاعدة بيانات مستقلاً، فلا يبدأ إلا بعد أن يُحفظ الربط فعلياً.
    connection_pk, callback_url = connection.pk, _woocommerce_callback_url(merchant)
    transaction.on_commit(lambda: start_woocommerce_sync(connection_pk, callback_url))


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


@staff_member_required
def tryon_stats(request, pk):
    merchant = get_object_or_404(ApiToken, pk=pk)
    range_key = tryon_stats_service.clean_range(request.GET.get("range", "30"))
    window_days = tryon_stats_service.clean_window(request.GET.get("window", "7"))
    tab = request.GET.get("tab", "overview")
    if tab not in ("overview", "activity"):
        tab = "overview"
    selected_sku = request.GET.get("sku", "").strip()
    shoppers = request.GET.get("shoppers", "all")
    if shoppers not in ("all", "identified"):
        shoppers = "all"
    stats = tryon_stats_service.build_stats(
        merchant, range_key, window_days, sku=selected_sku or None, shoppers=shoppers,
    )
    return render(
        request,
        "apitokens/tryon_stats.html",
        {
            "merchant": merchant,
            "stats": stats,
            "range_key": range_key,
            "window_days": window_days,
            "tab": tab,
            "selected_sku": selected_sku,
            "shoppers": shoppers,
            "currency": merchant.currency,
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


# ===== Staff store controls (settings page, top-up, new period, dashboard search) =====
# Kept in its own section at the end of the module so concurrent edits elsewhere merge cleanly.
import logging  # noqa: E402
import re  # noqa: E402

from .portal_views import SHOPPER_LIMIT_CHOICES  # noqa: E402

store_controls_logger = logging.getLogger("apitokens.staff_store_controls")

STORE_LABEL_MAX = 120
STORE_INFO_MAX = 8000
STORE_QUOTA_MAX = 1_000_000
STORE_TOPUP_MAX = 1_000_000
STORE_SEARCH_MAX = 120
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
STORE_STATUS_FILTERS = ("active", "inactive")
STORE_SETTINGS_FIELDS = ("label", "image_quota", "period", "currency", "tryons_per_shopper", "store_info")


def _positive_int(raw, maximum):
    """Strict ASCII digits only (no signs, spaces, or Arabic-Indic digits), 1..maximum, else None."""
    raw = (raw or "").strip()
    if not (raw.isascii() and raw.isdigit()):
        return None
    value = int(raw)
    return value if 1 <= value <= maximum else None


def _apply_store_filter(request, tokens):
    """Dashboard stores table: ?q= matches label (contains) or token prefix, case-insensitive;
    ?status=active|inactive. Returns (filtered queryset, filter state for the template)."""
    q = (request.GET.get("q") or "").strip()[:STORE_SEARCH_MAX]
    status = request.GET.get("status") or ""
    if status not in STORE_STATUS_FILTERS:
        status = ""
    if q:
        tokens = tokens.filter(Q(label__icontains=q) | Q(token__istartswith=q))
    if status:
        tokens = tokens.filter(is_active=(status == "active"))
    return tokens, {"q": q, "status": status, "active": bool(q or status)}


def _store_stats():
    """Dashboard header stats over all stores, independent of the table's search/filter."""
    agg = ApiToken.objects.aggregate(
        total=Count("pk"), active=Count("pk", filter=Q(is_active=True)), images_used=Sum("images_used"),
    )
    return {"total": agg["total"] or 0, "active": agg["active"] or 0, "images_used": agg["images_used"] or 0}


def _validate_store_settings(post, merchant, lang):
    """Returns (cleaned dict, list of error messages). Nothing is saved here."""
    errors = []
    cleaned = {}

    label = (post.get("label") or "").strip()
    if not label:
        errors.append(t("store_settings_err_label_required", lang))
    elif len(label) > STORE_LABEL_MAX:
        errors.append(_fill(t("store_settings_err_label_too_long", lang), n=STORE_LABEL_MAX))
    else:
        cleaned["label"] = label

    quota = _positive_int(post.get("image_quota"), STORE_QUOTA_MAX)
    if quota is None:
        errors.append(_fill(t("store_settings_err_quota", lang), max=STORE_QUOTA_MAX))
    else:
        cleaned["image_quota"] = quota

    period = post.get("period") or ""
    if period not in ApiToken.Period.values:
        errors.append(t("store_settings_err_period", lang))
    else:
        cleaned["period"] = period

    currency = (post.get("currency") or "").strip().upper()
    if currency and not _CURRENCY_RE.match(currency):
        errors.append(t("store_settings_err_currency", lang))
    else:
        cleaned["currency"] = currency

    # Same choices as the merchant portal (1/3/5/10/0=unlimited). A legacy value outside that list
    # may be kept as-is, but cannot be newly chosen.
    raw_limit = (post.get("tryons_per_shopper") or "").strip()
    allowed = {str(n): n for n in SHOPPER_LIMIT_CHOICES}
    allowed.setdefault(str(merchant.tryons_per_shopper), merchant.tryons_per_shopper)
    if raw_limit not in allowed:
        errors.append(t("store_settings_err_per_shopper", lang))
    else:
        cleaned["tryons_per_shopper"] = allowed[raw_limit]

    store_info = (post.get("store_info") or "").replace("\r\n", "\n").strip()
    if len(store_info) > STORE_INFO_MAX:
        errors.append(_fill(t("store_settings_err_store_info_too_long", lang), n=STORE_INFO_MAX))
    else:
        cleaned["store_info"] = store_info

    return cleaned, errors


def _log_value(field, value):
    # store_info is free text the merchant's assistant reads; log its size, not its content.
    return f"<{len(value)} chars>" if field == "store_info" else repr(value)


def _store_settings_context(merchant, lang, form=None):
    now = timezone.now()
    resets_at = merchant.period_start + timedelta(days=merchant.period_days)
    expired = now >= resets_at
    used = 0 if expired else merchant.images_used
    remaining = max(merchant.image_quota - used, 0)
    if form is None:
        form = {field: str(getattr(merchant, field)) for field in STORE_SETTINGS_FIELDS}
    shopper_choices = [
        {"value": str(n), "label": t("store_settings_unlimited", lang) if n == 0 else str(n)}
        for n in SHOPPER_LIMIT_CHOICES
    ]
    if merchant.tryons_per_shopper not in SHOPPER_LIMIT_CHOICES:
        current = str(merchant.tryons_per_shopper)
        shopper_choices.insert(0, {"value": current, "label": current})
    return {
        "merchant": merchant,
        "form": form,
        "periods": [(value, t("period_" + value, lang)) for value in ApiToken.Period.values],
        "shopper_choices": shopper_choices,
        "usage": {
            "period_start": merchant.period_start,
            "resets_at": resets_at,
            "expired": expired,
            "can_topup": not expired and merchant.images_used > 0,
            "line": _fill(t("store_settings_usage_line", lang), used=used, quota=merchant.image_quota, remaining=remaining),
        },
        "limits": {"label": STORE_LABEL_MAX, "quota": STORE_QUOTA_MAX, "topup": STORE_TOPUP_MAX, "info": STORE_INFO_MAX},
    }


@staff_member_required
@require_http_methods(["GET", "POST"])
def store_settings(request, pk):
    """Staff edit of a store's label, quota, period, currency, per-shopper limit and store info."""
    merchant = get_object_or_404(ApiToken, pk=pk)
    lang = request.session.get("lang", "ar")
    if request.method == "GET":
        return render(request, "apitokens/store_settings.html", _store_settings_context(merchant, lang))

    cleaned, errors = _validate_store_settings(request.POST, merchant, lang)
    if errors:
        # Nothing was changed, so re-render with the submitted values (staff keep their input).
        for error in errors:
            messages.error(request, error)
        form = {field: request.POST.get(field, "") for field in STORE_SETTINGS_FIELDS}
        return render(
            request, "apitokens/store_settings.html", _store_settings_context(merchant, lang, form), status=400,
        )

    with transaction.atomic():
        merchant = ApiToken.objects.select_for_update().get(pk=pk)
        changes = {
            field: (getattr(merchant, field), value)
            for field, value in cleaned.items() if getattr(merchant, field) != value
        }
        for field, (_old, new) in changes.items():
            setattr(merchant, field, new)
        if changes:
            merchant.save(update_fields=list(changes))
    if changes:
        store_controls_logger.info(
            "staff_store_settings_updated staff_user=%s store=%s changes=%s",
            request.user.pk, merchant.pk,
            "; ".join(f"{f}: {_log_value(f, o)} -> {_log_value(f, n)}" for f, (o, n) in changes.items()),
        )
        messages.success(request, t("store_settings_msg_saved", lang))
    else:
        messages.info(request, t("store_settings_msg_unchanged", lang))
    return redirect("apitokens:store-settings", pk=pk)


@staff_member_required
@require_http_methods(["POST"])
def store_topup(request, pk):
    """Give back N images to the CURRENT period by lowering images_used (never below 0).

    image_quota (the recurring plan) is left alone, so the extra images disappear when the period
    renews and nobody has to remember to undo a quota bump. The credit is therefore capped at what
    was already consumed this period."""
    get_object_or_404(ApiToken, pk=pk)
    lang = request.session.get("lang", "ar")
    amount = _positive_int(request.POST.get("amount"), STORE_TOPUP_MAX)
    if amount is None:
        messages.error(request, _fill(t("store_settings_err_topup_amount", lang), max=STORE_TOPUP_MAX))
        return redirect("apitokens:store-settings", pk=pk)

    with transaction.atomic():
        merchant = ApiToken.objects.select_for_update().get(pk=pk)
        expired = timezone.now() >= merchant.period_start + timedelta(days=merchant.period_days)
        old_used = merchant.images_used
        applied = 0
        if expired:
            error = "store_settings_err_topup_period_expired"
        elif old_used <= 0:
            error = "store_settings_err_topup_nothing_used"
        else:
            error = None
            applied = min(amount, old_used)
            merchant.images_used = old_used - applied
            merchant.save(update_fields=["images_used"])
    if error:
        messages.error(request, t(error, lang))
        return redirect("apitokens:store-settings", pk=pk)

    store_controls_logger.info(
        "staff_store_topup staff_user=%s store=%s requested=%s applied=%s images_used: %s -> %s",
        request.user.pk, merchant.pk, amount, applied, old_used, merchant.images_used,
    )
    messages.success(request, _fill(t("store_settings_msg_topup_done", lang), n=applied))
    if applied < amount:
        messages.warning(request, _fill(t("store_settings_msg_topup_capped", lang), requested=amount, n=applied))
    return redirect("apitokens:store-settings", pk=pk)


@staff_member_required
@require_http_methods(["POST"])
def store_new_period(request, pk):
    """Start a fresh period right now: images_used=0, period_start=now (quota alerts re-arm by
    themselves because they are keyed on period_start)."""
    get_object_or_404(ApiToken, pk=pk)
    lang = request.session.get("lang", "ar")
    with transaction.atomic():
        merchant = ApiToken.objects.select_for_update().get(pk=pk)
        old_used, old_start = merchant.images_used, merchant.period_start
        merchant.images_used = 0
        merchant.period_start = timezone.now()
        merchant.save(update_fields=["images_used", "period_start"])
    store_controls_logger.info(
        "staff_store_new_period staff_user=%s store=%s images_used: %s -> 0 period_start: %s -> %s",
        request.user.pk, merchant.pk, old_used, old_start.isoformat(), merchant.period_start.isoformat(),
    )
    messages.success(request, t("store_settings_msg_new_period", lang))
    return redirect("apitokens:store-settings", pk=pk)
