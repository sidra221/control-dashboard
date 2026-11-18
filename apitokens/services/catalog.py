"""الكتالوج اللي بيوصل للمساعد: مزامنة Shopify، واختيار المنتجات الأقرب لطلب
الزبون لما المتجر كبير (ما فينا نحط آلاف المنتجات بالبرومبت).
"""

import hashlib
import logging
import math

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import Product
from . import salla as salla_service
from . import shopify as shopify_service
from . import woocommerce as woocommerce_service
from . import zid as zid_service
from .sync_guard import CatalogIncomplete, should_prune

logger = logging.getLogger(__name__)

# لحد FULL_DETAIL_LIMIT منبعت كل المنتجات بكل تفاصيلها. لحد COMPACT_LIMIT منبعتهم
# كلهم بشكل مختصر (بدون وصف) — الموديل الرخيص بيقرا قائمة طويلة بسهولة وأسرع من
# أي بحث إضافي. فوقها منختار الأقرب دلالياً (embeddings) — كل استدعاء بياخد ~3 ثواني.
FULL_DETAIL_LIMIT = 60
COMPACT_LIMIT = 400
SELECTED_CATALOG_SIZE = 40
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 256
EMBEDDING_BATCH = 200
DESCRIPTION_CHARS_IN_PROMPT = 300


def _collect_rows(fetch, to_row):
    """بيجمع صفوف المنتجات. لو الجالب قال إنه ما جاب كل شي (CatalogIncomplete) منكمّل باللي انجاب
    ومنرجّع complete=False حتى ما نحذف شي."""
    rows, complete = [], True
    try:
        raws = fetch()
        for raw in raws:
            row = to_row(raw)
            if row is not None:
                rows.append(row)
    except CatalogIncomplete as exc:
        complete = False
        for raw in exc.partial:
            row = to_row(raw)
            if row is not None:
                rows.append(row)
    return rows, complete


def _save_rows(merchant, rows, complete, source):
    """upsert لكل المنتجات اللي رجعت، وحذف اللي ما رجعت بس إذا الجلب كامل ومعقول (sync_guard)."""
    fetched_ids = {row["external_id"] for row in rows}
    synced = Product.objects.filter(merchant=merchant).exclude(external_id="")
    with transaction.atomic():
        prune = should_prune(merchant, synced.count(), len(fetched_ids), complete, source)
        for row in rows:
            external_id = row.pop("external_id")
            Product.objects.update_or_create(
                merchant=merchant, external_id=external_id, defaults=row,
            )
        if prune:
            synced.exclude(external_id__in=fetched_ids).delete()


def sync_shopify(merchant, connection):
    """مزامنة كاملة: معلومات المتجر + كل المنتجات الفعّالة. المنتجات اللي
    انشالت من Shopify بتنشال عنا. برجّع عدد المنتجات."""
    token = shopify_service.get_access_token(connection)
    info = shopify_service.get_shop_info(connection.shop_domain, token)
    connection.primary_domain = info["primary_domain"]

    rows, complete = _collect_rows(
        lambda: shopify_service.fetch_all_products(connection.shop_domain, token),
        lambda raw: shopify_service.product_row_from_payload(
            connection.shop_domain, raw, connection.primary_domain
        ),
    )
    _save_rows(merchant, rows, complete, "shopify")

    merchant_fields = []
    if info["currency"] and info["currency"] != merchant.currency:
        merchant.currency = info["currency"]
        merchant_fields.append("currency")
    # سياسات Shopify بتعبّي معلومات المتجر بس إذا التاجر ما كتب شي بنفسه.
    if not merchant.store_info.strip():
        policies = shopify_service.get_store_policies_text(connection.shop_domain, token)
        if policies:
            merchant.store_info = policies
            merchant_fields.append("store_info")
    if merchant_fields:
        merchant.save(update_fields=merchant_fields)

    connection.last_synced_at = timezone.now()
    connection.save(update_fields=["last_synced_at", "primary_domain"])

    try:
        ensure_embeddings(list(merchant.products.filter(in_stock=True)))
    except Exception:  # noqa: BLE001 — التضمين تحسين، مش شرط لنجاح المزامنة
        logger.exception("embedding_refresh_failed merchant=%s", merchant.pk)
    return len(rows)


def sync_salla(merchant, connection):
    """مزامنة كاملة لمتجر سلة، بنفس منطق Shopify: المنتجات اللي انشالت من
    سلة بتنشال عنا. برجّع عدد المنتجات."""
    rows, complete = _collect_rows(
        lambda: salla_service.fetch_all_products(connection), salla_service.product_row_from_payload,
    )
    _save_rows(merchant, rows, complete, "salla")

    connection.last_synced_at = timezone.now()
    connection.save(update_fields=["last_synced_at", "updated_at"])

    try:
        ensure_embeddings(list(merchant.products.filter(in_stock=True)))
    except Exception:  # noqa: BLE001 — التضمين تحسين، مش شرط لنجاح المزامنة
        logger.exception("embedding_refresh_failed merchant=%s", merchant.pk)
    return len(rows)


def sync_zid(merchant, connection):
    """مزامنة كاملة لمتجر زد بنفس منطق سلة وShopify. برجّع عدد المنتجات."""
    rows, complete = _collect_rows(
        lambda: zid_service.fetch_all_products(connection), zid_service.product_row_from_payload,
    )
    _save_rows(merchant, rows, complete, "zid")

    connection.last_synced_at = timezone.now()
    connection.save(update_fields=["last_synced_at", "updated_at"])

    try:
        ensure_embeddings(list(merchant.products.filter(in_stock=True)))
    except Exception:  # noqa: BLE001 — التضمين تحسين، مش شرط لنجاح المزامنة
        logger.exception("embedding_refresh_failed merchant=%s", merchant.pk)
    return len(rows)


def sync_woocommerce(merchant, connection):
    """مزامنة كاملة لمتجر WooCommerce بنفس منطق Shopify: المنتجات اللي انشالت من
    المتجر (أو انسحبت من النشر) بتنشال عنا. برجّع عدد المنتجات."""
    info = woocommerce_service.get_store_info(connection)

    rows, complete = _collect_rows(
        lambda: woocommerce_service.fetch_all_products(connection), woocommerce_service.product_row_from_payload,
    )
    _save_rows(merchant, rows, complete, "woocommerce")

    if info["currency"] and info["currency"] != merchant.currency:
        merchant.currency = info["currency"]
        merchant.save(update_fields=["currency"])

    connection.last_synced_at = timezone.now()
    connection.save(update_fields=["last_synced_at"])

    try:
        ensure_embeddings(list(merchant.products.filter(in_stock=True)))
    except Exception:  # noqa: BLE001 — التضمين تحسين، مش شرط لنجاح المزامنة
        logger.exception("embedding_refresh_failed merchant=%s", merchant.pk)
    return len(rows)


# ---------------------------------------------------------------------------
# اختيار المنتجات
# ---------------------------------------------------------------------------


def _embedding_text(product):
    parts = [product.title, product.category, product.tags, product.description[:500]]
    for name, values in (product.options or {}).items():
        parts.append(f"{name}: {', '.join(values)}")
    return " | ".join(p for p in parts if p)


def _text_hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _embed(texts):
    import openai

    client = openai.OpenAI(api_key=settings.OPENAI_API_KEY, timeout=30, max_retries=1)
    resp = client.embeddings.create(
        model=EMBEDDING_MODEL, input=texts, dimensions=EMBEDDING_DIMENSIONS,
    )
    return [item.embedding for item in resp.data]


def ensure_embeddings(products):
    """بيحسب المتجهات الناقصة أو القديمة (تغيّر نص المنتج) دفعات دفعات."""
    if not settings.OPENAI_API_KEY:
        return
    stale = []
    for p in products:
        text = _embedding_text(p)
        source = _text_hash(text)
        if p.embedding is None or p.embedding_source != source:
            stale.append((p, text, source))
    for i in range(0, len(stale), EMBEDDING_BATCH):
        batch = stale[i:i + EMBEDDING_BATCH]
        vectors = _embed([text for _, text, _ in batch])
        for (p, _, source), vector in zip(batch, vectors):
            p.embedding = vector
            p.embedding_source = source
        Product.objects.bulk_update([p for p, _, _ in batch], ["embedding", "embedding_source"])


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def select_products(merchant, messages, shown_skus=()):
    """المنتجات اللي رح توصل للمساعد بهالدور: كلها لو المتجر صغير، وإلا
    الأقرب دلالياً لآخر طلبات الزبون (بأي لغة) + أي منتج سبق وانعرض بالمحادثة
    حتى يضل المساعد قادر يحكي عنه."""
    products = list(merchant.products.filter(in_stock=True))
    if len(products) <= COMPACT_LIMIT:
        return products

    shown = set(shown_skus)
    pinned = [p for p in products if p.sku in shown]
    query = " ".join(m["content"] for m in messages if m["role"] == "user")[-1500:]
    if not settings.OPENAI_API_KEY or not query:
        return pinned + [p for p in products if p.sku not in shown][:SELECTED_CATALOG_SIZE]
    try:
        ensure_embeddings(products)
        [query_vector] = _embed([query])
        ranked = sorted(
            (p for p in products if p.embedding and p.sku not in shown),
            key=lambda p: _cosine(query_vector, p.embedding),
            reverse=True,
        )
    except Exception:  # noqa: BLE001 — بدون تضمين منرجع لأول المنتجات بدل ما نفشل
        logger.exception("product_selection_failed merchant=%s", merchant.pk)
        ranked = [p for p in products if p.sku not in shown]
    return pinned + ranked[:SELECTED_CATALOG_SIZE]


def catalog_for_prompt(products, shown_skus=()):
    """الوصف بينبعت بس لما الكتالوج صغير، أو للمنتجات اللي انعرضت (الزبون
    عم يسأل عنها غالباً)."""
    detailed = len(products) <= FULL_DETAIL_LIMIT
    shown = set(shown_skus)
    rows = []
    for p in products:
        row = {
            "sku": p.sku,
            "title": p.title,
            "category": p.category,
            "tags": p.tag_list,
            "price": str(p.price),
        }
        if p.compare_at_price:
            row["was_price"] = str(p.compare_at_price)
        if p.description and (detailed or p.sku in shown):
            desc = p.description[:DESCRIPTION_CHARS_IN_PROMPT]
            row["description"] = desc + ("…" if len(p.description) > len(desc) else "")
        if p.options:
            row["available_options"] = p.options
        if not p.supports_tryon:
            row["try_on"] = False
        rows.append(row)
    return rows
