"""اللهجة السعودية (إلزامية): بياع ملابس سعودي طبيعي، مو شامي ولا مصري ولا فصحى متكلّفة.

نختبر 4 طبقات: (1) كاشف الكلمات غير السعودية، (2) كل نص عربي بيشوفه الزبون بالسيرفر والودجت
نظيف، (3) البرومبت فيه القاعدة، (4) محادثات v2 متعددة الأدوار: لو الموديل زلّ بكلمات شامية
بيرجع يكتب، ولو زلّ مرتين بتنستبدل حتمياً — والزبون الشامي بيتفهّم ويتردّ عليه بالسعودي.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

from . import widget_views as wv
from .services import guard, saudi
from .test_v2_memory import V2Base, analysis_budget, offer, pref, rec, res

EMBED_JS = Path(__file__).resolve().parent / "static" / "apitokens" / "embed.js"

SAUDI_LINES = [
    "يا هلا والله! وش تدور عليه اليوم؟",
    "أبشر، هذا الفستان مرّة يجنّن وعليك يهبّل.",
    "زين، تبي لون هادي ولا شي لافت؟",
    "ودّك تجرّبه عليك قبل لا تطلبه؟ ارفع صورتك وشوف.",
    "ما عليك، على راحتك، خذ وقتك.",
    "الحين عندك خيار ثاني أخف على الميزانية وبنفس الجودة.",
    "عشان كذا اخترته لك، قماشه ناعم وما يتجعّد.",
    "طيب، وش رايك نشوف مقاسك أول؟ عطني طولك ووزنك.",
    "تستاهل، يستاهل كل ريال، وراح يعيش معك سنين.",
    "هلا والله فيك مرة ثانية! كملنا من وين وقفنا؟",
    "أبغى أتأكد: المناسبة عرس ولا سهرة عادية؟",
    "لا تشيل هم، المقاس موجود والطلب يوصلك قريب.",
    "عمّي ما شاء الله عليه ذوقه عالي.",
    "وين توصّل؟ أقول لك مدة التوصيل بالضبط.",
]
SYRIAN_LINES = [
    "هلق شو بدك تشتري؟",
    "هاد الفستان كتير حلو وبيناسبك",
    "بدي أعرضلك شي منيح",
    "هيك أحسن لإلك",
    "عم يشتغل هاد المحل من زمان",
    "لسا ما وصل الطلب، بكرا بيوصل",
    "مشان هيك اخترته، هاي أحلى وحدة",
    "قديش ميزانيتك؟",
    "رح أساعدك يلي بدك ياه",
    "حابب تشوفه؟ كتيير حلو",
]
EGYPTIAN_LINES = ["عايز فستان إيه؟", "دلوقتي هنشوف الفستان", "إزاي أساعدك؟ ده حلو أوي", "عايزة تشوفي إيه؟"]


class DialectDetectorTests(SimpleTestCase):
    def test_natural_saudi_sales_lines_are_clean(self):
        for line in SAUDI_LINES:
            self.assertEqual(saudi.find_non_saudi(line), [], line)

    def test_syrian_and_egyptian_lines_are_all_caught(self):
        for line in SYRIAN_LINES + EGYPTIAN_LINES:
            self.assertTrue(saudi.find_non_saudi(line), line)

    def test_punctuation_diacritics_and_prefix_edge_cases(self):
        self.assertEqual(saudi.find_non_saudi("شو بدك؟"), ["شو", "بدك"])         # علامة الاستفهام ما بتلزق بالكلمة
        self.assertEqual(saudi.find_non_saudi("هلّق، كتّير حلو"), ["هلق", "كتير"])  # الشدّة/التشكيل ما بيخبّي الكلمة
        self.assertEqual(saudi.find_non_saudi("هَلَق"), ["هلق"])                    # التشكيل بيتشال قبل المقارنة
        self.assertEqual(saudi.find_non_saudi("يا عمّي"), [])                       # "عم" لوحدها مو شامية
        self.assertEqual(saudi.find_non_saudi("Hello بدي"), ["بدي"])
        # كلمات سعودية/حجازية سليمة ما لازم تنعلّم غلط (لون هادي، كده، لسه، فين، إيه = نعم)
        for ok in ("لون هادي ومريح", "إيه نعم مضبوط", "لسه ما وصل", "فين توصّل؟", "كده زين"):
            self.assertEqual(saudi.find_non_saudi(ok), [], ok)

    def test_deterministic_safety_net_replaces_the_common_words_only(self):
        fixed = saudi.to_saudi("هلق شو بدك؟ هاد الفستان كتير حلو، هيك أحسن")
        self.assertEqual(saudi.find_non_saudi(fixed), [])
        self.assertIn("الحين وش تبي", fixed)
        self.assertEqual(saudi.to_saudi(SAUDI_LINES[0]), SAUDI_LINES[0])  # نص سعودي ما بيتغيّر
        self.assertEqual(saudi.unmapped("حابب تشوفه"), ["حابب"])         # بدون بديل آمن ← بس بيتسجّل


def arabic_literals(source):
    out = []
    for match in re.finditer(r'"((?:[^"\\\n]|\\.)*)"', source):
        text = match.group(1)
        if re.search("[ء-ي]", text):
            out.append(text)
    return out


class CustomerFacingStringsTests(SimpleTestCase):
    """أي نص عربي بيطلع للزبون من الكود (مو من الموديل): قوالب السيرفر، الأخطاء، الودجت."""

    def server_strings(self):
        pools = [wv.TRYON_BUTTON_LABELS["ar"], wv.FALLBACK_DISCOUNT_REPLY, wv.DISCOUNT_APPENDIX,
                 wv.PURCHASE_REPLY, wv.FALLBACK_REPHRASE_REPLY, wv.SIZE_REPLY["ar"], guard.SAFE_REPLY]
        out = []
        for pool in pools:
            out.extend(pool.values() if isinstance(pool, dict) else [pool])
        out.append(wv.SIZE_REPLY["ar"]["fit"])
        return [s for s in out if isinstance(s, str) and s]

    def test_server_templates_are_saudi(self):
        strings = self.server_strings()
        self.assertGreater(len(strings), 10)
        for text in strings:
            self.assertEqual(saudi.find_non_saudi(text), [], text)

    def test_widget_ui_texts_nudges_and_errors_are_saudi(self):
        literals = arabic_literals(EMBED_JS.read_text(encoding="utf-8"))
        self.assertGreater(len(literals), 30)  # تأكد إنه فعلاً لقى النصوص
        for text in literals:
            self.assertEqual(saudi.find_non_saudi(text), [], text)

    def test_prompts_carry_the_same_saudi_rules(self):
        for prompt in (wv.SYSTEM_PROMPT_TEMPLATE.replace("{saudi_block}", saudi.STYLE_BLOCK),):
            self.assertIn("professional Saudi salesperson", prompt)
            self.assertIn("NEVER Syrian/Levantine", prompt)
            self.assertIn("NEVER stiff Modern Standard Arabic", prompt)
        # المفردات المقترحة نفسها لازم تكون سعودية 100%
        suggested = saudi.STYLE_BLOCK.split("Typical, natural words and phrases")[1].split("- NEVER Syrian")[0]
        self.assertEqual(saudi.find_non_saudi(suggested), [])
        # وقائمة الممنوع لازم تغطي كل كلمة بكاشف الكود
        forbidden = saudi.STYLE_BLOCK.split("- NEVER Syrian/Levantine:")[1].split("NEVER Egyptian")[0]
        for word in ("شو", "هلق", "كتير", "بدي", "منيح", "هيك", "لسا", "مشان", "هاد"):
            self.assertIn(word, forbidden)
            self.assertIn(word, saudi.find_non_saudi(word))


class SaudiConversationTests(V2Base):
    def clean(self, text):
        return saudi.find_non_saudi(text) == []

    def test_syrian_customer_is_understood_and_answered_in_saudi_across_turns(self):
        turns = [
            ("مرحبا، بدي فستان للعرس", res("يا هلا والله! عرس؟ مبروك مقدّماً. وش اللون اللي تفضّله؟"),
             {"preferences": [pref("occasion", "عرس", "فستان للعرس")]}),
            ("كتير غالي، شو في أرخص؟", res("", [rec("RED", "هذا أخف على جيبك وشكله فخم")]),
             {"objections": [{"type": "price", "evidence": "كتير غالي"}]}),
            ("ميزانيتي ما تزيد عن 150", res("أبشر، هذا بالضبط ضمن ميزانيتك."),
             analysis_budget(150, "ميزانيتي ما تزيد عن 150")),
            ("هاي حلوة، بدي ياها", res("", [rec("RED", "تطلبه الحين من الزر تحت وتستاهله")]),
             {"reaction": {"sku": "RED", "type": "liked", "evidence": "هاي حلوة"}}),
        ]
        replies = []
        for text, reply, analysis in turns:
            self.brain.turn(reply, analysis=analysis)
            data = self.w.say(text)
            self.assertTrue(data["ok"], data)
            replies.append(data)
        self.assertEqual(len(self.brain.seller_calls), 4)  # ولا إعادة كتابة لهجة: ردود الموديل كانت سعودية
        for data in replies:
            self.assertTrue(self.clean(data["reply"]), data["reply"])
            for card in data["recommendations"]:
                self.assertTrue(self.clean(card["reason"]) and self.clean(card["tryon_button_label"]))
        # المحلّل شاف كلام الزبون الشامي حرفياً (مو مترجم)
        self.assertIn("بدي فستان للعرس", self.brain.analyzer_calls[0]["messages"][0]["content"])
        # والذاكرة اتحدّثت من كلامه الشامي
        self.assertEqual([f.value for f in self.facts(status="active", key="occasion")], ["عرس"])
        self.assertEqual([f.value for f in self.facts(status="active", key="budget_max")], ["150"])
        self.assertEqual(self.brain.seller_calls[-1]["system"].count("SAUDI ARABIC — mandatory"), 1)

    def test_a_syrian_slip_is_regenerated_once_with_the_offending_words_named(self):
        self.brain.turn(
            res("هلق شو بدك؟ عندي كتير فساتين حلوة"),
            res("يا هلا والله! وش تدور عليه؟ عندي فساتين تجنّن."),
        )
        data = self.w.say("هلا")
        self.assertEqual(data["reply"], "يا هلا والله! وش تدور عليه؟ عندي فساتين تجنّن.")
        correction = self.brain.seller_calls[1]["system"].split("CORRECTION")[-1]
        for word in ("هلق", "شو", "بدك", "كتير"):
            self.assertIn(word, correction)
        self.assertEqual(len(self.brain.seller_calls), 2)
        from .models import TurnTrace

        self.assertEqual(TurnTrace.objects.get().guard["guard_first"], ["dialect"])

    def test_a_second_slip_is_fixed_deterministically_everywhere_customers_look(self):
        slip = res("هلق شو بدك؟ هاد الفستان كتير حلو", [rec("RED", "هيك أحسن لإلك")])
        self.brain.turn(slip, res("هلق شو بدك؟ هاد الفستان كتير حلو", [rec("RED", "هيك أحسن لإلك")]))
        data = self.w.say("أبي فستان")
        self.assertEqual(len(self.brain.seller_calls), 2)  # إعادة محاولة وحدة بس
        self.assertTrue(self.clean(data["reply"]), data["reply"])
        card = data["recommendations"][0]
        self.assertTrue(self.clean(card["reason"]) and self.clean(card["tryon_button_label"]))
        self.assertIn("الحين وش تبي", data["reply"])
        from .models import TurnTrace

        self.assertIn("هلق", TurnTrace.objects.get().guard["dialect_substituted"])

    def test_saudi_end_to_end_with_discount_size_and_guard_all_clean(self):
        from .models import DiscountCode

        DiscountCode.objects.create(merchant=self.merchant, code="SAVE10", description="خصم 10% على كل المنتجات")
        flow = [
            ("هلا", res("يا هلا والله فيك! وش تدور عليه؟"), {}),
            ("أبي فستان لعرس", res("", [rec("GREEN", "فستان أخضر مرّة يجنّن على عرس")]), {}),
            ("غالي شوي", res("", [offer("SAVE10", "معك حق، بس القماش يعيش سنين")]),
             {"objections": [{"type": "price", "evidence": "غالي شوي"}]}),
            ("طولي 165 ووزني 60 وش مقاسي؟",
             res("", [{"name": "recommend_size", "input": {"sku": "GREEN", "height_cm": 165, "weight_kg": 60}}]), {}),
        ]
        out = []
        for text, reply, analysis in flow:
            self.brain.turn(reply, analysis=analysis)
            out.append(self.w.say(text))
        for data in out:
            self.assertTrue(data["ok"], data)
            self.assertTrue(self.clean(data["reply"]), data["reply"])
        self.assertEqual(out[2]["discount_offer"]["code"], "SAVE10")
        self.assertIn("وعشان نسهّلها عليك", out[2]["reply"])        # قالب الخصم سعودي
        self.assertIn("مقاسك", out[3]["reply"])                       # جملة المقاس من الكود، سعودية
        self.assertEqual(guard.find_violations(out[2]["reply"], guard.build_context(
            products=list(self.merchant.products.all()), merchant_codes=["SAVE10"],
            approved_offer=out[2]["discount_offer"])), [])

    def test_english_customers_are_not_subjected_to_the_arabic_dialect_check(self):
        self.brain.turn(res("Hello! What are you looking for today?"))
        data = self.w.say("hi there")
        self.assertEqual(data["reply"], "Hello! What are you looking for today?")
        self.assertEqual(len(self.brain.seller_calls), 1)
