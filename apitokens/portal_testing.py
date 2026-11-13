"""أدوات مشتركة لاختبارات بوابة التاجر (مش كود إنتاج)."""

from django.contrib.auth.hashers import make_password
from django.test import override_settings

from . import portal_auth
from .models import MerchantAccount

# هاشر سريع للاختبارات بس (PBKDF2 الحقيقي بطيء). بيضل hash حقيقي مملّح، مش نص عادي.
FAST_HASHERS = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])

DEFAULT_PASSWORD = "Zebra-Orchid-4821"


def default_phone(merchant):
    return f"+9665{merchant.pk:08d}"


def make_account(merchant, name="Test Owner", phone=None, password=DEFAULT_PASSWORD, **kw):
    """بيعمل (أو بيحدّث) حساب البوابة للتاجر بـ hash حقيقي."""
    fields = {"name": name, "phone": phone or default_phone(merchant), "password": make_password(password), **kw}
    account, _ = MerchantAccount.objects.update_or_create(merchant=merchant, defaults=fields)
    return account


def login_portal(client, merchant, name="Test Owner", phone=None, password=DEFAULT_PASSWORD, **kw):
    """بينشئ حساب للتاجر وبيحط مفاتيح الجلسة الصح (متل ما بيعمل التسجيل/الدخول)."""
    account = make_account(merchant, name=name, phone=phone, password=password, **kw)
    session = client.session
    session[portal_auth.SESSION_ACCOUNT] = account.pk
    session[portal_auth.SESSION_FINGERPRINT] = portal_auth.session_fingerprint(account)
    session.save()
    return account


def make_pending_account(name="Pending Owner", phone="+966599990001", password=DEFAULT_PASSWORD, **kw):
    """حساب معلّق (سجّل بنفسه، بدون متجر) بـ hash حقيقي."""
    return MerchantAccount.objects.create(
        merchant=None, name=name, phone=phone, password=make_password(password), **kw
    )


def login_pending(client, **kw):
    """بينشئ حساب معلّق وبيحط مفاتيح الجلسة الصح."""
    account = make_pending_account(**kw)
    session = client.session
    session[portal_auth.SESSION_ACCOUNT] = account.pk
    session[portal_auth.SESSION_FINGERPRINT] = portal_auth.session_fingerprint(account)
    session.save()
    return account
