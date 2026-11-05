"""مصادقة endpoint الاستهلاك (/api/tokens/consume/) — منفصلة عن مفتاح الودجت.

القبول:
1. مفتاح الاستهلاك (X-Consume-Key أو Authorization: Bearer): سرّي، لكل تاجر، بنخزّن
   الـ hash بس. بيشتغل من أي مكان. هاد الطريق الصحيح لـ n8n.
2. التوكن العلني القديم (X-Api-Token): مفتاح الودجت — موجود بصفحات المتجر، فما
   بيصلح يفوّض عملية حساسة. منقبله انتقالياً، بس لما يتحقق الشرطين:
   - التاجر `legacy_consume_enabled` (التجار الموجودين وقت الفصل = True، الجدد = False)
   - الطلب جاي من شبكة داخلية موثوقة (افتراضياً loopback)، بدون ما يمرّ بالـ proxy.
   هيك n8n يلي على نفس السيرفر وبيحكي مع gunicorn مباشرة (settings.py) بيضل شغّال
   بدون تغيير، وأي حدا من الإنترنت (جاي عبر nginx) ما بيقدر يستخدم التوكن العلني.
   اطفاء المسار القديم: CONSUME_LEGACY_MODE=off، أو تعطيله لتاجر من الداشبورد.
"""

import ipaddress
import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from ..models import ApiToken
from . import ratelimit

logger = logging.getLogger(__name__)

LEGACY_TOUCH_INTERVAL = timedelta(hours=1)


@dataclass
class AuthResult:
    merchant: object = None
    method: str = ""
    error: str = ""


def extract_consume_key(request):
    key = request.headers.get("X-Consume-Key", "").strip()
    if key:
        return key
    auth = request.headers.get("Authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _networks():
    configured = getattr(settings, "CONSUME_LEGACY_ALLOWED_NETWORKS", None) or ["127.0.0.0/8", "::1/128"]
    networks = []
    for item in configured:
        try:
            networks.append(ipaddress.ip_network(str(item).strip(), strict=False))
        except ValueError:
            logger.error("consume_legacy_bad_network value=%r", item)
    return networks


def legacy_origin_allowed(request):
    """هل الطلب جاي من مصدر داخلي موثوق؟ طلب عبر proxy بدون عنوان عميل واضح بنرفضه
    (nginx دايماً بيضيف X-Forwarded-*؛ المتصل المباشر على gunicorn ما عندو)."""
    if getattr(settings, "CONSUME_LEGACY_MODE", "internal") == "off":
        return False
    forwarded = ratelimit.forwarded_ips(request)
    if forwarded and getattr(settings, "TRUSTED_PROXY_COUNT", 1) > 0:
        raw_ip = forwarded[-1]
    elif ratelimit.came_through_proxy(request):
        return False
    else:
        raw_ip = request.META.get("REMOTE_ADDR", "")
    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError:
        return False
    return any(ip in net for net in _networks())


def _touch_legacy(merchant):
    now = timezone.now()
    last = merchant.legacy_consume_last_used_at
    if last is None or now - last > LEGACY_TOUCH_INTERVAL:
        ApiToken.objects.filter(pk=merchant.pk).update(legacy_consume_last_used_at=now)
    logger.warning("consume_legacy_credential merchant=%s — migrate n8n to X-Consume-Key", merchant.pk)


def authenticate(request):
    consume_key = extract_consume_key(request)
    if consume_key:
        merchant = ApiToken.objects.filter(consume_key_hash=ApiToken.hash_consume_key(consume_key)).first()
        if merchant is None:
            return AuthResult(error="invalid_consume_key")
        if not merchant.is_active:
            return AuthResult(error="token_disabled")
        return AuthResult(merchant=merchant, method="consume_key")

    token_value = request.headers.get("X-Api-Token", "").strip()
    if not token_value:
        return AuthResult(error="missing_token")
    merchant = ApiToken.objects.filter(token=token_value).first()
    if merchant is None:
        return AuthResult(error="invalid_token")
    if not merchant.is_active:
        return AuthResult(error="token_disabled")
    if not (merchant.legacy_consume_enabled and legacy_origin_allowed(request)):
        logger.warning(
            "consume_public_key_rejected merchant=%s ip=%s", merchant.pk, ratelimit.client_ip(request)
        )
        return AuthResult(error="legacy_credential_not_allowed")
    _touch_legacy(merchant)
    return AuthResult(merchant=merchant, method="legacy_token")
