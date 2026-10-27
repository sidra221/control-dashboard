"""اختبارات /widget/chat/ — أول اختبارات آلية لهالتطبيق.

بنموّك طبقة الـ LLM (llm.get_provider / llm.chat_with_retry) حتى نقدر
نختبر منطق الـ backend الحتمي (دمج التوصيات، فلترة الميزانية، منع اختلاق
الخصم) بسرعة وبدون كلفة/تذبذب استدعاء نموذج حقيقي. هاد بيغطي Fix 1 وFix 2
بشكل كامل لأنهم منطق كود صرف.

Fix 3 (المقاسات/المخزون/الإرجاع/الشحن) تعليمات برومبت بس، بدون أي فحص
حتمي بالكود (بقرار صريح: فلاتر كلمات عريضة لهالموضوع خطرة/غير موثوقة) —
يعني ما في شي حتمي هون نقدر نختبره بموك واقعي؛ أي اختبار بموك بيكون بس
فحص لنفس الموك يلي كتبناه، مش فحص لسلوك الموديل الحقيقي. التحقق الحقيقي
لـ Fix 3 صار عبر استدعاءات حية فعلية موثّقة بتقرير التنفيذ، مش هون.
"""

import json
from unittest.mock import patch

from django.test import Client, TestCase

from .models import ApiToken, DiscountCode, Product
from .services import llm


class FakeProvider:
    def is_configured(self):
        return True


def fake_result(reply_text="", tool_calls=None):
    return llm.LLMResult(reply_text=reply_text, tool_calls=tool_calls or [])


class WidgetChatBackendLogicTests(TestCase):
    """يغطي Fix 1 (توصيات متعددة، دمج بدون فقدان، تكرار، ميزانية) وFix 2
    (منع اختلاق خصم مش موجود فعلياً، discount_offer متوافق مع الواقع)."""

    def setUp(self):
        self.merchant = ApiToken.objects.create(
            label="Test Merchant", token="TESTTOKEN0001", image_quota=10,
        )
        self.shirt_blue = Product.objects.create(
            merchant=self.merchant, sku="SHIRT-BLUE", title="Blue Shirt",
            price="29.99", image_url="https://example.com/1.jpg", category="shirts",
        )
        self.shirt_expensive = Product.objects.create(
            merchant=self.merchant, sku="SHIRT-EXPENSIVE", title="Expensive Shirt",
            price="150.00", image_url="https://example.com/2.jpg", category="shirts",
        )
        self.dress_black = Product.objects.create(
            merchant=self.merchant, sku="DRESS-BLACK", title="Black Dress",
            price="79.99", image_url="https://example.com/3.jpg", category="dresses",
        )
        self.client = Client()

    def post(self, messages):
        return self.client.post(
            "/widget/chat/",
            data=json.dumps({"messages": messages}),
            content_type="application/json",
            HTTP_X_API_TOKEN=self.merchant.token,
        )

    # ---------- Fix 1: multiple recommendations ----------

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_single_recommendation_both_fields_populated(self, mock_provider, mock_chat):
        """طلب منتج واحد: recommendation (توافق قديم) وrecommendations
        (الحقل الجديد) لازم يرجعوا نفس المنتج الوحيد."""
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(
            tool_calls=[{"name": "recommend_product", "input": {"sku": "SHIRT-BLUE", "reason": "تمام"}}],
        )
        resp = self.post([{"role": "user", "content": "بدي قميص أزرق"}])
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["recommendation"]["sku"], "SHIRT-BLUE")
        self.assertEqual([r["sku"] for r in data["recommendations"]], ["SHIRT-BLUE"])

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_two_recommendations_both_kept_within_budget(self, mock_provider, mock_chat):
        """الموديل بيرشّح منتجين بنفس الرد — لازم الاثنين يطلعوا، مش بس
        الأخير (هاد بالضبط كان الـ bug المؤكد تجريبياً C-04)."""
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(
            tool_calls=[
                {"name": "recommend_product", "input": {"sku": "SHIRT-BLUE", "reason": "خيار أول"}},
                {"name": "recommend_product", "input": {"sku": "DRESS-BLACK", "reason": "خيار ثاني"}},
            ],
        )
        resp = self.post([{"role": "user", "content": "فرجيني أفضل خيارين"}])
        data = resp.json()
        self.assertEqual(
            sorted(r["sku"] for r in data["recommendations"]),
            ["DRESS-BLACK", "SHIRT-BLUE"],
        )
        # التوافق القديم: recommendation لازم يكون أول عنصر بالقائمة، مش None
        self.assertEqual(data["recommendation"]["sku"], "SHIRT-BLUE")

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_duplicate_sku_deduplicated(self, mock_provider, mock_chat):
        """نفس الـ sku مرتين بنفس الرد — لازم يطلع مرة وحدة بس بالنتيجة."""
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(
            tool_calls=[
                {"name": "recommend_product", "input": {"sku": "SHIRT-BLUE", "reason": "أ"}},
                {"name": "recommend_product", "input": {"sku": "SHIRT-BLUE", "reason": "ب"}},
            ],
        )
        resp = self.post([{"role": "user", "content": "بدي قميص"}])
        data = resp.json()
        self.assertEqual(len(data["recommendations"]), 1)

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_budget_filter_drops_over_budget_item(self, mock_provider, mock_chat):
        """الزبون قال ميزانيته 50 دولار، والموديل رشّح قميص بـ29.99 وقميص
        بـ150 — لازم يضل بس القميص يلي ضمن الميزانية."""
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(
            tool_calls=[
                {"name": "recommend_product", "input": {"sku": "SHIRT-BLUE", "reason": "أ"}},
                {"name": "recommend_product", "input": {"sku": "SHIRT-EXPENSIVE", "reason": "ب"}},
            ],
        )
        resp = self.post([
            {"role": "user", "content": "بدي قميص"},
            {"role": "assistant", "content": "شو ميزانيتك؟"},
            {"role": "user", "content": "ميزانيتي 50 دولار، فرجيني خيارات"},
        ])
        data = resp.json()
        self.assertEqual([r["sku"] for r in data["recommendations"]], ["SHIRT-BLUE"])

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_budget_filter_never_returns_empty(self, mock_provider, mock_chat):
        """لو كل الترشيحات فوق الميزانية المذكورة، لازم نضل مع خيار وحد
        (الأول) بدل ما نرجّع قائمة فاضية بالكامل للزبون."""
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(
            tool_calls=[
                {"name": "recommend_product", "input": {"sku": "SHIRT-EXPENSIVE", "reason": "أ"}},
                {"name": "recommend_product", "input": {"sku": "DRESS-BLACK", "reason": "ب"}},
            ],
        )
        resp = self.post([{"role": "user", "content": "ميزانيتي 10 دولار بس، فرجيني خيارات"}])
        data = resp.json()
        self.assertEqual(len(data["recommendations"]), 2)  # ولا واحد منهم ضمن 10$، فبنضل مع الاثنين

    # ---------- Fix 2: discount integrity ----------

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_no_configured_codes_discount_offer_stays_null(self, mock_provider, mock_chat):
        """ما في ولا كود خصم فعّال — الرد العادي (بدون ذكر خصم) ما لازم
        يفعّل أي تصحيح، وlazم discount_offer يضل null."""
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(reply_text="هاد خيار حلو، جربيه!")
        resp = self.post([{"role": "user", "content": "غالي شوي"}])
        data = resp.json()
        self.assertIsNone(data["discount_offer"])

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_fabricated_discount_with_no_real_code_gets_corrected(self, mock_provider, mock_chat):
        """أهم اختبار بكل الملف: ما في ولا كود خصم فعّال بالداتا، بس
        الموديل (المموّك) اخترع ذكر خصم بالنص العادي من غير استدعاء أداة —
        بالضبط سيناريو C-01 الحقيقي. لازم السيستم يكتشفها ويصحح الرد."""
        mock_provider.return_value = FakeProvider()
        # أول استدعاء: اختلاق (مثل الحالة الحقيقية المسجّلة بتقرير QA)
        fabricated = fake_result(reply_text="ممكن تستفيد من خصم 15% إذا بدك تشتريه هلق!")
        # ثاني استدعاء (التصحيح): رد نظيف بدون أي ذكر خصم
        corrected = fake_result(reply_text="فهمتك، هاد القميص قيمته ممتازة لجودته وتصميمه.")
        mock_chat.side_effect = [fabricated, corrected]

        resp = self.post([{"role": "user", "content": "مو متأكد إذا اشتريه"}])
        data = resp.json()
        self.assertIsNone(data["discount_offer"])
        self.assertNotIn("خصم", data["reply"])
        self.assertNotIn("%", data["reply"])
        self.assertEqual(mock_chat.call_count, 2)  # تأكيد إنه فعلاً صار تصحيح، مش بس صدفة

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_real_discount_code_offered_correctly(self, mock_provider, mock_chat):
        """في كود فعّال حقيقي، والموديل استخدم الأداة الصح — لازم
        discount_offer يطلع متطابق تماماً مع الكود الحقيقي بالداتابيز."""
        DiscountCode.objects.create(
            merchant=self.merchant, code="SAVE10", description="خصم 10% على كل المنتجات",
        )
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(
            tool_calls=[{"name": "offer_discount_code", "input": {"code": "SAVE10"}}],
        )
        resp = self.post([{"role": "user", "content": "قررت اشتريه أكيد"}])
        data = resp.json()
        self.assertEqual(data["discount_offer"], {"code": "SAVE10", "description": "خصم 10% على كل المنتجات"})

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_hallucinated_code_not_matching_real_one_is_rejected(self, mock_provider, mock_chat):
        """في كود حقيقي SAVE10، بس الموديل (المموّك) استدعى الأداة بكود
        مختلف مختلق — ما لازم ينقبل، discount_offer لازم يضل None (وهاد
        بيفعّل مسار التصحيح لإنه النص التاني بيذكر رقم/خصم)."""
        DiscountCode.objects.create(
            merchant=self.merchant, code="SAVE10", description="خصم 10%",
        )
        mock_provider.return_value = FakeProvider()
        fabricated = fake_result(
            reply_text="",
            tool_calls=[{"name": "offer_discount_code", "input": {"code": "FAKE50"}}],
        )
        corrected = fake_result(reply_text="هاد خيار ممتاز بسعره الحالي.")
        mock_chat.side_effect = [fabricated, corrected]
        resp = self.post([{"role": "user", "content": "قررت اشتريه"}])
        data = resp.json()
        self.assertIsNone(data["discount_offer"])

    # ---------- H-01: purchase link ----------

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_purchase_intent_resurfaces_real_product_link(self, mock_provider, mock_chat):
        """الزبون بيسأل "كيف بطلبه؟" عن منتج انعرض قبل، والموديل رد بنص بدون
        أداة — لازم السيستم يجبر recommend_product ويرجّع الرابط الحقيقي."""
        self.dress_black.product_url = "https://shop.example.com/products/dress-black"
        self.dress_black.save()
        mock_provider.return_value = FakeProvider()
        stalled = fake_result(reply_text="الفستان الأسود مثالي إلك!")
        forced = fake_result(tool_calls=[{
            "name": "recommend_product",
            "input": {"sku": "DRESS-BLACK", "reason": "تقدر تطلبه من الزر هون 👇"},
        }])
        mock_chat.side_effect = [stalled, forced]
        resp = self.post([
            {"role": "user", "content": "بدي فستان أسود"},
            {"role": "assistant", "content": "عندي هاد الفستان الأسود."},
            {"role": "user", "content": "تمام، كيف بطلبه؟"},
        ])
        data = resp.json()
        self.assertEqual(mock_chat.call_args_list[1].kwargs.get("force_tool"), "recommend_product")
        self.assertEqual(data["recommendations"][0]["sku"], "DRESS-BLACK")
        self.assertEqual(
            data["recommendations"][0]["product_url"],
            "https://shop.example.com/products/dress-black",
        )

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_product_url_always_from_db_not_model(self, mock_provider, mock_chat):
        """حتى لو الموديل حاول يمرّر رابط من عنده، الرابط بالرد لازم يكون
        رابط المنتج الحقيقي من الداتابيز."""
        self.shirt_blue.product_url = "https://shop.example.com/products/shirt-blue"
        self.shirt_blue.save()
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(tool_calls=[{
            "name": "recommend_product",
            "input": {"sku": "SHIRT-BLUE", "reason": "x", "product_url": "https://evil.example.com"},
        }])
        resp = self.post([{"role": "user", "content": "ابعتلي الرابط"}])
        self.assertEqual(
            resp.json()["recommendations"][0]["product_url"],
            "https://shop.example.com/products/shirt-blue",
        )

    # ---------- gift try-on button label (already-shipped behavior, covered for regression safety) ----------

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_gift_recommendation_uses_gift_button_label(self, mock_provider, mock_chat):
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(
            tool_calls=[{
                "name": "recommend_product",
                "input": {"sku": "DRESS-BLACK", "reason": "لأختك", "for_someone_else": True},
            }],
        )
        resp = self.post([{"role": "user", "content": "فستان هدية لأختي"}])
        data = resp.json()
        self.assertIn("صاحب الهدية", data["recommendations"][0]["tryon_button_label"])


class WidgetChatValidationTests(TestCase):
    """تغطية أساسية للتحقق الموجود أصلاً بالـ endpoint — ما كان في ولا
    اختبار واحد لهالتطبيق قبل هالملف."""

    def setUp(self):
        self.merchant = ApiToken.objects.create(
            label="Test Merchant 2", token="TESTTOKEN0002", image_quota=10,
        )
        self.client = Client()

    def test_missing_token_rejected(self):
        resp = self.client.post(
            "/widget/chat/",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"], "missing_token")

    def test_too_many_messages_rejected(self):
        messages = [{"role": "user", "content": "hi"} for _ in range(31)]
        resp = self.client.post(
            "/widget/chat/",
            data=json.dumps({"messages": messages}),
            content_type="application/json",
            HTTP_X_API_TOKEN=self.merchant.token,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "too_many_messages")

    def test_last_message_must_be_user(self):
        resp = self.client.post(
            "/widget/chat/",
            data=json.dumps({"messages": [{"role": "assistant", "content": "hi"}]}),
            content_type="application/json",
            HTTP_X_API_TOKEN=self.merchant.token,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "last_message_must_be_user")
