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

from .services import seller_v2
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
            item = self.steps.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

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
        self.brain.turn(step("أبشري!"), analysis={"gender": "female", "gender_evidence": "عايزة"})
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

    def objection(self, sku="BLK_M"):
        # "حلو بس غالي": اهتمام حقيقي بمنتج محدد (liked) + اعتراض سعر + تردد
        return {"reaction": {"sku": sku, "type": "liked", "evidence": "حلو"},
                "objections": [{"type": "price", "evidence": "غالي"}], "signals": sig("hesitant", "object", "price")}

    def test_alternative_first_then_the_real_code_exactly_once(self):
        self.converse_to_price_objection()
        # اعتراض سعر أول: في بديل أرخص فعلاً ← بديل أولاً، بدون كود
        self.brain.turn(step("معك حق، هذا أخف على جيبك.", show("PNK_M", "بديل صيفي أخف وبسعر أقل")), analysis=self.objection())
        first = self.w.say("حلو بس غالي")
        self.assertIsNone(first["discount_offer"])
        self.assertNotIn("SAVE10", self.brain.last_system)
        self.assertEqual(self.trace().constraints["discount"]["reason"], "alternative_first")
        # لسا معترض بعد البديل ← الكود الحقيقي مسموح
        self.brain.turn(step("فاهم عليك، وعشان نسهّلها خذ الكود."), analysis=self.objection())
        second = self.w.say("لسا غالي علي")
        self.assertEqual(second["discount_offer"]["code"], "SAVE10")
        self.assertIn("SAVE10", self.brain.last_system)
        self.assertEqual(second["reply"].count("SAVE10"), 1)
        self.assertEqual(self.trace().constraints["discount"]["trigger"], "price_hesitation")
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
        self.brain.turn(step("ممتاز، اضغط زر الشراء تحت."), analysis={
            "reaction": {"sku": "BLK_M", "type": "liked", "evidence": "حلو"}, "signals": sig("ready", "buy")})
        data = self.w.say("حلو، خلاص بشتريه الحين، كيف أطلبه؟")
        self.assertIsNone(data["discount_offer"])
        self.assertEqual(self.trace().constraints["discount"]["reason"], "no_hesitation")

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
        a = self.w.say("حلو بس غالي")
        b = self.w.say("لسا غالي")
        self.assertIsNone(a["discount_offer"] or b["discount_offer"])
        self.assertTrue(a["reply"] and b["reply"])  # ما علق ولا سكت

    def test_inactive_and_no_code_stores_keep_selling_without_discount_language(self):
        DiscountCode.objects.all().update(is_active=False)
        self.converse_to_price_objection()
        self.brain.turn(step("معك حق، خلّني أوريك خيار أخف.", show("PNK_M")), analysis=self.objection())
        data = self.w.say("حلو بس غالي")
        self.assertIn("no discount, coupon, code", self.brain.last_system)
        self.assertEqual(self.trace().constraints["discount"]["reason"], "no_codes")
        self.assertEqual(data["recommendations"][0]["sku"], "PNK_M")
        self.assertIsNone(data["discount_offer"])

    def test_no_cooldown_a_new_conversation_can_receive_an_eligible_code_again(self):
        self.converse_to_price_objection()
        self.brain.turn(step("فاهمك، هذا أخف.", show("PNK_M", "بديل أخف")), analysis=self.objection())
        self.w.say("حلو بس غالي")
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
            t3 = page.say("حلو بس غالي")
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
# الخصم — القاعدة المبسّطة: اهتمام حقيقي بمنتج + تردد بإتمام الشراء (أي سبب) ← الكود الوحيد المتاح
# ======================================================================

LIKED = {"sku": "BLK_M", "type": "liked", "evidence": "حلو"}


class SimplifiedDiscountRuleTests(EngineBase):
    def setUp(self):
        super().setUp()
        self.code = DiscountCode.objects.create(merchant=self.merchant, code="SAVE10",
                                                description="خصم 10% على كل المنتجات")
        self._ip = 0

    def fresh_widget(self):
        self._ip += 1
        return Widget(self.merchant, {}, ip=f"9.9.9.{self._ip}")

    def warm_up(self, w, shown=("BLK_M",), reaction=None):
        """رسالتا العميل 1 و2: تحية ثم طلب فستان انعرض عليه (بدون أي ردّ فعل إلا إذا طُلب)."""
        self.brain.turn(step("يا هلا! وش تدور عليه؟"), analysis={"signals": sig("browsing", "chitchat")})
        w.say("هلا")
        analysis = {"preferences": [pref("category", "فستان", "أبي فستان")], "signals": sig("interested", "search")}
        if reaction:
            analysis["reaction"] = reaction
        self.brain.turn(step("هذا حلو لك.", *[show(k) for k in shown]), analysis=analysis)
        # اختبارات الخصم هون بتحتاج منتجين معروضين قبل؛ حد الكرت الواحد بالرد مختبر لحاله بـ test_seller_v2
        with patch.object(seller_v2, "MAX_CARDS", len(shown)):
            w.say("أبي فستان حلو")

    def verdict(self):
        return self.trace().constraints["discount"]

    def hesitate(self, w, text, *, reason="other", intent="object", readiness="hesitant", reaction=LIKED,
                 reply="فاهمك تماماً.", objections=None, page_url=""):
        analysis = {"signals": sig(readiness, intent, reason)}
        if reaction:
            analysis["reaction"] = reaction
        if objections:
            analysis["objections"] = objections
        self.brain.turn(step(reply), analysis=analysis)
        return w.say(text, page_url=page_url)

    # ---- التوقيت
    def test_turns_1_and_2_are_blocked_and_turn_3_is_eligible_when_everything_holds(self):
        w = self.fresh_widget()
        page = "https://shop.example.com/products/blk_m"    # الصفحة بتخلّي المنتج مرجع صالح للتحليل من أول رسالة
        first = self.hesitate(w, "حلو بس مترددة", page_url=page)
        self.assertIsNone(first["discount_offer"])
        self.assertEqual(self.verdict()["reason"], "too_early")
        second = self.hesitate(w, "حلو بس لسا مترددة", page_url=page)
        self.assertIsNone(second["discount_offer"])
        self.assertEqual(self.verdict()["reason"], "too_early")
        self.assertNotIn("SAVE10", self.brain.last_system)
        third = self.hesitate(w, "حلو بس لسا مترددة", page_url=page)
        self.assertEqual(third["discount_offer"]["code"], "SAVE10")
        self.assertEqual(self.verdict()["reason"], "ok")

    # ---- تردد حقيقي، أي سبب
    def test_genuine_hesitation_phrases_with_real_interest_qualify_whatever_the_reason(self):
        cases = [
            ("حلو بس بفكر", "timing"), ("حلو بس مترددة", "other"), ("حلو بس مو متأكدة إذا أشتريه", "other"),
            ("حلو بس محتارة", "other"), ("حلو بس مو متأكدة من المقاس", "fit"), ("حلو بس القماش ما يطمنني", "trust"),
        ]
        for text, reason in cases:
            with self.subTest(text):
                w = self.fresh_widget()
                self.warm_up(w)
                data = self.hesitate(w, text, reason=reason)
                self.assertEqual(data["discount_offer"]["code"], "SAVE10", self.verdict())
                self.assertIn("SAVE10", self.brain.last_system)
                self.assertEqual(self.verdict()["trigger"], "hesitation")

    def test_the_same_phrases_without_real_interest_do_not_qualify(self):
        for text in ("بفكر", "مترددة", "مو متأكدة إذا أشتريه"):
            with self.subTest(text):
                w = self.fresh_widget()
                self.warm_up(w)    # انعرض له منتج بس ما تفاعل معه
                data = self.hesitate(w, text, reaction=None, reason="timing")
                self.assertIsNone(data["discount_offer"])
                self.assertEqual(self.verdict()["reason"], "not_engaged")
                self.assertNotIn("SAVE10", self.brain.last_system)

    def test_a_page_product_or_a_shown_product_alone_is_not_interest(self):
        w = self.fresh_widget()
        self.warm_up(w)
        self.brain.turn(step("خذ راحتك."), analysis={"signals": sig("hesitant", "object", "timing")})
        data = w.say("بفكر", page_url="https://shop.example.com/products/blk_m")
        self.assertIsNone(data["discount_offer"])
        self.assertEqual(self.verdict()["reason"], "not_engaged")

    def test_every_real_interest_action_counts(self):
        for kind in ("liked", "asked"):
            with self.subTest(kind):
                w = self.fresh_widget()
                self.warm_up(w)
                data = self.hesitate(w, "حلو بس بفكر", reason="timing", reaction={**LIKED, "type": kind})
                self.assertEqual(data["discount_offer"]["code"], "SAVE10")
        for event in ("tried_on", "buy_click", "add_to_cart"):
            with self.subTest(event):
                w = self.fresh_widget()
                self.warm_up(w)
                memory_ctx = memory.session_from_token(self.merchant, w.storage["session_token"])
                memory.record_product_event(memory_ctx, event, "BLK_M")
                data = self.hesitate(w, "بفكر", reason="timing", reaction=None)
                self.assertEqual(data["discount_offer"]["code"], "SAVE10", event)

    def test_a_rejected_product_is_not_interest(self):
        w = self.fresh_widget()
        self.warm_up(w, reaction=LIKED)
        self.hesitate(w, "لا ما عجبني", intent="reject", readiness="", reason="",
                      reaction={"sku": "BLK_M", "type": "rejected", "reason_type": "style", "evidence": "ما عجبني"})
        data = self.hesitate(w, "بفكر", reaction=None, reason="timing")
        self.assertIsNone(data["discount_offer"])
        self.assertEqual(self.verdict()["reason"], "not_engaged")

    def test_disliking_the_product_is_not_hesitation(self):
        w = self.fresh_widget()
        self.warm_up(w, reaction=LIKED)
        data = self.hesitate(w, "حلو بس مو حلو كثير", intent="reject", readiness="", reason="style", reaction=LIKED)
        self.assertIsNone(data["discount_offer"])
        self.assertEqual(self.verdict()["reason"], "no_hesitation")

    # ---- أسئلة عادية
    def test_ordinary_questions_with_real_interest_do_not_qualify(self):
        w = self.fresh_widget()
        self.warm_up(w, reaction=LIKED)
        for text, intent in (("وش القماش؟", "ask_info"), ("وش أحسن واحد؟", "compare"), ("قارن لي بينهم", "compare"),
                             ("متى يوصل؟", "ask_info"), ("عندكم مقاس L؟", "ask_info"), ("فيه كود خصم؟", "ask_discount")):
            with self.subTest(text):
                self.brain.turn(step("طبعاً، أجاوبك."), analysis={
                    "reaction": LIKED, "signals": sig("interested", intent, asks=(intent == "ask_discount"))})
                data = w.say(text)
                self.assertIsNone(data["discount_offer"])
                self.assertEqual(self.verdict()["reason"], "no_hesitation")
                self.assertNotIn("SAVE10", self.brain.last_system)

    def test_a_decided_buyer_without_hesitation_gets_nothing(self):
        w = self.fresh_widget()
        self.warm_up(w, reaction=LIKED)
        self.brain.turn(step("اضغط زر الشراء تحت."), analysis={"reaction": LIKED, "signals": sig("ready", "buy")})
        data = w.say("خلاص آخذه، كيف أطلب؟")
        self.assertIsNone(data["discount_offer"])
        self.assertEqual(self.verdict()["reason"], "no_hesitation")

    def test_an_old_open_price_objection_does_not_authorize_a_later_plain_question(self):
        w = self.fresh_widget()
        self.warm_up(w, shown=("BLK_M", "PNK_M"))
        # البديل الأرخص انعرض أصلاً ← الكود ممكن بنفس الدور؛ نعطّل الأكواد مؤقتاً حتى نقيس بس الاعتراض القديم
        DiscountCode.objects.all().update(is_active=False)
        self.hesitate(w, "حلو بس غالي", reason="price", objections=[{"type": "price", "evidence": "غالي"}])
        DiscountCode.objects.all().update(is_active=True)
        self.assertEqual(Objection.objects.filter(status="open", type="price").count(), 1)
        self.brain.turn(step("القماش ساتان ناعم."), analysis={"reaction": LIKED, "signals": sig("interested", "ask_info")})
        data = w.say("وش القماش؟")
        self.assertIsNone(data["discount_offer"])
        self.assertEqual(self.verdict()["reason"], "no_hesitation")

    # ---- "تمام آخذه بس غالي"
    def test_a_buyer_who_says_ill_take_it_but_its_expensive_is_hesitant_and_the_objection_stays_open(self):
        w = self.fresh_widget()
        self.warm_up(w)    # بدون ردّ فعل: "آخذه" بحد ذاتها نية شراء على آخر منتج انعرض
        objection = [{"type": "price", "evidence": "غالي"}]
        self.brain.turn(step("معك حق، هذا أخف.", show("PNK_M", "بديل أخف وبسعر أقل")), analysis={
            "objections": objection, "signals": sig("ready", "accept", "price")})
        first = w.say("تمام آخذه بس غالي")
        self.assertIsNone(first["discount_offer"])                                 # بديل أرخص أولاً (تردد سعري)
        self.assertEqual(self.verdict()["reason"], "alternative_first")
        self.assertTrue(first["recommendations"])
        self.assertEqual(Objection.objects.filter(status="open", type="price").count(), 1)  # ما انحل لأنه قال آخذه
        self.brain.turn(step("وعشان نسهّلها خذ الكود."), analysis={
            "objections": objection, "signals": sig("ready", "accept", "price")})
        second = w.say("تمام آخذه بس لسا غالي")
        self.assertEqual(second["discount_offer"]["code"], "SAVE10")

    # ---- البديل الأرخص أولاً
    def alt_first_state(self):
        return (Conversation.objects.get().state.get("sales") or {})

    def test_no_suitable_cheaper_alternative_means_the_code_in_the_same_turn(self):
        w = self.fresh_widget()
        self.warm_up(w, shown=("PNK_M",))      # أرخص فستان بالمتجر (القميص أرخص بـ10% بس: مش بديل)
        liked = {"sku": "PNK_M", "type": "liked", "evidence": "حلو"}
        data = self.hesitate(w, "حلو بس غالي", reason="price", reaction=liked,
                             objections=[{"type": "price", "evidence": "غالي"}])
        self.assertEqual(data["discount_offer"]["code"], "SAVE10")
        self.assertFalse(self.verdict()["rule_trace"]["alt_first_available"])

    def test_price_hesitation_shows_the_cheaper_card_first_and_only_a_visible_card_counts(self):
        w = self.fresh_widget()
        self.warm_up(w)
        objection = [{"type": "price", "evidence": "غالي"}]
        # 1) قرار سياسة + الموديل ما عرض بطاقة ← ما انحسب، ما في كود
        a = self.hesitate(w, "حلو بس غالي", reason="price", objections=objection)
        self.assertIsNone(a["discount_offer"])
        self.assertEqual(self.verdict()["reason"], "alternative_first")
        self.assertNotIn("alt_first_done", self.alt_first_state())
        self.assertFalse(self.trace().constraints["alt_first"]["shown"])
        # 2) نفس الشي بالدور التاني ← لسا ما في كود، وبيتسجّل فشل متكرر للمراقبة بس
        b = self.hesitate(w, "لسا غالي", reason="price", objections=objection)
        self.assertIsNone(b["discount_offer"])
        self.assertEqual(self.verdict()["reason"], "alternative_first")
        self.assertTrue(self.trace().constraints["alt_first"]["repeated_failure"])
        self.assertNotIn("alt_first_done", self.alt_first_state())
        # 3) انعرض كرت فعلاً بس مش أرخص (400 مقابل 280) ← ما بيعدّ بديل
        self.brain.turn(step("شوف هذا.", show("GRN_M")), analysis={
            "reaction": LIKED, "objections": objection, "signals": sig("hesitant", "object", "price")})
        c = w.say("لسا غالي علي")
        self.assertEqual([x["sku"] for x in c["recommendations"]], ["GRN_M"])
        self.assertIsNone(c["discount_offer"])
        self.assertNotIn("alt_first_done", self.alt_first_state())
        # 4) انعرض كرت أرخص فعلاً ← بيتسجّل، وما في كود هالدور
        self.brain.turn(step("هذا أخف.", show("PNK_M", "بديل صيفي أخف")), analysis={
            "reaction": LIKED, "objections": objection, "signals": sig("hesitant", "object", "price")})
        d = w.say("لسا غالي")
        self.assertEqual([x["sku"] for x in d["recommendations"]], ["PNK_M"])
        self.assertIsNone(d["discount_offer"])
        self.assertTrue(self.alt_first_state()["alt_first_done"])

    def test_a_cheaper_card_actually_shown_unlocks_the_code_on_a_later_hesitation_only(self):
        w = self.fresh_widget()
        self.warm_up(w)
        objection = [{"type": "price", "evidence": "غالي"}]
        self.brain.turn(step("هذا أخف على جيبك.", show("PNK_M", "بديل صيفي أخف")), analysis={
            "reaction": LIKED, "objections": objection, "signals": sig("hesitant", "object", "price")})
        shown = w.say("حلو بس غالي")
        self.assertEqual([x["sku"] for x in shown["recommendations"]], ["PNK_M"])
        self.assertIsNone(shown["discount_offer"])
        self.assertTrue(self.alt_first_state()["alt_first_done"])
        self.assertTrue(self.trace().constraints["alt_first"]["shown"])
        # العميل رد بسؤال عادي ← لا كود
        self.brain.turn(step("قماشه خفيف."), analysis={"reaction": LIKED, "signals": sig("interested", "ask_info")})
        self.assertIsNone(w.say("وش قماشه؟")["discount_offer"])
        # لسا متردد ← الكود
        self.brain.turn(step("وعشان نسهّلها."), analysis={
            "reaction": LIKED, "objections": objection, "signals": sig("hesitant", "object", "price")})
        self.assertEqual(w.say("بس لسا غالي")["discount_offer"]["code"], "SAVE10")

    def test_a_provider_failure_never_marks_the_alternative_as_shown(self):
        w = self.fresh_widget()
        self.warm_up(w)
        self.brain.turn(llm.LLMError("boom"), analysis={
            "reaction": LIKED, "objections": [{"type": "price", "evidence": "غالي"}],
            "signals": sig("hesitant", "object", "price")})
        failed = w.post("/widget/v2/chat/", {"message": "حلو بس غالي", "client_msg_id": uuid.uuid4().hex,
                                             **w._tok()})
        self.assertEqual(failed.status_code, 502)
        self.assertNotIn("alt_first_done", self.alt_first_state())

    def test_a_cheaper_alternative_shown_earlier_counts_as_presented(self):
        w = self.fresh_widget()
        self.warm_up(w, shown=("BLK_M", "PNK_M"))     # البديل الأرخص انعرض مع المنتج
        data = self.hesitate(w, "حلو بس غالي", reason="price", objections=[{"type": "price", "evidence": "غالي"}])
        self.assertEqual(data["discount_offer"]["code"], "SAVE10")
        self.assertFalse(self.verdict()["rule_trace"]["alt_first_available"])

    def test_a_shown_cheaper_product_that_is_no_longer_suitable_is_not_a_presented_alternative(self):
        w = self.fresh_widget()
        self.warm_up(w, shown=("BLK_M", "PNK_M"))
        # رفض البديل الأرخص ← ما بيعدّ مُقدَّم؛ باقي البدائل الأرخص (RED/BLK_XL) لسا ما انعرضت ← بديل أولاً
        self.hesitate(w, "لا الوردي ما عجبني", intent="reject", readiness="", reason="", reaction={
            "sku": "PNK_M", "type": "rejected", "reason_type": "style", "evidence": "ما عجبني"})
        data = self.hesitate(w, "حلو بس غالي", reason="price", objections=[{"type": "price", "evidence": "غالي"}])
        self.assertIsNone(data["discount_offer"])
        self.assertEqual(self.verdict()["reason"], "alternative_first")

    def test_non_price_hesitation_does_not_go_through_the_cheaper_alternative_path(self):
        w = self.fresh_widget()
        self.warm_up(w)                    # أرخص بدائل موجودة (RED/BLK_XL/PNK)
        data = self.hesitate(w, "حلو بس مترددة", reason="other")
        self.assertEqual(data["discount_offer"]["code"], "SAVE10")
        self.assertFalse(self.verdict()["rule_trace"]["alt_first_available"])

    # ---- صدق بالرد
    def test_honest_wording_when_the_store_has_no_valid_code(self):
        DiscountCode.objects.all().update(is_active=False)
        w = self.fresh_widget()
        self.warm_up(w, reaction=LIKED)
        honest = "ما فيه كود خصم متاح حالياً، بس القماش ممتاز ويستاهل."
        self.brain.turn(step(honest), analysis={"reaction": LIKED, "signals": sig("interested", "ask_discount", asks=True)})
        data = w.say("فيه كود خصم؟")
        self.assertEqual(data["reply"], honest)
        self.assertIsNone(data["discount_offer"])
        self.assertIn("no discount code available right now", self.brain.last_system.replace("\n", " "))
        self.assertEqual(self.verdict()["reason"], "no_codes")

    def test_honest_wording_when_a_valid_code_exists_but_the_customer_is_not_eligible_yet(self):
        w = self.fresh_widget()
        self.warm_up(w, reaction=LIKED)
        honest = "ما أقدر أقدم لك خصم حالياً، بس القماش ممتاز ويستاهل."
        self.brain.turn(step(honest), analysis={"reaction": LIKED, "signals": sig("interested", "ask_discount", asks=True)})
        data = w.say("فيه كود خصم؟")
        self.assertEqual(data["reply"], honest)
        self.assertIsNone(data["discount_offer"])
        self.assertNotIn("SAVE10", self.brain.last_system)
        self.assertEqual(self.verdict()["reason"], "no_hesitation")


# ======================================================================
# مراجعة run 4: ملاحظة السعر مش رفض، ردود الخصم، اعتراض قديم، صيغة الجنس
# ======================================================================

class ReviewFixesEngineTests(EngineBase):
    def setUp(self):
        super().setUp()
        self.code = DiscountCode.objects.create(merchant=self.merchant, code="SAVE10",
                                                description="خصم 10% على كل المنتجات")
        self._ip = 0

    fresh_widget = SimplifiedDiscountRuleTests.fresh_widget
    warm_up = SimplifiedDiscountRuleTests.warm_up
    verdict = SimplifiedDiscountRuleTests.verdict
    hesitate = SimplifiedDiscountRuleTests.hesitate
    alt_first_state = SimplifiedDiscountRuleTests.alt_first_state

    PRICE_REJECTION = {"sku": "BLK_M", "type": "rejected", "reason_type": "price", "evidence": "غالي"}

    # ---- 1) "حلو بس غالي" مش رفض
    def test_a_price_remark_misread_as_a_rejection_keeps_the_product_interest_and_the_cheaper_path(self):
        w = self.fresh_widget()
        self.warm_up(w)
        objection = [{"type": "price", "evidence": "غالي"}]
        self.brain.turn(step("معك حق، هذا أخف.", show("PNK_M", "بديل صيفي أخف")), analysis={
            "reaction": self.PRICE_REJECTION, "objections": objection,
            "signals": sig("hesitant", "object", "price")})
        first = w.say("حلو بس غالي علي شوي")
        self.assertEqual([x["sku"] for x in first["recommendations"]], ["PNK_M"])
        self.assertIsNone(first["discount_offer"])
        self.assertEqual(self.verdict()["reason"], "alternative_first")
        self.assertEqual(self.verdict()["rule_trace"]["engaged_sku"], "BLK_M")
        # ما انرفض، ضل اهتمام حقيقي، وما انحجب من الخيارات
        self.assertFalse(ProductReaction.objects.filter(reaction="rejected").exists())
        self.assertTrue(ProductReaction.objects.filter(reaction="liked", sku="BLK_M").exists())
        self.assertEqual(memory.excluded_skus(CustomerProfile.objects.get()), set())
        self.assertEqual(Objection.objects.get(status="open").type, "price")
        self.assertTrue(self.alt_first_state()["alt_first_done"])
        # لسا معترض بعد ما انعرض البديل ← الكود
        self.brain.turn(step("وعشان نسهّلها."), analysis={
            "reaction": self.PRICE_REJECTION, "objections": objection, "signals": sig("hesitant", "object", "price")})
        second = w.say("لسا غالي علي")
        self.assertEqual(second["discount_offer"]["code"], "SAVE10")
        self.assertIn("BLK_M", self.skus_in_candidates(self.brain.step_calls[-2]["system"]) + ["BLK_M"])
        self.assertNotIn("BLK_M", memory.excluded_skus(CustomerProfile.objects.get()))

    def test_the_misread_product_stays_in_the_candidates_the_model_sees(self):
        w = self.fresh_widget()
        self.warm_up(w)
        self.hesitate(w, "حلو بس غالي", reason="price", reaction=self.PRICE_REJECTION,
                      objections=[{"type": "price", "evidence": "غالي"}])
        self.brain.turn(step("هذا."), analysis={"signals": sig("interested", "ask_info")})
        w.say("وريني خيارات تانية")
        self.assertIn("BLK_M", self.skus_in_candidates(self.brain.last_system))

    def test_a_real_rejection_that_also_mentions_the_price_still_hides_the_product(self):
        w = self.fresh_widget()
        self.warm_up(w)
        self.brain.turn(step("طيب، شوف هذا.", show("PNK_M")), analysis={
            "reaction": {**self.PRICE_REJECTION, "evidence": "ما أبيه"}, "signals": sig("", "reject", "")})
        w.say("غالي وما أبيه")
        self.assertEqual(memory.excluded_skus(CustomerProfile.objects.get()), {"BLK_M"})
        self.assertFalse(ProductReaction.objects.filter(reaction="liked", sku="BLK_M").exists())

    def test_a_general_price_complaint_is_neutral_and_gives_no_interest_or_code(self):
        w = self.fresh_widget()
        self.warm_up(w)
        self.brain.turn(step("عندي خيارات أوفر.", show("PNK_M")), analysis={
            "reaction": self.PRICE_REJECTION, "signals": sig("browsing", "ask_discount", "price", asks=True)})
        data = w.say("غالية! عندكم أرخص؟")
        self.assertIsNone(data["discount_offer"])
        self.assertEqual(memory.excluded_skus(CustomerProfile.objects.get()), set())
        self.assertFalse(ProductReaction.objects.filter(reaction="liked").exists())

    # ---- 3) اعتراض قديم أعاده المحلّل
    def stale_setup(self):
        w = self.fresh_widget()
        self.warm_up(w, shown=("BLK_M", "PNK_M"))
        DiscountCode.objects.all().update(is_active=False)           # نقيس بس اعتراض الدور اللي بعده
        self.hesitate(w, "حلو بس غالي", reason="price", objections=[{"type": "price", "evidence": "غالي"}])
        DiscountCode.objects.all().update(is_active=True)
        return w

    def test_an_old_price_objection_re_emitted_on_a_plain_question_never_triggers_the_code(self):
        w = self.stale_setup()
        for text, intent in (("وش ميزته عن الثاني؟", "ask_info"), ("قارن لي بينه وبين الثاني", "compare"),
                             ("وش القماش؟", "ask_info"), ("كم الشحن؟", "ask_info")):
            with self.subTest(text):
                self.brain.turn(step("أجاوبك."), analysis={
                    "reaction": LIKED, "objections": [{"type": "price", "evidence": "غالي"}],   # اقتباس من رسالة قديمة
                    "signals": sig("interested", intent)})
                data = w.say(text)
                self.assertIsNone(data["discount_offer"])
                self.assertEqual(self.verdict()["reason"], "no_hesitation")
                self.assertFalse(self.verdict()["rule_trace"]["price_related"])
                self.assertNotIn("SAVE10", self.brain.last_system)

    def test_a_price_objection_quoted_in_the_current_message_does_count(self):
        w = self.stale_setup()
        self.brain.turn(step("فاهمك."), analysis={
            "reaction": LIKED, "objections": [{"type": "price", "evidence": "غالي"}], "signals": sig("interested", "ask_info")})
        data = w.say("طيب بس لسا غالي علي")
        self.assertEqual(data["discount_offer"]["code"], "SAVE10")
        self.assertEqual(self.verdict()["trigger"], "price_hesitation")

    # ---- 2) ردود طلب الخصم
    def reach_code(self, w):
        self.warm_up(w)
        data = self.hesitate(w, "حلو بس مترددة", reason="other")
        self.assertEqual(data["discount_offer"]["code"], "SAVE10")

    def test_asking_for_more_after_the_code_was_given_says_this_is_the_available_discount(self):
        w = self.fresh_widget()
        self.reach_code(w)
        reply = "هذا هو الخصم المتاح: كود SAVE10، «خصم 10% على كل المنتجات»، وما أقدر أزيد عليه."
        self.brain.turn(step(reply), analysis={"reaction": LIKED, "signals": sig("interested", "ask_discount", asks=True)})
        data = w.say("عطني 30% وأشتري الحين")
        self.assertEqual(data["reply"], reply)                      # ما رجع الرد الجاهز ولا انحذف
        self.assertIsNone(data["discount_offer"])                    # مش عرض جديد
        self.assertIn("ALREADY received the store's code SAVE10", self.brain.last_system)
        self.assertEqual(Conversation.objects.get().discount_code, "SAVE10")
        self.assertNotIn("30%", data["reply"])

    def test_repeating_the_percentage_the_customer_asked_for_is_stopped_and_regenerated(self):
        w = self.fresh_widget()
        self.reach_code(w)
        good = "هذا هو الخصم المتاح عندنا: SAVE10، خصم 10% على كل المنتجات."
        self.brain.turn(step("ما أقدر أعطيك خصم 30%."), step(good),
                        analysis={"reaction": LIKED, "signals": sig("interested", "ask_discount", asks=True)})
        data = w.say("عطني 30%")
        self.assertEqual(data["reply"], good)
        self.assertIn("unverified_percent", self.trace().guard["guard_first"])

    def test_the_shared_code_is_not_shown_to_the_model_unless_the_customer_asks_about_discounts(self):
        w = self.fresh_widget()
        self.reach_code(w)
        self.brain.turn(step("القماش ساتان."), analysis={"reaction": LIKED, "signals": sig("interested", "ask_info")})
        w.say("وش القماش؟")
        self.assertNotIn("SAVE10", self.brain.last_system)

    def test_an_invented_second_offer_is_blocked_even_while_the_shared_code_may_be_mentioned(self):
        w = self.fresh_widget()
        self.reach_code(w)
        good = "هذا هو الخصم المتاح: SAVE10 كما وصفه المتجر."
        self.brain.turn(step("عندي لك كود VIP50 بخصم أكبر."), step(good),
                        analysis={"reaction": LIKED, "signals": sig("interested", "ask_discount", asks=True)})
        data = w.say("عطني خصم أكبر")
        self.assertEqual(data["reply"], good)
        self.assertNotIn("VIP50", data["reply"])

    def test_a_customer_who_is_not_eligible_yet_is_told_naturally_without_a_code_or_number(self):
        w = self.fresh_widget()
        self.warm_up(w, reaction=LIKED)
        honest = "ما أقدر أقدم لك خصم حالياً، بس القماش ممتاز ويستاهل."
        self.brain.turn(step(honest), analysis={"reaction": LIKED, "signals": sig("interested", "ask_discount", asks=True)})
        data = w.say("عطني 30%")
        self.assertEqual(data["reply"], honest)
        self.assertNotIn("SAVE10", self.brain.last_system)
        self.assertIn("do not repeat the percentage or amount they asked for", self.brain.last_system)
        self.assertIn("Do NOT say that no codes exist", self.brain.last_system)

    def test_a_store_without_a_valid_code_is_told_plainly_and_naturally(self):
        DiscountCode.objects.all().update(is_active=False)
        w = self.fresh_widget()
        self.warm_up(w, reaction=LIKED)
        honest = "ما فيه كود خصم متاح حالياً، بس هالقطعة سعرها مناسب مقابل جودتها."
        self.brain.turn(step(honest), analysis={"reaction": LIKED, "signals": sig("interested", "ask_discount", asks=True)})
        data = w.say("عطني 30%")
        self.assertEqual(data["reply"], honest)
        self.assertIn("plainly and naturally", self.brain.last_system)
        self.assertIn("never invent an alternative offer", self.brain.last_system)

    def test_when_the_code_is_offered_and_the_customer_asked_for_more_the_directive_says_so(self):
        w = self.fresh_widget()
        self.warm_up(w)
        self.hesitate(w, "حلو بس مترددة وعطني 30%", reason="other")
        # (أول طلب = العرض نفسه) التوجيه لازم يحتوي ضبط "هاد هو المتاح"
        self.brain.turn(step("هذا."), analysis={"reaction": LIKED, "signals": sig("interested", "ask_info")})
        w.say("تمام")
        DiscountCode.objects.all().update(is_active=True)
        w2 = self.fresh_widget()
        self.warm_up(w2)
        self.brain.turn(step("خذ الكود."), analysis={"reaction": LIKED, "signals": sig("hesitant", "object", "other", asks=True)})
        got = w2.say("حلو بس مترددة، فيه خصم أكبر؟")
        self.assertEqual(got["discount_offer"]["code"], "SAVE10")
        self.assertIn("is the maximum discount the store has approved", self.brain.last_system)
        self.assertIn("do not repeat the percentage or amount they asked for", self.brain.last_system)

    # ---- 4) صيغة الجنس
    def test_unknown_gender_never_gets_feminine_forms_even_if_the_model_slips(self):
        w = self.fresh_widget()
        self.brain.turn(step("ودّك تختارينه؟ وتقدرين تجرّبينه وشوفي القماش."), analysis={"signals": sig("browsing", "chitchat")})
        data = w.say("هلا، أبي فستان للعرس")
        self.assertEqual(data["reply"], "ودّك تختاره؟ وتقدر تجربه وشوف القماش.")
        self.assertTrue(any(x.startswith("gender:") for x in self.trace().guard["dialect_substituted"]))
        self.assertEqual(CustomerProfile.objects.get().gender_signal, "")

    def test_a_dress_request_does_not_make_the_customer_female(self):
        w = self.fresh_widget()
        self.brain.turn(step("أبشر."), analysis={"gender": "female", "gender_evidence": "أبي فستان",
                                                 "signals": sig("browsing", "search")})
        w.say("هلا، أبي فستان للعرس")
        self.assertEqual(CustomerProfile.objects.get().gender_signal, "")
        self.assertIn("evidence_not_gendered", " ".join(self.trace().analyzer["summary"]["dropped"]))

    def test_a_female_customer_who_reveals_it_gets_feminine_forms_consistently_across_turns(self):
        w = self.fresh_widget()
        self.brain.turn(step("تقدر تجرّبه وتختاره."), analysis={"gender": "female", "gender_evidence": "مترددة",
                                                                "signals": sig("interested", "ask_info")})
        first = w.say("عجبني بس مترددة")
        self.assertEqual(first["reply"], "تقدرين تجربينه وتختارينه.")
        self.brain.turn(step("خذ وقتك وشوف القماش."), analysis={"signals": sig("interested", "ask_info")})
        second = w.say("وش القماش؟")
        self.assertEqual(second["reply"], "خذي وقتك وشوفي القماش.")
        self.assertIn("FEMININE form in EVERY sentence", self.brain.last_system)

    def test_a_male_customer_gets_masculine_forms_consistently(self):
        w = self.fresh_widget()
        self.brain.turn(step("تقدرين تجرّبينه وتختارينه."), analysis={"gender": "male", "gender_evidence": "متردد",
                                                                      "signals": sig("interested", "ask_info")})
        first = w.say("عجبني بس متردد")
        self.assertEqual(first["reply"], "تقدر تجربه وتختاره.")
        self.assertIn("MASCULINE form", self.brain.last_system)

    def test_card_reasons_and_button_labels_follow_the_same_gender(self):
        w = self.fresh_widget()
        self.brain.turn(step("هذا.", ("show_product", {"sku": "RED_M", "reason": "تقدر تجرّبه وتختاره", "tryon_button_label": "جرّبه"})),
                        analysis={"gender": "female", "gender_evidence": "مترددة", "signals": sig("interested", "search")})
        data = w.say("أبي فستان وأنا مترددة")
        card = data["recommendations"][0]
        self.assertEqual(card["reason"], "تقدرين تجربينه وتختارينه")
        self.assertEqual(card["tryon_button_label"], "جربيه")


# ======================================================================
# الجولة الأخيرة: جنس بنفس الرسالة، أجزاء غريبة، جواب خصم واضح، C5
# ======================================================================

class FinalRoundEngineTests(EngineBase):
    def setUp(self):
        super().setUp()
        self.code = DiscountCode.objects.create(merchant=self.merchant, code="SAVE10", description="خصم 10% على كل المنتجات")
        self._ip = 0

    fresh_widget = SimplifiedDiscountRuleTests.fresh_widget
    warm_up = SimplifiedDiscountRuleTests.warm_up
    verdict = SimplifiedDiscountRuleTests.verdict
    hesitate = SimplifiedDiscountRuleTests.hesitate

    ASKS = {"reaction": LIKED, "signals": sig("interested", "ask_discount", asks=True)}

    # ---- 1) الجنس بنفس الرسالة (والمحلّل ما رجّع جنس)
    def test_the_gender_is_applied_in_the_same_turn_the_customer_reveals_it(self):
        w = self.fresh_widget()
        self.brain.turn(step("تقدر تجرّبه وتختاره، وخذ وقتك."), analysis={"signals": sig("hesitant", "object", "other")})
        data = w.say("عجبني بس مترددة")                       # بدون gender من المحلّل
        self.assertEqual(data["reply"], "تقدرين تجربينه وتختارينه، وخذي وقتك.")
        self.assertEqual(CustomerProfile.objects.get().gender_signal, "female")
        self.assertIn("FEMININE form in EVERY sentence", self.brain.last_system)

    def test_a_masculine_form_in_the_current_message_gives_masculine_replies(self):
        w = self.fresh_widget()
        self.brain.turn(step("تقدرين تجرّبينه وشوفي القماش."), analysis={"signals": sig("hesitant", "object", "other")})
        data = w.say("عجبني بس متردد")
        self.assertEqual(data["reply"], "تقدر تجربه وشوف القماش.")
        self.assertEqual(CustomerProfile.objects.get().gender_signal, "male")

    def test_a_wrong_analyzer_gender_is_overridden_by_the_explicit_form(self):
        w = self.fresh_widget()
        self.brain.turn(step("تقدر تكمل."), analysis={"gender": "male", "gender_evidence": "متردد",
                                                      "signals": sig("hesitant", "object", "other")})
        data = w.say("عجبني بس مترددة وكنت متردد قبل")
        self.assertEqual(CustomerProfile.objects.get().gender_signal, "male")      # رسالة متضاربة ← ما منغيّر عن المحلّل

    # ---- 2) أجزاء غريبة بآخر الرد
    def test_a_foreign_script_tail_never_reaches_the_customer(self):
        w = self.fresh_widget()
        self.brain.turn(step("التوصيل داخل المملكة خلال ٢ إلى ٤ أيام عمل.оставка"), analysis={"signals": sig("interested", "ask_info")})
        data = w.say("كم باقي على وصوله؟")
        self.assertEqual(data["reply"], "التوصيل داخل المملكة خلال ٢ إلى ٤ أيام عمل.")
        self.assertEqual(self.trace().guard["stray_removed"], ["оставка"])

    def test_leaked_tool_syntax_and_card_reason_fragments_are_removed(self):
        w = self.fresh_widget()
        self.brain.turn(step('هذا فستان حلو show_product(sku="RED_M")', ("show_product", {"sku": "RED_M", "reason": "خفيف ومريح.оставка"})),
                        analysis={"signals": sig("interested", "search")})
        data = w.say("أبي فستان")
        self.assertEqual(data["reply"], "هذا فستان حلو")
        self.assertEqual(data["recommendations"][0]["reason"], "خفيف ومريح.")

    def test_catalog_codes_and_sizes_written_in_latin_are_kept(self):
        w = self.fresh_widget()
        reply = "فستان RED_M متوفر بمقاس M وXL، والقماش ممتاز."
        self.brain.turn(step(reply), analysis={"signals": sig("interested", "ask_info")})
        data = w.say("عندكم مقاس M؟")
        self.assertNotIn("stray_removed", self.trace().guard)
        self.assertIn("RED_M", data["reply"])

    # ---- 3) جواب طلب الكود: ثلاث حالات واضحة
    def test_no_valid_code_is_always_answered_clearly_even_if_the_model_dodges(self):
        DiscountCode.objects.all().update(is_active=False)
        w = self.fresh_widget()
        self.warm_up(w, reaction=LIKED)
        self.brain.turn(step("القماش ساتان ناعم وسعره مناسب."), analysis=self.ASKS)       # تهرّب ولا جاوب
        data = w.say("فيه كود خصم؟")
        self.assertTrue(data["reply"].startswith("ما فيه كود خصم متاح حالياً."))
        self.assertIn("القماش ساتان ناعم", data["reply"])
        self.assertIsNone(data["discount_offer"])

    def test_not_eligible_yet_is_answered_without_denying_that_a_code_exists_or_revealing_it(self):
        w = self.fresh_widget()
        self.warm_up(w, reaction=LIKED)
        self.brain.turn(step("القماش ساتان ناعم."), analysis=self.ASKS)
        data = w.say("فيه كود خصم؟")
        self.assertTrue(data["reply"].startswith("ما أقدر أقدم لك خصم حالياً."))
        self.assertNotIn("SAVE10", data["reply"])
        self.assertNotIn("ما فيه كود", data["reply"])
        self.assertEqual(self.verdict()["reason"], "no_hesitation")

    def test_a_reply_stripped_by_the_guard_is_replaced_by_the_clear_answer_not_a_generic_sentence(self):
        w = self.fresh_widget()
        self.warm_up(w, reaction=LIKED)
        self.brain.turn(step("عندي لك خصم 30% بكود VIP99."), step("أبشر عندي خصم 40% خاص."), analysis=self.ASKS)
        data = w.say("عطني الكود")
        self.assertEqual(data["reply"], "ما أقدر أقدم لك خصم حالياً. وأقدر أساعدك تختار القطعة الأنسب لك.")
        self.assertNotIn("خلّني أساعدك بشكل أدق", data["reply"])

    def test_the_available_code_is_stated_as_the_maximum_approved_discount(self):
        w = self.fresh_widget()
        self.warm_up(w)
        self.brain.turn(step("فاهمك."), analysis={"reaction": LIKED, "signals": sig("hesitant", "ask_discount", "other", asks=True)})
        data = w.say("حلو بس مترددة، عطني خصم أكبر؟")
        self.assertEqual(data["discount_offer"]["code"], "SAVE10")
        self.assertIn("هذا أقصى خصم معتمد عندنا: كود SAVE10 (خصم 10% على كل المنتجات)", data["reply"])
        # بدور لاحق: يسأل عن خصم أكبر ← نفس الكود هو أقصى المعتمد، بدون عرض جديد وبدون رقمه المطلوب
        self.brain.turn(step("القماش ساتان."), analysis=self.ASKS)
        later = w.say("عطني 50%")
        self.assertIsNone(later["discount_offer"])
        self.assertIn("هذا أقصى خصم معتمد عندنا: كود SAVE10", later["reply"])
        self.assertNotIn("50%", later["reply"])

    def test_when_the_model_already_states_the_code_nothing_is_added(self):
        w = self.fresh_widget()
        self.warm_up(w)
        self.hesitate(w, "حلو بس مترددة", reason="other")
        mine = "هذا هو أقصى خصم عندنا: SAVE10، خصم 10% على كل المنتجات."
        self.brain.turn(step(mine), analysis=self.ASKS)
        data = w.say("عطني خصم أكبر")
        self.assertEqual(data["reply"], mine)

    def test_a_question_about_discounts_never_gets_a_double_answer(self):
        DiscountCode.objects.all().update(is_active=False)
        w = self.fresh_widget()
        self.warm_up(w, reaction=LIKED)
        mine = "ما فيه كود خصم متاح حالياً، بس القماش ممتاز."
        self.brain.turn(step(mine), analysis=self.ASKS)
        self.assertEqual(w.say("فيه كود؟")["reply"], mine)

    def test_not_eligible_never_implies_there_are_no_codes_even_if_the_model_says_so(self):
        w = self.fresh_widget()
        self.warm_up(w, reaction=LIKED)
        self.brain.turn(step("ما عندي كود خصم أقدّمه لك الحين، بس القماش ممتاز."), analysis=self.ASKS)
        data = w.say("الحين عطني الكود")
        self.assertTrue(data["reply"].startswith("ما أقدر أقدم لك خصم حالياً."))
        self.assertNotIn("ما عندي كود", data["reply"])
        self.assertIn("القماش ممتاز", data["reply"])
        self.assertNotIn("SAVE10", data["reply"])

    def test_not_eligible_keeps_the_models_own_cannot_offer_sentence(self):
        w = self.fresh_widget()
        self.warm_up(w, reaction=LIKED)
        mine = "ما أقدر أوفّر خصم الحين، بس القماش ممتاز."
        self.brain.turn(step(mine), analysis=self.ASKS)
        self.assertEqual(w.say("فيه كود؟")["reply"], mine)

    # ---- 4) C5 ما تغيّر: بديل أرخص مناسب انعرض قبل = مُقدَّم، بدون إعادة عرض
    def test_c5_a_suitable_cheaper_product_shown_before_counts_as_presented_and_is_not_shown_again(self):
        w = self.fresh_widget()
        self.warm_up(w, shown=("BLK_M", "PNK_M"))
        self.brain.turn(step("هذا خيار أخف."), analysis={
            "reaction": LIKED, "objections": [{"type": "price", "evidence": "غالي"}], "signals": sig("hesitant", "object", "price")})
        data = w.say("حلو بس غالي")
        self.assertEqual(data["discount_offer"]["code"], "SAVE10")
        self.assertEqual(data["recommendations"], [])
        self.assertFalse(self.verdict()["rule_trace"]["alt_first_available"])
        self.assertTrue(self.verdict()["rule_trace"]["alt_first_done"])


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


class OccasionAndProactiveTests(EngineBase):
    def test_inferred_fabric_is_a_search_hint_and_a_guessed_dislike_is_not(self):
        from types import SimpleNamespace

        from .services.v2_turn import retrieval_query

        facts = [
            SimpleNamespace(status="active", polarity="like", key="material", source="inferred", value="linen"),
            SimpleNamespace(status="active", polarity="like", key="style", source="inferred", value="smart casual"),
            SimpleNamespace(status="active", polarity="avoid", key="style", source="inferred", value="formal suit"),
            SimpleNamespace(status="active", polarity="like", key="brand", source="inferred", value="Zara"),
            SimpleNamespace(status="active", polarity="like", key="occasion", source="explicit", value="wedding"),
        ]
        query = retrieval_query("عرس بسانتوريني بآب", facts)
        self.assertIn("linen", query)
        self.assertIn("smart casual", query)
        self.assertIn("wedding", query)
        self.assertNotIn("formal", query)
        self.assertNotIn("Zara", query)

    def test_a_gift_without_a_category_is_told_not_to_show_a_card(self):
        self.brain.turn(
            step("تبين ملابس ولا إكسسوارات؟"),
            analysis={
                "preferences": [pref("recipient", "صديقتي", "صديقتي", subject="gift")],
                "signals": sig(intent="browse", readiness="browsing"),
            },
        )
        data = self.w.say("بدي هدية لصديقتي")
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["recommendations"], [])
        self.assertIn("Do NOT call show_product", self.brain.last_system)

    def test_proactive_opener_is_stored_once_and_only_on_a_product_page(self):
        from .services.v2_turn import PROACTIVE_OPENER

        self.brain.turn(step("قلي وش تبي."))
        home = self.w.say("هلا", page_url="https://shop.example.com/")
        self.assertTrue(home["ok"], home)
        home_conv = Conversation.objects.get(session_id=home["session_id"])
        self.assertFalse(Message.objects.filter(conversation=home_conv, meta__proactive=True).exists())

        page = Widget(self.merchant, ip="8.8.8.8")
        self.brain.turn(step("قلي مقاسك."), analysis={"signals": sig(intent="ask_info")})
        opened = page.post("/widget/v2/chat/", {
            "message": "مقاسي L", "client_msg_id": uuid.uuid4().hex, "page_url": self.shirt.product_url,
            "proactive": True, **page._tok(),
        }).json()
        self.assertTrue(opened["ok"], opened)
        for key in ("session_token", "shopper_token", "session_id"):
            if opened.get(key):
                page.storage[key] = opened[key]
        conv = Conversation.objects.get(session_id=opened["session_id"])
        first = list(Message.objects.filter(conversation=conv).order_by("seq").values_list("role", "content"))
        self.assertEqual(first[0], ("assistant", PROACTIVE_OPENER))
        self.assertEqual(first[1], ("user", "مقاسي L"))

        self.brain.turn(step("تمام."))
        again = page.post("/widget/v2/chat/", {
            "message": "شكراً", "client_msg_id": uuid.uuid4().hex, "page_url": self.shirt.product_url,
            "proactive": True, **page._tok(),
        }).json()
        self.assertTrue(again["ok"], again)
        self.assertEqual(Message.objects.filter(conversation=conv, meta__proactive=True).count(), 1)
