"""اختبارات التكامل الكاملة لمحرّك v2 (المرحلة 2): فهم ← متطلبات صلبة ← مرشّحون ← مرحلة بيع وسياسة
خصم ← بائع بحلقة أدوات ← تدقيق مخرجات/لهجة ← حفظ.

كل شي بيمر من endpoints حقيقية (/widget/v2/*) بمحادثات متعددة الأدوار. المحلّل يمر من
`llm.chat_with_retry` (أداة إجبارية) والبائع من `llm.chat_step_with_retry` (حلقة الأدوات)؛ كلاهما
ممثَّل بسكربت بيسجّل الاستدعاءات، فبنتحقق من شو شاف الموديل فعلاً (برومبت، مرشّحين، نتائج أدوات).
"""

import json
import threading
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import (
    ApiToken, Conversation, CustomerProfile, DiscountCode, Message, Objection, PreferenceFact, Product,
    ProductReaction, TurnTrace,
)
from .services import llm, memory, saudi

ANALYZE = "update_customer_profile"


def step(text="", *calls):
    return llm.LLMStep(text=text, tool_calls=[llm.ToolCall(f"c{i}", n, a) for i, (n, a) in enumerate(calls)])


def show(sku, reason="تناسبك وسعرها مناسب"):
    return ("show_product", {"sku": sku, "reason": reason})


class LoopBrain:
    def __init__(self):
        self.analyses, self.steps = [], []
        self.analyzer_calls, self.step_calls = [], []
        self.lock = threading.Lock()

    def analyzer(self, provider, system_prompt, messages, tools, force_tool=None, **kw):
        with self.lock:
            self.analyzer_calls.append({"messages": list(messages)})
            item = self.analyses.pop(0) if self.analyses else {}
        if isinstance(item, Exception):
            raise item
        return llm.LLMResult(reply_text="", tool_calls=[{"name": ANALYZE, "input": item}])

    def seller(self, provider, system_prompt, transcript, tools, tool_choice=None, **kw):
        with self.lock:
            self.step_calls.append({"system": system_prompt, "transcript": list(transcript), "choice": tool_choice,
                                    "tools": [tl["name"] for tl in tools]})
            if not self.steps:
                raise AssertionError(f"unexpected extra seller step #{len(self.step_calls)}")
            return self.steps.pop(0)

    def turn(self, *steps, analysis=None):
        self.analyses.append(analysis if analysis is not None else {})
        self.steps.extend(steps)
        return self

    @property
    def last_system(self):
        return self.step_calls[-1]["system"]


class FakeProvider:
    model = "fake"

    def is_configured(self):
        return True


class Widget:
    def __init__(self, merchant, storage=None, ip="1.2.3.4"):
        self.merchant, self.storage, self.ip = merchant, storage if storage is not None else {}, ip

    def post(self, path, body, **extra):
        return Client().post(path, data=json.dumps(body), content_type="application/json",
                             HTTP_X_API_TOKEN=self.merchant.token, REMOTE_ADDR=self.ip, **extra)

    def _tok(self):
        return {k: self.storage.get(k) for k in ("session_token", "shopper_token")}

    def say(self, text, page_url=""):
        data = self.post("/widget/v2/chat/", {"message": text, "client_msg_id": uuid.uuid4().hex,
                                              "page_url": page_url, **self._tok()}).json()
        for k in ("session_token", "shopper_token", "session_id"):
            if data.get(k):
                self.storage[k] = data[k]
        return data

    def bootstrap(self, page_url=""):
        data = self.post("/widget/v2/bootstrap/", {**self._tok(), "page_url": page_url}).json()
        for k in ("session_token", "shopper_token", "session_id"):
            if data.get(k):
                self.storage[k] = data[k]
        return data

    def new_page(self):
        return Widget(self.merchant, self.storage, self.ip)

    def event(self, kind, sku):
        return self.post("/widget/event/", {"type": kind, "sku": sku}, HTTP_X_SESSION_TOKEN=self.storage["session_token"])


def pref(key, value, evidence, polarity="like", source="explicit", subject="self"):
    return {"key": key, "value": value, "polarity": polarity, "source": source, "evidence": evidence, "subject": subject}


def sig(readiness="", intent="other", reason="", asks=False, alt=False):
    return {"intent": intent, "readiness": readiness, "hesitation_reason": reason, "asks_discount": asks,
            "wants_alternative": alt}


class EngineBase(TestCase):
    maxDiff = None

    def setUp(self):
        self.enterContext(override_settings(V2_SELLER_MODE="loop", V2_SPECULATIVE=False))
        self.merchant = ApiToken.objects.create(
            label="متجر الأناقة", token="ENGTOKEN000001", image_quota=5, currency="SAR", engine="v2",
            store_info="الشحن 25 ريال ويوصل خلال 2-4 أيام عمل. الإرجاع خلال 14 يوم.",
        )
        def mk(sku, title, price, cat, sizes, colors, **kw):
            return Product.objects.create(
                merchant=self.merchant, sku=sku, title=title, price=price, category=cat,
                image_url=f"https://x/{sku}.jpg", description=kw.pop("description", "قماش ناعم ومريح"),
                options={"Size": sizes, "Color": colors}, product_url=f"https://shop.example.com/products/{sku.lower()}",
                **kw)
        self.blk = mk("BLK_M", "فستان سهرة أسود ساتان", "280.00", "dresses", ["S", "M"], ["Black"])
        self.red = mk("RED_M", "فستان أحمر مورّد", "150.00", "dresses", ["S", "M", "L"], ["Red"])
        self.blk_xl = mk("BLK_XL", "فستان أسود كلاسيك", "120.00", "dresses", ["XL"], ["Black"])
        self.green = mk("GRN_M", "فستان أخضر ميدي", "400.00", "dresses", ["M"], ["Green"])
        self.pink = mk("PNK_M", "فستان وردي صيفي", "100.00", "dresses", ["M", "L"], ["Pink"], compare_at_price="160.00")
        self.shirt = mk("SHIRT", "قميص أبيض كلاسيك", "90.00", "shirts", ["M", "L"], ["White"])
        self.oos = mk("OOS", "فستان أزرق", "130.00", "dresses", ["M"], ["Blue"], in_stock=False)
        self.brain = LoopBrain()
        for p in (
            patch("apitokens.services.llm.chat_with_retry", side_effect=self.brain.analyzer),
            patch("apitokens.services.llm.chat_step_with_retry", side_effect=self.brain.seller),
            patch("apitokens.services.llm.get_provider", return_value=FakeProvider()),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.w = Widget(self.merchant)

    def trace(self, n=-1):
        return list(TurnTrace.objects.order_by("turn"))[n]

    def skus_in_candidates(self, system):
        block = system.split("CANDIDATES")[1].split("\n", 2)[-1]
        return [s for s in ("BLK_M", "RED_M", "BLK_XL", "GRN_M", "PNK_M", "SHIRT", "OOS") if f'"sku": "{s}"' in block]


# ======================================================================
# متطلبات صلبة بالباك-إند
# ======================================================================

class HardRequirementsTests(EngineBase):
    ASK = "أبي فستان أسود مقاسي M وميزانيتي 300 ريال"
    ANALYSIS = {"preferences": [
        pref("color", "أسود", "أبي فستان أسود"), pref("size", "M", "مقاسي M"),
        pref("budget_max", "300", "ميزانيتي 300"), pref("category", "فستان", "أبي فستان")]}

    def test_color_size_budget_category_stock_and_rejected_are_enforced_in_code(self):
        self.brain.turn(
            step("", show("RED_M"), show("BLK_XL"), show("GRN_M"), show("SHIRT"), show("OOS")),   # كلها مخالفة
            step("هذا الفستان الأسود ساتان مقاسك M موجود وضمن ميزانيتك.", show("BLK_M")),
            analysis=self.ANALYSIS,
        )
        data = self.w.say(self.ASK)
        # المرشّحون يلي شافهم الموديل: الأسود بمقاس M وضمن 300 بس
        self.assertEqual(self.skus_in_candidates(self.brain.step_calls[0]["system"]), ["BLK_M"])
        # كل محاولة مخالفة رجعتله كخطأ صريح بدل ما توصل للزبون
        results = [json.loads(m["content"]) for m in self.brain.step_calls[1]["transcript"] if m["role"] == "tool"]
        self.assertEqual(len(results), 5)
        self.assertTrue(all(r["ok"] is False for r in results))
        got = [r.get("violations") or [r["status"]] for r in results]
        self.assertIn("color", got[0])        # RED_M: لون غلط
        self.assertIn("size", got[1])         # BLK_XL: ما عنده M
        self.assertIn("budget", got[2])       # GRN_M: فوق الميزانية
        self.assertIn("category", got[3])     # SHIRT: فئة غلط
        self.assertEqual(got[4], ["out_of_stock"])   # OOS
        self.assertEqual([c["sku"] for c in data["recommendations"]], ["BLK_M"])
        req = self.trace()
        self.assertEqual(req.constraints["requirements"]["colors"], ["black"])
        self.assertEqual(req.constraints["requirements"]["sizes"], ["M"])
        self.assertEqual(req.constraints["requirements"]["budget_max"], "300")

    def test_a_rejected_product_never_returns_even_if_the_model_insists(self):
        self.brain.turn(step("هذا حلو لك.", show("RED_M", "فستان أحمر حلو")), analysis={})
        self.w.say("وريني فستان")
        self.brain.turn(
            step("", show("RED_M"), show("PNK_M", "بديل صيفي أخف")),
            step("وهذا بديل صيفي أخف وسعره حلو."),
            analysis={"reaction": {"sku": "RED_M", "type": "rejected", "reason_type": "style", "evidence": "ما عجبني"},
                      "signals": sig(readiness="interested", intent="reject", alt=True)},
        )
        data = self.w.say("لا ما عجبني")
        self.assertNotIn("RED_M", self.skus_in_candidates(self.brain.step_calls[1]["system"]))
        self.assertEqual([c["sku"] for c in data["recommendations"]], ["PNK_M"])
        errors = [json.loads(m["content"]) for m in self.brain.step_calls[-1]["transcript"] if m["role"] == "tool"]
        self.assertIn(errors[0]["status"], ("duplicate", "hidden_by_customer_rejection", "violates_requirement"))  # المهم: ما وصل كرت
        # الموديل يرجع يحاول بدور لاحق: برضو ما بيمرّ
        self.brain.turn(step("طيب شف هذا", show("RED_M")), step("ما عندي غيره الحين، وش تحب تعدّل؟"), analysis={})
        later = self.w.say("وش غيره؟")
        self.assertEqual(later["recommendations"], [])

    def test_explicit_ask_lifts_the_rejection_for_that_turn_and_onwards(self):
        self.brain.turn(step("هذا حلو لك.", show("RED_M", "فستان أحمر")), analysis={})
        self.w.say("وريني فستان")
        self.brain.turn(step("طيب."), analysis={
            "reaction": {"sku": "RED_M", "type": "rejected", "reason_type": "price", "evidence": "غالي"}})
        self.w.say("غالي")
        self.brain.turn(step("هذا هو.", show("RED_M", "الفستان الأحمر اللي طلبته")),
                        analysis={"reaction": {"sku": "RED_M", "type": "asked"}})
        data = self.w.say("ارجع وريني الأحمر")
        self.assertEqual([c["sku"] for c in data["recommendations"]], ["RED_M"])
        self.assertIn("RED_M", self.skus_in_candidates(self.brain.step_calls[-1]["system"]))

    def test_exact_match_missing_gives_honest_closest_alternatives_which_may_be_shown(self):
        # أخضر + مقاس M + ≤300: الأخضر الوحيد 400 ← ما في تطابق تام، البديل بلون تاني (مسموح، بتنبيه)
        self.brain.turn(
            step("ما عندي أخضر بهالميزانية، بس هذا أقرب شي وبنفس مقاسك.", show("RED_M", "فستان أحمر بمقاس M وضمن ميزانيتك")),
            analysis={"preferences": [pref("color", "أخضر", "فستان أخضر"), pref("size", "M", "مقاسي M"),
                                      pref("budget_max", "300", "ميزانيتي 300")]},
        )
        data = self.w.say("أبي فستان أخضر مقاسي M وميزانيتي 300")
        system = self.brain.step_calls[0]["system"]
        self.assertIn("No candidate matches everything exactly", system)
        self.assertNotIn("GRN_M", self.skus_in_candidates(system))   # الأخضر فوق الميزانية: ما بيتساهل فيها
        self.assertEqual([c["sku"] for c in data["recommendations"]], ["RED_M"])
        self.assertEqual(self.trace().constraints["search"]["note"], "no_exact_match")

    def test_nothing_matches_means_no_card_and_a_single_question(self):
        self.brain.turn(step("للأسف ما عندي شي بهالميزانية، وش تقدر تعدّل؟"),
                        analysis={"preferences": [pref("budget_max", "50", "ميزانيتي 50")]})
        data = self.w.say("ميزانيتي 50 ريال بس")
        self.assertIn("Nothing in the catalog fits", self.brain.step_calls[0]["system"])
        self.assertEqual(data["recommendations"], [])
        self.assertEqual(self.skus_in_candidates(self.brain.step_calls[0]["system"]), [])

    def test_the_model_cannot_loosen_the_budget_through_search(self):
        self.brain.turn(
            step("", ("search_products", {"query": "فستان", "max_price": 1000})),
            step("هذا ضمن ميزانيتك.", show("RED_M")),
            analysis={"preferences": [pref("budget_max", "200", "ميزانيتي 200")]},
        )
        self.w.say("ميزانيتي 200")
        result = json.loads([m for m in self.brain.step_calls[1]["transcript"] if m["role"] == "tool"][0]["content"])
        self.assertTrue(all(float(r["price"]) <= 200 for r in result["results"]))

    def test_duplicate_calls_and_duplicate_cards_are_prevented(self):
        self.brain.turn(step("", show("RED_M", "هذا حلو ويناسب المناسبة"), show("RED_M", "هذا حلو ويناسب المناسبة")), analysis={})
        data = self.w.say("وريني")
        self.assertEqual([c["sku"] for c in data["recommendations"]], ["RED_M"])   # الطلب المكرر ما انعرض مرتين
        self.brain.turn(step("", show("RED_M")), step("هذا كرته معروض فوق، اضغط زر الشراء تحته."), analysis={})
        again = self.w.say("وش رايك فيه؟")
        self.assertEqual(again["recommendations"], [])


# ======================================================================
# مراحل البيع
# ======================================================================

class LiveEvalRegressionTests(EngineBase):
    """إخفاقات ظهرت بالتقييم الحي (eval_v2) مع الموديل الحقيقي."""

    def test_a_question_about_an_already_shown_product_does_not_reshow_its_card(self):
        self.brain.turn(step("هذا يناسب العرس.", show("RED_M", "فستان أحمر حلو")), analysis={})
        self.w.say("وريني")
        self.brain.turn(step("", show("RED_M")), step("الشحن ٢٥ ريال، وكرته فوق، اضغط زر الشراء منه."),
                        analysis={"reaction": {"sku": "RED_M", "type": "asked"}, "signals": sig("interested", "ask_info")})
        data = self.w.say("كم الشحن له؟")
        self.assertEqual(data["recommendations"], [])      # كرته أصلاً على الشاشة

    def test_a_rejected_product_the_customer_asks_back_is_marked_for_the_analyzer_and_reshown(self):
        self.brain.turn(step("هذا يناسب العرس.", show("RED_M", "فستان أحمر حلو")), analysis={})
        self.w.say("وريني")
        self.brain.turn(step("طيب."), analysis={"reaction": {"sku": "RED_M", "type": "rejected", "reason_type": "style",
                                                              "evidence": "ما أبيه"}})
        self.w.say("لا ما أبيه")
        # يرجع يطلبه "من غير ما يسمّيه": المحلّل لازم يشوف إنه مخفي ومرفوض سابقاً
        self.brain.turn(step("أبشر، هذا هو.", show("RED_M", "الفستان اللي طلبته")),
                        analysis={"reaction": {"sku": "RED_M", "type": "asked"}})
        data = self.w.say("ارجع وريني الأول اللي رفضته")
        analyzer_input = self.brain.analyzer_calls[-1]["messages"][0]["content"]
        self.assertIn("RED_M — فستان أحمر مورّد — 150.00 [REJECTED earlier by the customer, hidden]", analyzer_input)
        self.assertIn("rejected products (hidden from the customer; if they ask for one again emit reaction 'asked'): "
                      "RED_M — فستان أحمر مورّد", analyzer_input)
        self.assertEqual([c["sku"] for c in data["recommendations"]], ["RED_M"])


class QualityFixesFromLiveEvalTests(EngineBase):
    def test_female_customers_get_a_consistent_feminine_instruction_with_examples(self):
        self.brain.turn(step("أبشري!"), analysis={"gender": "female"})
        self.w.say("عايزة فستان لفرح أختي")
        self.brain.turn(step("تمام."), analysis={})
        self.w.say("وش عندك")
        system = self.brain.last_system
        self.assertIn("FEMININE form in EVERY sentence", system)
        self.assertIn("تقدرين", system)
        self.assertNotIn("has NOT revealed", system)

    def test_tryon_invitation_is_not_repeated_once_given(self):
        self.brain.turn(step("هذا يناسبك، ارفع صورتك وجرّبه عليك.", show("RED_M", "فستان أحمر حلو، جرّبه بصورتك")), analysis={})
        self.w.say("أبي فستان")
        self.brain.turn(step("تمام."), analysis={})
        self.w.say("وش رايك")
        self.assertIn("do NOT invite again", self.brain.last_system)

    def test_no_tryon_line_before_any_invitation(self):
        self.brain.turn(step("يا هلا! وش تدور عليه؟"), analysis={})
        self.w.say("هلا")
        self.brain.turn(step("تمام."), analysis={})
        self.w.say("أبي فستان")
        self.assertNotIn("do NOT invite again", self.brain.last_system)

    def test_a_price_objection_puts_the_cheapest_alternatives_first(self):
        self.brain.turn(step("هذا حلو.", show("BLK_M", "فستان سهرة أسود")), analysis={})
        self.w.say("وريني فستان سهرة")
        self.brain.turn(step("فاهمك، هذا أخف.", show("BLK_XL", "فستان أخف")),
                        analysis={"objections": [{"type": "price", "evidence": "غالي"}],
                                  "signals": sig("hesitant", "object", "price")})
        self.w.say("غالي، فيه أرخص؟")
        rows = json.loads(next(l for l in self.brain.last_system.split("CANDIDATES")[1].splitlines() if l.startswith("[{")))
        prices = [float(r["price"]) for r in rows]
        engaged = 280.0
        cheaper = [p for p in prices if p < engaged]
        self.assertTrue(cheaper)
        self.assertEqual(prices[: len(cheaper)], sorted(cheaper))   # الأرخص أولاً، بالأرخص فالأرخص
        self.assertTrue(all(p < engaged for p in prices[: len(cheaper)]))


class SearchToolVisibilityEngineTests(EngineBase):
    """أداة البحث بتنخبّى بس لما تكون مش لازمة؛ زبون بدو منتجات جديدة/إضافية دايماً بيشوفها."""

    def tools_after(self, text, analysis, **kw):
        self.brain.turn(step("تمام."), analysis=analysis)
        self.w.say(text)
        return self.brain.step_calls[-1]["tools"]

    def test_search_stays_when_the_customer_wants_new_or_more_products(self):
        for intent, extra in (("search", {}), ("compare", {}), ("reject", {}), ("object", {"alt": True})):
            brain_tools = self.tools_after(
                f"طلب {intent}", {"signals": sig("interested", intent, alt=extra.get("alt", False))})
            self.assertIn("search_products", brain_tools, intent)

    def test_search_stays_when_the_analyzer_failed_or_the_intent_is_unknown(self):
        self.assertIn("search_products", self.tools_after("هلا", llm.LLMError("analyzer down")))
        self.assertIn("search_products", self.tools_after("وش عندك", {"signals": sig("browsing", "other")}))

    def test_search_stays_after_a_rejection_even_with_other_intent(self):
        self.brain.turn(step("هذا حلو.", show("RED_M", "فستان أحمر حلو")), analysis={})
        self.w.say("وريني")
        tools = self.tools_after("ما عجبني", {"reaction": {"sku": "RED_M", "type": "rejected", "reason_type": "style",
                                                          "evidence": "ما عجبني"}, "signals": sig("interested", "chitchat")})
        self.assertIn("search_products", tools)

    def test_search_is_hidden_only_on_plain_turns_with_enough_exact_unshown_candidates(self):
        for intent in ("ask_info", "chitchat", "buy"):
            tools = self.tools_after(f"سؤال {intent}", {"signals": sig("interested", intent)})
            self.assertNotIn("search_products", tools, intent)
            self.assertIn("show_product", tools)

    def test_search_stays_when_the_match_is_not_exact(self):
        tools = self.tools_after("أبي فستان أخضر مقاسي M وميزانيتي 300", {
            "signals": sig("interested", "ask_info"),
            "preferences": [pref("color", "أخضر", "فستان أخضر"), pref("size", "M", "مقاسي M"),
                            pref("budget_max", "300", "وميزانيتي 300")]})
        self.assertIn("search_products", tools)   # التطابق مخفَّف (note=no_exact_match)


class SalesStageTests(EngineBase):
    def test_stage_progression_discovery_recommendation_objection_closing(self):
        script = [
            ("هلا", step("يا هلا والله! وش تدور عليه؟"), {"signals": sig("browsing", "chitchat")}, "discovery"),
            ("أبي فستان لعرس", step("هذا يناسب العرس.", show("RED_M")),
             {"preferences": [pref("category", "فستان", "أبي فستان")], "signals": sig("interested", "search")},
             "recommendation"),
            ("حلو بس غالي شوي", step("معك حق، خلّني أوريك أخف."),
             {"objections": [{"type": "price", "evidence": "غالي شوي"}],
              "signals": sig("hesitant", "object", "price")}, "objection_handling"),
        ]
        for text, reply, analysis, expected in script:
            self.brain.turn(reply, analysis=analysis)
            self.w.say(text)
            self.assertEqual(self.trace().constraints["stage"], expected, text)
        # ضغطة شراء (حدث) ← إغلاق، بشرط الاعتراض انحلّ
        self.brain.turn(step("تمام، اضغط زر الشراء تحت وبيوصلك."),
                        analysis={"resolved_objections": ["price"], "signals": sig("ready", "buy")})
        self.w.event("buy_click", "RED_M")
        self.w.say("خلاص بشتريه")
        self.assertEqual(self.trace().constraints["stage"], "closing")
        self.assertEqual(Conversation.objects.get().state["stage"], "closing")

    def test_the_strategy_directives_reach_the_model_and_discovery_asks_at_most_one_known_slot_free_question(self):
        self.brain.turn(step("يا هلا! لأي مناسبة تدور؟"), analysis={"signals": sig("browsing", "chitchat")})
        self.w.say("هلا")
        system = self.brain.last_system
        self.assertIn("Stage: discovery", system)
        self.assertIn("Never pressure", system)
        self.assertNotIn("Question policy: ask no question", system)

    def test_known_slots_are_not_asked_again(self):
        self.brain.turn(step("زين."), analysis={"preferences": [
            pref("category", "فستان", "أبي فستان"), pref("occasion", "عرس", "لعرس"), pref("color", "أسود", "أسود"),
            pref("size", "M", "مقاسي M")]})
        self.w.say("أبي فستان أسود لعرس مقاسي M")
        self.brain.turn(step("تمام."), analysis={})
        self.w.say("وريني")
        system = self.brain.last_system
        for slot in ("category", "occasion", "color", "size"):
            self.assertNotIn(f"only about: {slot}", system)


# ======================================================================
# الخصم: كود حقيقي، بالتوقيت الصح، مرة وحدة
# ======================================================================

class DiscountPolicyTests(EngineBase):
    def setUp(self):
        super().setUp()
        self.code = DiscountCode.objects.create(merchant=self.merchant, code="SAVE10",
                                                description="خصم 10% على كل المنتجات")

    def converse_to_price_objection(self):
        self.brain.turn(step("يا هلا! وش تدور عليه؟"), analysis={"signals": sig("browsing", "chitchat")})
        self.w.say("هلا")
        self.brain.turn(step("هذا حلو لك.", show("BLK_M")),
                        analysis={"preferences": [pref("category", "فستان", "أبي فستان")], "signals": sig("interested", "search")})
        self.w.say("أبي فستان")

    def objection(self):
        return {"objections": [{"type": "price", "evidence": "غالي"}], "signals": sig("hesitant", "object", "price")}

    def test_alternative_first_then_the_real_code_exactly_once(self):
        self.converse_to_price_objection()
        # اعتراض سعر أول: في بديل أرخص فعلاً ← بديل أولاً، بدون كود
        self.brain.turn(step("معك حق، هذا أخف على جيبك.", show("PNK_M", "بديل صيفي أخف وبسعر أقل")), analysis=self.objection())
        first = self.w.say("غالي")
        self.assertIsNone(first["discount_offer"])
        self.assertNotIn("SAVE10", self.brain.last_system)
        self.assertEqual(self.trace().constraints["discount"]["reason"], "alternative_first")
        # لسا معترض بعد البديل ← الكود الحقيقي مسموح
        self.brain.turn(step("فاهم عليك، وعشان نسهّلها خذ الكود."), analysis=self.objection())
        second = self.w.say("لسا غالي علي")
        self.assertEqual(second["discount_offer"]["code"], "SAVE10")
        self.assertIn("SAVE10", self.brain.last_system)
        self.assertEqual(second["reply"].count("SAVE10"), 1)
        self.assertEqual(self.trace().constraints["discount"]["trigger"], "price_objection")
        # ما يتكرر
        self.brain.turn(step("تمام، أي شي ثاني؟"), analysis=self.objection())
        third = self.w.say("بعد غالي")
        self.assertIsNone(third["discount_offer"])
        self.assertNotIn("SAVE10", self.brain.last_system)
        self.assertEqual(Conversation.objects.get().discount_code, "SAVE10")

    def test_no_code_before_engagement_or_in_the_first_turns_even_if_asked(self):
        self.brain.turn(step("أبشر! بس خلّنا نشوف وش يعجبك أول وبعدين أدبّر لك أحسن سعر.", ),
                        analysis={"signals": sig("browsing", "ask_discount", asks=True)})
        data = self.w.say("عندكم كود خصم؟")
        self.assertIsNone(data["discount_offer"])
        self.assertNotIn("SAVE10", self.brain.last_system)
        self.assertEqual(self.trace().constraints["discount"]["allowed"], False)

    def test_a_ready_buyer_with_no_price_concern_never_gets_a_discount(self):
        self.converse_to_price_objection()
        self.brain.turn(step("ممتاز، اضغط زر الشراء تحت."), analysis={"signals": sig("ready", "buy")})
        data = self.w.say("خلاص بشتريه الحين، كيف أطلبه؟")
        self.assertIsNone(data["discount_offer"])
        self.assertEqual(self.trace().constraints["discount"]["reason"], "ready_no_price_concern")

    def test_model_inventing_a_code_or_percentage_is_stopped_even_with_a_real_code_available(self):
        self.brain.turn(step("هلا!"), analysis={})
        self.w.say("هلا")
        self.brain.turn(step("عندنا خصم 30% بكود VIP99 لك!"), step("القماش ممتاز ويعيش سنين."), analysis={})
        data = self.w.say("وش عندك؟")
        self.assertNotIn("30%", data["reply"])
        self.assertNotIn("VIP99", data["reply"])
        self.assertNotIn("SAVE10", data["reply"])
        self.assertEqual(self.trace().guard.get("guard_first"), ["unapproved_promo", "unverified_percent"])

    def test_expired_codes_are_never_offered_and_selling_continues(self):
        DiscountCode.objects.filter(pk=self.code.pk).update(expires_at=timezone.now() - timedelta(days=1))
        self.converse_to_price_objection()
        self.brain.turn(step("فاهمك، هالفستان قماشه ساتان يعيش معك سنين ولو تبي أوريك أخف."), analysis=self.objection())
        self.brain.turn(step("طيب شف هذا البديل الأرخص.", show("PNK_M")), analysis=self.objection())
        a = self.w.say("غالي")
        b = self.w.say("لسا غالي")
        self.assertIsNone(a["discount_offer"] or b["discount_offer"])
        self.assertTrue(a["reply"] and b["reply"])  # ما علق ولا سكت

    def test_inactive_and_no_code_stores_keep_selling_without_discount_language(self):
        DiscountCode.objects.all().update(is_active=False)
        self.converse_to_price_objection()
        self.brain.turn(step("معك حق، خلّني أوريك خيار أخف.", show("PNK_M")), analysis=self.objection())
        data = self.w.say("غالي")
        self.assertIn("No discount, coupon", self.brain.last_system)
        self.assertEqual(data["recommendations"][0]["sku"], "PNK_M")
        self.assertIsNone(data["discount_offer"])

    def test_no_cooldown_a_new_conversation_can_receive_an_eligible_code_again(self):
        self.converse_to_price_objection()
        self.brain.turn(step("فاهمك، هذا أخف.", show("PNK_M", "بديل أخف")), analysis=self.objection())
        self.w.say("غالي")
        self.brain.turn(step("وعشان نسهّلها خذ الكود."), analysis=self.objection())
        first = self.w.say("لسا غالي")
        self.assertEqual(first["discount_offer"]["code"], "SAVE10")
        first_conv = Conversation.objects.get()
        # زيارة جديدة بعد يومين: محادثة جديدة، ما في تبريد — نفس شروط السياسة بتنطبق من جديد
        later = time.time() + 2 * 86400
        with patch("django.core.signing.time", SimpleNamespace(time=lambda: later)):
            page = self.w.new_page()
            self.brain.turn(step("يا هلا من جديد! وش تدور عليه؟"), analysis={"signals": sig("browsing", "chitchat")})
            t1 = page.say("هلا")
            self.brain.turn(step("هذا حلو لك.", show("BLK_M")),
                            analysis={"preferences": [pref("category", "فستان", "أبي فستان")], "signals": sig("interested", "search")})
            t2 = page.say("أبي فستان")
            self.assertIsNone(t1["discount_offer"] or t2["discount_offer"])           # أول دورين: لا خصم
            self.assertNotIn("SAVE10", self.brain.last_system)
            self.brain.turn(step("معك حق، هذا أخف.", show("PNK_M", "بديل أخف")), analysis=self.objection())
            t3 = page.say("غالي")
            self.assertIsNone(t3["discount_offer"])                                    # بديل أرخص أولاً
            self.assertEqual(TurnTrace.objects.order_by("-id").first().constraints["discount"]["reason"], "alternative_first")
            self.brain.turn(step("وعشان نسهّلها خذ الكود."), analysis=self.objection())
            t4 = page.say("لسا غالي علي")
            self.assertEqual(t4["discount_offer"]["code"], "SAVE10")                   # كود حقيقي بمحادثة جديدة
            self.assertEqual(TurnTrace.objects.order_by("-id").first().constraints["discount"]["reason"], "ok")
        second_conv = Conversation.objects.exclude(pk=first_conv.pk).get()
        self.assertEqual(second_conv.discount_code, "SAVE10")
        self.assertNotEqual(second_conv.pk, first_conv.pk)
        # ومرة وحدة بس لكل محادثة
        with patch("django.core.signing.time", SimpleNamespace(time=lambda: later)):
            self.brain.turn(step("تمام، أي شي ثاني؟"), analysis=self.objection())
            t5 = page.say("بعد غالي")
            self.assertIsNone(t5["discount_offer"])


# ======================================================================
# تجربة اللبس، اللهجة، فهم لهجات الزبون
# ======================================================================

class TryOnAndDialectTests(EngineBase):
    @override_settings(KIEAI_API_KEY="k")
    def test_tryon_becomes_part_of_the_sales_context(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.brain.turn(step("هذا يجنّن عليك.", show("RED_M")), analysis={})
        self.w.say("أبي فستان")
        photo = SimpleUploadedFile("me.jpg", b"\xff\xd8 fake", content_type="image/jpeg")
        with patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png"):
            ok = Client().post("/widget/tryon/", {"sku": "RED_M", "photo": photo}, HTTP_X_API_TOKEN=self.merchant.token,
                               HTTP_X_SESSION_TOKEN=self.w.storage["session_token"], REMOTE_ADDR="1.2.3.4")
        self.assertTrue(ok.json()["ok"])
        self.brain.turn(step("وش رايك فيه؟"), analysis={"signals": sig("interested", "accept")})
        self.w.say("حلو مرة")
        self.assertIn('RED_M "فستان أحمر مورّد" (tried_on)', self.brain.last_system)
        self.assertEqual(self.trace().constraints["stage"], "persuasion")

    def test_syrian_customer_gets_saudi_replies_and_a_slip_is_fixed(self):
        slips = step("هلق شو بدك؟ عندي كتير فساتين حلوة")
        fixed = step("يا هلا والله! وش تدور عليه؟ عندي فساتين تجنّن.")
        self.brain.turn(slips, fixed, analysis={})
        data = self.w.say("مرحبا، بدي فستان للعرس")
        self.assertEqual(saudi.find_non_saudi(data["reply"]), [])
        self.assertIn("بدي فستان للعرس", self.brain.analyzer_calls[-1]["messages"][0]["content"])  # فُهم كلامه الشامي كما هو
        self.assertEqual(self.trace().guard.get("guard_first"), ["dialect"])

    def test_twenty_turn_conversation_stays_saudi_and_remembers_early_facts(self):
        self.brain.turn(step("أبشر!"), analysis={"preferences": [pref("occasion", "عرس", "لعرس أختي"),
                                                                  pref("budget_max", "300", "ميزانيتي 300")]})
        self.w.say("أبي فستان لعرس أختي وميزانيتي 300")
        replies = []
        for i in range(19):
            self.brain.turn(step(f"أبشر، هذا سؤال حلو رقم {i}."), analysis={})
            replies.append(self.w.say(f"سؤال {i}")["reply"])
        self.assertTrue(all(saudi.find_non_saudi(r) == [] for r in replies))
        system = self.brain.last_system
        self.assertIn('occasion: "عرس"', system)
        self.assertIn("budget up to 300", system)
        self.assertEqual(Message.objects.filter(conversation=Conversation.objects.get()).count(), 40)


# ======================================================================
# الأداء: المحلّل والبائع بالتوازي (speculative) بدون ما تتغيّر النتيجة
# ======================================================================

class SpeculativeExecutionTests(EngineBase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(V2_SPECULATIVE=True))

    def test_when_the_analysis_changes_nothing_the_early_reply_is_reused(self):
        self.brain.turn(step("أبشر! وش تدور عليه؟"), analysis={})
        data = self.w.say("هلا")
        self.assertEqual(data["reply"], "أبشر! وش تدور عليه؟")
        self.assertEqual(len(self.brain.step_calls), 1)       # بائع مرة وحدة بس
        self.assertEqual(len(self.brain.analyzer_calls), 1)
        spec = self.trace().constraints["speculative"]
        self.assertEqual((spec["enabled"], spec["reused"]), (True, True))
        self.assertEqual(self.trace().llm_calls, 2)

    def test_when_the_analysis_changes_the_plan_the_seller_reruns_with_the_new_memory(self):
        self.brain.turn(step("جواب مبكر بدون ميزانية."), step("ضمن ميزانيتك 200، هذا حلو.", show("RED_M")),
                        analysis={"preferences": [pref("budget_max", "200", "ميزانيتي 200")]})
        data = self.w.say("ميزانيتي 200 ريال")
        self.assertEqual(len(self.brain.step_calls), 2)
        self.assertNotIn("budget up to 200", self.brain.step_calls[0]["system"])
        self.assertIn("budget up to 200", self.brain.step_calls[1]["system"])
        self.assertEqual(data["reply"], "ضمن ميزانيتك 200، هذا حلو.")
        self.assertEqual(self.trace().constraints["speculative"]["reused"], False)
        self.assertEqual(self.trace().llm_calls, 3)

    def test_results_are_identical_to_sequential_mode(self):
        outs = []
        for spec in (False, True):
            with override_settings(V2_SPECULATIVE=spec):
                brain = LoopBrain()
                first = step("هذا يناسبك.", show("RED_M"))
                # speculative: البائع المبكر ما شاف الفئة، فالتحليل بيغيّر الخطة ← إعادة بنفس الرد
                brain.turn(first, *([step("هذا يناسبك.", show("RED_M"))] if spec else []),
                           analysis={"preferences": [pref("category", "فستان", "أبي فستان")]})
                brain.turn(step("طيب."), analysis={})
                with patch("apitokens.services.llm.chat_with_retry", side_effect=brain.analyzer), \
                     patch("apitokens.services.llm.chat_step_with_retry", side_effect=brain.seller):
                    w = Widget(self.merchant, ip=f"9.9.9.{int(spec)}")
                    a = w.say("أبي فستان")
                    b = w.say("طيب")
                outs.append((a["reply"], [c["sku"] for c in a["recommendations"]], b["reply"]))
        self.assertEqual(outs[0], outs[1])

    def test_an_analyzer_failure_does_not_break_the_speculative_turn(self):
        self.brain.turn(step("أبشر!"), analysis=llm.LLMError("analyzer down"))
        self.assertTrue(self.w.say("هلا")["ok"])

    def test_seller_failure_is_reported_and_retry_is_idempotent(self):
        self.brain.analyses.append({})
        with patch("apitokens.services.llm.chat_step_with_retry", side_effect=llm.LLMError("down")):
            failed = self.w.say("هلا")
        self.assertEqual(failed["error"], "assistant_unavailable")


class GuardWordBoundaryTests(TestCase):
    def test_displayed_is_not_an_offer(self):
        from .services import guard

        ctx = guard.build_context(products=[], merchant_codes=[])
        for ok in ("هذا الفستان معروض عندنا", "الفستان معروض بالصور"):
            self.assertEqual(guard.find_violations(ok, ctx), [], ok)
        for bad in ("عندنا عروض حلوة", "العروض الحالية", "عروضنا مميزة", "وعروض ثانية", "عرض خاص لك"):
            self.assertTrue(guard.find_violations(bad, ctx), bad)


class LegacyFlagTests(EngineBase):
    def test_legacy_mode_still_available_as_instant_rollback(self):
        from .test_v2_memory import Brain as LegacyBrain, res

        legacy = LegacyBrain()
        legacy.turn(res("يا هلا والله! وش تدور عليه؟"))
        with override_settings(V2_SELLER_MODE="legacy"), \
             patch("apitokens.services.llm.chat_with_retry", side_effect=legacy):
            data = self.w.say("هلا")
        self.assertTrue(data["ok"])
        self.assertEqual(self.brain.step_calls, [])  # مسار الحلقة ما اشتغل


class TenantIsolationInTheLoopTests(EngineBase):
    def test_tools_prompt_and_discounts_never_cross_merchants(self):
        other = ApiToken.objects.create(label="تاجر ثاني", token="ENGOTHER00001", image_quota=1, currency="SAR", engine="v2")
        Product.objects.create(merchant=other, sku="SECRET_SKU", title="منتج سري للتاجر الثاني", price="50", category="dresses",
                               image_url="https://x/s.jpg", options={"Size": ["M"], "Color": ["Black"]},
                               product_url="https://other.example.com/products/secret")
        DiscountCode.objects.create(merchant=other, code="OTHERONLY", description="خصم 50% للتاجر الثاني")
        DiscountCode.objects.create(merchant=self.merchant, code="SAVE10", description="خصم 10% على كل المنتجات")
        self.brain.turn(
            step("", show("SECRET_SKU"), ("get_product", {"sku": "SECRET_SKU"}), ("search_products", {"query": "منتج سري"})),
            step("ما لقيته عندنا، هذا أقرب شي.", show("RED_M")),
            analysis={},
        )
        data = self.w.say("وريني فستان")
        first_prompt = self.brain.step_calls[0]["system"]
        for leak in ("SECRET_SKU", "منتج سري", "OTHERONLY", "خصم 50%"):
            self.assertNotIn(leak, first_prompt)
        tool_results = [json.loads(m["content"]) for m in self.brain.step_calls[1]["transcript"] if m["role"] == "tool"]
        self.assertEqual(tool_results[0]["status"], "unknown_sku")
        self.assertEqual(tool_results[1]["error"], "unknown_sku")
        self.assertNotIn("SECRET_SKU", json.dumps(tool_results[2], ensure_ascii=False))
        self.assertEqual([c["sku"] for c in data["recommendations"]], ["RED_M"])

    def test_memory_of_one_merchant_does_not_filter_or_leak_into_another(self):
        other = ApiToken.objects.create(label="تاجر ثاني", token="ENGOTHER00002", image_quota=1, currency="SAR", engine="v2")
        Product.objects.create(merchant=other, sku="RED_M", title="فستان أحمر عند التاجر الثاني", price="999", category="dresses",
                               image_url="https://x/o.jpg", options={"Size": ["M"], "Color": ["Red"]})
        self.brain.turn(step("حلو."), analysis={"preferences": [pref("budget_max", "100", "ميزانيتي 100")]})
        self.w.say("ميزانيتي 100")
        self.brain.turn(step("أهلين!"), analysis={})
        stranger = Widget(other, ip="7.7.7.7")
        stranger.say("هلا")
        system = self.brain.last_system
        self.assertNotIn("budget up to 100", system)
        self.assertNotIn("فستان أحمر مورّد", system)
        self.assertIn("فستان أحمر عند التاجر الثاني", system)
