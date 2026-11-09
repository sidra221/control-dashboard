"""إحصائيات التجارب الافتراضية لتاجر: عدد التجارب، الزباين، وإسناد الطلبات للتجارب.

الإسناد: الطلب بينحسب "مُسنَد" إذا نفس الجلسة (تاجر + session_id) إلها تجربة ناجحة
قبل وقت الطلب (order_at) وضمن نافذة الإسناد. كل محادثة بتنحسب مرة وحدة.
"""

from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from apitokens.models import Conversation, Product, TryOnEvent

WINDOW_CHOICES = (7, 14, 30)
DEFAULT_WINDOW = 7
RANGE_CHOICES = ("7", "30", "period")
DEFAULT_RANGE = "30"
TOP_PRODUCTS_LIMIT = 10
EVENTS_LIMIT = 100


def clean_range(range_key):
    range_key = str(range_key)
    return range_key if range_key in RANGE_CHOICES else DEFAULT_RANGE


def clean_window(window_days):
    try:
        window_days = int(window_days)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW
    return window_days if window_days in WINDOW_CHOICES else DEFAULT_WINDOW


def _last_days_start(now, days):
    """بداية (منتصف الليل المحلي) آخر `days` يوم شاملة اليوم الحالي."""
    first_day = timezone.localdate(now) - timedelta(days=days - 1)
    return timezone.make_aware(datetime.combine(first_day, time.min))


def resolve_range(merchant, range_key, now):
    range_key = clean_range(range_key)
    if range_key == "period":
        days = merchant.period_days
        period_end = merchant.period_start + timedelta(days=days)
        # الريسيت كسول: إذا الفترة المخزّنة خلصت، منعتبر آخر period_days يوم.
        if now >= period_end:
            return _last_days_start(now, days), now
        return min(merchant.period_start, now), now
    return _last_days_start(now, int(range_key)), now


def _product_names(merchant, skus):
    names = {}
    for sku, title in Product.objects.filter(merchant=merchant, sku__in=list(skus)).values_list("sku", "title"):
        names.setdefault(sku, title)
    return names


def _attributed_orders(merchant, range_start, range_end, window_days, sku):
    """بترجّع [(conversation_id, session_id, order_at, order_total, {skus المجرّبة})]
    للطلبات المُسنَدة. استعلامين بس: المحادثات + تجارب جلساتها."""
    orders = Conversation.objects.filter(
        merchant=merchant, order_at__gte=range_start, order_at__lte=range_end,
    ).exclude(order_id="").exclude(session_id="")
    rows = list(orders.values_list("id", "session_id", "order_at", "order_total"))
    if not rows:
        return []

    window = timedelta(days=window_days)
    events = TryOnEvent.objects.filter(
        merchant=merchant, success=True,
        session_id__in=orders.values("session_id"),
        created_at__gte=range_start - window, created_at__lte=range_end,
    )
    if sku:
        events = events.filter(sku=sku)
    by_session = defaultdict(list)
    for session_id, created_at, ev_sku in events.values_list("session_id", "created_at", "sku"):
        by_session[session_id].append((created_at, ev_sku))

    attributed = []
    for conv_id, session_id, order_at, total in rows:
        tried = {
            ev_sku for created_at, ev_sku in by_session.get(session_id, ())
            if created_at <= order_at and order_at - created_at <= window
        }
        if tried:
            attributed.append((conv_id, session_id, order_at, total, tried))
    return attributed


def build_stats(merchant, range_key, window_days, sku=None, shoppers="all", now=None):
    now = now or timezone.now()
    window_days = clean_window(window_days)
    range_start, range_end = resolve_range(merchant, range_key, now)

    events_qs = TryOnEvent.objects.filter(
        merchant=merchant, created_at__gte=range_start, created_at__lte=range_end,
    )
    if sku:
        events_qs = events_qs.filter(sku=sku)

    agg = events_qs.aggregate(
        ok=Count("id", filter=Q(success=True)),
        failed=Count("id", filter=Q(success=False)),
        shoppers=Count("session_id", distinct=True, filter=Q(success=True) & ~Q(session_id="")),
    )
    tryons, unique = agg["ok"], agg["shoppers"]

    attributed = _attributed_orders(merchant, range_start, range_end, window_days, sku)
    revenue = sum((total or Decimal("0") for _, _, _, total, _ in attributed), Decimal("0"))

    # الشارت: يوم لكل يوم تقويمي بالمدى، مصفّر.
    first_day, last_day = timezone.localdate(range_start), timezone.localdate(range_end)
    days = [first_day + timedelta(days=i) for i in range((last_day - first_day).days + 1)]
    tryons_by_day = {
        row["day"]: row["n"]
        for row in events_qs.filter(success=True).annotate(day=TruncDate("created_at"))
        .values("day").annotate(n=Count("id"))
    }
    orders_by_day = defaultdict(int)
    for _, _, order_at, _, _ in attributed:
        orders_by_day[timezone.localdate(order_at)] += 1
    chart = [
        {"date": d.isoformat(), "tryons": tryons_by_day.get(d, 0), "orders": orders_by_day.get(d, 0)}
        for d in days
    ]

    # أكثر المنتجات تجربة + الطلبات المُسنَدة لكل منتج.
    top_rows = list(
        events_qs.filter(success=True).values("sku").annotate(n=Count("id")).order_by("-n", "sku")[:TOP_PRODUCTS_LIMIT]
    )
    orders_per_sku = defaultdict(int)
    for *_, tried in attributed:
        for tried_sku in tried:
            orders_per_sku[tried_sku] += 1
    names = _product_names(merchant, [r["sku"] for r in top_rows])
    top_products = [
        {
            "sku": r["sku"], "name": names.get(r["sku"], r["sku"]),
            "tryons": r["n"], "orders": orders_per_sku.get(r["sku"], 0),
        }
        for r in top_rows
    ]

    # سجل النشاط: الأحدث أولاً. "identified" = الجلسة إلها محادثة.
    activity = events_qs
    if shoppers == "identified":
        activity = activity.filter(
            session_id__in=Conversation.objects.filter(merchant=merchant).exclude(session_id="").values("session_id")
        )
    event_rows = list(
        activity.order_by("-created_at", "-id")[:EVENTS_LIMIT]
        .values_list("created_at", "sku", "success", "error", "session_id")
    )
    event_names = _product_names(merchant, {r[1] for r in event_rows})
    orders_by_session = defaultdict(list)
    for _, session_id, order_at, _, _ in attributed:
        orders_by_session[session_id].append(order_at)
    window = timedelta(days=window_days)
    events = [
        {
            "created_at": created_at, "sku": ev_sku, "name": event_names.get(ev_sku, ev_sku),
            "success": success, "error": error, "session_short": session_id[:8],
            "has_order": bool(success and session_id and any(
                created_at <= order_at and order_at - created_at <= window
                for order_at in orders_by_session.get(session_id, ())
            )),
        }
        for created_at, ev_sku, success, error, session_id in event_rows
    ]

    # قائمة فلتر المنتجات: كل SKU إلو تجربة (حتى لو المنتج انحذف).
    event_skus = set(TryOnEvent.objects.filter(merchant=merchant).values_list("sku", flat=True).distinct())
    all_names = _product_names(merchant, event_skus)
    products = sorted(
        ({"sku": s, "name": all_names.get(s, s)} for s in event_skus),
        key=lambda p: (p["name"].lower(), p["sku"]),
    )

    return {
        "range_start": range_start,
        "range_end": range_end,
        "totals": {
            "tryons": tryons,
            "failed": agg["failed"],
            "unique_shoppers": unique,
            "per_shopper": round(tryons / unique, 1) if unique else 0.0,
            "attributed_orders": len(attributed),
            "attributed_revenue": revenue,
        },
        "chart": chart,
        "top_products": top_products,
        "events": events,
        "products": products,
    }
