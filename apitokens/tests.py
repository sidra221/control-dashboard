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

from .models import ApiToken, DiscountCode, Product, SallaConnection
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

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_reply_text_mentions_offered_code(self, mock_provider, mock_chat):
        """لو discount_offer حقيقي رجع بس النص ما ذكره، النص النهائي لازم
        يتضمن الكود نفسه — الرد والعرض المنظّم ما بيتناقضوا."""
        DiscountCode.objects.create(merchant=self.merchant, code="DRESS10", description="خصم 10% على الفساتين فقط")
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(
            reply_text="هاد الفستان بيستاهل!",
            tool_calls=[
                {"name": "recommend_product", "input": {"sku": "DRESS-BLACK", "reason": "x"}},
                {"name": "offer_discount_code", "input": {"code": "DRESS10"}},
            ],
        )
        data = self.post([{"role": "user", "content": "غالي شوي"}]).json()
        self.assertEqual(data["discount_offer"]["code"], "DRESS10")
        self.assertIn("DRESS10", data["reply"])

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_code_mentioned_when_model_returns_tools_without_text(self, mock_provider, mock_chat):
        """الموديل رجّع بطاقة + كود بلا أي نص: الرد بيتعبّى من reason البطاقة،
        ولازم الكود ينذكر فيه كمان."""
        DiscountCode.objects.create(merchant=self.merchant, code="DRESS10", description="خصم 10% على الفساتين فقط")
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(
            reply_text="",
            tool_calls=[
                {"name": "recommend_product", "input": {"sku": "DRESS-BLACK", "reason": "فستان أنيق لسهرتك"}},
                {"name": "offer_discount_code", "input": {"code": "DRESS10"}},
            ],
        )
        data = self.post([{"role": "user", "content": "غالي شوي"}]).json()
        self.assertEqual(data["discount_offer"]["code"], "DRESS10")
        self.assertIn("فستان أنيق لسهرتك", data["reply"])
        self.assertIn("DRESS10", data["reply"])
        self.assertEqual(data["reply"].count("DRESS10"), 1)

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_named_product_without_card_gets_card_and_keeps_discount(self, mock_provider, mock_chat):
        """الموديل استدعى أداة الخصم وسمّى منتج بالنص بس ما استدعى
        recommend_product — لازم يطلع الكارت، وما يضيع الخصم."""
        DiscountCode.objects.create(merchant=self.merchant, code="SHIRTS15", description="خصم 15% على القمصان")
        mock_provider.return_value = FakeProvider()
        first = fake_result(
            reply_text="شو رأيك بـ Blue Shirt؟ استخدم SHIRTS15",
            tool_calls=[{"name": "offer_discount_code", "input": {"code": "SHIRTS15"}}],
        )
        forced = fake_result(tool_calls=[{"name": "recommend_product", "input": {"sku": "SHIRT-BLUE", "reason": "x"}}])
        mock_chat.side_effect = [first, forced]
        data = self.post([{"role": "user", "content": "بدي قميص بدالو"}]).json()
        self.assertEqual([r["sku"] for r in data["recommendations"]], ["SHIRT-BLUE"])
        self.assertEqual(data["discount_offer"]["code"], "SHIRTS15")

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


# ======================================================================
# جاهزية Shopify: تحويل المنتجات، تجديد التوكن، ربط الطلبات بالمحادثة
# ======================================================================

import base64  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
from datetime import timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from django.utils import timezone  # noqa: E402

from .models import Conversation, ShopifyConnection  # noqa: E402
from .services import catalog as catalog_service  # noqa: E402
from .services import shopify as shopify_service  # noqa: E402


def rest_product(**overrides):
    """منتج بشكل webhook الـ REST تبع Shopify."""
    payload = {
        "id": 111,
        "title": "Linen Dress",
        "handle": "linen-dress",
        "body_html": "<p>Soft <b>linen</b></p>",
        "product_type": "dresses",
        "tags": "linen, summer",
        "status": "active",
        "options": [{"name": "Size", "position": 1}, {"name": "Color", "position": 2}],
        "images": [{"src": "https://cdn.example.com/a.jpg"}],
        "variants": [
            {"id": 1, "sku": "LD-S", "price": "50.00", "compare_at_price": "70.00",
             "option1": "S", "option2": "Beige", "inventory_management": "shopify",
             "inventory_policy": "deny", "inventory_quantity": 0},
            {"id": 2, "sku": "LD-M", "price": "50.00", "compare_at_price": "70.00",
             "option1": "M", "option2": "Beige", "inventory_management": "shopify",
             "inventory_policy": "deny", "inventory_quantity": 3},
        ],
    }
    payload.update(overrides)
    return payload


class ShopifyProductParsingTests(TestCase):
    def test_rest_webhook_payload_uses_only_available_variants(self):
        row = shopify_service.product_row_from_payload(
            "shop.myshopify.com", rest_product(), primary_domain="shop.example.com"
        )
        self.assertEqual(row["sku"], "LD-M")
        self.assertEqual(row["variant_id"], "2")
        self.assertEqual(row["options"], {"Size": ["M"], "Color": ["Beige"]})
        self.assertTrue(row["in_stock"])
        self.assertEqual(row["compare_at_price"], Decimal("70.00"))
        self.assertEqual(row["description"], "Soft linen")
        self.assertEqual(row["product_url"], "https://shop.example.com/products/linen-dress")

    def test_sold_out_product_is_marked_out_of_stock(self):
        payload = rest_product()
        payload["variants"][1]["inventory_quantity"] = 0
        row = shopify_service.product_row_from_payload("shop.myshopify.com", payload)
        self.assertFalse(row["in_stock"])
        self.assertEqual(row["variant_id"], "")

    def test_draft_product_is_dropped(self):
        self.assertIsNone(
            shopify_service.product_row_from_payload("s.myshopify.com", rest_product(status="draft"))
        )

    def test_fake_compare_at_price_is_ignored(self):
        payload = rest_product()
        for v in payload["variants"]:
            v["compare_at_price"] = "50.00"
        row = shopify_service.product_row_from_payload("s.myshopify.com", payload)
        self.assertIsNone(row["compare_at_price"])

    def test_graphql_node_is_normalized(self):
        node = {
            "legacyResourceId": "222", "title": "Shirt", "handle": "shirt", "descriptionHtml": "Cotton",
            "productType": "shirts", "tags": ["men", "office"], "status": "ACTIVE",
            "onlineStoreUrl": "https://shop.example.com/products/shirt",
            "options": [{"name": "Title", "position": 1}],
            "media": {"nodes": [{"preview": {"image": {"url": "https://cdn.example.com/s.jpg"}}}]},
            "variants": {"nodes": [{
                "legacyResourceId": "9", "sku": "SH-1", "price": "30.00", "compareAtPrice": None,
                "availableForSale": True, "selectedOptions": [{"name": "Title", "value": "Default Title"}],
            }]},
        }
        payload = shopify_service._graphql_product_to_payload(node)
        row = shopify_service.product_row_from_payload("s.myshopify.com", payload)
        self.assertEqual(row["external_id"], "222")
        self.assertEqual(row["tags"], "men, office")
        self.assertEqual(row["options"], {})  # "Default Title" مش خيار حقيقي
        self.assertEqual(row["variant_id"], "9")
        self.assertEqual(row["product_url"], "https://shop.example.com/products/shirt")


class ShopifyTokenTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="M", image_quota=1)

    def test_legacy_static_token_is_returned_as_is(self):
        conn = ShopifyConnection(merchant=self.merchant, shop_domain="s.myshopify.com",
                                 access_token="shpat_x", api_secret="sec")
        self.assertEqual(shopify_service.get_access_token(conn), "shpat_x")

    @patch("apitokens.services.shopify.requests.post")
    def test_client_credentials_token_is_fetched_and_cached(self, mock_post):
        mock_post.return_value = MagicMock(ok=True, json=lambda: {"access_token": "tok1", "expires_in": 86399})
        conn = ShopifyConnection.objects.create(
            merchant=self.merchant, shop_domain="s.myshopify.com", client_id="cid", api_secret="sec",
        )
        self.assertEqual(shopify_service.get_access_token(conn), "tok1")
        self.assertEqual(shopify_service.get_access_token(conn), "tok1")
        self.assertEqual(mock_post.call_count, 1)
        conn.refresh_from_db()
        self.assertEqual(conn.access_token, "tok1")

    @patch("apitokens.services.shopify.requests.post")
    def test_expiring_token_is_renewed(self, mock_post):
        mock_post.return_value = MagicMock(ok=True, json=lambda: {"access_token": "tok2", "expires_in": 86399})
        conn = ShopifyConnection.objects.create(
            merchant=self.merchant, shop_domain="s.myshopify.com", client_id="cid", api_secret="sec",
            access_token="old", token_expires_at=timezone.now() + timedelta(minutes=5),
        )
        self.assertEqual(shopify_service.get_access_token(conn), "tok2")

    @patch("apitokens.services.shopify.requests.post")
    def test_other_organization_error_is_explained(self, mock_post):
        mock_post.return_value = MagicMock(ok=False, status_code=400, text='{"error":"shop_not_permitted"}')
        conn = ShopifyConnection(merchant=self.merchant, shop_domain="s.myshopify.com",
                                 client_id="cid", api_secret="sec")
        with self.assertRaisesMessage(shopify_service.ShopifyError, "organization"):
            shopify_service.get_access_token(conn)


class ShopifyWebhookTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="M", image_quota=1)
        ShopifyConnection.objects.create(
            merchant=self.merchant, shop_domain="s.myshopify.com", access_token="t", api_secret="sec",
        )
        self.client = Client()

    def send(self, topic, payload):
        body = json.dumps(payload).encode()
        sig = base64.b64encode(hmac.new(b"sec", body, hashlib.sha256).digest()).decode()
        return self.client.post(
            f"/webhooks/shopify/{self.merchant.token}/", data=body, content_type="application/json",
            HTTP_X_SHOPIFY_TOPIC=topic, HTTP_X_SHOPIFY_HMAC_SHA256=sig,
        )

    def test_order_is_linked_to_its_conversation(self):
        Conversation.objects.create(merchant=self.merchant, session_id="sess-123456")
        resp = self.send("orders/create", {
            "id": 999, "total_price": "120.50", "currency": "USD",
            "note_attributes": [{"name": "_sw_conversation", "value": "sess-123456"}],
        })
        self.assertEqual(resp.status_code, 200)
        convo = Conversation.objects.get(session_id="sess-123456")
        self.assertEqual(convo.order_id, "999")
        self.assertEqual(convo.order_total, Decimal("120.50"))

    def test_order_without_widget_attribute_is_ignored(self):
        Conversation.objects.create(merchant=self.merchant, session_id="sess-123456")
        self.send("orders/create", {"id": 1, "total_price": "10", "note_attributes": []})
        self.assertEqual(Conversation.objects.get(session_id="sess-123456").order_id, "")

    def test_bad_signature_is_rejected(self):
        resp = self.client.post(
            f"/webhooks/shopify/{self.merchant.token}/", data=b"{}", content_type="application/json",
            HTTP_X_SHOPIFY_TOPIC="orders/create", HTTP_X_SHOPIFY_HMAC_SHA256="nope",
        )
        self.assertEqual(resp.status_code, 401)

    def test_product_going_draft_is_removed(self):
        self.send("products/create", rest_product())
        self.assertTrue(Product.objects.filter(merchant=self.merchant, external_id="111").exists())
        self.send("products/update", rest_product(status="draft"))
        self.assertFalse(Product.objects.filter(merchant=self.merchant, external_id="111").exists())


# ======================================================================
# المحادثة: منع تكرار الكرت، تسجيل المحادثة، الأحداث، اختيار الكتالوج، شبكة اللغة
# ======================================================================


class WidgetChatShopifyReadyTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="M", image_quota=1, currency="USD",
                                                store_info="Delivery in 2-4 days.")
        self.dress = Product.objects.create(
            merchant=self.merchant, sku="DRESS-1", title="Black Dress", price="80.00",
            image_url="https://example.com/d.jpg", variant_id="55",
        )
        Product.objects.create(
            merchant=self.merchant, sku="DRESS-OOS", title="Navy Dress", price="80.00",
            image_url="https://example.com/n.jpg", in_stock=False,
        )
        self.client = Client()

    def post(self, messages, **extra):
        return self.client.post(
            "/widget/chat/", data=json.dumps({"messages": messages, **extra}),
            content_type="application/json", HTTP_X_API_TOKEN=self.merchant.token,
        )

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_already_shown_card_is_not_repeated_but_its_text_is_kept(self, mock_provider, mock_chat):
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(tool_calls=[{
            "name": "recommend_product", "input": {"sku": "DRESS-1", "reason": "متوفر بمقاس M بس"},
        }])
        data = self.post([{"role": "user", "content": "في منه XL؟"}], shown_skus=["DRESS-1"]).json()
        self.assertEqual(data["recommendations"], [])
        self.assertEqual(data["reply"], "متوفر بمقاس M بس")

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_shown_card_is_resurfaced_when_customer_wants_to_buy(self, mock_provider, mock_chat):
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(reply_text="Tap the button below", tool_calls=[{
            "name": "recommend_product", "input": {"sku": "DRESS-1", "reason": "here"},
        }])
        data = self.post([{"role": "user", "content": "how do I buy it?"}], shown_skus=["DRESS-1"]).json()
        self.assertEqual(data["recommendations"][0]["sku"], "DRESS-1")
        self.assertEqual(data["recommendations"][0]["variant_id"], "55")
        self.assertEqual(data["recommendations"][0]["currency"], "USD")

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_prompt_has_store_info_and_only_in_stock_products(self, mock_provider, mock_chat):
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(reply_text="hello")
        self.post([{"role": "user", "content": "hi"}])
        system_prompt = mock_chat.call_args[0][1]
        self.assertIn("Delivery in 2-4 days.", system_prompt)
        self.assertIn("DRESS-1", system_prompt)
        self.assertNotIn("DRESS-OOS", system_prompt)
        self.assertIn("Prices are in USD", system_prompt)

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_conversation_is_logged_and_events_counted(self, mock_provider, mock_chat):
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(reply_text="Great pick", tool_calls=[{
            "name": "recommend_product", "input": {"sku": "DRESS-1", "reason": "x"},
        }])
        self.post([{"role": "user", "content": "a black dress please"}], session_id="abc12345-xyz")
        convo = Conversation.objects.get(merchant=self.merchant, session_id="abc12345-xyz")
        self.assertEqual(convo.recommended_skus, ["DRESS-1"])
        self.assertEqual(convo.messages[-1], {"role": "assistant", "content": "Great pick"})

        resp = self.client.post(
            "/widget/event/", data=json.dumps({"type": "add_to_cart", "session_id": "abc12345-xyz"}),
            content_type="application/json", HTTP_X_API_TOKEN=self.merchant.token,
        )
        self.assertEqual(resp.status_code, 200)
        convo.refresh_from_db()
        self.assertEqual(convo.add_to_carts, 1)

    @patch("apitokens.widget_views._save_conversation")
    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_failed_log_write_still_returns_the_reply(self, mock_provider, mock_chat, mock_save):
        from django.db import DatabaseError

        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(reply_text="Hello!")
        mock_save.side_effect = DatabaseError("database is locked")
        resp = self.post([{"role": "user", "content": "hi"}], session_id="abc12345-xyz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["reply"], "Hello!")

    def test_unknown_event_is_rejected(self):
        resp = self.client.post(
            "/widget/event/", data=json.dumps({"type": "hack", "session_id": "abc12345-xyz"}),
            content_type="application/json", HTTP_X_API_TOKEN=self.merchant.token,
        )
        self.assertEqual(resp.status_code, 400)

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_wrong_language_reply_is_retried(self, mock_provider, mock_chat):
        mock_provider.return_value = FakeProvider()
        mock_chat.side_effect = [fake_result(reply_text="أهلاً وسهلاً"), fake_result(reply_text="Welcome!")]
        data = self.post([{"role": "user", "content": "hello there"}]).json()
        self.assertEqual(data["reply"], "Welcome!")

    def test_arabizi_counts_as_arabic(self):
        from .widget_views import _is_arabic_text, _script_mismatch

        self.assertTrue(_is_arabic_text("marhaba, baddi fostan a7mar"))
        self.assertTrue(_is_arabic_text("3ndkon shi kamees?"))
        self.assertFalse(_is_arabic_text("hi, I need a summer dress"))
        self.assertFalse(_is_arabic_text("Bonjour, je cherche une robe"))
        self.assertFalse(_is_arabic_text("this is my 2nd order, delivered by 5pm?"))
        self.assertFalse(_script_mismatch("baddi fostan a7mar", "هالفستان الأحمر بيجنن"))
        self.assertTrue(_script_mismatch("I need a dress", "هالفستان بيجنن"))

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_discount_pitch_is_used_when_model_writes_no_text(self, mock_provider, mock_chat):
        from .models import DiscountCode

        DiscountCode.objects.create(merchant=self.merchant, code="SAVE10", description="10% off")
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(tool_calls=[{
            "name": "offer_discount_code", "input": {"code": "SAVE10", "pitch": "معك حق، بس هالقماش بيعيش معك سنين — وخود SAVE10"},
        }])
        data = self.post([{"role": "user", "content": "غالي شوي"}], shown_skus=["DRESS-1"]).json()
        self.assertEqual(data["discount_offer"]["code"], "SAVE10")
        self.assertIn("بيعيش معك سنين", data["reply"])


class TryOnSupportTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="T", image_quota=1)

    def make(self, title, category="", tags=""):
        return Product(merchant=self.merchant, sku="X", title=title, category=category, tags=tags,
                       price="1", image_url="https://e.com/x.jpg")

    def test_wearables_support_tryon(self):
        for title in ["Black Satin Evening Gown", "Men Check Shirt", "Gold Clutch", "فستان سهرة",
                      "Winter Jacket", "Running Sneakers", "Corset With Black Skirt"]:
            self.assertTrue(self.make(title).supports_tryon, title)

    def test_gear_and_gift_cards_do_not(self):
        for title, category, tags in [("The Complete Snowboard", "snowboard", "Premium, Snow"),
                                      ("Selling Plans Ski Wax", "accessories", "Accessory, Sport"),
                                      ("Gift Card", "giftcard", ""), ("Laptop stand", "", "")]:
            self.assertFalse(self.make(title, category, tags).supports_tryon, title)

    def test_catalog_marks_non_wearables(self):
        rows = catalog_service.catalog_for_prompt([self.make("The Complete Snowboard"), self.make("Red Dress")])
        self.assertEqual(rows[0].get("try_on"), False)
        self.assertNotIn("try_on", rows[1])


class CatalogSelectionTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="Big", image_quota=1)
        for i in range(catalog_service.COMPACT_LIMIT + 10):
            Product.objects.create(
                merchant=self.merchant, sku=f"P{i}", title=f"Product {i}", price="10",
                image_url="https://example.com/x.jpg",
            )

    def test_medium_catalog_is_compact_except_shown(self):
        products = list(self.merchant.products.filter(sku__in=[f"P{i}" for i in range(100)]))
        for p in products:
            p.description = "long description"
        rows = catalog_service.catalog_for_prompt(products, shown_skus=["P5"])
        with_desc = [r["sku"] for r in rows if "description" in r]
        self.assertEqual(with_desc, ["P5"])

    def test_small_catalog_sends_everything(self):
        small = ApiToken.objects.create(label="Small", image_quota=1)
        Product.objects.create(merchant=small, sku="A", title="A", price="1", image_url="https://e.com/a.jpg")
        self.assertEqual([p.sku for p in catalog_service.select_products(small, [])], ["A"])

    @patch("apitokens.services.catalog._embed")
    def test_large_catalog_ranks_by_meaning_and_keeps_shown(self, mock_embed):
        def fake_embed(texts):
            # منتج P7 هو الأقرب للطلب، وكل الباقي بعيد.
            return [[1.0, 0.0] if ("Product 7 " in t + " " or t.startswith("فستان")) else [0.0, 1.0] for t in texts]

        mock_embed.side_effect = fake_embed
        with self.settings(OPENAI_API_KEY="test"):
            picked = catalog_service.select_products(
                self.merchant, [{"role": "user", "content": "فستان أسود"}], shown_skus=["P3"],
            )
        skus = [p.sku for p in picked]
        self.assertEqual(skus[0], "P3")  # المعروض سابقاً دايماً موجود
        self.assertEqual(skus[1], "P7")  # بعده الأقرب دلالياً
        self.assertEqual(len(skus), 1 + catalog_service.SELECTED_CATALOG_SIZE)

    def test_large_catalog_without_embeddings_still_answers(self):
        with self.settings(OPENAI_API_KEY=""):
            picked = catalog_service.select_products(self.merchant, [{"role": "user", "content": "x"}])
        self.assertEqual(len(picked), catalog_service.SELECTED_CATALOG_SIZE)


class PageProductTests(TestCase):
    """الودجت بيبعت رابط الصفحة: لو صفحة منتج من الكتالوج منرجّع كرته."""

    def setUp(self):
        self.merchant = ApiToken.objects.create(label="Page", token="PAGETOKEN0001", image_quota=5, currency="SAR")
        self.dress = Product.objects.create(
            merchant=self.merchant, sku="DRESS-GREEN", title="Green Midi Dress", price="199.00",
            image_url="https://example.com/d.jpg", category="dresses",
            product_url="https://shop.example.com/products/green-midi-dress",
        )
        Product.objects.create(
            merchant=self.merchant, sku="DRESS-GREEN-2", title="Green Midi Dress Long", price="250.00",
            image_url="https://example.com/d2.jpg", category="dresses",
            product_url="https://shop.example.com/products/green-midi-dress-long",
        )
        Product.objects.create(
            merchant=self.merchant, sku="ABAYA-1", title="Black Abaya", price="300.00",
            image_url="https://example.com/a.jpg", category="abayas",
            product_url="https://salla.example.com/black-abaya/p123456",
        )
        Product.objects.create(
            merchant=self.merchant, sku="GONE", title="Sold Out Dress", price="99.00",
            image_url="https://example.com/g.jpg", category="dresses", in_stock=False,
            product_url="https://shop.example.com/products/sold-out-dress",
        )
        other = ApiToken.objects.create(label="Other", token="OTHERTOKEN001", image_quota=5)
        Product.objects.create(
            merchant=other, sku="OTHER", title="Other Store Dress", price="10.00",
            image_url="https://example.com/o.jpg", product_url="https://other.example.com/products/other-dress",
        )
        self.client = Client()

    def lookup(self, page_url, token="PAGETOKEN0001"):
        return self.client.post(
            "/widget/page-product/", data=json.dumps({"page_url": page_url}),
            content_type="application/json", HTTP_X_API_TOKEN=token,
        ).json()

    def test_exact_product_url(self):
        data = self.lookup("https://shop.example.com/products/green-midi-dress")
        self.assertEqual(data["product"]["sku"], "DRESS-GREEN")
        self.assertEqual(data["product"]["product_url"], self.dress.product_url)
        self.assertEqual(data["product"]["currency"], "SAR")
        self.assertTrue(data["product"]["tryon_supported"])

    def test_shopify_variants_of_same_page(self):
        for url in (
            "https://my-store.myshopify.com/products/green-midi-dress?variant=123#reviews",
            "https://shop.example.com/collections/summer/products/green-midi-dress",
            "https://shop.example.com/ar/products/green-midi-dress/",
            "https://SHOP.example.com/products/Green-Midi-Dress",
        ):
            self.assertEqual(self.lookup(url)["product"]["sku"], "DRESS-GREEN", url)

    def test_handle_prefix_does_not_match_longer_handle(self):
        data = self.lookup("https://shop.example.com/products/green-midi-dress-long")
        self.assertEqual(data["product"]["sku"], "DRESS-GREEN-2")

    def test_non_shopify_path_match(self):
        data = self.lookup("https://salla.example.com/black-abaya/p123456?utm_source=ig")
        self.assertEqual(data["product"]["sku"], "ABAYA-1")

    def test_non_product_pages_return_null(self):
        for url in ("https://shop.example.com/", "https://shop.example.com/collections/summer",
                    "https://shop.example.com/products/unknown-item", "", "not a url"):
            data = self.lookup(url)
            self.assertTrue(data["ok"])
            self.assertIsNone(data["product"], url)

    def test_out_of_stock_product_not_offered(self):
        self.assertIsNone(self.lookup("https://shop.example.com/products/sold-out-dress")["product"])

    def test_other_merchants_products_never_match(self):
        self.assertIsNone(self.lookup("https://other.example.com/products/other-dress")["product"])

    def test_requires_valid_token(self):
        resp = self.client.post("/widget/page-product/", data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 401)


class PageProductChatTests(TestCase):
    """المساعد بيعرف المنتج يلي الزبون فاتح صفحته، وما بيعيد كرته لما ما يعجبه."""

    def setUp(self):
        self.merchant = ApiToken.objects.create(label="PageChat", token="PAGECHAT00001", image_quota=5)
        Product.objects.create(
            merchant=self.merchant, sku="DRESS-GREEN", title="Green Midi Dress", price="199.00",
            image_url="https://example.com/d.jpg", category="dresses",
            product_url="https://shop.example.com/products/green-midi-dress",
        )
        Product.objects.create(
            merchant=self.merchant, sku="DRESS-NAVY", title="Navy Midi Dress", price="189.00",
            image_url="https://example.com/n.jpg", category="dresses",
            product_url="https://shop.example.com/products/navy-midi-dress",
        )
        self.client = Client()

    def post(self, text, page_url=None, shown_skus=()):
        body = {"messages": [{"role": "user", "content": text}], "shown_skus": list(shown_skus)}
        if page_url is not None:
            body["page_url"] = page_url
        return self.client.post(
            "/widget/chat/", data=json.dumps(body), content_type="application/json",
            HTTP_X_API_TOKEN=self.merchant.token,
        ).json()

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_prompt_names_the_page_product(self, mock_provider, mock_chat):
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(reply_text="أكيد")
        self.post("كم سعره؟", page_url="https://shop.example.com/products/green-midi-dress?variant=1")
        system_prompt = mock_chat.call_args[0][1]
        self.assertIn("product page of DRESS-GREEN", system_prompt)
        self.assertIn("Don't recommend DRESS-GREEN again", system_prompt)

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_no_page_section_off_product_pages(self, mock_provider, mock_chat):
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(reply_text="أهلين")
        self.post("هلا", page_url="https://shop.example.com/")
        self.post("هلا")
        for call in mock_chat.call_args_list:
            self.assertNotIn("product page of", call[0][1])

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_disliked_page_product_not_reshown_alternative_is(self, mock_provider, mock_chat):
        """الويدجت بيحط كرت منتج الصفحة بـ shown_skus — لو الموديل رجّعه مع بديل،
        بيطلع البديل بس."""
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(
            reply_text="ولا يهمك، شوف هذا",
            tool_calls=[
                {"name": "recommend_product", "input": {"sku": "DRESS-GREEN", "reason": "x"}},
                {"name": "recommend_product", "input": {"sku": "DRESS-NAVY", "reason": "لون أهدأ"}},
            ],
        )
        data = self.post(
            "ما عجبني لونه", page_url="https://shop.example.com/products/green-midi-dress",
            shown_skus=["DRESS-GREEN"],
        )
        self.assertEqual([r["sku"] for r in data["recommendations"]], ["DRESS-NAVY"])


class SaudiPhrasingTests(TestCase):
    """الزبون السعودي بيعبّر عن نية الشراء والميزانية بكلمات غير الشامي."""

    def test_saudi_purchase_intent(self):
        from .widget_views import _has_purchase_intent
        for text in ("أبيه", "ابغاه الحين", "أبي أشتريه", "كيف أشتريه؟"):
            self.assertTrue(_has_purchase_intent(text), text)
        self.assertFalse(_has_purchase_intent("أبي فستان أبيض"))

    def test_saudi_budget(self):
        from .widget_views import _extract_budget_ceiling
        for text in ("أبي فستان في حدود 300", "ميزانيتي 250", "ما ابي ادفع اكثر من 400", "مو اكثر من 150"):
            self.assertIsNotNone(_extract_budget_ceiling([{"role": "user", "content": text}]), text)


class ToolOnlyReplyLanguageTests(TestCase):
    """الموديل رجّع كرت بلا نص و reason بلغة غلط: شبكة اللغة لازم تلقطه."""

    def setUp(self):
        self.merchant = ApiToken.objects.create(label="Lang", token="LANGTOKEN0001", image_quota=5)
        Product.objects.create(
            merchant=self.merchant, sku="SHIRT-WHITE", title="White Linen Shirt", price="45.00",
            image_url="https://example.com/s.jpg", category="shirts",
        )

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_wrong_language_reason_is_corrected(self, mock_provider, mock_chat):
        mock_provider.return_value = FakeProvider()
        card = lambda reason: fake_result(
            tool_calls=[{"name": "recommend_product", "input": {"sku": "SHIRT-WHITE", "reason": reason}}]
        )
        mock_chat.side_effect = [card("اضغط زر الشراء تحت"), card("Tap the buy button below")]
        data = Client().post(
            "/widget/chat/", data=json.dumps({"messages": [{"role": "user", "content": "which shirt would you pick for me?"}]}),
            content_type="application/json", HTTP_X_API_TOKEN=self.merchant.token,
        ).json()
        self.assertEqual(data["reply"], "Tap the buy button below")
        self.assertEqual(data["recommendations"][0]["reason"], "Tap the buy button below")


class PurchaseReplyTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="Buy", token="BUYTOKEN00001", image_quota=5)
        Product.objects.create(
            merchant=self.merchant, sku="SHIRT-WHITE", title="White Linen Shirt", price="45.00",
            image_url="https://example.com/s.jpg", category="shirts", product_url="https://s.example.com/products/w",
        )

    @patch("apitokens.widget_views.llm.chat_with_retry")
    @patch("apitokens.widget_views.llm.get_provider")
    def test_tool_only_buy_reply_uses_fixed_sentence_in_customer_language(self, mock_provider, mock_chat):
        mock_provider.return_value = FakeProvider()
        mock_chat.return_value = fake_result(
            tool_calls=[{"name": "recommend_product", "input": {"sku": "SHIRT-WHITE", "reason": "Pulsa el botón"}}]
        )
        for text, expected in (("great, how do I buy it?", "buy button"), ("زين أبيه، كيف أطلبه؟", "زر الشراء")):
            data = Client().post(
                "/widget/chat/", data=json.dumps({"messages": [{"role": "user", "content": text}]}),
                content_type="application/json", HTTP_X_API_TOKEN=self.merchant.token,
            ).json()
            self.assertIn(expected, data["reply"])
            self.assertEqual(data["recommendations"][0]["product_url"], "https://s.example.com/products/w")


class TryonLabelLanguageTests(TestCase):
    def test_label_in_wrong_script_falls_back_to_customer_language(self):
        from .widget_views import _tryon_label
        self.assertEqual(_tryon_label("جرّبه بصورتك", "en", False), "Try it on with your photo 📸")
        self.assertEqual(_tryon_label("Try it", "ar", True), "جرّبه على صاحب الهدية 📸")
        self.assertEqual(_tryon_label("جرّبه على أختك 📸", "ar", True), "جرّبه على أختك 📸")
        self.assertEqual(_tryon_label("", "en", False), "Try it on with your photo 📸")


class SallaPageProductTests(TestCase):
    """روابط سلة: /{lang}/{اسم}/p{رقم} — منطابق على الرقم."""

    def setUp(self):
        self.merchant = ApiToken.objects.create(label="Salla", token="SALLATOKEN001", image_quota=5)
        Product.objects.create(
            merchant=self.merchant, sku="ABAYA-1", title="عباية سوداء", price="300.00",
            image_url="https://example.com/a.jpg", category="abayas",
            product_url="https://salla.example.com/black-abaya/p123456",
        )
        Product.objects.create(
            merchant=self.merchant, sku="ABAYA-2", title="عباية بيج", price="320.00",
            image_url="https://example.com/b.jpg", category="abayas",
            product_url="https://salla.example.com/beige-abaya/p1234567",
        )
        Product.objects.create(
            merchant=self.merchant, sku="DRESS-9", external_id="987654", title="فستان", price="200.00",
            image_url="https://example.com/c.jpg", category="dresses", product_url="",
        )
        Product.objects.create(
            merchant=self.merchant, sku="DRESS-10", external_id="555", title="فستان ثاني", price="200.00",
            image_url="https://example.com/d.jpg", category="dresses",
            product_url="https://salla.example.com/other/p555",
        )

    def lookup(self, page_url):
        return self.client.post(
            "/widget/page-product/", data=json.dumps({"page_url": page_url}),
            content_type="application/json", HTTP_X_API_TOKEN="SALLATOKEN001",
        ).json()["product"]

    def test_same_product_any_language_or_slug(self):
        for url in (
            "https://salla.example.com/black-abaya/p123456",
            "https://salla.example.com/ar/black-abaya/p123456",
            "https://salla.example.com/en/black-abaya/p123456?utm_source=snap",
            "https://salla.example.com/ar/%D8%B9%D8%A8%D8%A7%D9%8A%D8%A9-%D8%B3%D9%88%D8%AF%D8%A7%D8%A1/p123456",
            "https://my-store.salla.sa/ar/black-abaya/p123456/",
        ):
            self.assertEqual(self.lookup(url)["sku"], "ABAYA-1", url)

    def test_id_prefix_does_not_match_longer_id(self):
        self.assertEqual(self.lookup("https://salla.example.com/ar/x/p1234567")["sku"], "ABAYA-2")
        self.assertIsNone(self.lookup("https://salla.example.com/ar/x/p12345"))

    def test_falls_back_to_external_id(self):
        self.assertEqual(self.lookup("https://salla.example.com/ar/dress/p987654")["sku"], "DRESS-9")

    def test_non_product_salla_pages(self):
        for url in ("https://salla.example.com/ar", "https://salla.example.com/ar/category/c12345",
                    "https://salla.example.com/ar/x/p999"):
            self.assertIsNone(self.lookup(url), url)


class SallaAuthTests(TestCase):
    """تركيب تطبيق سلة: حساب بينعمل لحاله، والتجديد آمن (refresh بينستخدم مرة وحدة)."""

    def authorize(self, merchant_id="1234509876", access="AT1", refresh="RT1", expires=None):
        from datetime import timedelta
        from django.utils import timezone
        from .services import salla
        expires = expires or int((timezone.now() + timedelta(days=14)).timestamp())
        with patch("apitokens.services.salla.requests.get") as mock_get:
            mock_get.return_value.json.return_value = {
                "merchant": {"id": int(merchant_id), "name": "متجر الأناقة", "domain": "https://elegance.salla.sa"}
            }
            mock_get.return_value.raise_for_status.return_value = None
            return salla.save_authorization(
                merchant_id, {"access_token": access, "refresh_token": refresh, "expires": expires, "scope": "offline_access products.read"},
            )

    def test_install_creates_merchant_and_connection(self):
        conn = self.authorize()
        self.assertEqual(conn.merchant.label, "متجر الأناقة")
        self.assertEqual(conn.store_domain, "https://elegance.salla.sa")
        self.assertEqual(conn.merchant.currency, "SAR")
        self.assertTrue(conn.merchant.token)
        self.assertEqual(conn.access_token, "AT1")

    def test_reauthorize_updates_same_merchant(self):
        first = self.authorize()
        second = self.authorize(access="AT2", refresh="RT2")
        self.assertEqual(first.merchant_id, second.merchant_id)
        self.assertEqual(ApiToken.objects.filter(salla_connection__isnull=False).count(), 1)
        second.refresh_from_db()
        self.assertEqual((second.access_token, second.refresh_token), ("AT2", "RT2"))

    def test_uninstall_deactivates_widget(self):
        from .services import salla
        conn = self.authorize()
        salla.deactivate("1234509876")
        conn.refresh_from_db()
        conn.merchant.refresh_from_db()
        self.assertFalse(conn.is_active)
        self.assertFalse(conn.merchant.is_active)
        self.assertEqual(conn.refresh_token, "")
        again = self.authorize(access="AT3", refresh="RT3")
        again.merchant.refresh_from_db()
        self.assertTrue(again.merchant.is_active)

    def test_valid_token_is_not_refreshed(self):
        from .services import salla
        conn = self.authorize()
        with patch("apitokens.services.salla.requests.post") as mock_post:
            self.assertEqual(salla.get_access_token(conn), "AT1")
            mock_post.assert_not_called()

    def test_expiring_token_refreshes_and_saves_both_tokens(self):
        from datetime import timedelta
        from django.utils import timezone
        from .services import salla
        conn = self.authorize(expires=int((timezone.now() + timedelta(hours=2)).timestamp()))
        new_expiry = int((timezone.now() + timedelta(days=14)).timestamp())
        with patch("apitokens.services.salla.requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = {"access_token": "AT2", "refresh_token": "RT2", "expires": new_expiry}
            self.assertEqual(salla.get_access_token(conn), "AT2")
            self.assertEqual(mock_post.call_args.kwargs["data"]["refresh_token"], "RT1")
        conn.refresh_from_db()
        self.assertEqual((conn.access_token, conn.refresh_token), ("AT2", "RT2"))

    def test_stale_copy_reuses_token_refreshed_by_another_process(self):
        """عمليتين معهم نسخة قديمة: التانية لازم ما تحرق الـ refresh من جديد."""
        from datetime import timedelta
        from django.utils import timezone
        from .services import salla
        conn = self.authorize(expires=int((timezone.now() + timedelta(hours=2)).timestamp()))
        stale_copy = SallaConnection.objects.get(pk=conn.pk)
        with patch("apitokens.services.salla.requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = {
                "access_token": "AT2", "refresh_token": "RT2",
                "expires": int((timezone.now() + timedelta(days=14)).timestamp()),
            }
            salla.get_access_token(conn)
            self.assertEqual(salla.get_access_token(stale_copy), "AT2")
            self.assertEqual(mock_post.call_count, 1)

    def test_api_get_retries_once_after_401(self):
        from .services import salla
        conn = self.authorize()
        unauthorized = type("R", (), {"status_code": 401, "ok": False, "text": ""})()
        ok = type("R", (), {"status_code": 200, "ok": True, "json": lambda self: {"data": []}})()
        with patch("apitokens.services.salla.requests.get", side_effect=[unauthorized, ok]), \
                patch("apitokens.services.salla.requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = {"access_token": "AT2", "refresh_token": "RT2", "expires": 4102444800}
            self.assertEqual(salla.api_get(conn, "/products"), {"data": []})
            self.assertEqual(mock_post.call_count, 1)


from django.test import override_settings  # noqa: E402


@override_settings(SALLA_WEBHOOK_SECRET="whsec_test")
class SallaWebhookTests(TestCase):
    PRODUCT = {
        "id": 123456, "name": "عباية سوداء مطرزة", "sku": "AB-1", "description": "<p>قماش <b>كريب</b> فاخر</p>",
        "price": {"amount": 250, "currency": "SAR"}, "regular_price": {"amount": 300, "currency": "SAR"},
        "sale_price": {"amount": 250, "currency": "SAR"}, "status": "sale", "quantity": 4,
        "images": [{"url": "https://cdn.salla.sa/a2.jpg", "main": False}, {"url": "https://cdn.salla.sa/a1.jpg", "main": True}],
        "urls": {"customer": "https://elegance.salla.sa/black-abaya/p123456"},
        "categories": [{"name": "عبايات"}], "tags": [{"name": "سهرة"}],
        "options": [{"name": "المقاس", "values": [{"name": "S"}, {"name": "M"}]}],
    }

    def setUp(self):
        self.merchant = ApiToken.objects.create(label="متجر", image_quota=5)
        self.connection = SallaConnection.objects.create(
            merchant=self.merchant, salla_merchant_id="777", access_token="AT", refresh_token="RT",
        )

    def send(self, payload, signature=None, raw=None):
        import hashlib
        import hmac
        body = raw if raw is not None else json.dumps(payload).encode()
        if signature is None:
            signature = hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()
        return self.client.post(
            "/webhooks/salla/", data=body, content_type="application/json",
            HTTP_X_SALLA_SIGNATURE=signature,
        )

    def test_bad_signature_rejected(self):
        resp = self.send({"event": "product.created", "merchant": 777, "data": self.PRODUCT}, signature="00ff")
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(Product.objects.exists())

    def test_token_strategy_accepted(self):
        resp = self.client.post(
            "/webhooks/salla/", data=json.dumps({"event": "product.created", "merchant": 777, "data": self.PRODUCT}),
            content_type="application/json", HTTP_AUTHORIZATION="whsec_test",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Product.objects.filter(external_id="123456").exists())

    @override_settings(SALLA_WEBHOOK_SECRET="")
    def test_unconfigured_secret_refuses_everything(self):
        self.assertEqual(self.send({"event": "product.created"}, signature="x").status_code, 503)

    def test_product_created_maps_fields(self):
        self.assertEqual(self.send({"event": "product.created", "merchant": 777, "data": self.PRODUCT}).status_code, 200)
        p = Product.objects.get(merchant=self.merchant, external_id="123456")
        self.assertEqual((p.sku, p.title), ("AB-1", "عباية سوداء مطرزة"))
        self.assertEqual((str(p.price), str(p.compare_at_price)), ("250.00", "300.00"))
        self.assertEqual(p.image_url, "https://cdn.salla.sa/a1.jpg")
        self.assertEqual(p.product_url, "https://elegance.salla.sa/black-abaya/p123456")
        self.assertEqual(p.options, {"المقاس": ["S", "M"]})
        self.assertEqual((p.category, p.tags), ("عبايات", "سهرة"))
        self.assertNotIn("<", p.description)
        self.assertTrue(p.in_stock)

    def test_product_updated_twice_is_idempotent(self):
        payload = {"event": "product.updated", "merchant": 777, "data": dict(self.PRODUCT, quantity=0)}
        self.send(payload)
        self.send(payload)
        self.assertEqual(Product.objects.filter(external_id="123456").count(), 1)
        self.assertFalse(Product.objects.get(external_id="123456").in_stock)

    def test_plain_number_prices_and_no_sale(self):
        data = dict(self.PRODUCT, price=199, regular_price=199, sale_price=0, sku="")
        self.send({"event": "product.created", "merchant": 777, "data": data})
        p = Product.objects.get(external_id="123456")
        self.assertEqual((str(p.price), p.compare_at_price, p.sku), ("199.00", None, "SALLA-123456"))

    def test_hidden_product_removed(self):
        self.send({"event": "product.created", "merchant": 777, "data": self.PRODUCT})
        self.send({"event": "product.updated", "merchant": 777, "data": dict(self.PRODUCT, status="hidden")})
        self.assertFalse(Product.objects.filter(external_id="123456").exists())

    def test_product_deleted(self):
        self.send({"event": "product.created", "merchant": 777, "data": self.PRODUCT})
        self.send({"event": "product.deleted", "merchant": 777, "data": {"id": 123456}})
        self.assertFalse(Product.objects.filter(external_id="123456").exists())

    def test_unknown_or_uninstalled_store_ignored_with_200(self):
        self.assertEqual(self.send({"event": "product.created", "merchant": 999, "data": self.PRODUCT}).status_code, 200)
        self.assertFalse(Product.objects.exists())

    def test_authorize_and_uninstall_events(self):
        with patch("apitokens.services.salla.requests.get") as mock_get, \
                patch("apitokens.salla_webhooks.start_initial_sync"):
            mock_get.return_value.json.return_value = {"merchant": {"name": "متجر جديد", "domain": "https://new.salla.sa"}}
            resp = self.send({"event": "app.store.authorize", "merchant": 555, "data": {
                "access_token": "A", "refresh_token": "R", "expires": 4102444800, "scope": "products.read"}})
        self.assertEqual(resp.status_code, 200)
        conn = SallaConnection.objects.get(salla_merchant_id="555")
        self.assertEqual(conn.merchant.label, "متجر جديد")
        self.send({"event": "app.uninstalled", "merchant": 555, "data": {}})
        conn.refresh_from_db()
        self.assertFalse(conn.is_active)


class SallaSyncTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="متجر", image_quota=5)
        self.connection = SallaConnection.objects.create(
            merchant=self.merchant, salla_merchant_id="888", access_token="AT", refresh_token="RT",
        )
        from datetime import datetime, timezone as dt_timezone
        self.connection.token_expires_at = datetime(2099, 1, 1, tzinfo=dt_timezone.utc)
        self.connection.save()

    @staticmethod
    def product(pid, **extra):
        return dict({"id": pid, "name": f"منتج {pid}", "price": 100, "status": "sale",
                     "images": [{"url": f"https://cdn.salla.sa/{pid}.jpg", "main": True}],
                     "urls": {"customer": f"https://s.salla.sa/x/p{pid}"}}, **extra)

    def pages(self, *pages):
        responses = []
        for i, items in enumerate(pages, start=1):
            r = type("R", (), {"status_code": 200, "ok": True})()
            body = {"data": items, "pagination": {"currentPage": i, "totalPages": len(pages)}}
            r.json = (lambda b: (lambda: b))(body)
            responses.append(r)
        return responses

    @patch("apitokens.services.catalog.ensure_embeddings")
    def test_full_sync_walks_all_pages_and_removes_deleted(self, _emb):
        from .services import catalog
        Product.objects.create(merchant=self.merchant, external_id="old", sku="OLD", title="قديم",
                               price="1", image_url="https://x/o.jpg")
        Product.objects.create(merchant=self.merchant, sku="MANUAL", title="يدوي", price="1",
                               image_url="https://x/m.jpg")
        responses = self.pages([self.product(1), self.product(2)], [self.product(3), self.product(4, status="hidden")])
        with patch("apitokens.services.salla.requests.get", side_effect=responses) as mock_get:
            count = catalog.sync_salla(self.merchant, self.connection)
        self.assertEqual(count, 3)
        self.assertEqual([c.kwargs["params"]["page"] for c in mock_get.call_args_list], [1, 2])
        self.assertEqual(
            sorted(self.merchant.products.values_list("external_id", flat=True)), ["", "1", "2", "3"],
        )
        self.connection.refresh_from_db()
        self.assertIsNotNone(self.connection.last_synced_at)

    @patch("apitokens.services.salla.time.sleep")
    @patch("apitokens.services.catalog.ensure_embeddings")
    def test_rate_limit_waits_and_retries(self, _emb, mock_sleep):
        from .services import catalog
        limited = type("R", (), {"status_code": 429, "ok": False, "text": "Too Many Requests"})()
        with patch("apitokens.services.salla.requests.get", side_effect=[limited] + self.pages([self.product(1)])):
            self.assertEqual(catalog.sync_salla(self.merchant, self.connection), 1)
        mock_sleep.assert_called_once()

    @override_settings(SALLA_WEBHOOK_SECRET="whsec_test")
    def test_install_starts_initial_sync(self):
        import hashlib
        import hmac
        body = json.dumps({"event": "app.store.authorize", "merchant": 4321, "data": {
            "access_token": "A", "refresh_token": "R", "expires": 4102444800}}).encode()
        with patch("apitokens.services.salla.requests.get") as mock_get, \
                patch("apitokens.salla_webhooks.start_initial_sync") as mock_start:
            mock_get.return_value.json.return_value = {"merchant": {"name": "متجر", "domain": ""}}
            self.client.post("/webhooks/salla/", data=body, content_type="application/json",
                             HTTP_X_SALLA_SIGNATURE=hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest())
        mock_start.assert_called_once_with(SallaConnection.objects.get(salla_merchant_id="4321").pk)


@override_settings(SALLA_WEBHOOK_SECRET="whsec_test")
class SallaWidgetTests(TestCase):
    def setUp(self):
        from .models import Conversation
        self.merchant = ApiToken.objects.create(label="متجر", image_quota=5)
        self.connection = SallaConnection.objects.create(merchant=self.merchant, salla_merchant_id="2468")
        self.conversation = Conversation.objects.create(merchant=self.merchant, session_id="sess-abcdef12")

    def test_store_key_for_connected_store_only(self):
        data = self.client.get("/widget/salla-key/2468/").json()
        self.assertEqual(data["store_key"], self.merchant.token)
        self.assertEqual(self.client.get("/widget/salla-key/9999/").status_code, 404)
        self.connection.is_active = False
        self.connection.save()
        self.assertEqual(self.client.get("/widget/salla-key/2468/").status_code, 404)

    def test_card_carries_platform_product_id_and_options_flag(self):
        Product.objects.create(
            merchant=self.merchant, sku="A", external_id="123", title="عباية", price="10",
            image_url="https://x/a.jpg", product_url="https://s.salla.sa/x/p123", options={"المقاس": ["S"]},
        )
        product = self.client.post(
            "/widget/page-product/", data=json.dumps({"page_url": "https://s.salla.sa/ar/x/p123"}),
            content_type="application/json", HTTP_X_API_TOKEN=self.merchant.token,
        ).json()["product"]
        self.assertEqual((product["product_id"], product["has_options"]), ("123", True))

    def order(self, data):
        import hashlib
        import hmac
        body = json.dumps({"event": "order.created", "merchant": 2468, "data": data}).encode()
        return self.client.post(
            "/webhooks/salla/", data=body, content_type="application/json",
            HTTP_X_SALLA_SIGNATURE=hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest(),
        )

    def test_add_to_cart_event_links_cart_then_order(self):
        self.client.post(
            "/widget/event/", data=json.dumps({"type": "add_to_cart", "session_id": "sess-abcdef12", "cart_id": "C-77"}),
            content_type="application/json", HTTP_X_API_TOKEN=self.merchant.token,
        )
        self.conversation.refresh_from_db()
        self.assertEqual((self.conversation.platform_cart_id, self.conversation.add_to_carts), ("C-77", 1))

        self.order({"id": 5551, "cart": {"id": "C-77"}, "amounts": {"total": {"amount": 300, "currency": "SAR"}}})
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.order_id, "5551")
        self.assertEqual((str(self.conversation.order_total), self.conversation.order_currency), ("300.00", "SAR"))

    def test_order_from_unrelated_cart_ignored(self):
        self.assertEqual(self.order({"id": 1, "cart_id": "OTHER"}).status_code, 200)
        self.assertEqual(self.order({"id": 2}).status_code, 200)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.order_id, "")


class SallaDashboardTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user("staff", password="x", is_staff=True)
        self.client.force_login(user)
        self.merchant = ApiToken.objects.create(label="متجر الأناقة", image_quota=5)
        self.connection = SallaConnection.objects.create(
            merchant=self.merchant, salla_merchant_id="13579", store_name="متجر الأناقة",
            access_token="AT", refresh_token="RT",
        )

    @override_settings(SALLA_CLIENT_ID="cid", SALLA_WEBHOOK_SECRET="")
    def test_page_lists_stores_config_and_snippet(self):
        html = self.client.get("/salla/").content.decode()
        self.assertIn("13579", html)
        self.assertIn("/webhooks/salla/", html)
        self.assertIn("salla.config.get", html)
        self.assertIn("SALLA_WEBHOOK_SECRET", html)

    def test_dashboard_marks_salla_stores(self):
        html = self.client.get("/").content.decode()
        self.assertIn("/salla/", html)

    @patch("apitokens.views.catalog_service.sync_salla", return_value=12)
    def test_manual_resync(self, mock_sync):
        resp = self.client.post("/salla/", {"action": "resync", "connection_id": self.connection.pk})
        self.assertEqual(resp.status_code, 302)
        mock_sync.assert_called_once()

    def test_requires_staff(self):
        self.client.logout()
        self.assertEqual(self.client.get("/salla/").status_code, 302)


@override_settings(ZID_CLIENT_ID="zid-client", ZID_CLIENT_SECRET="zid-secret", SITE_BASE_URL="https://control.example.com")
class ZidInstallTests(TestCase):
    TOKENS = {"token_type": "Bearer", "expires_in": 94608000, "authorization": "AUTH1",
              "access_token": "MGR1", "refresh_token": "REF1"}
    STORE = {"status": "object", "store": {"id": 4242, "uuid": "3dfc16e6-ad19", "title": "بوتيك الورد",
                                           "url": "https://ward.zid.store/"}}

    def start(self):
        resp = self.client.get("/zid/install/")
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(resp["Location"]).query)
        return resp, query

    def finish(self, state, code="CODE1"):
        with patch("apitokens.services.zid.requests.post") as mock_post, \
                patch("apitokens.services.zid.requests.get") as mock_get, \
                patch("apitokens.zid_webhooks.start_initial_sync") as mock_sync:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = dict(self.TOKENS)
            mock_get.return_value.json.return_value = self.STORE
            mock_get.return_value.raise_for_status.return_value = None
            resp = self.client.get("/zid/callback/", {"code": code, "state": state})
        return resp, mock_post, mock_get, mock_sync

    def test_install_redirects_straight_to_zid_oauth(self):
        resp, query = self.start()
        self.assertTrue(resp["Location"].startswith("https://oauth.zid.sa/oauth/authorize?"))
        self.assertEqual(query["client_id"], ["zid-client"])
        self.assertEqual(query["redirect_uri"], ["https://control.example.com/zid/callback/"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertTrue(query["state"][0])

    def test_callback_creates_merchant_and_connection(self):
        _, query = self.start()
        resp, mock_post, mock_get, mock_sync = self.finish(query["state"][0])
        self.assertRedirects(resp, "/zid/app/", fetch_redirect_response=False)
        data = mock_post.call_args.kwargs["data"]
        self.assertEqual((data["grant_type"], data["code"]), ("authorization_code", "CODE1"))
        headers = mock_get.call_args.kwargs["headers"]
        self.assertEqual((headers["Authorization"], headers["X-Manager-Token"]), ("Bearer AUTH1", "MGR1"))
        from .models import ZidConnection
        conn = ZidConnection.objects.get(store_id="4242")
        self.assertEqual((conn.store_name, conn.store_uuid), ("بوتيك الورد", "3dfc16e6-ad19"))
        self.assertEqual((conn.authorization_token, conn.manager_token, conn.refresh_token), ("AUTH1", "MGR1", "REF1"))
        self.assertEqual(conn.merchant.label, "بوتيك الورد")
        mock_sync.assert_called_once_with(conn.pk)
        page = self.client.get("/zid/app/").content.decode()
        self.assertIn("تم تفعيل Control على متجرك", page)

    def test_wrong_state_rejected_without_spending_the_code(self):
        self.start()
        resp, mock_post, _, _ = self.finish("forged-state")
        self.assertEqual(resp.status_code, 400)
        mock_post.assert_not_called()

    def test_reinstall_reuses_same_merchant(self):
        from .models import ZidConnection
        _, q1 = self.start()
        self.finish(q1["state"][0])
        _, q2 = self.start()
        self.finish(q2["state"][0], code="CODE2")
        self.assertEqual(ZidConnection.objects.count(), 1)

    def test_app_page_without_session_restarts_oauth(self):
        self.assertRedirects(self.client.get("/zid/app/"), "/zid/install/", fetch_redirect_response=False)

    def test_scheduled_refresh_replaces_whole_token_set(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import ZidConnection
        from .services import zid
        _, query = self.start()
        self.finish(query["state"][0])
        conn = ZidConnection.objects.get(store_id="4242")
        self.assertFalse(zid.needs_scheduled_refresh(conn))
        conn.tokens_refreshed_at = timezone.now() - timedelta(days=301)
        conn.save()
        self.assertTrue(zid.needs_scheduled_refresh(conn))
        with patch("apitokens.services.zid.requests.post") as mock_post:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = dict(self.TOKENS, authorization="AUTH2", access_token="MGR2", refresh_token="REF2")
            zid.refresh_tokens(conn)
            data = mock_post.call_args.kwargs["data"]
            self.assertEqual((data["grant_type"], data["refresh_token"]), ("refresh_token", "REF1"))
        conn.refresh_from_db()
        self.assertEqual((conn.authorization_token, conn.manager_token, conn.refresh_token), ("AUTH2", "MGR2", "REF2"))
        self.assertFalse(zid.needs_scheduled_refresh(conn))

    def test_uninstall_deactivates(self):
        from .models import ZidConnection
        from .services import zid
        _, query = self.start()
        self.finish(query["state"][0])
        zid.deactivate(store_uuid="3dfc16e6-ad19")
        conn = ZidConnection.objects.get(store_id="4242")
        conn.merchant.refresh_from_db()
        self.assertFalse(conn.is_active or conn.merchant.is_active)
        self.assertEqual(conn.manager_token, "")


class PrivacyPageTests(TestCase):
    @override_settings(SUPPORT_EMAIL="support@example.com")
    def test_public_bilingual_page_with_contact(self):
        resp = self.client.get("/privacy/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("سياسة الخصوصية", html)
        self.assertIn("Privacy Policy", html)
        self.assertIn("support@example.com", html)


@override_settings(ZID_WEBHOOK_SECRET="zid-hook-secret", ZID_PLAN_QUOTAS={"Basic": 100, "904": 500},
                   ZID_DEFAULT_IMAGE_QUOTA=20)
class ZidSubscriptionTests(TestCase):
    def setUp(self):
        from .models import ZidConnection
        self.merchant = ApiToken.objects.create(label="بوتيك", image_quota=20, images_used=7)
        self.connection = ZidConnection.objects.create(
            merchant=self.merchant, store_id="507530", store_uuid="d78468cd", authorization_token="A",
            manager_token="M", refresh_token="R",
        )

    def event(self, name, secret="zid-hook-secret", **extra):
        payload = dict({"app_id": 1292, "store_id": 507530, "store_uuid": "d78468cd", "plan_name": "Basic",
                        "plan_id": 77, "plan_type": "Paid", "status": "active",
                        "end_date": "2026-12-01T08:05:29.000000Z", "event_name": name}, **extra)
        headers = {"HTTP_X_CONTROL_SECRET": secret} if secret else {}
        return self.client.post("/webhooks/zid/app/", data=json.dumps(payload),
                                content_type="application/json", **headers)

    def test_missing_or_wrong_secret_rejected(self):
        self.assertEqual(self.event("app.market.subscription.active", secret=None).status_code, 401)
        self.assertEqual(self.event("app.market.subscription.active", secret="nope").status_code, 401)

    def test_active_sets_plan_quota_and_resets_usage(self):
        self.assertEqual(self.event("app.market.subscription.active").status_code, 200)
        self.merchant.refresh_from_db()
        self.assertEqual((self.merchant.image_quota, self.merchant.images_used), (100, 0))
        self.assertTrue(self.merchant.is_active)

    def test_upgrade_by_plan_id(self):
        self.event("app.market.subscription.active")
        self.merchant.images_used = 40
        self.merchant.save()
        self.event("app.market.subscription.upgrade", plan_name="Growth", plan_id=904)
        self.merchant.refresh_from_db()
        self.assertEqual((self.merchant.image_quota, self.merchant.images_used), (500, 0))

    def test_expired_turns_widget_off_and_renew_turns_it_back_on(self):
        self.event("app.market.subscription.active")
        self.event("app.market.subscription.warning", status="warning")
        self.merchant.refresh_from_db()
        self.assertTrue(self.merchant.is_active)
        self.event("app.market.subscription.expired", status="expire")
        self.merchant.refresh_from_db()
        self.assertFalse(self.merchant.is_active)
        self.event("app.market.subscription.renew")
        self.merchant.refresh_from_db()
        self.assertTrue(self.merchant.is_active)

    def test_subscription_before_oauth_applies_on_install(self):
        """زد بتقبض قبل OAuth — الاشتراك بيوصل قبل ما يكون عنا ربط."""
        from .models import ZidConnection, ZidSubscription
        from .services import zid
        self.event("app.market.subscription.active", store_id=999001, store_uuid="new-uuid")
        self.assertTrue(ZidSubscription.objects.get(store_id="999001").is_active)
        tokens = {"authorization": "A2", "access_token": "M2", "refresh_token": "R2", "expires_in": 100}
        with patch("apitokens.services.zid.requests.post") as mock_post, \
                patch("apitokens.services.zid.requests.get") as mock_get:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = tokens
            mock_get.return_value.json.return_value = {"store": {"id": 999001, "uuid": "new-uuid", "title": "جديد"}}
            mock_get.return_value.raise_for_status.return_value = None
            conn = zid.complete_installation("CODE")
        conn.merchant.refresh_from_db()
        self.assertEqual(conn.merchant.image_quota, 100)
        self.assertTrue(conn.merchant.is_active)
        self.assertEqual(ZidConnection.objects.filter(store_id="999001").count(), 1)

    def test_unknown_plan_falls_back_to_default_quota(self):
        self.event("app.market.subscription.active", plan_name="Mystery", plan_id=1)
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.image_quota, 20)

    def test_uninstall_event_deactivates(self):
        self.event("app.market.application.uninstall", status="active")
        self.connection.refresh_from_db()
        self.merchant.refresh_from_db()
        self.assertFalse(self.connection.is_active or self.merchant.is_active)

    @override_settings(ZID_WEBHOOK_SECRET="")
    def test_unconfigured_secret_refuses(self):
        self.assertEqual(self.event("app.market.subscription.active", secret="x").status_code, 503)


@override_settings(ZID_WEBHOOK_SECRET="zid-hook-secret", ZID_CLIENT_ID="8467", SITE_BASE_URL="https://control.example.com")
class ZidProductTests(TestCase):
    PRODUCT = {
        "id": "a1b2c3d4-uuid", "sku": "ABY-01", "name": {"ar": "عباية كريب سوداء", "en": "Black Crepe Abaya"},
        "description": {"ar": "<p>قماش <b>كريب</b> ناعم</p>", "en": "Soft crepe"},
        "price": 300, "sale_price": 250, "currency": "SAR", "quantity": 5, "is_infinite": False,
        "is_published": True, "is_draft": False, "html_url": "https://ward.zid.store/products/black-abaya",
        "images": [{"id": "i1", "image": {"thumbnail": "https://cdn.zid/t.jpg", "full_size": "https://cdn.zid/full.jpg"}}],
        "categories": [{"id": 1, "name": {"ar": "عبايات", "en": "Abayas"}}],
        "options": [{"name": {"ar": "المقاس", "en": "Size"}, "choices": [{"value": {"ar": "S", "en": "S"}}, {"value": "M"}]}],
    }

    def setUp(self):
        from .models import ZidConnection
        self.merchant = ApiToken.objects.create(label="ورد", image_quota=20)
        self.connection = ZidConnection.objects.create(
            merchant=self.merchant, store_id="4242", authorization_token="A", manager_token="M", refresh_token="R",
        )

    def test_mapping_prefers_arabic_and_reads_sale(self):
        from .services import zid
        row = zid.product_row_from_payload(self.PRODUCT)
        self.assertEqual(row["title"], "عباية كريب سوداء")
        self.assertEqual((str(row["price"]), str(row["compare_at_price"])), ("250", "300"))
        self.assertEqual(row["image_url"], "https://cdn.zid/full.jpg")
        self.assertEqual(row["product_url"], "https://ward.zid.store/products/black-abaya")
        self.assertEqual(row["options"], {"المقاس": ["S", "M"]})
        self.assertEqual(row["category"], "عبايات")
        self.assertNotIn("<", row["description"])
        self.assertTrue(row["in_stock"])

    def test_draft_unpublished_or_imageless_dropped(self):
        from .services import zid
        for change in ({"is_draft": True}, {"is_published": False}, {"images": []}):
            self.assertIsNone(zid.product_row_from_payload(dict(self.PRODUCT, **change)), change)
        self.assertFalse(zid.product_row_from_payload(dict(self.PRODUCT, quantity=0))["in_stock"])
        self.assertTrue(zid.product_row_from_payload(dict(self.PRODUCT, quantity=0, is_infinite=True))["in_stock"])

    @patch("apitokens.services.catalog.ensure_embeddings")
    def test_full_sync_pages_and_removes_deleted(self, _emb):
        from .services import catalog
        Product.objects.create(merchant=self.merchant, external_id="gone", sku="G", title="x", price="1",
                               image_url="https://x/g.jpg")
        page1 = {"count": 3, "next": "https://api.zid.sa/v1/products/?page=2", "results": [self.PRODUCT, dict(self.PRODUCT, id="p2", sku="")]}
        page2 = {"count": 3, "next": None, "results": [dict(self.PRODUCT, id="p3", is_draft=True)]}
        with patch("apitokens.services.zid.requests.request") as mock_req:
            mock_req.return_value.ok = True
            mock_req.return_value.content = b"{}"
            mock_req.return_value.json.side_effect = [page1, page2]
            self.assertEqual(catalog.sync_zid(self.merchant, self.connection), 2)
            headers = mock_req.call_args.kwargs["headers"]
        self.assertEqual(headers["Accept-Language"], "all-languages")
        self.assertEqual((headers["X-Manager-Token"], headers["Store-Id"]), ("M", "4242"))
        self.assertEqual(sorted(self.merchant.products.values_list("external_id", flat=True)), ["a1b2c3d4-uuid", "p2"])
        self.assertEqual(self.merchant.products.get(external_id="p2").sku, "ZID-p2")

    def test_register_store_webhooks_with_basic_auth(self):
        from .services import zid
        with patch("apitokens.services.zid.requests.request") as mock_req:
            mock_req.return_value.ok = True
            mock_req.return_value.content = b"{}"
            mock_req.return_value.json.return_value = {}
            events = zid.register_store_webhooks(self.connection)
        self.assertEqual(events, zid.STORE_WEBHOOK_EVENTS)
        body = mock_req.call_args_list[0].kwargs["json"]
        self.assertEqual(body["target_url"], "https://control.example.com/webhooks/zid/store/4242/product.create/")
        self.assertEqual((body["username"], body["password"], body["original_id"]), ("control", "zid-hook-secret", "8467"))

    def store_event(self, event, data, password="zid-hook-secret", store="4242"):
        import base64
        auth = "Basic " + base64.b64encode(f"control:{password}".encode()).decode()
        return self.client.post(f"/webhooks/zid/store/{store}/{event}/", data=json.dumps(data),
                                content_type="application/json", HTTP_AUTHORIZATION=auth)

    def test_store_webhook_product_lifecycle(self):
        self.assertEqual(self.store_event("product.create", self.PRODUCT).status_code, 200)
        self.assertTrue(self.merchant.products.filter(external_id="a1b2c3d4-uuid").exists())
        self.store_event("product.update", dict(self.PRODUCT, sale_price=None))
        self.assertIsNone(self.merchant.products.get(external_id="a1b2c3d4-uuid").compare_at_price)
        self.store_event("product.delete", {"id": "a1b2c3d4-uuid"})
        self.assertFalse(self.merchant.products.filter(external_id="a1b2c3d4-uuid").exists())

    def test_store_webhook_rejects_bad_auth_and_ignores_unknown_store(self):
        self.assertEqual(self.store_event("product.create", self.PRODUCT, password="nope").status_code, 401)
        self.assertEqual(self.store_event("product.create", self.PRODUCT, store="999").status_code, 200)
        self.assertFalse(Product.objects.exists())

    def test_order_links_to_conversation_by_cart(self):
        from .models import Conversation
        conv = Conversation.objects.create(merchant=self.merchant, session_id="sess-zid-0001", platform_cart_id="CART-9")
        self.store_event("order.create", {"id": 77001, "cart": {"id": "CART-9"},
                                          "order_total": "450.00", "currency_code": "SAR"})
        conv.refresh_from_db()
        self.assertEqual((conv.order_id, str(conv.order_total), conv.order_currency), ("77001", "450.00", "SAR"))


class ZidWidgetTests(TestCase):
    def setUp(self):
        from .models import ZidConnection
        self.merchant = ApiToken.objects.create(label="ورد", image_quota=20)
        self.connection = ZidConnection.objects.create(
            merchant=self.merchant, store_id="4242", store_url="https://ward.zid.store/", authorization_token="A",
        )
        ApiToken.objects.create(label="other", image_quota=1)
        other = ApiToken.objects.create(label="ward2", image_quota=1)
        ZidConnection.objects.create(merchant=other, store_id="5555", store_url="https://award.zid.store/")

    def key(self, host):
        return self.client.get("/widget/zid-key/", {"host": host})

    def test_store_key_by_host(self):
        for host in ("ward.zid.store", "WWW.ward.zid.store", "ward.zid.store:443"):
            self.assertEqual(self.key(host).json()["store_key"], self.merchant.token, host)

    def test_similar_or_unknown_host_not_matched(self):
        self.assertEqual(self.key("zid.store").status_code, 404)
        self.assertEqual(self.key("ard.zid.store").status_code, 404)
        self.assertEqual(self.key("").status_code, 404)

    def test_inactive_store_gets_no_key(self):
        self.merchant.is_active = False
        self.merchant.save()
        self.assertEqual(self.key("ward.zid.store").status_code, 404)

    def test_zid_product_pages_already_match(self):
        Product.objects.create(merchant=self.merchant, sku="A", external_id="u-1", title="عباية", price="10",
                               image_url="https://x/a.jpg", product_url="https://ward.zid.store/products/black-abaya")
        product = self.client.post(
            "/widget/page-product/", data=json.dumps({"page_url": "https://ward.zid.store/products/black-abaya?ref=ig"}),
            content_type="application/json", HTTP_X_API_TOKEN=self.merchant.token,
        ).json()["product"]
        self.assertEqual((product["sku"], product["product_id"]), ("A", "u-1"))


class ZidDashboardTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from .models import ZidConnection, ZidSubscription
        user = get_user_model().objects.create_user("staff2", password="x", is_staff=True)
        self.client.force_login(user)
        self.merchant = ApiToken.objects.create(label="ورد", image_quota=100, images_used=12)
        self.connection = ZidConnection.objects.create(
            merchant=self.merchant, store_id="4242", store_name="بوتيك الورد", store_url="https://ward.zid.store/",
            authorization_token="A", manager_token="M",
        )
        ZidSubscription.objects.create(store_id="4242", plan_name="Basic", is_active=True)

    @override_settings(ZID_CLIENT_ID="8467", ZID_CLIENT_SECRET="", SITE_BASE_URL="https://control.example.com")
    def test_page_lists_store_plan_portal_values_and_snippet(self):
        html = self.client.get("/zid/").content.decode()
        for expected in ("بوتيك الورد", "4242", "Basic", "12 / 100", "https://control.example.com/zid/callback/",
                         "https://control.example.com/webhooks/zid/app/", "X-Control-Secret", "/widget/zid-key/",
                         "ZID_CLIENT_SECRET"):
            self.assertIn(expected, html, expected)
        self.assertNotIn(">8467<", html)

    def test_dashboard_marks_zid_stores(self):
        self.assertIn("/zid/", self.client.get("/").content.decode())

    @patch("apitokens.views.catalog_service.sync_zid", return_value=9)
    def test_manual_resync(self, mock_sync):
        self.assertEqual(self.client.post("/zid/", {"action": "resync", "connection_id": self.connection.pk}).status_code, 302)
        mock_sync.assert_called_once()

    def test_requires_staff(self):
        self.client.logout()
        self.assertEqual(self.client.get("/zid/").status_code, 302)


class TryOnLimitSettingsTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.client.force_login(get_user_model().objects.create_user("staff3", password="x", is_staff=True))
        self.merchant = ApiToken.objects.create(label="m", image_quota=10)

    def test_defaults(self):
        self.assertEqual((self.merchant.tryons_per_shopper, self.merchant.tryon_limit_days), (2, 30))

    def test_merchant_page_saves_limit(self):
        resp = self.client.post(f"/{self.merchant.pk}/catalog/",
                                {"action": "tryon_limit", "tryons_per_shopper": "3", "tryon_limit_days": "7"})
        self.assertEqual(resp.status_code, 302)
        self.merchant.refresh_from_db()
        self.assertEqual((self.merchant.tryons_per_shopper, self.merchant.tryon_limit_days), (3, 7))

    def test_invalid_values_rejected_and_clamped(self):
        self.client.post(f"/{self.merchant.pk}/catalog/",
                         {"action": "tryon_limit", "tryons_per_shopper": "abc", "tryon_limit_days": "7"})
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.tryons_per_shopper, 2)
        self.client.post(f"/{self.merchant.pk}/catalog/",
                         {"action": "tryon_limit", "tryons_per_shopper": "-5", "tryon_limit_days": "0"})
        self.merchant.refresh_from_db()
        self.assertEqual((self.merchant.tryons_per_shopper, self.merchant.tryon_limit_days), (0, 1))
        self.assertIn("حد تجربة اللبس", self.client.get(f"/{self.merchant.pk}/catalog/").content.decode())


@override_settings(KIEAI_API_KEY="kie-test")
class TryOnShopperLimitTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="m", image_quota=100, tryons_per_shopper=2, tryon_limit_days=30)
        Product.objects.create(merchant=self.merchant, sku="D1", title="فستان", price="10",
                               image_url="https://x/d.jpg", category="dresses")

    def tryon(self, shopper="shopper-aaaa1111", ip="10.0.0.1"):
        from django.core.files.uploadedfile import SimpleUploadedFile
        photo = SimpleUploadedFile("me.jpg", b"\xff\xd8fakejpeg", content_type="image/jpeg")
        with patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://cdn/result.jpg"):
            return self.client.post(
                "/widget/tryon/", {"sku": "D1", "photo": photo, "shopper_id": shopper},
                HTTP_X_API_TOKEN=self.merchant.token, HTTP_X_FORWARDED_FOR=ip,
            ).json()

    def test_shopper_gets_exactly_the_limit(self):
        first, second, third = self.tryon(), self.tryon(), self.tryon()
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual((first["shopper_remaining"], second["shopper_remaining"]), (1, 0))
        self.assertEqual((third["ok"], third["error"]), (False, "shopper_limit_reached"))
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.images_used, 2)  # المرفوض ما بيخصم من رصيد التاجر

    def test_other_shoppers_unaffected(self):
        self.tryon(), self.tryon()
        self.assertTrue(self.tryon(shopper="shopper-bbbb2222", ip="10.0.0.2")["ok"])

    def test_clearing_browser_id_still_capped_by_ip(self):
        results = [self.tryon(shopper=f"fresh-id-{i:08d}") for i in range(7)]
        self.assertEqual([r["ok"] for r in results], [True] * 6 + [False])

    def test_limit_resets_after_period(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import TryOnRecord
        self.tryon(), self.tryon()
        TryOnRecord.objects.update(created_at=timezone.now() - timedelta(days=31))
        self.assertTrue(self.tryon()["ok"])

    def test_zero_means_unlimited(self):
        self.merchant.tryons_per_shopper = 0
        self.merchant.save()
        results = [self.tryon() for _ in range(4)]
        self.assertTrue(all(r["ok"] for r in results))
        self.assertIsNone(results[-1]["shopper_remaining"])


class SizingTests(TestCase):
    def test_parse_chart_and_round_trip(self):
        from .services import sizing
        chart = sizing.parse_chart("# comment\nS, 150-162, 46-56, 62-68\nM، 158-170، 55-66، -\n")
        self.assertEqual(chart[0], {"size": "S", "height": [150, 162], "weight": [46, 56], "waist": [62, 68]})
        self.assertNotIn("waist", chart[1])
        self.assertEqual(sizing.parse_chart(sizing.chart_to_text(chart)), chart)

    def test_parse_chart_rejects_bad_lines(self):
        from .services import sizing
        for bad in ("M, tall", "M", ", 150-160"):
            with self.assertRaises(sizing.SizeChartError):
                sizing.parse_chart(bad)

    def test_recommend_by_measures(self):
        from .services import sizing
        chart = sizing.DEFAULT_CHART
        self.assertEqual(sizing.recommend(chart, {"height": 165, "weight": 60}), "M")
        self.assertEqual(sizing.recommend(chart, {"waist": 80}), "L")
        self.assertEqual(sizing.recommend(chart, {"height": 152, "weight": 44}), "XS")
        self.assertEqual(sizing.recommend(chart, {"height": 195, "weight": 120}), "XXL")
        self.assertIsNone(sizing.recommend(chart, {}))

    def test_advise_respects_available_sizes(self):
        from .services import sizing
        chart = sizing.DEFAULT_CHART
        self.assertEqual(sizing.advise(chart, {"height": 165, "weight": 60}, ["S", "M", "L"]),
                         {"size": "M", "available": True, "alternative": None})
        advice = sizing.advise(chart, {"height": 165, "weight": 60}, ["S", "L"])
        self.assertEqual((advice["size"], advice["available"], advice["alternative"]), ("M", False, "L"))
        self.assertEqual(sizing.advise(chart, {"weight": 60}, [])["available"], True)

    def test_product_sizes_reads_size_option_only(self):
        from .services import sizing
        product = Product(options={"اللون": ["أسود"], "المقاس": ["S", "M"]})
        self.assertEqual(sizing.product_sizes(product), ["S", "M"])
        self.assertEqual(sizing.product_sizes(Product(options={"Size": ["L"]})), ["L"])
        self.assertEqual(sizing.product_sizes(Product(options={})), [])

    def test_dashboard_saves_and_deletes_chart(self):
        from django.contrib.auth import get_user_model
        from .models import SizeChart
        self.client.force_login(get_user_model().objects.create_user("staff4", password="x", is_staff=True))
        merchant = ApiToken.objects.create(label="m", image_quota=1)
        url = f"/{merchant.pk}/catalog/"
        self.client.post(url, {"action": "size_chart", "category": "فساتين", "chart": "S, 150-160, 45-55, 60-66"})
        self.assertEqual(SizeChart.objects.get(merchant=merchant, category="فساتين").chart[0]["size"], "S")
        self.client.post(url, {"action": "size_chart", "category": "", "chart": "M, bad"})
        self.assertFalse(SizeChart.objects.filter(merchant=merchant, category="").exists())
        self.assertIn("S, 150-160, 45-55, 60-66", self.client.get(url).content.decode())
        self.client.post(url, {"action": "size_chart_delete", "category": "فساتين"})
        self.assertFalse(SizeChart.objects.exists())
