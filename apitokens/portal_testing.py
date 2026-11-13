"""أدوات مشتركة لاختبارات بوابة التاجر (مش كود إنتاج)."""
import itertools
from unittest import mock

from django.contrib.auth.hashers import make_password
from django.test import override_settings
from django.utils import timezone

from . import portal_auth
from .models import MerchantAccount
from .services import lightotp

# هاشر سريع للاختبارات بس (PBKDF2 الحقيقي بطيء). بيضل hash حقيقي مملّح، مش نص عادي.
FAST_HASHERS = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])

DEFAULT_PASSWORD = "Zebra-Orchid-4821"

SEND_TARGET = "apitokens.services.lightotp.send_otp_message"


def patch_otp_send(testcase, side_effect=None):
    """بيستبدل إرسال LightOTP بـ mock (ما في شبكة أبداً). افتراضياً بيرجّع نجاح Pending برقم رسالة جديد.
    الأكواد المرسلة: sent_codes(mock)."""
    counter = itertools.count(1)

    def default(*, phone, code, event, lang="ar"):
        return lightotp.SendResult(f"msg-{next(counter)}", "Pending")

    patcher = mock.patch(SEND_TARGET, side_effect=side_effect or default)
    sender = patcher.start()
    testcase.addCleanup(patcher.stop)
    sender.default_effect = default  # لإرجاع النجاح بعد ما الاختبار يحط فشل
    return sender


def sent_codes(sender):
    return [call.kwargs["code"] for call in sender.call_args_list]


def verified_fields(verified=True):
    if verified:
        return {"phone_verified": True, "phone_verified_at": timezone.now()}
    return {"phone_verified": False, "phone_verified_at": None}


def default_phone(merchant):
    return f"+9665{merchant.pk:08d}"


def make_account(merchant, name="Test Owner", phone=None, password=DEFAULT_PASSWORD, verified=True, **kw):
    """بيعمل (أو بيحدّث) حساب البوابة للتاجر بـ hash حقيقي. موثّق افتراضياً (verified=False لغير الموثّق)."""
    fields = {
        "name": name, "phone": phone or default_phone(merchant), "password": make_password(password),
        **verified_fields(verified), **kw,
    }
    account, _ = MerchantAccount.objects.update_or_create(merchant=merchant, defaults=fields)
    return account


def _put_session(client, account):
    session = client.session
    session[portal_auth.SESSION_ACCOUNT] = account.pk
    session[portal_auth.SESSION_FINGERPRINT] = portal_auth.session_fingerprint(account)
    session.save()


def login_portal(client, merchant, name="Test Owner", phone=None, password=DEFAULT_PASSWORD, verified=True, **kw):
    """بينشئ حساب للتاجر وبيحط مفاتيح الجلسة الصح (متل ما بيعمل التسجيل/الدخول)."""
    account = make_account(merchant, name=name, phone=phone, password=password, verified=verified, **kw)
    _put_session(client, account)
    return account


def make_unverified_account(merchant, **kw):
    return make_account(merchant, verified=False, **kw)


def login_unverified(client, merchant, **kw):
    return login_portal(client, merchant, verified=False, **kw)


def make_pending_account(
    name="Pending Owner", phone="+966599990001", password=DEFAULT_PASSWORD, verified=True, **kw
):
    """حساب معلّق (سجّل بنفسه، بدون متجر) بـ hash حقيقي. موثّق افتراضياً."""
    return MerchantAccount.objects.create(
        merchant=None, name=name, phone=phone, password=make_password(password), **verified_fields(verified), **kw
    )


def login_pending(client, **kw):
    """بينشئ حساب معلّق وبيحط مفاتيح الجلسة الصح."""
    account = make_pending_account(**kw)
    _put_session(client, account)
    return account
