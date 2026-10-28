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
