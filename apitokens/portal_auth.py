"""دخول بوابة التاجر برابط سرّي طويل (مش بـ token الويدجت لأنه علني بسكربت المتجر).

المفتاح الأصلي ما بينخزّن أبداً — بس sha256 تبعه على ApiToken.portal_key_hash.
جلسة التاجر بتحمل portal_merchant_id + الـ hash، وكل طلب بنقارنها بالقاعدة، فأي
تجديد/إلغاء للمفتاح أو تعطيل للتاجر بيطلّعه من الجلسة عالطلب الجاي.
"""
import hashlib
import hmac
import secrets
from functools import wraps

from django.http import HttpResponseNotFound
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import ApiToken

# نسخة سياسة الخصوصية اللي بيوافق عليها التاجر. غيّرها لما تتغيّر السياسة جوهرياً،
# فبنطلب الموافقة من كل التجار من جديد.
PORTAL_TERMS_VERSION = "2026-11-09"

SESSION_MERCHANT = "portal_merchant_id"
SESSION_KEY_HASH = "portal_key_hash"
_SESSION_KEYS = (SESSION_MERCHANT, SESSION_KEY_HASH)


def hash_portal_key(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_portal_key(merchant):
    """بيولّد مفتاح جديد (القديم بيبطل) ويرجّع النص الأصلي — بينعرض مرة وحدة بس."""
    key = secrets.token_urlsafe(32)
    merchant.portal_key_hash = hash_portal_key(key)
    merchant.portal_key_created_at = timezone.now()
    merchant.save(update_fields=["portal_key_hash", "portal_key_created_at"])
    return key


def revoke_portal_key(merchant):
    merchant.portal_key_hash = ""
    merchant.portal_key_created_at = None
    merchant.save(update_fields=["portal_key_hash", "portal_key_created_at"])


def _harden(response):
    # same-origin مش no-referrer: مع no-referrer المتصفح بيبعت `Origin: null` بكل POST
    # وجانغو بيرفضه (CSRF) — فكانوا زر الموافقة والخروج واللغة ما بيشتغلوا. هيك بنضل
    # ما منسرّب الرابط لأي موقع تاني.
    response["Referrer-Policy"] = "same-origin"
    response["Cache-Control"] = "no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _clear_portal_session(request):
    # بس مفاتيح البوابة — جلسة الستاف (إن وجدت) بتضل متل ما هي
    for name in _SESSION_KEYS:
        request.session.pop(name, None)


def _session_merchant(request):
    """التاجر صاحب الجلسة لو لسا مفتاحه صالح، وإلا None."""
    merchant_id = request.session.get(SESSION_MERCHANT)
    session_hash = request.session.get(SESSION_KEY_HASH)
    if not isinstance(merchant_id, int) or not isinstance(session_hash, str) or not session_hash:
        return None
    merchant = ApiToken.objects.filter(pk=merchant_id).first()
    if merchant is None or not merchant.is_active or not merchant.portal_key_hash:
        return None
    if not hmac.compare_digest(merchant.portal_key_hash.encode(), session_hash.encode()):
        return None
    return merchant


def portal_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        merchant = _session_merchant(request)
        if merchant is None:
            _clear_portal_session(request)
            return _harden(redirect("portal:expired"))
        request.portal_merchant = merchant
        return _harden(view(request, *args, **kwargs))

    return wrapper


def terms_accepted_required(view):
    """بتنحط تحت portal_required: التاجر اللي ما وافق (أو وافق على نسخة قديمة) بيروح لصفحة الموافقة."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.portal_merchant.terms_accepted_version != PORTAL_TERMS_VERSION:
            return redirect("portal:accept")
        return view(request, *args, **kwargs)

    return wrapper


def accept_terms(merchant):
    merchant.terms_accepted_at = timezone.now()
    merchant.terms_accepted_version = PORTAL_TERMS_VERSION
    merchant.save(update_fields=["terms_accepted_at", "terms_accepted_version"])


@require_GET
def enter(request, key):
    key_hash = hash_portal_key(key)
    merchant = (
        ApiToken.objects.filter(is_active=True, portal_key_hash=key_hash).exclude(portal_key_hash="").first()
    )
    if merchant is None:
        # ردّ موحّد: ما منفرّق بين مفتاح غلط/ملغي/تاجر معطّل
        return _harden(HttpResponseNotFound("Not found"))
    request.session.cycle_key()
    request.session[SESSION_MERCHANT] = merchant.pk
    request.session[SESSION_KEY_HASH] = merchant.portal_key_hash
    return _harden(redirect("portal:home"))


def expired(request):
    return _harden(render(request, "apitokens/portal/expired.html", status=403))


@require_POST
def logout(request):
    _clear_portal_session(request)
    return _harden(redirect("portal:expired"))
