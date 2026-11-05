"""اختبارات المرحلة 0 (تقوية v1): مدقّق المخرجات، بوابة الخصم، سقف التاريخ،
تحديد المعدّل، حجز رصيد التجربة.

الاختبارات متعددة الأدوار فعلاً: بتحاكي الودجت (بيحتفظ بالتاريخ وshown_skus
وsession_id ويبعتهم كل دور) ضد الـ view الحقيقي، والـ LLM ممثَّل بسكربت بيرمي
خطأ لو انطلب منه أكتر من المتوقع — فأي استدعاء إضافي مش مقصود بيكسر الاختبار.
"""

import json
from decimal import Decimal
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from .models import ApiToken, Conversation, DiscountCode, Product
from .services import guard, kieai, llm, policy, ratelimit


def res(text="", calls=None):
    return llm.LLMResult(reply_text=text, tool_calls=calls or [])


def rec(sku, reason="قطعة حلوة وتناسبك", **extra):
    return {"name": "recommend_product", "input": {"sku": sku, "reason": reason, **extra}}


def offer(code, pitch=""):
    return {"name": "offer_discount_code", "input": {"code": code, "pitch": pitch}}


class Script:
    """LLM ممثَّل: بيرجّع النتائج بالترتيب، وبيسجّل كل استدعاء. لو انطلب أكتر
    من المخطط ← فشل صريح بدل رد وهمي."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, provider, system_prompt, messages, tools, force_tool=None, **kw):
        self.calls.append(
            {"system": system_prompt, "messages": list(messages), "tools": [t["name"] for t in tools],
             "force_tool": force_tool}
        )
        if not self.results:
            raise AssertionError(f"unexpected extra LLM call #{len(self.calls)} (force={force_tool})")
        return self.results.pop(0)


class FakeProvider:
    def is_configured(self):
        return True


def make_product(sku="P", title="Item", price="10", description="", compare_at=None):
    return Product(
        sku=sku, title=title, price=Decimal(price), description=description,
        compare_at_price=Decimal(compare_at) if compare_at else None,
    )


# ======================================================================
# المدقّق: وحدات صرفة (بدون DB ولا LLM)
# ======================================================================

class GuardUnitTests(TestCase):
    def setUp(self):
        self.shirt = make_product("S", "Cotton Shirt", "29.99", "Soft 100% cotton, relaxed fit")
        self.dress = make_product("D", "Black Dress", "79.99", "Satin evening dress")
        self.sale = make_product("R", "Red Dress", "49", "Cotton blend 50% cotton", compare_at="69")
        self.plain = guard.build_context(
            products=[self.shirt, self.dress], merchant_codes=["SAVE10", "VIP50"],
            store_info="Shipping: 7 USD, delivery 2-4 days.",
        )

    def kinds(self, text, ctx=None):
        return guard.kinds(guard.find_violations(text, ctx or self.plain))

    # ---- لغات ولهجات: كلها لازم تنكشف بدون ما نعتمد على لغة الزبون ----
    def test_fabricated_discount_in_every_language_is_caught(self):
        samples = [
            "عندنا خصم 15% اليوم",                                  # سعودي/فصحى
            "عنا عرض خاص، بنعطيك عشرين بالمية خصم",                # شامي، نسبة بالحروف
            "نعطيك تخفيض خاص ٪٢٠",                                  # رمز النسبة العربي وأرقام هندية
            "خذ كوبون بقيمة عشرين ريال",                             # كوبون (ما كان بالقائمة القديمة)
            "I can give you 20 off today",
            "Here is a 10% discount just for you",
            "Je peux vous faire une réduction exceptionnelle de vingt pour cent",
            "Te hago un descuento del 15 por ciento",
            "Posso fazer um desconto especial",
            "Ich gebe Ihnen 10 Prozent Rabatt",
        ]
        for text in samples:
            self.assertTrue(self.kinds(text), text)

    def test_invented_code_and_known_code_without_approval(self):
        self.assertIn(guard.UNAPPROVED_CODE, self.kinds("استخدم SAVE10 وبتوفر"))
        self.assertIn(guard.UNAPPROVED_CODE, self.kinds("خذ vip50 الحين"))
        self.assertIn(guard.UNAPPROVED_PROMO, self.kinds("use code WELCOME15 at checkout"))

    def test_unverified_prices_and_shipping_claims(self):
        self.assertIn(guard.UNVERIFIED_AMOUNT, self.kinds("سعره 25 دولار بس"))
        self.assertIn(guard.UNVERIFIED_STORE_PROMO, self.kinds("والشحن مجاني لك"))
        self.assertIn(guard.UNVERIFIED_STORE_PROMO, self.kinds("It comes with free shipping"))

    def test_grounded_text_passes(self):
        for text in [
            "هالقميص قطن 100% وسعره 29.99 دولار",
            "سعره حوالي 30 دولار",                       # تقريب لأعلى لسعر حقيقي
            "الشحن 7 دولار ويوصل بـ 2-4 أيام",              # أرقام معلومات المتجر
            "Soft cotton and a relaxed fit — a great everyday pick.",
            "وش لونك المفضل؟",
        ]:
            self.assertEqual(self.kinds(text), [], text)

    def test_customer_budget_can_be_echoed_but_not_a_discount_percent(self):
        ctx = guard.build_context(
            products=[self.dress], merchant_codes=[], customer_texts=["ميزانيتي 60 دولار", "ابي خصم 20%"],
        )
        self.assertEqual(self.kinds("ضمن ميزانيتك 60 دولار", ctx), [])
        self.assertTrue(self.kinds("أكيد، خصم 20% لك", ctx))  # صدى طلب الزبون مش دليل

    def test_percent_from_a_product_description_does_not_authorize_a_discount(self):
        ctx = guard.build_context(products=[self.sale], merchant_codes=[])
        self.assertEqual(self.kinds("قطن 50% وباقيه ألياف", ctx), [])
        sale_ctx = guard.build_context(
            products=[self.sale], merchant_codes=[], cards=[{"compare_at_price": "69.00"}]
        )
        self.assertIn(guard.UNVERIFIED_PERCENT, self.kinds("عليه خصم 50% اليوم", sale_ctx))

    # ---- كود معتمد ----
    def test_approved_offer_allows_only_its_own_code_and_numbers(self):
        offer = {"code": "SAVE10", "description": "خصم 10% على كل المنتجات"}
        ctx = guard.build_context(
            products=[self.dress], merchant_codes=["SAVE10", "VIP50"], approved_offer=offer
        )
        self.assertEqual(self.kinds("خذ كود SAVE10 وبتوفر 10%", ctx), [])
        self.assertIn(guard.UNVERIFIED_PERCENT, self.kinds("خذ SAVE10 وبتوفر 20%", ctx))
        self.assertIn(guard.UNAPPROVED_CODE, self.kinds("أو استخدم VIP50", ctx))
        # حساب السعر بعد الخصم من الموديل = ادعاء ما فينا نتحقق منه
        self.assertIn(guard.UNVERIFIED_AMOUNT, self.kinds("وبيصير سعره 71 دولار", ctx))

    # ---- تخفيض حقيقي على المنتج ----
    def test_real_sale_allows_its_own_figures_only(self):
        ctx = guard.build_context(
            products=[self.sale], merchant_codes=["SAVE10"], cards=[{"compare_at_price": "69.00"}]
        )
        self.assertEqual(self.kinds("عليه تخفيض حقيقي: كان 69 وصار 49", ctx), [])
        self.assertEqual(self.kinds("وفّرت 20 دولار، يعني حوالي 29%", ctx), [])
        self.assertIn(guard.UNVERIFIED_PERCENT, self.kinds("وفّر 40% الحين", ctx))
        # تخفيض حقيقي ما بيبرّر كود ولا كود مخترع (D1 بالتدقيق)
        self.assertTrue(self.kinds("استخدم كود WELCOME15 كمان", ctx))
        self.assertIn(guard.UNAPPROVED_CODE, self.kinds("استخدم SAVE10 كمان", ctx))

    def test_store_promise_allowed_only_when_store_info_says_it(self):
        ctx = guard.build_context(
            products=[self.dress], merchant_codes=[], store_info="Free shipping on orders over 100 USD",
        )
        # سياسة المتجر بالإنجليزي بتبرّر الرد بلغة الزبون (المدقّق ما بيعتمد على اللغة)
        self.assertEqual(self.kinds("الشحن مجاني للطلبات فوق 100 دولار", ctx), [])
        self.assertEqual(self.kinds("There is free shipping on orders over 100 USD", ctx), [])
        # بس الرقم لازم يطابق: عتبة مخترعة ممنوعة
        self.assertIn(guard.UNVERIFIED_AMOUNT, self.kinds("الشحن مجاني للطلبات فوق 50 دولار", ctx))
        no_promo = guard.build_context(products=[self.dress], merchant_codes=[], store_info="Shipping: 7 USD")
        self.assertIn(guard.UNVERIFIED_STORE_PROMO, self.kinds("الشحن مجاني للطلبات فوق 100 دولار", no_promo))

    # ---- الحذف ----
    def test_strip_removes_only_the_offending_sentence_and_keeps_decimals(self):
        text = "هالفستان سعره 79.99 دولار وقماشه ساتان. عندنا خصم 15% لك! تبي تجرّبه؟"
        cleaned = guard.strip_violations(text, self.plain)
        self.assertIn("79.99", cleaned)
        self.assertIn("تبي تجرّبه", cleaned)
        self.assertNotIn("15%", cleaned)
        self.assertEqual(guard.strip_violations("عندنا خصم 15% لك", self.plain), "")


class PolicyGateTests(TestCase):
    def test_gate_rules(self):
        self.assertEqual(policy.basic_offer_gate(user_turns=3, engaged=True, already_offered=False), (True, "ok"))
        self.assertEqual(policy.basic_offer_gate(user_turns=1, engaged=True, already_offered=False)[1], "too_early")
        self.assertEqual(policy.basic_offer_gate(user_turns=5, engaged=False, already_offered=False)[1], "no_engaged_product")
        self.assertEqual(policy.basic_offer_gate(user_turns=5, engaged=True, already_offered=True)[1], "already_offered")

    def test_history_mentions_code_only_for_assistant_turns(self):
        msgs = [{"role": "user", "content": "عندك كود save10؟"}, {"role": "assistant", "content": "أبشر"}]
        self.assertFalse(policy.history_mentions_code(msgs, ["SAVE10"]))
        msgs.append({"role": "assistant", "content": "خذ كود SAVE10"})
        self.assertTrue(policy.history_mentions_code(msgs, ["SAVE10"]))


# ======================================================================
# محادثات متعددة الأدوار ضد /widget/chat/ الحقيقي
# ======================================================================

class WidgetDriver:
    """بيحاكي embed.js: تاريخ بالذاكرة، shown_skus، session_id ثابت بالجلسة."""

    def __init__(self, merchant, session_id="sess-test-0001", window=30):
        self.client = Client()
        self.merchant = merchant
        self.session_id = session_id
        self.history = []
        self.shown = []
        self.window = window
        self.last = None

    def say(self, text):
        self.history.append({"role": "user", "content": text})
        win = self.history[-self.window:]
        while win and win[0]["role"] != "user":
            win = win[1:]
        resp = self.client.post(
            "/widget/chat/",
            data=json.dumps({"messages": win, "session_id": self.session_id, "shown_skus": self.shown}),
            content_type="application/json",
            HTTP_X_API_TOKEN=self.merchant.token,
        )
        self.status = resp.status_code
        data = resp.json()
        self.last = data
        if data.get("ok"):
            self.history.append({"role": "assistant", "content": data["reply"]})
            for card in data["recommendations"]:
                if card["sku"] not in self.shown:
                    self.shown.append(card["sku"])
        elif self.history and self.history[-1]["role"] == "user":
            self.history.pop()  # نفس dropFailedTurn بالودجت
        return data


@patch("apitokens.widget_views.llm.get_provider", return_value=FakeProvider())
class Phase0ConversationTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="M", token="P0TOKEN000001", image_quota=5, currency="USD")
        Product.objects.create(merchant=self.merchant, sku="DRESS", title="Black Satin Dress", price="129.00",
                               image_url="https://x/1.jpg", category="dresses", description="Satin evening dress")
        Product.objects.create(merchant=self.merchant, sku="SHIRT", title="Blue Shirt", price="29.99",
                               image_url="https://x/2.jpg", category="shirts", description="Soft 100% cotton")
        Product.objects.create(merchant=self.merchant, sku="SALE", title="Red Dress", price="49.00",
                               compare_at_price="69.00", image_url="https://x/3.jpg", category="dresses")
        DiscountCode.objects.create(merchant=self.merchant, code="SAVE10", description="خصم 10% على كل المنتجات")

    def run_script(self, script, driver, *turns):
        with patch("apitokens.widget_views.llm.chat_with_retry", side_effect=script):
            return [driver.say(t) for t in turns]

    # ---------- توقيت الخصم: مبكر، مناسب، مكرر ----------
    def test_discount_timeline_early_then_allowed_then_never_again(self, _p):
        script = Script(
            res("هلا! وش تدور عليه؟"),                                            # دور 1: ما فيه منتج
            res("", [rec("DRESS", "فستان سهرة يجنن")]),                            # دور 2
            res("", [offer("SAVE10", "معك حق، القماش يعيش سنين")]),                 # دور 3: اعتراض سعر
            res("تمام، أي شي ثاني؟"),                                              # دور 4: ما لازم عرض ثاني
        )
        d = WidgetDriver(self.merchant)
        t1, t2, t3, t4 = self.run_script(
            script, d, "هلا", "بدي فستان أسود", "غالي شوي", "طيب والكود ما ينفع مرة ثانية؟",
        )
        self.assertIsNone(t1["discount_offer"])
        self.assertIsNone(t2["discount_offer"])
        self.assertEqual(t3["discount_offer"]["code"], "SAVE10")
        self.assertIsNone(t4["discount_offer"])
        # الموديل ما شاف الكود ولا أداته إلا بالدور 3 (البوابة بالكود، مش بالبرومبت)
        shows_code = ["SAVE10" in c["system"] for c in script.calls]
        has_tool = ["offer_discount_code" in c["tools"] for c in script.calls]
        self.assertEqual(shows_code, [False, False, True, False])
        self.assertEqual(has_tool, [False, False, True, False])

    def test_model_cannot_force_an_offer_through_a_closed_gate(self, _p):
        # الموديل (أو حقن برومبت) نادى الأداة بدور مبكر والأداة ما انعرضت له أصلاً
        script = Script(res("خذ هالكود", [offer("SAVE10", "خذ SAVE10")]), res("بدي أفهم أكتر شو تدور عليه."))
        d = WidgetDriver(self.merchant)
        (turn,) = self.run_script(script, d, "عطوني الكود")
        self.assertIsNone(turn["discount_offer"])
        self.assertNotIn("SAVE10", turn["reply"])

    def test_new_page_load_with_same_history_does_not_repeat_the_offer(self, _p):
        d = WidgetDriver(self.merchant)
        script = Script(res("", [rec("DRESS")]), res("", [offer("SAVE10", "خذ")]))
        self.run_script(script, d, "بدي فستان", "غالي")
        self.assertEqual(Conversation.objects.get(session_id=d.session_id).discount_code, "SAVE10")
        # نفس الجلسة، دور جديد: البوابة بتقرا السجل المخزّن بالسيرفر كمان مش بس التاريخ المرسل
        d.history = d.history[-1:]  # الودجت "نسي" (قصّ التاريخ)
        d.history = [{"role": "user", "content": "وين الكود؟"}]
        script2 = Script(res("بسّ يا غالي، وين وصلنا؟"))
        (turn,) = self.run_script(script2, d, "بدي كود تاني")
        self.assertIsNone(turn["discount_offer"])
        self.assertNotIn("offer_discount_code", script2.calls[0]["tools"])

    # ---------- ثغرات التدقيق D1..D5 مُغلقة ----------
    def test_d1_fabricated_promo_behind_a_real_sale_card_is_corrected(self, _p):
        script = Script(
            res("عندنا خصم 15% بكود WELCOME15 على هالفستان!", [rec("SALE")]),
            res("هالفستان عليه تخفيض حقيقي، كان 69 وصار 49!", [rec("SALE")]),
        )
        d = WidgetDriver(self.merchant)
        (turn,) = self.run_script(script, d, "غالي شوي")
        self.assertEqual(len(script.calls), 2)
        self.assertNotIn("WELCOME15", turn["reply"])
        self.assertNotIn("15%", turn["reply"])
        self.assertIn("49", turn["reply"])

    def test_d2_discount_inside_tool_only_reason_never_reaches_the_customer(self, _p):
        script = Script(
            res("", [rec("SHIRT", "عليه خصم 30% اليوم بس!")]),
            res("", [rec("SHIRT", "قطن ناعم يريّحك طول اليوم")]),
        )
        d = WidgetDriver(self.merchant)
        (turn,) = self.run_script(script, d, "وش عندكم؟")
        self.assertNotIn("30%", turn["reply"])
        self.assertNotIn("خصم", turn["reply"])
        self.assertNotIn("30%", turn["recommendations"][0]["reason"])

    def test_d3_vocabulary_gaps_are_closed(self, _p):
        leaks = [
            ("خذ كوبون تخفيض بقيمة عشرين ريال", "غالي"),
            ("I can give you 20 off plus free shipping today", "too expensive"),
            ("نعطيك تخفيض خاص ٪٢٠", "غالي"),
            ("Je peux vous faire une réduction exceptionnelle de vingt pour cent", "trop cher"),
            ("عنا عرض خاص، بنعطيك عشرين بالمية خصم", "غالي كتير"),   # شامي
        ]
        for leak, customer in leaks:
            script = Script(res(leak), res("Great value for the quality." if customer[0].isascii() else "قيمته ممتازة لجودته."))
            d = WidgetDriver(self.merchant, session_id=f"sess-d3-{abs(hash(leak)) % 10**8:08d}")
            (turn,) = self.run_script(script, d, customer)
            self.assertEqual(len(script.calls), 2, leak)
            self.assertNotEqual(turn["reply"], leak)
            self.assertEqual(guard.find_violations(turn["reply"], guard.build_context(products=[], merchant_codes=["SAVE10"])), [], turn["reply"])

    def test_d4_regeneration_that_still_violates_is_stripped_not_shipped(self, _p):
        script = Script(
            res("عندنا خصم 15% لك"),
            res("طيب خلني أعطيك خصم 10% بدل كذا. القماش ممتاز ويدوم."),
        )
        d = WidgetDriver(self.merchant)
        (turn,) = self.run_script(script, d, "غالي")
        self.assertEqual(len(script.calls), 2)  # إعادة محاولة وحدة بس، بعدها حذف حتمي
        self.assertNotIn("خصم", turn["reply"])
        self.assertIn("القماش ممتاز", turn["reply"])

    def test_d4_everything_violating_falls_back_to_a_safe_reply(self, _p):
        script = Script(res("خصم 15% لك"), res("خصم 20% لك الحين"))
        d = WidgetDriver(self.merchant)
        (turn,) = self.run_script(script, d, "غالي")
        self.assertEqual(turn["reply"], guard.SAFE_REPLY["ar"])

    def test_d5_real_code_in_text_next_to_a_card_while_gate_is_closed(self, _p):
        script = Script(
            res("استخدم SAVE10 وبتوفر", [rec("SHIRT")]),
            res("هالقميص قطن ناعم وبسعر حلو.", [rec("SHIRT")]),
        )
        d = WidgetDriver(self.merchant)
        (turn,) = self.run_script(script, d, "هلا")
        self.assertNotIn("SAVE10", turn["reply"])
        self.assertIsNone(turn["discount_offer"])

    def test_model_cannot_calculate_a_discounted_price(self, _p):
        d = WidgetDriver(self.merchant)
        script = Script(
            res("", [rec("DRESS")]),
            res("", [offer("SAVE10", "خذ الكود. وبتاخذه بـ 116 دولار بس!")]),
            res("خذ الكود الحين!", [offer("SAVE10", "x")]),
        )
        self.run_script(script, d, "بدي فستان")
        (turn,) = self.run_script(script, d, "غالي")
        self.assertEqual(turn["discount_offer"]["code"], "SAVE10")
        self.assertNotIn("116", turn["reply"])  # السعر بعد الخصم ما انحسب إلا من الكود لاحقاً

    # ---------- محادثات طويلة وحدّ التاريخ ----------
    def test_long_conversation_past_30_messages_keeps_working_and_log_stays_complete(self, _p):
        d = WidgetDriver(self.merchant, session_id="sess-long-0001")
        script = Script(*[res(f"رد {i}") for i in range(25)])
        with patch("apitokens.widget_views.llm.chat_with_retry", side_effect=script):
            for i in range(25):
                data = d.say(f"سؤال {i}")
                self.assertTrue(data["ok"], (i, data))
        # الموديل ما شاف أبداً أكتر من 30 رسالة، وتاريخه بيبدأ برسالة زبون
        self.assertTrue(all(len(c["messages"]) <= 30 for c in script.calls))
        self.assertTrue(all(c["messages"][0]["role"] == "user" for c in script.calls))
        self.assertEqual(len(script.calls[-1]["messages"]), 29)  # 30 → بتبدأ بزبون فبصير 29
        # سجل الداشبورد ما انقصّ: 25 دور = 50 رسالة
        stored = Conversation.objects.get(merchant=self.merchant, session_id="sess-long-0001").messages
        self.assertEqual(len(stored), 50)
        self.assertEqual(stored[0]["content"], "سؤال 0")
        self.assertEqual(stored[-1]["content"], "رد 24")

    def test_oversized_history_sent_directly_is_trimmed_not_rejected(self, _p):
        messages = []
        for i in range(60):
            messages += [{"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": f"a{i}"}]
        messages.append({"role": "user", "content": "آخر سؤال"})
        script = Script(res("تمام"))
        with patch("apitokens.widget_views.llm.chat_with_retry", side_effect=script):
            resp = Client().post("/widget/chat/", data=json.dumps({"messages": messages}),
                                 content_type="application/json", HTTP_X_API_TOKEN=self.merchant.token)
        self.assertEqual(resp.status_code, 200)
        sent = script.calls[0]["messages"]
        self.assertLessEqual(len(sent), 30)
        self.assertEqual(sent[0]["role"], "user")
        self.assertEqual(sent[-1]["content"], "آخر سؤال")

    def test_payload_cap_and_non_object_body(self, _p):
        big = json.dumps({"messages": [{"role": "user", "content": "x" * 2000}] * 140})
        resp = Client().post("/widget/chat/", data=big, content_type="application/json",
                             HTTP_X_API_TOKEN=self.merchant.token)
        self.assertEqual(resp.status_code, 413)
        resp = Client().post("/widget/chat/", data="[1,2]", content_type="application/json",
                             HTTP_X_API_TOKEN=self.merchant.token)
        self.assertEqual(resp.status_code, 400)

    # ---------- حوار شامي كامل ----------
    def test_syrian_dialect_conversation_end_to_end(self, _p):
        script = Script(
            res("أهلين! شو بتدور عليه؟"),
            res("", [rec("DRESS", "فستان ساتان للسهرة")]),
            res("", [rec("SHIRT", "قميص قطن بسعر خفيف")]),                       # بدّل بدون عرض
            res("", [offer("SAVE10", "فهمت عليك، ومنشان هيك خود الكود")]),          # اعتراض سعر على منتج معروض
        )
        d = WidgetDriver(self.merchant)
        outs = self.run_script(
            script, d, "مرحبا", "بدي فستان للسهرة", "كتير غالي، في شي أرخص؟", "لسا غالي عليي",
        )
        self.assertEqual([o["discount_offer"] is not None for o in outs], [False, False, False, True])
        self.assertEqual([c["sku"] for o in outs for c in o["recommendations"]], ["DRESS", "SHIRT"])
        self.assertIn("SAVE10", outs[3]["reply"])


# ======================================================================
# تحديد المعدّل
# ======================================================================

@patch("apitokens.widget_views.llm.get_provider", return_value=FakeProvider())
class RateLimitTests(TestCase):
    def setUp(self):
        self.a = ApiToken.objects.create(label="A", token="RLTOKEN0000A1", image_quota=5)
        self.b = ApiToken.objects.create(label="B", token="RLTOKEN0000B1", image_quota=5)

    def chat(self, merchant, ip="1.2.3.4"):
        return Client().post(
            "/widget/chat/", data=json.dumps({"messages": [{"role": "user", "content": "هلا"}]}),
            content_type="application/json", HTTP_X_API_TOKEN=merchant.token, REMOTE_ADDR=ip,
        )

    @override_settings(RATE_LIMITS={"chat_ip": (3, 60), "chat_ip_hour": (100, 3600), "chat_token": (1000, 60)})
    def test_chat_is_limited_per_ip_and_token_and_recovers_per_window(self, _p):
        with patch("apitokens.widget_views.llm.chat_with_retry", side_effect=lambda *a, **k: res("أهلين")):
            self.assertEqual([self.chat(self.a).status_code for _ in range(3)], [200, 200, 200])
            limited = self.chat(self.a)
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.json()["error"], "rate_limited")
            self.assertGreaterEqual(int(limited["Retry-After"]), 1)
            # تاجر تاني أو عنوان تاني ما بيتأثر
            self.assertEqual(self.chat(self.b).status_code, 200)
            self.assertEqual(self.chat(self.a, ip="9.9.9.9").status_code, 200)

    @override_settings(RATE_LIMITS={"chat_ip": (10**6, 60), "chat_ip_hour": (10**6, 3600), "chat_token": (2, 60)})
    def test_per_token_ceiling_protects_the_merchant_bill(self, _p):
        with patch("apitokens.widget_views.llm.chat_with_retry", side_effect=lambda *a, **k: res("أهلين")):
            codes = [self.chat(self.a, ip=f"10.0.0.{i}").status_code for i in range(4)]
        self.assertEqual(codes, [200, 200, 429, 429])

    @override_settings(RATE_LIMITS={"event_ip": (2, 60)})
    def test_event_endpoint_is_limited(self, _p):
        def ev():
            return Client().post("/widget/event/", data=json.dumps({"type": "buy_click", "session_id": "sess-ev-00001"}),
                                 content_type="application/json", HTTP_X_API_TOKEN=self.a.token, REMOTE_ADDR="5.5.5.5")
        self.assertEqual([ev().status_code for _ in range(3)], [200, 200, 429])

    @override_settings(RATE_LIMITS={"consume_token": (2, 60)})
    def test_consume_endpoint_limited_but_keeps_its_always_200_contract(self, _p):
        self.a.legacy_consume_enabled = True  # n8n القديم: توكن علني من مصدر داخلي
        self.a.save()

        def consume():
            return Client().post("/api/tokens/consume/", data=json.dumps({"count": 1}), content_type="application/json",
                                 HTTP_X_API_TOKEN=self.a.token)
        out = [consume() for _ in range(3)]
        self.assertEqual([r.status_code for r in out], [200, 200, 200])
        self.assertEqual([r.json().get("ok") for r in out], [True, True, False])
        self.assertEqual(out[2].json()["error"], "rate_limited")
        self.a.refresh_from_db()
        self.assertEqual(self.a.images_used, 2)  # الطلب المحجوب ما خصم شي

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_client_ip_uses_the_address_the_proxy_appended(self, _p):
        class Req:
            META = {"HTTP_X_FORWARDED_FOR": "6.6.6.6, 7.7.7.7", "REMOTE_ADDR": "127.0.0.1"}
        self.assertEqual(ratelimit.client_ip(Req), "7.7.7.7")  # الأول بيكتبه العميل
        with override_settings(TRUSTED_PROXY_COUNT=0):
            self.assertEqual(ratelimit.client_ip(Req), "127.0.0.1")


# ======================================================================
# حجز رصيد التجربة (try-on)
# ======================================================================

@override_settings(KIEAI_API_KEY="test-key")
class TryOnQuotaReservationTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="T", token="TRYTOKEN00001", image_quota=1)
        Product.objects.create(merchant=self.merchant, sku="DRESS", title="Dress", price="10",
                               image_url="https://x/1.jpg", category="dresses")

    def post(self, sku="DRESS"):
        photo = SimpleUploadedFile("me.jpg", b"\xff\xd8\xff fake", content_type="image/jpeg")
        return Client().post("/widget/tryon/", {"sku": sku, "photo": photo, "session_id": "sess-try-00001"},
                             HTTP_X_API_TOKEN=self.merchant.token)

    def test_success_consumes_exactly_one_image(self):
        with patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png"):
            data = self.post().json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["remaining"], 0)
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.images_used, 1)

    def test_quota_is_reserved_before_generation_so_concurrent_requests_cannot_overspend(self):
        inner = {}

        def slow_generation(**kwargs):
            # طلب تاني بيوصل والأول لسا عم يتولّد: لازم الرصيد يكون محجوز
            self.merchant.refresh_from_db()
            inner["used_during_generation"] = self.merchant.images_used
            inner["second"] = self.post().json()
            return "https://img/out.png"

        with patch("apitokens.widget_views.kieai.generate_tryon", side_effect=slow_generation) as gen:
            first = self.post().json()
        self.assertEqual(inner["used_during_generation"], 1)
        self.assertEqual(inner["second"]["error"], "quota_exceeded")
        self.assertTrue(first["ok"])
        self.assertEqual(gen.call_count, 1)  # التوليد الثاني ما انطلب (ما انصرفت فلوس)
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.images_used, 1)

    def test_failed_generation_refunds_the_reserved_image(self):
        with patch("apitokens.widget_views.kieai.generate_tryon", side_effect=kieai.TryOnError("upstream_failed")):
            resp = self.post()
        self.assertEqual(resp.status_code, 502)
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.images_used, 0)
        with patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png"):
            self.assertTrue(self.post().json()["ok"])  # الرصيد رجع فبيشتغل

    def test_unexpected_exception_also_refunds_and_propagates(self):
        with patch("apitokens.widget_views.kieai.generate_tryon", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.post()
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.images_used, 0)

    def test_unknown_product_does_not_consume_quota(self):
        resp = self.post(sku="NOPE")
        self.assertEqual(resp.status_code, 404)
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.images_used, 0)

    def test_tryon_is_rate_limited_per_ip(self):
        self.merchant.image_quota = 100
        self.merchant.save()
        with override_settings(RATE_LIMITS={"tryon_ip": (2, 60), "tryon_token": (100, 60)}), \
             patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png"):
            codes = [self.post().status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])
