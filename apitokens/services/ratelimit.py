"""حدود الطلبات بنافذة ثابتة على قاعدة البيانات (RateBucket) — صح مع أكتر من worker لأنه كل
زيادة بتتم بجملة UPDATE ذرّية على الصف، مش بذاكرة العملية.

    allowed, retry_after = ratelimit.hit("chat_ip", ratelimit.client_ip(request))

الحدود (عدد، مدة النافذة بالثواني) من settings.RATE_LIMITS[name]. اسم غير موجود = بلا حد.
لو قاعدة البيانات فشلت بنفتح (بنسمح) وبنسجّل خطأ: الحد حماية إضافية، ما بدنا نوقف الودجت بسببه.
"""
import hashlib
import logging
import math
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apitokens.models import RateBucket

logger = logging.getLogger(__name__)

KEY_MAX_LENGTH = 160


def client_ip(request):
    # نفس منطق البوابة (بيحترم PORTAL_CLIENT_IP_HEADER): ما منكرّر الكود ولا منصدّق هيدر الزبون.
    from apitokens.portal_auth import _client_ip

    return _client_ip(request)


def ip_hash(request):
    """sha256 لـ IP مملّح بـ SECRET_KEY (64 خانة hex) — ما منخزّن العنوان نفسه أبداً.
    فاضي إذا العنوان مجهول، حتى ما تنحسب كل الطلبات المجهولة كزبون واحد."""
    ip = client_ip(request)
    if not ip or ip == "unknown":
        return ""
    return hash_ip(ip)


def hash_ip(ip):
    return hashlib.sha256(f"{settings.SECRET_KEY}:{ip}".encode()).hexdigest()


def _bucket_key(name, ident):
    key = f"{name}:{ident}"
    if len(key) > KEY_MAX_LENGTH:
        key = f"{name}:" + hashlib.sha256(str(ident).encode()).hexdigest()
    return key


def hit_key(key, limit, window_seconds, now=None):
    """بيسجّل طلب على المفتاح. بيرجّع (allowed, retry_after_seconds). retry_after = 0 لما مسموح."""
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=window_seconds)
    with transaction.atomic():
        # 1) النافذة انتهت: بنبدّل نافذة جديدة بعدّاد 1 (جملة وحدة، فمحاولتين متوازيتين ما بيعيدوا التصفير).
        updated = RateBucket.objects.filter(key=key, window_start__lte=cutoff).update(
            window_start=now, count=1,
        )
        if not updated:
            # 2) نافذة جارية: زيادة ذرّية.
            updated = RateBucket.objects.filter(key=key).update(count=F("count") + 1)
        if not updated:
            # 3) أول طلب: إنشاء، وإذا سبقنا worker تاني (unique) بنزيد على صفّه.
            try:
                with transaction.atomic():
                    RateBucket.objects.create(key=key, window_start=now, count=1)
            except IntegrityError:
                RateBucket.objects.filter(key=key).update(count=F("count") + 1)
        row = RateBucket.objects.filter(key=key).values("window_start", "count").first()
    if row is None:  # انحذف بنفس اللحظة (purge): نعتبره أول طلب
        return True, 0
    if row["count"] <= limit:
        return True, 0
    window_end = row["window_start"] + timedelta(seconds=window_seconds)
    return False, max(1, math.ceil((window_end - now).total_seconds()))


def hit(name, ident, now=None):
    """طلب واحد على الحد المسمّى. (allowed, retry_after_seconds)."""
    limits = getattr(settings, "RATE_LIMITS", {})
    if name not in limits:
        return True, 0
    limit, window = limits[name]
    try:
        return hit_key(_bucket_key(name, ident), limit, window, now=now)
    except Exception:
        logger.exception("ratelimit_failed name=%s", name)
        return True, 0
