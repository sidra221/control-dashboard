"""سقف رسائل الشات اليومي لكل متجر (CHAT_MAX_MESSAGES_PER_STORE_DAY): بعده رد ثابت بلا استدعاء LLM."""
import json
import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import ApiToken, RateBucket
from .services import llm
from .test_quota_widget import HIGH_LIMITS, FakeProvider
from .widget_views import STORE_DAILY_CAP_REPLY

LOGGER = "apitokens.widget_views"


def make_merchant(token):
    return ApiToken.objects.create(label=token, token=token, image_quota=10)


@override_settings(RATE_LIMITS=HIGH_LIMITS, CHAT_MAX_MESSAGES_PER_STORE_DAY=2, CHAT_MAX_MESSAGES_PER_SHOPPER_DAY=1000)
class ChatStoreDailyCapTests(TestCase):
    def setUp(self):
        self.merchant = make_merchant("QSTORECAP001")
        self.client = Client()
        patcher = patch("apitokens.widget_views.llm.get_provider", return_value=FakeProvider(
            [llm.LLMResult(reply_text="أهلا فيك", tool_calls=[])],
        ))
        patcher.start()
        self.addCleanup(patcher.stop)
        chat_patch = patch("apitokens.widget_views.llm.chat_with_retry")
        self.mock_chat = chat_patch.start()
        self.addCleanup(chat_patch.stop)
        self.mock_chat.return_value = llm.LLMResult(reply_text="أهلا فيك", tool_calls=[])
        self.n = 0

    def chat(self, text="مرحبا", token=None):
        # كل رسالة من زبون مختلف: السقف على المتجر كله، مش على الزبون.
        self.n += 1
        body = {"messages": [{"role": "user", "content": text}], "shopper_id": f"shopper-{self.n:08d}"}
        return self.client.post(
            "/widget/chat/", data=json.dumps(body), content_type="application/json",
            HTTP_X_API_TOKEN=token or self.merchant.token,
        )

    def fill_cap(self):
        for _ in range(2):
            self.assertEqual(self.chat().json()["reply"], "أهلا فيك")

    def assert_cap_reply(self, resp, lang):
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {
            "ok": True, "reply": STORE_DAILY_CAP_REPLY[lang], "recommendation": None,
            "recommendations": [], "discount_offer": None, "size_advice": None,
        })

    def test_below_cap_calls_llm(self):
        self.fill_cap()
        self.assertEqual(self.mock_chat.call_count, 2)

    def test_at_cap_no_llm_call_arabic(self):
        self.fill_cap()
        with self.assertLogs(LOGGER, level="ERROR"):
            self.assert_cap_reply(self.chat("عندكم فساتين؟"), "ar")
        self.assertEqual(self.mock_chat.call_count, 2)

    def test_at_cap_no_llm_call_english(self):
        self.fill_cap()
        with self.assertLogs(LOGGER, level="ERROR"):
            self.assert_cap_reply(self.chat("Do you have dresses?"), "en")
        self.assertEqual(self.mock_chat.call_count, 2)

    def test_cap_is_per_merchant(self):
        self.fill_cap()
        other = make_merchant("QSTORECAP002")
        with self.assertLogs(LOGGER, level="ERROR"):
            self.assert_cap_reply(self.chat(), "ar")
        self.assertEqual(self.chat(token=other.token).json()["reply"], "أهلا فيك")
        self.assertEqual(self.mock_chat.call_count, 3)

    def test_window_rolls_after_a_day(self):
        self.fill_cap()
        with self.assertLogs(LOGGER, level="ERROR"):
            self.assert_cap_reply(self.chat(), "ar")
        RateBucket.objects.filter(key__startswith="chatstore").update(
            window_start=timezone.now() - timedelta(hours=24, seconds=1),
        )
        self.assertEqual(self.chat().json()["reply"], "أهلا فيك")
        self.assertEqual(self.mock_chat.call_count, 3)

    def test_error_logged_once_per_merchant_per_day(self):
        self.fill_cap()
        with self.assertLogs(LOGGER, level="ERROR") as logs:
            for _ in range(3):
                self.assert_cap_reply(self.chat(), "ar")
        self.assertEqual(logs.output, [f"ERROR:{LOGGER}:chat_store_daily_cap_reached merchant={self.merchant.pk}"])
        # نافذة يوم جديدة: بينسجّل مرة كمان
        RateBucket.objects.filter(key__startswith="chatstore").update(
            window_start=timezone.now() - timedelta(hours=24, seconds=1),
        )
        self.fill_cap()
        with self.assertLogs(LOGGER, level="ERROR") as logs:
            self.chat()
            self.chat()
        self.assertEqual(len(logs.output), 1)

    def test_staff_dashboard_shows_cap_hit(self):
        other = make_merchant("QSTORECAP003")
        self.fill_cap()
        with self.assertLogs(LOGGER, level="ERROR"):
            self.chat()
        staff = get_user_model().objects.create_user("staff", password="x", is_staff=True)
        self.client.force_login(staff)
        session = self.client.session
        session["lang"] = "en"
        session.save()
        html = self.client.get(reverse("apitokens:dashboard")).content.decode()
        rows = re.findall(r"<tr>.*?</tr>", html, flags=re.S)
        capped = next(r for r in rows if f"<td>{self.merchant.label}</td>" in r)
        fine = next(r for r in rows if f"<td>{other.label}</td>" in r)
        self.assertIn("Hit daily chat cap", capped)
        self.assertNotIn("Hit daily chat cap", fine)
