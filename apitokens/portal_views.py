"""صفحات بوابة التاجر (قراءة فقط). كل شي بيتحسب من `request.portal_merchant`
اللي بيحطه الديكوريتر — ما بنقبل أي id من الرابط أو الـ GET، حتى ما يشوف تاجر بيانات تاجر تاني."""

from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.shortcuts import render
from django.utils import timezone

from .models import Conversation, TryOnEvent
from .portal_auth import portal_required
from .services import tryon_stats as tryon_stats_service


def _conversation_kpis(merchant):
    """نفس منطق صفحة المحادثات عند الستاف (آخر 30 يوم) بس لهالتاجر."""
    since = timezone.now() - timedelta(days=30)
    recent = Conversation.objects.filter(merchant=merchant, started_at__gte=since)
    agg = recent.aggregate(
        total=Count("id"),
        intent=Count("id", filter=Q(buy_clicks__gt=0) | Q(add_to_carts__gt=0)),
        orders=Count("id", filter=~Q(order_id="")),
        revenue=Sum("order_total"),
    )
    with_recommendation = sum(1 for skus in recent.values_list("recommended_skus", flat=True) if skus)
    return {
        "total": agg["total"],
        "with_recommendation": with_recommendation,
        "intent": agg["intent"],
        "orders": agg["orders"],
        "revenue": agg["revenue"] or 0,
    }


@portal_required
def home(request):
    merchant = request.portal_merchant
    range_key = tryon_stats_service.clean_range(request.GET.get("range", tryon_stats_service.DEFAULT_RANGE))
    window_days = tryon_stats_service.clean_window(request.GET.get("window", tryon_stats_service.DEFAULT_WINDOW))
    stats = tryon_stats_service.build_stats(merchant, range_key, window_days)
    # حالة فاضية: لا تجارب ناجحة ولا محادثات أبداً (مش بس ضمن الفترة المختارة).
    has_data = (
        TryOnEvent.objects.filter(merchant=merchant, success=True).exists()
        or Conversation.objects.filter(merchant=merchant).exists()
    )
    return render(
        request,
        "apitokens/portal/home.html",
        {
            "merchant": merchant,
            "stats": stats,
            "conv": _conversation_kpis(merchant),
            "range_key": range_key,
            "window_days": window_days,
            "currency": merchant.currency,
            "has_data": has_data,
        },
    )
