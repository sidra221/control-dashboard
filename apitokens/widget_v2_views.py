"""endpoints الودجت لمحرّك v2 (ذاكرة بالسيرفر). نفس مصادقة الودجت العلنية + حدود المعدّل
من المرحلة 0؛ الهوية الحقيقية للزبون من توكنات موقّعة (services/session_token.py).

/widget/v2/bootstrap/  استئناف الجلسة وإرجاع المحادثة (ما بيكتب شي لو ما في جلسة)
/widget/v2/session/    إنشاء/استئناف جلسة (قبل أول تجربة لبس/حدث لو ما في توكن)
/widget/v2/chat/       دور محادثة
/widget/v2/memory/     عرض/تصحيح ما بنتذكره (list | remove_fact | unreject)
/widget/v2/forget/     مسح كل ذاكرة الزبون عند هالتاجر
"""

import json
import logging

from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import widget_views as wv
from .models import ProductReaction
from .services import memory, ratelimit, v2_turn

logger = logging.getLogger(__name__)


def _prepare(request, *, require_v2=True):
    """(merchant, body, error_response)."""
    if request.method == "OPTIONS":
        return None, None, wv._cors_response({}, status=200)
    merchant, error = wv._authenticate(request)
    if error:
        return None, None, error
    if len(request.body or b"") > wv.MAX_BODY_BYTES:
        return None, None, wv._cors_response({"ok": False, "error": "payload_too_large"}, status=413)
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return None, None, wv._cors_response({"ok": False, "error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return None, None, wv._cors_response({"ok": False, "error": "invalid_json"}, status=400)
    if require_v2 and merchant.engine != "v2":
        return None, None, wv._cors_response({"ok": False, "error": "engine_not_enabled"}, status=409)
    return merchant, body, None


def _chat_limits(request, merchant):
    ip = f"{merchant.pk}:{ratelimit.client_ip(request)}"
    return wv._rate_limited(
        request, [("chat_ip", ip), ("chat_ip_hour", ip), ("chat_token", str(merchant.pk))]
    )


def _page_product_card(merchant, product):
    return {
        **wv._product_card(product, merchant.currency),
        "tryon_supported": product.supports_tryon,
        "tryon_button_label": wv.TRYON_BUTTON_LABELS["ar"]["self"],
    }


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def bootstrap(request):
    merchant, body, error = _prepare(request, require_v2=False)
    if error:
        return error
    if merchant.engine != "v2":
        return wv._cors_response({"ok": True, "engine": "v1"})
    limited = wv._rate_limited(
        request, [("pageproduct_ip", f"{merchant.pk}:{ratelimit.client_ip(request)}")]
    )
    if limited:
        return limited

    ctx = memory.resolve_session(merchant, body.get("session_token"), body.get("shopper_token"), create=False)
    page_product = wv._find_page_product(merchant, str(body.get("page_url") or "")[:2000])
    payload = {
        "ok": True, "engine": "v2", "session_token": None, "shopper_token": None, "session_id": None,
        "messages": [], "returning": False,
        "page_product": _page_product_card(merchant, page_product) if page_product else None,
    }
    if ctx is not None:
        payload["returning"] = True
        if ctx.conversation is not None:
            payload.update(memory.issue_tokens(ctx))
            payload["messages"] = memory.history_payload(ctx.conversation)
            if page_product is not None:
                memory.record_view(ctx, page_product, ctx.conversation.turn_count)
        else:
            from .services import session_token

            payload["shopper_token"] = session_token.issue_shopper(merchant, ctx.profile.public_id)
    return wv._cors_response(payload)


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def session(request):
    merchant, body, error = _prepare(request)
    if error:
        return error
    limited = _chat_limits(request, merchant)
    if limited:
        return limited
    ctx = memory.resolve_session(merchant, body.get("session_token"), body.get("shopper_token"), create=True)
    return wv._cors_response({"ok": True, **memory.issue_tokens(ctx)})


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def chat(request):
    merchant, body, error = _prepare(request)
    if error:
        return error
    limited = _chat_limits(request, merchant)
    if limited:
        return limited
    payload, err, extra = v2_turn.run_chat(merchant, body)
    if err:
        return wv._cors_response({"ok": False, "error": err[0], **(extra or {})}, status=err[1])
    return wv._cors_response(payload)


def _profile_from_shopper(merchant, body):
    from .services import session_token
    from .models import CustomerProfile
    from django.utils import timezone

    data = session_token.read_shopper(body.get("shopper_token"), merchant)
    if not data:
        return None
    return CustomerProfile.objects.filter(
        merchant=merchant, public_id=data.get("p", ""), expires_at__gt=timezone.now()
    ).first()


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def memory_view(request):
    merchant, body, error = _prepare(request)
    if error:
        return error
    limited = wv._rate_limited(request, [("memory_ip", f"{merchant.pk}:{ratelimit.client_ip(request)}")])
    if limited:
        return limited
    profile = _profile_from_shopper(merchant, body)
    if profile is None:
        return wv._cors_response({"ok": False, "error": "no_memory"}, status=404)
    action = body.get("action", "list")
    if action == "remove_fact":
        fact = profile.facts.filter(pk=body.get("fact_id"), status="active").first()
        if fact is None:
            return wv._cors_response({"ok": False, "error": "fact_not_found"}, status=404)
        fact.status = "retracted"
        fact.save(update_fields=["status", "updated_at"])
    elif action == "unreject":
        sku = str(body.get("sku") or "")[:64]
        if sku in memory.excluded_skus(profile):
            ProductReaction.objects.create(
                merchant=merchant, profile=profile, sku=sku, reaction="neutral", reason_text="customer_unrejected"
            )
    elif action != "list":
        return wv._cors_response({"ok": False, "error": "invalid_action"}, status=400)
    facts = [
        {"id": f.pk, "key": f.key, "value": f.value, "polarity": f.polarity, "source": f.source, "subject": f.subject}
        for f in memory.active_facts(profile)
    ]
    return wv._cors_response({"ok": True, "facts": facts, "rejected_skus": sorted(memory.excluded_skus(profile))})


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def forget(request):
    merchant, body, error = _prepare(request)
    if error:
        return error
    limited = wv._rate_limited(request, [("memory_ip", f"{merchant.pk}:{ratelimit.client_ip(request)}")])
    if limited:
        return limited
    profile = _profile_from_shopper(merchant, body)
    if profile is None:
        return wv._cors_response({"ok": True, "forgotten": False})
    memory.forget_profile(profile)
    return wv._cors_response({"ok": True, "forgotten": True})
