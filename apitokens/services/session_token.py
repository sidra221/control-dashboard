"""توكنات الهوية (v2): الزبون ما بيختار هويته ولا بيرسل تاريخ محادثته — السيرفر بيصدر
توكنات موقّعة (HMAC بـ SECRET_KEY) والمتصفح بيحملها فقط.

- session: محادثة وحدة. بتنتهي بعد خمول SESSION_TTL، وبتتجدد مع كل دور (sliding).
- shopper: الزبون عبر الزيارات. صلاحيتها SHOPPER_TTL، والاحتفاظ الفعلي بالبيانات
  بيحكمه `ApiToken.profile_retention_days` (مش التوكن).

كل توكن بيحمل رقم التاجر: توكن تاجر ما بينفع عند تاجر تاني حتى لو سرّبوه.
أي توكن فاسد/منتهي/لتاجر تاني بيرجع None (ما بنرمي) — المتصل بيبدأ جلسة جديدة.
"""

import logging

from django.conf import settings
from django.core import signing

logger = logging.getLogger(__name__)

SESSION_SALT = "apitokens.v2.session"
SHOPPER_SALT = "apitokens.v2.shopper"


def _ttl(name, default):
    return int(getattr(settings, name, default))


def issue_session(merchant, session_id, profile_public_id):
    return signing.dumps(
        {"m": merchant.pk, "s": session_id, "p": profile_public_id}, salt=SESSION_SALT, compress=False
    )


def issue_shopper(merchant, profile_public_id):
    return signing.dumps({"m": merchant.pk, "p": profile_public_id}, salt=SHOPPER_SALT, compress=False)


def _read(token, salt, merchant, max_age, kind):
    if not token or not isinstance(token, str) or len(token) > 600:
        return None
    try:
        data = signing.loads(token, salt=salt, max_age=max_age)
    except signing.SignatureExpired:
        return None
    except signing.BadSignature:
        logger.warning("v2_token_invalid kind=%s merchant=%s", kind, merchant.pk)
        return None
    if not isinstance(data, dict) or data.get("m") != merchant.pk:
        logger.warning("v2_token_wrong_merchant kind=%s merchant=%s", kind, merchant.pk)
        return None
    return data


def read_session(token, merchant):
    return _read(token, SESSION_SALT, merchant, _ttl("V2_SESSION_TTL_SECONDS", 24 * 3600), "session")


def read_shopper(token, merchant):
    return _read(token, SHOPPER_SALT, merchant, _ttl("V2_SHOPPER_TTL_SECONDS", 180 * 24 * 3600), "shopper")
