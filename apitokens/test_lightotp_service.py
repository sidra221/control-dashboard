"""طبقة LightOTP: التحويل E.164، المفتاح، الإرسال، تصنيف الأخطاء. كله بـ mock (ما في اتصال حقيقي)."""

from unittest import mock

import requests
from django.test import SimpleTestCase, override_settings

from .services import lightotp


def fake_response(status=200, body=None, bad_json=False):
    r = mock.Mock()
    r.status_code = status
    if bad_json:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = body if body is not None else {}
    return r


@override_settings(LIGHTOTP_API_KEY="key-123", LIGHTOTP_TEMPLATE_ID="tpl-1", LIGHTOTP_DEFAULT_REGION="SA")
class SendTests(SimpleTestCase):
    def send(self, response=None, **kw):
        with mock.patch("apitokens.services.lightotp.requests.post", return_value=response or fake_response(
                body={"id": "msg-1", "messageStatus": "Pending"})) as post:
            result = lightotp.send_otp_message(phone="+966551234567", code="123456", event="otp:1", **kw)
        return result, post

    def test_success_returns_id_and_pending_and_sends_the_contract(self):
        result, post = self.send()
        self.assertEqual((result.message_id, result.status), ("msg-1", "Pending"))
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.lightotp.com/SendMessage")
        self.assertEqual(kwargs["headers"]["X-Api-Key"], "key-123")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        self.assertEqual(kwargs["headers"]["Accept-Language"], "ar")
        self.assertEqual(kwargs["timeout"], 15)
        payload = kwargs["json"]
        self.assertEqual(payload, {
            "otpCode": "123456",
            "toPhoneE164": "+966551234567",
            "languageCode": "ar",
            "idempotencyKey": lightotp.event_key("otp:1"),
        })  # ما في templateId ولا bodyParameters: القالب عند LightOTP

    def test_language_goes_to_the_whatsapp_message_and_to_the_header(self):
        _, post = self.send(lang="en")
        self.assertEqual(post.call_args.kwargs["headers"]["Accept-Language"], "en")
        self.assertEqual(post.call_args.kwargs["json"]["languageCode"], "en")
        _, post = self.send(lang="fr")  # لغة غير مدعومة عندنا ← عربي
        self.assertEqual(post.call_args.kwargs["headers"]["Accept-Language"], "ar")
        self.assertEqual(post.call_args.kwargs["json"]["languageCode"], "ar")

    def test_idempotency_key_is_a_uuid(self):
        import uuid
        _, post = self.send()
        uuid.UUID(post.call_args.kwargs["json"]["idempotencyKey"])

    def test_template_id_is_not_needed_for_otp(self):
        with override_settings(LIGHTOTP_TEMPLATE_ID=""):
            result, post = self.send()
        self.assertEqual(result.message_id, "msg-1")
        self.assertNotIn("templateId", post.call_args.kwargs["json"])

    def test_otp_code_constraints_are_checked_before_any_network_call(self):
        for bad in ("", "123456789", "12 34", "12-34", "١٢٣٤٥٦", "12.4", "ab#1"):
            with mock.patch("apitokens.services.lightotp.requests.post") as post:
                with self.assertRaises(ValueError, msg=repr(bad)):
                    lightotp.send_otp_message(phone="+966551234567", code=bad, event="e")
            post.assert_not_called()
        for ok in ("1", "12345678", "AbC123"):
            with mock.patch("apitokens.services.lightotp.requests.post", return_value=fake_response(body={"id": "x"})):
                lightotp.send_otp_message(phone="+966551234567", code=ok, event="e")

    def test_missing_status_defaults_to_pending(self):
        result, _ = self.send(fake_response(body={"id": "m"}))
        self.assertEqual(result.status, "Pending")

    def test_permanent_errors_are_not_retryable(self):
        for code in ("OTPCodeIsRequired", "OTPCodeMustContainOnlyLettersOrDigits", "PhoneNumberCanNotBeEmpty", "SenderNumberNotFound", "ApiKeyExpired", "InvalidphoneNumber"):
            with self.assertRaises(lightotp.LightOtpError) as cm:
                self.send(fake_response(400, {"errorMessage": code}))
            self.assertEqual((cm.exception.code, cm.exception.status, cm.exception.retryable), (code, 400, False))

    def test_insufficient_balance_is_not_retryable(self):
        with self.assertRaises(lightotp.LightOtpError) as cm:
            self.send(fake_response(400, {"errorMessage": "InsufficientBalance"}))
        self.assertFalse(cm.exception.retryable)

    def test_cooldown_style_400_and_429_and_5xx_are_retryable(self):
        for status, body in ((400, {"errorMessage": "يرجى الانتظار 30 ثانية قبل الإرسال مجدداً"}),
                             (429, {}), (500, {}), (503, {"errorMessage": "x"})):
            with self.assertRaises(lightotp.LightOtpError) as cm:
                self.send(fake_response(status, body))
            self.assertTrue(cm.exception.retryable, (status, body))

    def test_non_json_error_body(self):
        with self.assertRaises(lightotp.LightOtpError) as cm:
            self.send(fake_response(502, bad_json=True))
        self.assertEqual(cm.exception.code, "HTTP 502")
        self.assertTrue(cm.exception.retryable)

    def test_network_failure_is_retryable(self):
        with mock.patch("apitokens.services.lightotp.requests.post", side_effect=requests.ConnectionError):
            with self.assertRaises(lightotp.LightOtpError) as cm:
                lightotp.send_otp_message(phone="+966551234567", code="1", event="e")
        self.assertEqual((cm.exception.code, cm.exception.retryable), ("NetworkError", True))

    def test_invalid_phone_never_calls_the_api(self):
        with mock.patch("apitokens.services.lightotp.requests.post") as post:
            with self.assertRaises(ValueError):
                lightotp.send_otp_message(phone="+999999999999", code="1", event="e")
        post.assert_not_called()

    def test_local_number_uses_default_region(self):
        _, post = self.send()
        with mock.patch("apitokens.services.lightotp.requests.post", return_value=fake_response(body={"id": "x"})) as p:
            lightotp.send_otp_message(phone="0551234567", code="1", event="e")
        self.assertEqual(p.call_args.kwargs["json"]["toPhoneE164"], "+966551234567")


class ConfigTests(SimpleTestCase):
    @override_settings(LIGHTOTP_API_KEY="")
    def test_not_configured_raises_without_calling_the_network(self):
        with mock.patch("apitokens.services.lightotp.requests.post") as post:
            with self.assertRaises(lightotp.LightOtpNotConfigured):
                lightotp.send_otp_message(phone="+966551234567", code="1", event="e")
            with self.assertRaises(lightotp.LightOtpNotConfigured):
                lightotp.check_status(message_id="m")
        post.assert_not_called()


class UtilityTests(SimpleTestCase):
    """رسائل الخدمة بقالب معتمد (مو للأكواد): SendUtilityMessage."""

    def call(self, **kw):
        defaults = dict(api_key="k", template_id="tpl", phone="+966551234567", default_region="SA", body_params=["a"])
        defaults.update(kw)
        with mock.patch("apitokens.services.lightotp.requests.post", return_value=fake_response(
                body={"id": "u-1", "messageStatus": "Pending"})) as post:
            result = lightotp.send_utility(**defaults)
        return result, post

    def test_payload_and_endpoint(self):
        result, post = self.call(idempotency_key="key-1")
        self.assertEqual(result.message_id, "u-1")
        self.assertEqual(post.call_args.args[0], "https://api.lightotp.com/SendUtilityMessage")
        self.assertEqual(post.call_args.kwargs["json"], {
            "templateId": "tpl", "toPhoneE164": "+966551234567", "bodyParameters": ["a"], "idempotencyKey": "key-1",
        })

    def test_header_parameter_only_when_given(self):
        _, post = self.call(header_param="Ahmad")
        self.assertEqual(post.call_args.kwargs["json"]["headerParameter"], "Ahmad")
        _, post = self.call()
        self.assertNotIn("headerParameter", post.call_args.kwargs["json"])

    def test_template_errors_are_permanent(self):
        with mock.patch("apitokens.services.lightotp.requests.post", return_value=fake_response(
                400, {"errorMessage": "BodyParameterCountMismatch"})):
            with self.assertRaises(lightotp.LightOtpError) as cm:
                lightotp.send_utility(api_key="k", template_id="t", phone="+966551234567",
                                      default_region="SA", body_params=["a", "b"])
        self.assertFalse(cm.exception.retryable)


class E164Tests(SimpleTestCase):
    def test_valid_numbers(self):
        for raw, region, want in (
            ("+966551234567", "SA", "+966551234567"),
            ("+201001234567", "SA", "+201001234567"),
            ("+14155552671", "SA", "+14155552671"),
            ("0551234567", "SA", "+966551234567"),
            ("+966 55 123 4567", "SA", "+966551234567"),
        ):
            self.assertEqual(lightotp.to_e164(raw, region), want, raw)

    def test_invalid_numbers_are_rejected(self):
        for raw in ("+999999999999", "123", "abc", "", "+966"):
            with self.assertRaises(ValueError, msg=raw):
                lightotp.to_e164(raw, "SA")

    def test_local_number_is_not_guessed_in_another_region(self):
        with self.assertRaises(ValueError):
            lightotp.to_e164("0551234567", "JO")  # 05... مو رقم أردني صالح


class EventKeyTests(SimpleTestCase):
    def test_same_event_same_key_different_event_different_key(self):
        self.assertEqual(lightotp.event_key("otp:1"), lightotp.event_key("otp:1"))
        self.assertNotEqual(lightotp.event_key("otp:1"), lightotp.event_key("otp:2"))


@override_settings(LIGHTOTP_API_KEY="key-123")
class CheckStatusTests(SimpleTestCase):
    def test_ok(self):
        body = {"id": "m", "messageStatus": "Delivered", "failureCode": None}
        with mock.patch("apitokens.services.lightotp.requests.post", return_value=fake_response(body=body)) as post:
            self.assertEqual(lightotp.check_status(message_id="m")["messageStatus"], "Delivered")
        self.assertEqual(post.call_args.kwargs["json"], {"id": "m"})
        self.assertEqual(post.call_args.args[0], "https://api.lightotp.com/CheckMessageStatus")

    def test_error_and_network(self):
        with mock.patch("apitokens.services.lightotp.requests.post", return_value=fake_response(404, {"errorMessage": "x"})):
            with self.assertRaises(lightotp.LightOtpError):
                lightotp.check_status(message_id="m")
        with mock.patch("apitokens.services.lightotp.requests.post", side_effect=requests.Timeout):
            with self.assertRaises(lightotp.LightOtpError) as cm:
                lightotp.check_status(message_id="m")
        self.assertTrue(cm.exception.retryable)
