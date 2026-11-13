"""إرسال واتساب عبر LightOTP.

- كود التحقق (OTP): POST /SendMessage — بنرسل الكود (otpCode) والرقم واللغة بس؛ القالب بيملكه LightOTP
  ومعتمد مسبقاً (فئة Authentication)، وما في templateId. LightOTP ما بيولّد الكود ولا بيتحقق منه:
  التحقق عندنا (portal_otp.py).
- رسائل الخدمة بقوالبنا (Utility): POST /SendUtilityMessage (send_utility) — مو للأكواد، واتساب بيرفضها
  (INCORRECT_CATEGORY).

نقاط من المرجع:
- 200 OK يعني "قُبلت" (غالباً Pending) مو "وصلت": التسليم الحقيقي بيجي من CheckMessageStatus.
- الرقم لازم يكون E.164 صالح فعلاً: التحويل بمكتبة phonenumbers، مو يدوياً.
- idempotencyKey (UUID) مشتق من الحدث (uuid5) مو uuid4: إعادة المحاولة ما بتخصم رصيد مرتين.
- تهدئة تصاعدية لكل رقم عند LightOTP (30ث، 60، 120، 240...). وإعادة محاولة مع فشل شبكة بنفس المفتاح.
"""
import re
import uuid
from dataclasses import dataclass

import phonenumbers
import requests
from django.conf import settings

BASE = "https://api.lightotp.com"
TIMEOUT = 15

# مفتاح ثابت لمشروعنا لاشتقاق idempotencyKey (مو المثال اللي بالمرجع).
NAMESPACE = uuid.UUID("4f446bb1-eaad-4814-bd01-721378761f8e")

# رموز لا ينفع معها إعادة المحاولة — الخطأ بالطلب نفسه
PERMANENT = {
    "ApiKeyNotFound", "ApiKeyExpired", "ApiKeyIsRequired",
    "TemplateNotFound", "TemplateIsRejected",
    "TemplateIsNotApproved", "TemplateIsDeactivated",
    "TemplateIsNotUtility", "ThisTemplateIsForAnotherClient",
    "BodyParameterCountMismatch", "HeaderParameterIsRequired",
    "HeaderParameterNotAllowed", "InvalidphoneNumber",
    "DestinationPhoneNumberIsRequired",
    # /SendMessage (OTP)
    "OTPCodeIsRequired", "OTPCodeLengthMustBeLessThanOrEqualTo8",
    "OTPCodeMustContainOnlyLettersOrDigits", "PhoneNumberCanNotBeEmpty",
    "SenderNumberNotFound",
}


class LightOtpError(Exception):
    def __init__(self, code, status, retryable):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable


class LightOtpNotConfigured(Exception):
    """LIGHTOTP_API_KEY أو LIGHTOTP_TEMPLATE_ID ناقصين بالبيئة."""


@dataclass
class SendResult:
    message_id: str
    status: str


def to_e164(raw, default_region):
    """بيحوّل رقم لـ E.164. default_region مثل "SA". ما منكتب قواعد كل بلد بإيدينا."""
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException:
        raise ValueError(f"رقم غير صالح لـ {default_region}: {raw!r}")
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError(f"رقم غير صالح لـ {default_region}: {raw!r}")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def event_key(event):
    """مفتاح ثابت للحدث نفسه (مو uuid4 عند كل نداء) حتى إعادة المحاولة ما تخصم رصيد."""
    return str(uuid.uuid5(NAMESPACE, event))


def _error_from(response):
    try:
        code = response.json().get("errorMessage") or f"HTTP {response.status_code}"
    except ValueError:
        code = f"HTTP {response.status_code}"
    code = str(code)[:200]
    retryable = (
        response.status_code >= 500
        or response.status_code == 429
        or (code not in PERMANENT and code != "InsufficientBalance")
    )
    return LightOtpError(code, response.status_code, retryable)


def _post(path, api_key, payload, lang):
    try:
        r = requests.post(
            f"{BASE}{path}",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json", "Accept-Language": lang},
            json=payload,
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        # انقطاع شبكة: بنعيد بنفس المفتاح
        raise LightOtpError("NetworkError", 0, True)
    if r.status_code == 200:
        data = r.json()
        # Pending = "بالطابور" مو "وصلت"
        return SendResult(data["id"], data.get("messageStatus", "Pending"))
    raise _error_from(r)


def send_utility(
    *, api_key, template_id, phone, default_region, body_params,
    header_param=None, idempotency_key=None, lang="ar",
):
    payload = {
        "templateId": template_id,
        "toPhoneE164": to_e164(phone, default_region),
        "bodyParameters": list(body_params),
    }
    # رأس القالب: بنرسله بس إذا كان القالب يحوي متغيّراً
    if header_param is not None:
        payload["headerParameter"] = header_param
    if idempotency_key:
        payload["idempotencyKey"] = idempotency_key
    return _post("/SendUtilityMessage", api_key, payload, lang)


OTP_CODE_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")


def send_otp(*, api_key, phone, default_region, code, idempotency_key=None, lang="ar"):
    """كود تحقق عبر /SendMessage. الكود 1–8 أحرف/أرقام بس. lang بيختار لغة رسالة واتساب (languageCode)."""
    if not OTP_CODE_RE.match(str(code)):
        raise ValueError("otpCode: 1-8 letters or digits only")
    payload = {
        "otpCode": str(code),
        "toPhoneE164": to_e164(phone, default_region),
        "languageCode": lang,
    }
    if idempotency_key:
        payload["idempotencyKey"] = idempotency_key
    return _post("/SendMessage", api_key, payload, lang)


def send_otp_message(*, phone, code, event, lang="ar"):
    """بيرسل كود التحقق. المفتاح من الإعدادات؛ idempotencyKey مشتق من الحدث."""
    api_key = getattr(settings, "LIGHTOTP_API_KEY", "")
    if not api_key:
        raise LightOtpNotConfigured("LIGHTOTP_API_KEY")
    lang = "en" if lang == "en" else "ar"
    return send_otp(
        api_key=api_key,
        phone=phone,
        default_region=getattr(settings, "LIGHTOTP_DEFAULT_REGION", "SA"),
        code=code,
        idempotency_key=event_key(event),
        lang=lang,
    )


def check_status(*, message_id):
    """حالة الرسالة: Pending/Sent/Delivered/Read/Failed/Deleted (+ failureCode/failureReason)."""
    api_key = getattr(settings, "LIGHTOTP_API_KEY", "")
    if not api_key:
        raise LightOtpNotConfigured("LIGHTOTP_API_KEY")
    try:
        r = requests.post(
            f"{BASE}/CheckMessageStatus",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            json={"id": message_id},
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        raise LightOtpError("NetworkError", 0, True)
    if r.status_code != 200:
        raise _error_from(r)
    return r.json()
