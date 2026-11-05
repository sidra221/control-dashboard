"""مراحل البيع + سياسة الخصم v2 + حقول صلاحية الأكواد بالداشبورد.

الاختبارات النقية (stage/decide) SimpleTestCase: أي لمسة DB بتفشل الاختبار — وهيك منثبّت إنها نقية.
"""

import copy
import itertools
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from . import i18n
from .models import ApiToken, DiscountCode, Product
from .services import contracts, policy, stage
from .services.contracts import Candidate, Requirements, SearchResult
from .services.saudi import find_non_saudi

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=dt_timezone.utc)
ARABIC = re.compile("[؀-ۿ]")
PRESSURE = ("hurry", "last chance", "act now", "don't miss", "do not miss", "limited time", "only a few left",
            "before it's gone", "run out", "while stocks last", "today only", "right now or")
END_CHAT = ("end the conversation", "end the chat", "stop the conversation", "say goodbye", "give up", "let them go")


# ====================================================================== next_stage

def ns(**over):
    kw = dict(previous="discovery", signals={}, open_objections=[], shown_count=0, liked_or_asked_count=0,
              events=set(), turn_no=1, wants_alternative=False)
    kw.update(over)
    return stage.next_stage(**kw)


class NextStageTableTests(SimpleTestCase):
    CASES = [
        # (label, overrides, expected)
        ("fresh conversation", {}, "discovery"),
        ("unknown previous stage", {"previous": "banana"}, "discovery"),
        ("None previous stage", {"previous": None}, "discovery"),
        ("missing signals", {"signals": None}, "discovery"),
        ("chitchat, nothing shown", {"signals": {"intent": "chitchat"}}, "discovery"),
        ("ask_info, nothing shown", {"signals": {"intent": "ask_info"}}, "discovery"),
        ("concrete search, nothing shown yet", {"signals": {"intent": "search"}}, "recommendation"),
        ("shown, evaluating", {"previous": "discovery", "shown_count": 2}, "recommendation"),
        ("shown, still evaluating", {"previous": "recommendation", "shown_count": 3, "signals": {"intent": "compare"}}, "recommendation"),
        ("recommendation does not flap back to discovery", {"previous": "recommendation", "shown_count": 1, "signals": {"intent": "chitchat"}}, "recommendation"),
        ("liked event after shown", {"shown_count": 1, "events": {"liked"}}, "persuasion"),
        ("asked event after shown", {"shown_count": 1, "events": {"asked"}}, "persuasion"),
        ("tried_on event after shown", {"shown_count": 1, "events": {"tried_on"}}, "persuasion"),
        ("liked_or_asked count", {"shown_count": 2, "liked_or_asked_count": 1}, "persuasion"),
        ("interested after shown", {"shown_count": 1, "signals": {"readiness": "interested"}}, "persuasion"),
        ("accept after shown", {"shown_count": 1, "signals": {"intent": "accept"}}, "persuasion"),
        ("interested but nothing shown", {"signals": {"readiness": "interested"}}, "discovery"),
        ("open price objection", {"shown_count": 1, "open_objections": ["price"]}, "objection_handling"),
        ("open objection, nothing shown", {"open_objections": ["trust"]}, "objection_handling"),
        ("open objection beats liked", {"shown_count": 1, "events": {"liked"}, "open_objections": ["quality"]}, "objection_handling"),
        ("open objection beats ready", {"shown_count": 1, "signals": {"readiness": "ready"}, "open_objections": ["price"]}, "objection_handling"),
        ("hesitant after shown", {"shown_count": 1, "signals": {"readiness": "hesitant"}}, "objection_handling"),
        ("hesitation reason alone", {"shown_count": 1, "signals": {"hesitation_reason": "timing"}}, "objection_handling"),
        ("hesitant, nothing shown stays discovery", {"signals": {"readiness": "hesitant"}}, "discovery"),
        ("ready after shown", {"shown_count": 1, "signals": {"readiness": "ready"}}, "closing"),
        ("buy intent after shown", {"shown_count": 1, "signals": {"intent": "buy"}}, "closing"),
        ("buy intent, nothing shown", {"signals": {"intent": "buy"}}, "discovery"),
        ("ready, nothing shown, search", {"signals": {"readiness": "ready", "intent": "search"}}, "recommendation"),
        ("buy_click event", {"shown_count": 1, "events": {"buy_click"}}, "closing"),
        ("add_to_cart event", {"shown_count": 1, "events": {"add_to_cart"}}, "closing"),
        ("buy_click beats browsing analyzer", {"shown_count": 1, "events": {"buy_click"}, "signals": {"readiness": "browsing", "intent": "chitchat"}}, "closing"),
        ("buy_click beats hesitant analyzer without objection", {"shown_count": 1, "events": {"buy_click"}, "signals": {"readiness": "hesitant"}}, "closing"),
        ("buy_click with open objection", {"shown_count": 1, "events": {"buy_click"}, "open_objections": ["price"]}, "objection_handling"),
        ("add_to_cart with open objection", {"shown_count": 1, "events": {"add_to_cart"}, "open_objections": ["timing"]}, "objection_handling"),
        ("buy_click then asks for something else", {"shown_count": 1, "events": {"buy_click"}, "wants_alternative": True}, "recommendation"),
        ("rejection after shown", {"shown_count": 2, "signals": {"intent": "reject"}}, "recommendation"),
        ("rejection, nothing known", {"signals": {"intent": "reject"}}, "discovery"),
        ("wants_alternative param", {"shown_count": 1, "wants_alternative": True}, "recommendation"),
        ("wants_alternative in signals", {"shown_count": 1, "signals": {"wants_alternative": True}}, "recommendation"),
        ("wants_alternative, nothing known", {"wants_alternative": True}, "discovery"),
        ("rejection beats liked history", {"shown_count": 2, "events": {"liked"}, "signals": {"intent": "reject"}}, "recommendation"),
        ("rejection beats hesitation reason (style)", {"shown_count": 1, "signals": {"intent": "reject", "readiness": "hesitant", "hesitation_reason": "style"}}, "recommendation"),
        ("rejection with an open objection is handled as an objection", {"shown_count": 1, "signals": {"intent": "reject"}, "open_objections": ["price"]}, "objection_handling"),
        ("empty-string objections are ignored", {"shown_count": 1, "open_objections": ["", None]}, "recommendation"),
        ("None events/objections tolerated", {"shown_count": 1, "events": None, "open_objections": None}, "recommendation"),
    ]

    def test_table(self):
        for label, over, expected in self.CASES:
            with self.subTest(label):
                self.assertEqual(ns(**over), expected)

    def test_result_is_always_a_known_stage(self):
        for _label, over, _expected in self.CASES:
            self.assertIn(ns(**over), contracts.STAGES)


class NextStageHysteresisTests(SimpleTestCase):
    NEUTRAL = [
        {"intent": "chitchat"}, {"intent": "ask_info"}, {"intent": "browse"}, {"readiness": "browsing"},
        {"readiness": "interested"}, {"intent": "accept"}, {}, {"intent": "other", "readiness": ""},
    ]

    def test_closing_survives_every_neutral_message(self):
        for sig in self.NEUTRAL:
            for extra in ({}, {"events": {"liked"}}, {"liked_or_asked_count": 2}):
                with self.subTest(sig=sig, extra=extra):
                    self.assertEqual(ns(previous="closing", shown_count=2, signals=sig, **extra), "closing")

    def test_closing_goes_back_only_on_explicit_signals(self):
        base = dict(previous="closing", shown_count=2)
        self.assertEqual(ns(**base, open_objections=["price"]), "objection_handling")
        self.assertEqual(ns(**base, signals={"readiness": "hesitant"}), "objection_handling")
        self.assertEqual(ns(**base, signals={"hesitation_reason": "trust"}), "objection_handling")
        self.assertEqual(ns(**base, signals={"intent": "reject"}), "recommendation")
        self.assertEqual(ns(**base, wants_alternative=True), "recommendation")

    def test_persuasion_survives_neutral_messages(self):
        for sig in ({"intent": "chitchat"}, {"intent": "ask_info"}, {"readiness": "browsing"}, {}):
            with self.subTest(sig=sig):
                self.assertEqual(ns(previous="persuasion", shown_count=1, signals=sig), "persuasion")

    def test_persuasion_transitions(self):
        base = dict(previous="persuasion", shown_count=1)
        self.assertEqual(ns(**base, signals={"readiness": "ready"}), "closing")
        self.assertEqual(ns(**base, signals={"readiness": "hesitant"}), "objection_handling")
        self.assertEqual(ns(**base, signals={"intent": "reject"}), "recommendation")
        self.assertEqual(ns(**base, open_objections=["quality"]), "objection_handling")

    def test_persuasion_without_anything_shown_falls_back(self):
        self.assertEqual(ns(previous="persuasion"), "discovery")

    def test_objection_handling_ends_when_objection_resolved(self):
        self.assertEqual(ns(previous="objection_handling", shown_count=1, events={"liked"}), "persuasion")
        self.assertEqual(ns(previous="objection_handling", shown_count=1), "recommendation")
        self.assertEqual(ns(previous="objection_handling", shown_count=1, signals={"readiness": "ready"}), "closing")

    def test_objection_handling_stays_while_open(self):
        self.assertEqual(ns(previous="objection_handling", shown_count=1, open_objections=["price"],
                            signals={"intent": "chitchat"}), "objection_handling")

    def test_no_flapping_across_a_realistic_conversation(self):
        # (إشارات/أحداث كل دور) ← المرحلة المتوقعة
        timeline = [
            (dict(signals={"intent": "search"}), "recommendation"),
            (dict(shown_count=2, signals={"intent": "compare"}), "recommendation"),
            (dict(shown_count=2, events={"liked"}, signals={"readiness": "interested"}), "persuasion"),
            (dict(shown_count=2, events={"liked"}, signals={"intent": "ask_info"}), "persuasion"),
            (dict(shown_count=2, events={"liked"}, signals={"intent": "chitchat"}), "persuasion"),
            (dict(shown_count=2, events={"liked"}, signals={"readiness": "ready", "intent": "buy"}), "closing"),
            (dict(shown_count=2, events={"liked"}, signals={"intent": "chitchat"}), "closing"),
            (dict(shown_count=2, events={"liked", "buy_click"}, signals={"readiness": "browsing"}), "closing"),
            (dict(shown_count=2, events={"liked", "buy_click"}, signals={"readiness": "interested"}), "closing"),
            (dict(shown_count=2, events={"liked", "buy_click"}, open_objections=["price"]), "objection_handling"),
            (dict(shown_count=2, events={"liked", "buy_click"}), "closing"),
        ]
        previous = "discovery"
        for i, (over, expected) in enumerate(timeline, start=1):
            previous = ns(previous=previous, turn_no=i, **over)
            self.assertEqual(previous, expected, f"turn {i}")

    def test_deterministic(self):
        kw = dict(previous="persuasion", shown_count=2, signals={"readiness": "hesitant"}, events={"liked"})
        self.assertEqual({ns(**kw) for _ in range(20)}, {"objection_handling"})

    def test_inputs_not_mutated(self):
        events, objs, sig = {"liked"}, ["price"], {"intent": "buy"}
        snapshot = (copy.deepcopy(events), copy.deepcopy(objs), copy.deepcopy(sig))
        ns(shown_count=1, events=events, open_objections=objs, signals=sig)
        self.assertEqual((events, objs, sig), snapshot)


# ====================================================================== readiness_of

class ReadinessTableTests(SimpleTestCase):
    CASES = [
        ("nothing", {}, set(), [], "browsing"),
        ("browsing signal", {"readiness": "browsing"}, set(), [], "browsing"),
        ("chitchat", {"intent": "chitchat"}, set(), [], "browsing"),
        ("interested signal", {"readiness": "interested"}, set(), [], "interested"),
        ("accept intent", {"intent": "accept"}, set(), [], "interested"),
        ("liked event", {}, {"liked"}, [], "interested"),
        ("asked event", {}, {"asked"}, [], "interested"),
        ("tried_on event", {}, {"tried_on"}, [], "interested"),
        ("hesitant signal", {"readiness": "hesitant"}, set(), [], "hesitant"),
        ("hesitation reason", {"hesitation_reason": "price"}, set(), [], "hesitant"),
        ("open objection", {}, set(), ["quality"], "hesitant"),
        ("open objection beats ready signal", {"readiness": "ready"}, set(), ["price"], "hesitant"),
        ("open objection beats buy_click", {}, {"buy_click"}, ["price"], "hesitant"),
        ("ready signal", {"readiness": "ready"}, set(), [], "ready"),
        ("buy intent", {"intent": "buy"}, set(), [], "ready"),
        ("buy_click", {}, {"buy_click"}, [], "ready"),
        ("add_to_cart", {}, {"add_to_cart"}, [], "ready"),
        ("buy_click beats browsing analyzer", {"readiness": "browsing"}, {"buy_click"}, [], "ready"),
        ("buy_click beats hesitant analyzer w/o objection", {"readiness": "hesitant"}, {"buy_click"}, [], "ready"),
        ("hesitant beats liked", {"readiness": "hesitant"}, {"liked"}, [], "hesitant"),
        ("ready beats liked", {"readiness": "ready"}, {"liked"}, [], "ready"),
        ("rejection drops ready", {"intent": "reject", "readiness": "ready"}, set(), [], "browsing"),
        ("rejection drops buy_click", {"wants_alternative": True}, {"buy_click"}, [], "browsing"),
        ("rejection keeps liked history", {"intent": "reject"}, {"liked"}, [], "interested"),
        ("None signals", None, set(), [], "browsing"),
        ("bad readiness value ignored", {"readiness": "mega"}, set(), [], "browsing"),
        ("None events and objections", {}, None, None, "browsing"),
    ]

    def test_table(self):
        for label, sig, events, objs, expected in self.CASES:
            with self.subTest(label):
                result = stage.readiness_of(sig, events, objs)
                self.assertEqual(result, expected)
                self.assertIn(result, contracts.READINESS)


# ====================================================================== decide

def code(pk=1, text="SAVE", *, active=True, starts=None, expires=None, age_days=10, desc="خصم 10% على كل المنتجات"):
    return DiscountCode(id=pk, code=text, description=desc, is_active=active, starts_at=starts, expires_at=expires,
                        created_at=NOW - timedelta(days=age_days))


def prod(sku="A1", price="200", **kw):
    return Product(sku=sku, title=f"Item {sku}", price=Decimal(price), **kw)


def cand(sku, price, *, exact=True, relaxed=(), **kw):
    return Candidate(product=prod(sku, price, **kw), exact=exact, relaxed=relaxed)


def search(*cands, note=""):
    return SearchResult(candidates=list(cands), exact_count=sum(1 for c in cands if c.exact), note=note)


def decide(**over):
    kw = dict(
        stage="objection_handling", signals={}, readiness="hesitant", objections=[("price", "A1")],
        engaged_product=prod(), turn_no=3, shown_count=2, known_slots=set(), questions_asked_in_conversation=0,
        requirements=Requirements(), discount_codes=[code()], offer_history=False, search_result=None,
        conversation_state=None, now=NOW,
    )
    kw.update(over)
    return policy.decide(**kw)


class ValidCodesTests(SimpleTestCase):
    def test_active_and_within_dates(self):
        ok = code(1)
        inactive = code(2, active=False)
        future = code(3, starts=NOW + timedelta(hours=1))
        past = code(4, expires=NOW - timedelta(hours=1))
        windowed = code(5, starts=NOW - timedelta(days=1), expires=NOW + timedelta(days=1))
        self.assertEqual([c.id for c in policy.valid_codes([ok, inactive, future, past, windowed], NOW)], [1, 5])

    def test_boundaries(self):
        self.assertEqual(policy.valid_codes([code(1, starts=NOW)], NOW)[0].id, 1)          # starts_at <= now
        self.assertEqual(policy.valid_codes([code(2, expires=NOW)], NOW), [])               # expires_at > now
        self.assertEqual(policy.valid_codes([code(3, expires=NOW + timedelta(seconds=1))], NOW)[0].id, 3)

    def test_stable_order_by_created_then_id(self):
        codes = [code(3, age_days=1), code(2, age_days=5), code(5, age_days=5), code(1, age_days=9)]
        self.assertEqual([c.id for c in policy.valid_codes(codes, NOW)], [1, 2, 5, 3])
        self.assertEqual([c.id for c in policy.valid_codes(reversed(codes), NOW)], [1, 2, 5, 3])

    def test_default_now_is_current_time(self):
        live = DiscountCode(id=1, code="A", is_active=True, expires_at=timezone.now() + timedelta(hours=1))
        dead = DiscountCode(id=2, code="B", is_active=True, expires_at=timezone.now() - timedelta(hours=1))
        self.assertEqual([c.id for c in policy.valid_codes([live, dead])], [1])

    def test_empty(self):
        self.assertEqual(policy.valid_codes([], NOW), [])

    def test_v1_helpers_unchanged(self):
        self.assertEqual(policy.MIN_USER_TURNS, 2)
        self.assertEqual(policy.basic_offer_gate(user_turns=1, engaged=True, already_offered=False), (False, "too_early"))
        self.assertEqual(policy.basic_offer_gate(user_turns=2, engaged=True, already_offered=False), (True, "ok"))
        self.assertEqual(policy.basic_offer_gate(user_turns=5, engaged=False, already_offered=False), (False, "no_engaged_product"))
        self.assertEqual(policy.basic_offer_gate(user_turns=5, engaged=True, already_offered=True), (False, "already_offered"))
        self.assertTrue(policy.history_mentions_code([{"role": "assistant", "content": "use SAVE10"}], ["save10"]))


class DiscountTriggerTests(SimpleTestCase):
    def assertAllowed(self, strategy, trigger):
        v = strategy.discount
        self.assertTrue(v.allowed, (v.reason, v.rule_trace))
        self.assertEqual((v.reason, v.trigger), ("ok", trigger))
        self.assertIsInstance(v.code, DiscountCode)
        self.assertNotIn("push_discount", strategy.forbid)

    def assertDenied(self, strategy, reason):
        v = strategy.discount
        self.assertFalse(v.allowed)
        self.assertIsNone(v.code)
        self.assertEqual(v.reason, reason, v.rule_trace)
        self.assertIn("push_discount", strategy.forbid)
        self.assertEqual(v.rule_trace["denied_by"], reason)

    # ---- محفّزات
    def test_price_objection_on_engaged_product(self):
        self.assertAllowed(decide(), "price_objection")

    def test_competitor_price_objection(self):
        self.assertAllowed(decide(objections=[("competitor_price", "A1")]), "price_objection")

    def test_any_open_price_objection_even_on_another_sku(self):
        self.assertAllowed(decide(objections=[("price", "ZZ")]), "price_objection")
        self.assertAllowed(decide(objections=[("price", "")]), "price_objection")

    def test_hesitant_readiness_with_product_shown(self):
        self.assertAllowed(decide(objections=[], readiness="hesitant"), "hesitation")

    def test_hesitation_reason_price_or_other(self):
        for reason in ("price", "other"):
            with self.subTest(reason):
                self.assertAllowed(decide(objections=[], readiness="interested",
                                          signals={"hesitation_reason": reason}), "hesitation")

    def test_open_timing_objection(self):
        self.assertAllowed(decide(objections=[("timing", "A1")], readiness="hesitant"), "hesitation")
        self.assertAllowed(decide(objections=[("timing", "")], readiness="interested"), "hesitation")

    def test_hesitation_needs_a_product_shown(self):
        s = decide(objections=[], readiness="hesitant", shown_count=0)
        self.assertDenied(s, "no_trigger")

    def test_asks_discount_while_engaged_and_interested(self):
        for readiness in ("interested", "hesitant"):
            with self.subTest(readiness):
                self.assertAllowed(decide(objections=[], readiness=readiness, stage="persuasion",
                                          signals={"asks_discount": True, "hesitation_reason": "style" if readiness == "hesitant" else ""}),
                                   "asked")

    def test_asks_discount_when_customer_reacted_positively(self):
        s = decide(objections=[], readiness="browsing", stage="recommendation",
                   signals={"asks_discount": True, "intent": "accept"})
        self.assertAllowed(s, "asked")

    def test_bare_asks_discount_while_browsing_is_denied(self):
        s = decide(objections=[], readiness="browsing", stage="recommendation", signals={"asks_discount": True})
        self.assertDenied(s, "no_trigger")

    def test_bare_discount_question_before_engagement(self):
        s = decide(stage="discovery", objections=[], readiness="browsing", engaged_product=None, shown_count=0,
                   turn_no=1, signals={"asks_discount": True, "intent": "ask_discount"})
        self.assertDenied(s, "not_engaged")
        text = " ".join(s.directives)
        self.assertIn("best deal", text)
        self.assertIn("REAL sale items", text)
        self.assertNotIn("Present the approved code", text)

    def test_asks_discount_without_engagement_even_when_interested(self):
        s = decide(stage="recommendation", objections=[], readiness="interested", engaged_product=None,
                   signals={"asks_discount": True})
        self.assertDenied(s, "not_engaged")

    # ---- مش محفّز
    def test_ready_without_concern_gets_no_discount(self):
        s = decide(stage="closing", objections=[], readiness="ready")
        self.assertDenied(s, "ready_no_price_concern")

    def test_ready_who_asks_for_a_discount_still_gets_none(self):
        s = decide(stage="closing", objections=[], readiness="ready", signals={"asks_discount": True})
        self.assertDenied(s, "ready_no_price_concern")
        self.assertIn("do not offer a code", " ".join(s.directives))

    def test_closing_with_open_price_objection_may_get_the_code(self):
        self.assertAllowed(decide(stage="closing", readiness="hesitant"), "price_objection")

    def test_non_price_objections_alone_are_not_a_trigger(self):
        for kind in ("quality", "fit", "trust", "shipping"):
            with self.subTest(kind):
                s = decide(objections=[(kind, "A1")], readiness="hesitant")
                self.assertDenied(s, "no_trigger")

    def test_non_price_hesitation_reasons_alone_are_not_a_trigger(self):
        for reason in ("style", "fit", "trust"):
            with self.subTest(reason):
                s = decide(objections=[], readiness="hesitant", signals={"hesitation_reason": reason})
                self.assertDenied(s, "no_trigger")

    def test_quality_objection_plus_price_objection_is_a_trigger(self):
        self.assertAllowed(decide(objections=[("quality", "A1"), ("price", "A1")]), "price_objection")

    def test_interested_without_anything_is_no_trigger(self):
        self.assertDenied(decide(objections=[], readiness="interested", stage="persuasion"), "no_trigger")
        self.assertDenied(decide(objections=[], readiness="browsing", stage="recommendation"), "no_trigger")

    # ---- بوّابات
    def test_no_codes(self):
        self.assertDenied(decide(discount_codes=[]), "no_codes")

    def test_only_inactive_codes_is_no_codes(self):
        self.assertDenied(decide(discount_codes=[code(1, active=False)]), "no_codes")

    def test_all_codes_expired(self):
        s = decide(discount_codes=[code(1, expires=NOW - timedelta(days=1)), code(2, expires=NOW)])
        self.assertDenied(s, "expired")
        self.assertEqual(s.discount.rule_trace["codes_valid"], 0)
        self.assertEqual(s.discount.rule_trace["codes_total"], 2)

    def test_not_started_code_is_not_offered(self):
        s = decide(discount_codes=[code(1, starts=NOW + timedelta(days=2))])
        self.assertDenied(s, "no_codes")

    def test_expired_plus_not_started_plus_inactive(self):
        s = decide(discount_codes=[code(1, active=False), code(2, starts=NOW + timedelta(days=1)),
                                   code(3, expires=NOW - timedelta(days=1))])
        self.assertDenied(s, "expired")

    def test_inactive_expired_code_alone_is_no_codes(self):
        self.assertDenied(decide(discount_codes=[code(1, active=False, expires=NOW - timedelta(days=1))]), "no_codes")

    def test_a_valid_code_wins_over_expired_ones(self):
        s = decide(discount_codes=[code(1, expires=NOW - timedelta(days=1)), code(2, "OK")])
        self.assertAllowed(s, "price_objection")
        self.assertEqual(s.discount.code.id, 2)

    def test_not_engaged(self):
        self.assertDenied(decide(engaged_product=None), "not_engaged")

    def test_too_early(self):
        self.assertDenied(decide(turn_no=1), "too_early")
        self.assertDenied(decide(turn_no=0), "too_early")
        self.assertAllowed(decide(turn_no=2), "price_objection")

    def test_never_repeat(self):
        self.assertDenied(decide(offer_history=True), "already_offered")
        self.assertDenied(decide(offer_history=True, objections=[("price", "A1"), ("competitor_price", "A1")]), "already_offered")

    def test_discovery_stage_never_discounts(self):
        self.assertDenied(decide(stage="discovery"), "not_engaged")

    def test_gate_precedence(self):
        # كل بوّابة بتغلب اللي بعدها
        everything_wrong = dict(discount_codes=[], engaged_product=None, turn_no=0, offer_history=True, stage="discovery")
        self.assertDenied(decide(**everything_wrong), "no_codes")
        self.assertDenied(decide(**{**everything_wrong, "discount_codes": [code()]}), "not_engaged")
        self.assertDenied(decide(**{**everything_wrong, "discount_codes": [code()], "engaged_product": prod()}), "too_early")
        self.assertDenied(decide(**{**everything_wrong, "discount_codes": [code()], "engaged_product": prod(), "turn_no": 5}), "already_offered")
        self.assertDenied(decide(**{**everything_wrong, "discount_codes": [code()], "engaged_product": prod(), "turn_no": 5,
                                    "offer_history": False}), "not_engaged")

    # ---- اختيار الكود
    def test_oldest_valid_code_is_selected(self):
        codes = [code(1, "NEW", age_days=1), code(2, "OLD", age_days=30), code(3, "MID", age_days=10)]
        self.assertEqual(decide(discount_codes=codes).discount.code.code, "OLD")

    def test_selection_never_looks_at_percentages(self):
        codes = [code(1, "BIG", age_days=1, desc="خصم 90% على كل شي"), code(2, "SMALL", age_days=30, desc="خصم 5%")]
        self.assertEqual(decide(discount_codes=codes).discount.code.code, "SMALL")
        # نفس النتيجة لو الوصف فاضي
        codes = [code(1, "B", age_days=1, desc=""), code(2, "A", age_days=30, desc="")]
        self.assertEqual(decide(discount_codes=codes).discount.code.code, "A")

    def test_inactive_and_expired_codes_skipped_when_choosing(self):
        codes = [code(1, "DEAD", age_days=90, active=False), code(2, "OLDEXP", age_days=80, expires=NOW - timedelta(days=1)),
                 code(3, "GOOD", age_days=5)]
        self.assertEqual(decide(discount_codes=codes).discount.code.code, "GOOD")

    def test_tie_on_created_at_broken_by_id(self):
        codes = [code(9, "Z", age_days=3), code(4, "A", age_days=3)]
        self.assertEqual(decide(discount_codes=codes).discount.code.code, "A")

    def test_selected_code_id_in_trace(self):
        self.assertEqual(decide(discount_codes=[code(7)]).discount.rule_trace["selected_code_id"], 7)

    def test_allowed_directive_uses_only_the_approved_offer(self):
        text = " ".join(decide().directives)
        self.assertIn("APPROVED OFFER", text)
        self.assertIn("merchant's own description", text)
        self.assertIn("no calculated prices", text)
        self.assertIn("no free shipping", text)
        self.assertNotIn("SAVE", text)  # الكود ما بينذكر بالتوجيهات: بيجي بكتلة approved_offer
        self.assertNotIn("Do not offer, hint at or promise any discount", text)


class AlternativeFirstTests(SimpleTestCase):
    def test_cheaper_exact_candidate_goes_first_and_blocks_the_code(self):
        sr = search(cand("B7", "150"))
        s = decide(search_result=sr)
        self.assertTrue(s.alternative_first)
        self.assertFalse(s.discount.allowed)
        self.assertEqual(s.discount.reason, "alternative_first")
        self.assertEqual(s.discount.trigger, "price_objection")
        self.assertEqual(s.state_updates, {"sales": {"alt_first_done": True}})
        text = " ".join(s.directives)
        self.assertIn("cheaper real alternative", text)
        self.assertIn("B7", text)
        self.assertIn("push_discount", s.forbid)

    def test_second_price_objection_gets_the_code(self):
        sr = search(cand("B7", "150"))
        first = decide(search_result=sr)
        state = {"sales": dict(first.state_updates["sales"])}
        second = decide(search_result=sr, conversation_state=state)
        self.assertFalse(second.alternative_first)
        self.assertTrue(second.discount.allowed)
        self.assertEqual(second.discount.reason, "ok")
        self.assertEqual(second.state_updates, {})
        self.assertNotIn("push_discount", second.forbid)

    def test_two_step_flow_with_state_roundtrip(self):
        sr = search(cand("B7", "150"))
        state = {"other": 1}
        verdicts = []
        for turn in (3, 4, 5):
            s = decide(search_result=sr, conversation_state=state, turn_no=turn)
            for k, v in s.state_updates.items():
                state.setdefault(k, {}).update(v)
            verdicts.append((s.alternative_first, s.discount.reason))
        self.assertEqual(verdicts, [(True, "alternative_first"), (False, "ok"), (False, "ok")])
        self.assertEqual(state["other"], 1)

    def test_saving_threshold_is_15_percent(self):
        self.assertFalse(decide(search_result=search(cand("B", "180"))).alternative_first)   # 10%
        self.assertFalse(decide(search_result=search(cand("B", "171"))).alternative_first)   # 14.5%
        self.assertTrue(decide(search_result=search(cand("B", "170"))).alternative_first)    # 15% بالضبط
        self.assertTrue(decide(search_result=search(cand("B", "100"))).alternative_first)

    def test_same_price_or_more_expensive_is_not_an_alternative(self):
        self.assertFalse(decide(search_result=search(cand("B", "200"), cand("C", "260"))).alternative_first)

    def test_not_a_price_trigger_means_no_alternative_first(self):
        sr = search(cand("B7", "100"))
        s = decide(search_result=sr, objections=[], readiness="hesitant")
        self.assertFalse(s.alternative_first)
        self.assertTrue(s.discount.allowed)
        self.assertEqual(s.state_updates, {})

    def test_same_sku_as_engaged_is_ignored(self):
        self.assertFalse(decide(search_result=search(cand("A1", "100"))).alternative_first)

    def test_requirements_are_respected(self):
        sr = search(cand("B", "150"))
        self.assertFalse(decide(search_result=sr, requirements=Requirements(excluded_skus=frozenset({"B"}))).alternative_first)
        self.assertFalse(decide(search_result=sr, requirements=Requirements(budget_max=Decimal("120"))).alternative_first)
        self.assertTrue(decide(search_result=sr, requirements=Requirements(budget_max=Decimal("150"))).alternative_first)
        oos = search(cand("B", "150", in_stock=False))
        self.assertFalse(decide(search_result=oos).alternative_first)
        self.assertTrue(decide(search_result=oos, requirements=Requirements(include_out_of_stock=True)).alternative_first)

    def test_relaxed_candidates(self):
        self.assertFalse(decide(search_result=search(cand("B", "150", exact=False, relaxed=("color",)))).alternative_first)
        self.assertFalse(decide(search_result=search(cand("B", "150", exact=False, relaxed=("category",)))).alternative_first)
        self.assertTrue(decide(search_result=search(cand("B", "150", exact=False, relaxed=("tag",)))).alternative_first)

    def test_exact_candidate_preferred_over_best_effort(self):
        sr = search(cand("RELAXED", "100", exact=False, relaxed=("tag",)), cand("EXACT", "160"))
        s = decide(search_result=sr)
        self.assertIn("EXACT", " ".join(s.directives))
        self.assertNotIn("RELAXED", " ".join(s.directives))

    def test_no_search_result_or_no_candidates(self):
        self.assertFalse(decide(search_result=None).alternative_first)
        self.assertFalse(decide(search_result=search()).alternative_first)
        self.assertTrue(decide(search_result=search()).discount.allowed)

    def test_no_engaged_product_no_alternative_first(self):
        self.assertFalse(decide(engaged_product=None, search_result=search(cand("B", "50"))).alternative_first)

    def test_alternative_first_still_offered_when_discount_gates_fail(self):
        s = decide(search_result=search(cand("B", "100")), discount_codes=[])
        self.assertTrue(s.alternative_first)
        self.assertEqual(s.discount.reason, "no_codes")
        s = decide(search_result=search(cand("B", "100")), turn_no=1)
        self.assertTrue(s.alternative_first)
        self.assertEqual(s.discount.reason, "too_early")

    def test_already_offered_beats_alternative_first_reason(self):
        s = decide(search_result=search(cand("B", "100")), offer_history=True)
        self.assertEqual(s.discount.reason, "already_offered")

    def test_competitor_price_also_goes_alternative_first(self):
        s = decide(objections=[("competitor_price", "A1")], search_result=search(cand("B", "100")))
        self.assertTrue(s.alternative_first)

    def test_unparseable_prices_do_not_crash(self):
        bad = Product(sku="B", title="x", price=None)
        s = decide(search_result=SearchResult(candidates=[Candidate(product=bad)]))
        self.assertFalse(s.alternative_first)


class StrategyShapeTests(SimpleTestCase):
    def grid(self):
        stages = contracts.STAGES
        readiness = contracts.READINESS
        for st in stages:
            for rd in readiness:
                for objections in ([], [("price", "A1")], [("quality", "A1")], [("timing", "")]):
                    for engaged in (None, prod()):
                        for codes in ([], [code()], [code(expires=NOW - timedelta(days=1))]):
                            for sig in ({}, {"asks_discount": True}, {"hesitation_reason": "price"}):
                                for questions in (0, 2, 5):
                                    yield dict(stage=st, readiness=rd, objections=objections, engaged_product=engaged,
                                               discount_codes=codes, signals=sig, questions_asked_in_conversation=questions,
                                               turn_no=3 if engaged else 1, shown_count=2 if engaged else 0,
                                               search_result=search(cand("B", "100")))

    def test_directives_never_empty_english_calm_and_never_end_the_chat(self):
        seen = 0
        for kw in self.grid():
            s = decide(**kw)
            seen += 1
            self.assertTrue(s.directives, kw)
            for line in s.directives:
                self.assertIsInstance(line, str)
                self.assertFalse(ARABIC.search(line), line)
                low = line.lower()
                for bad in PRESSURE + END_CHAT:
                    self.assertNotIn(bad, low, line)
            joined = " ".join(s.directives)
            self.assertIn("Never pressure", joined)
            self.assertIn("Never invent product facts", joined)
        self.assertGreater(seen, 1000)

    def test_stage_and_readiness_are_echoed_and_valid(self):
        s = decide(stage="persuasion", readiness="interested", objections=[])
        self.assertEqual((s.stage, s.readiness), ("persuasion", "interested"))
        s = decide(stage="nonsense", readiness="nonsense", objections=[])
        self.assertEqual((s.stage, s.readiness), ("discovery", "browsing"))

    def test_discount_denial_always_has_a_constructive_path_for_price_concerns(self):
        for kw in (dict(discount_codes=[]), dict(offer_history=True), dict(turn_no=1),
                   dict(discount_codes=[code(expires=NOW - timedelta(days=1))])):
            with self.subTest(kw):
                s = decide(**kw)
                self.assertFalse(s.discount.allowed)
                joined = " ".join(s.directives)
                self.assertIn("Keep selling constructively", joined)
                self.assertIn("reframe the value", joined)
                self.assertIn("cheaper real alternative", joined)
                self.assertIn("reassure", joined)
                self.assertIn("Do not offer, hint at or promise any discount", joined)

    def test_forbid_flags(self):
        self.assertIn("upsell", decide(stage="closing", readiness="ready", objections=[]).forbid)
        self.assertIn("upsell", decide(stage="objection_handling").forbid)
        self.assertNotIn("upsell", decide(stage="recommendation", objections=[], readiness="browsing").forbid)
        self.assertNotIn("push_discount", decide().forbid)
        self.assertIn("push_discount", decide(discount_codes=[]).forbid)

    def test_forbid_has_only_known_values(self):
        for kw in self.grid():
            self.assertLessEqual(set(decide(**kw).forbid), {"ask_question", "push_discount", "upsell"})

    def test_ask_slot_only_known_values(self):
        for kw in self.grid():
            self.assertIn(decide(**kw).ask_slot, (None, "category", "occasion", "color", "size", "budget"))


class DiscoverySlotTests(SimpleTestCase):
    def d(self, **over):
        kw = dict(stage="discovery", readiness="browsing", objections=[], engaged_product=None, shown_count=0,
                  turn_no=1, discount_codes=[])
        kw.update(over)
        return decide(**kw)

    def test_priority_category_occasion_color_size(self):
        known = set()
        order = []
        for _ in range(4):
            slot = self.d(known_slots=known).ask_slot
            order.append(slot)
            known = known | {slot}
        self.assertEqual(order, ["category", "occasion", "color", "size"])

    def test_never_asks_a_known_slot(self):
        slots = ("category", "occasion", "color", "size", "budget")
        for r in range(len(slots) + 1):
            for known in itertools.combinations(slots, r):
                s = self.d(known_slots=set(known))
                self.assertNotIn(s.ask_slot, known)

    def test_skips_known_slots_in_priority_order(self):
        self.assertEqual(self.d(known_slots={"category"}).ask_slot, "occasion")
        self.assertEqual(self.d(known_slots={"category", "occasion"}).ask_slot, "color")
        self.assertEqual(self.d(known_slots={"category", "color"}).ask_slot, "occasion")
        self.assertEqual(self.d(known_slots={"category", "occasion", "color"}).ask_slot, "size")

    def test_budget_not_asked_by_default(self):
        s = self.d(known_slots={"category", "occasion", "color", "size"})
        self.assertIsNone(s.ask_slot)
        self.assertIn("show 1-2 best matches", " ".join(s.directives))

    def test_budget_when_catalog_spread_is_wide(self):
        known = {"category", "occasion", "color", "size"}
        wide = search(cand("A", "50"), cand("B", "400"))
        narrow = search(cand("A", "100"), cand("B", "150"))
        self.assertEqual(self.d(known_slots=known, search_result=wide).ask_slot, "budget")
        self.assertIsNone(self.d(known_slots=known, search_result=narrow).ask_slot)

    def test_budget_when_customer_hints_at_price(self):
        known = {"category", "occasion", "color", "size"}
        self.assertEqual(self.d(known_slots=known, signals={"asks_discount": True}).ask_slot, "budget")
        self.assertEqual(self.d(known_slots=known, signals={"hesitation_reason": "price"}).ask_slot, "budget")
        self.assertIsNone(self.d(known_slots=known | {"budget"}, signals={"asks_discount": True}).ask_slot)

    def test_question_cap(self):
        for asked in (2, 3, 9):
            s = self.d(questions_asked_in_conversation=asked)
            self.assertIn("ask_question", s.forbid)
            self.assertIsNone(s.ask_slot)
            text = " ".join(s.directives)
            self.assertIn("Do NOT ask any question", text)
            self.assertIn("Show 1-2 concrete products", text)
            self.assertNotIn("Ask at most ONE", text)
        for asked in (0, 1):
            s = self.d(questions_asked_in_conversation=asked)
            self.assertNotIn("ask_question", s.forbid)
            self.assertEqual(s.ask_slot, "category")
            self.assertIn("Ask at most ONE short", " ".join(s.directives))

    def test_directive_names_the_slot(self):
        self.assertIn("occasion", " ".join(self.d(known_slots={"category"}).directives))

    def test_other_stages_ask_nothing(self):
        for st in ("recommendation", "persuasion", "objection_handling"):
            self.assertIsNone(decide(stage=st).ask_slot)

    def test_question_cap_applies_to_every_stage(self):
        for st in contracts.STAGES:
            self.assertIn("ask_question", decide(stage=st, questions_asked_in_conversation=2).forbid)
            self.assertIsNone(decide(stage=st, questions_asked_in_conversation=2).ask_slot)


class StageDirectiveTests(SimpleTestCase):
    def text(self, **over):
        return " ".join(decide(**over).directives)

    def test_recommendation(self):
        t = self.text(stage="recommendation", objections=[], readiness="browsing")
        self.assertIn("1-2 best matches", t)
        self.assertIn("concrete reason", t)

    def test_recommendation_search_notes(self):
        sr = SearchResult(note="no_exact_match")
        self.assertIn("exact match is not available", self.text(stage="recommendation", objections=[], search_result=sr))
        sr = SearchResult(note="nothing_matches")
        self.assertIn("Nothing matches exactly", self.text(stage="recommendation", objections=[], search_result=sr))

    def test_objection_handling(self):
        t = self.text(stage="objection_handling")
        self.assertIn("Acknowledge the concern", t)
        self.assertIn("no apology", t)
        self.assertIn("Reframe the value", t)
        self.assertIn("keep the conversation going", t)

    def test_objection_types_get_tailored_lines(self):
        self.assertIn("Respect that they want to think", self.text(objections=[("timing", "")]))
        self.assertIn("size guide", self.text(objections=[("fit", "A1")]))
        self.assertIn("real facts only", self.text(objections=[("quality", "A1")]))
        self.assertIn("store info", self.text(objections=[("shipping", "")]))
        self.assertIn("what is holding them back", self.text(objections=[], readiness="hesitant"))

    def test_persuasion(self):
        t = self.text(stage="persuasion", objections=[], readiness="interested")
        self.assertIn("why it fits THEM", t)
        self.assertIn("buy button", t)

    def test_closing(self):
        s = decide(stage="closing", readiness="ready", objections=[])
        t = " ".join(s.directives)
        self.assertIn("buying effortless", t)
        self.assertIn("No upsell", t)
        self.assertEqual(s.ask_slot, "size")
        self.assertIn("same short message as the buy step", t)
        self.assertNotIn("Present the approved code", t)

    def test_closing_with_known_size_asks_nothing(self):
        s = decide(stage="closing", readiness="ready", objections=[], known_slots={"size"})
        self.assertIsNone(s.ask_slot)

    def test_closing_respects_question_cap(self):
        s = decide(stage="closing", readiness="ready", objections=[], questions_asked_in_conversation=2)
        self.assertIsNone(s.ask_slot)

    def test_hesitant_outside_objection_stage_gets_reassurance(self):
        self.assertIn("seems unsure", self.text(stage="persuasion", objections=[], readiness="hesitant"))
        self.assertNotIn("seems unsure", self.text(stage="objection_handling", readiness="hesitant"))

    def test_discount_never_mentioned_as_an_offer_when_denied(self):
        for st in contracts.STAGES:
            t = self.text(stage=st, discount_codes=[])
            self.assertNotIn("Present the approved code", t)

    def test_no_scarcity_wording_when_allowed(self):
        t = self.text()
        self.assertIn("no deadline", t)

    def test_other_denial_directives(self):
        t = self.text(objections=[], readiness="interested", stage="persuasion", offer_history=True,
                      signals={"asks_discount": True})
        self.assertIn("already shared", t)
        t = self.text(objections=[], readiness="interested", stage="persuasion", discount_codes=[],
                      signals={"asks_discount": True})
        self.assertIn("no extra code", t)


class TraceAndPurityTests(SimpleTestCase):
    def test_rule_trace_records_inputs_as_plain_data(self):
        trace = decide().discount.rule_trace
        for key in ("codes_total", "codes_valid", "stage", "readiness", "engaged_sku", "turn_no", "shown_count",
                    "offer_history", "price_objection", "hesitation", "asks_discount", "trigger", "open_objections"):
            self.assertIn(key, trace)
        self.assertEqual((trace["codes_total"], trace["codes_valid"], trace["engaged_sku"], trace["turn_no"]), (1, 1, "A1", 3))
        self.assertEqual(trace["trigger"], "price_objection")
        for value in trace.values():
            self.assertIsInstance(value, (str, int, bool, list, type(None)))

    def test_denied_trace_names_the_gate(self):
        self.assertEqual(decide(turn_no=1).discount.rule_trace["denied_by"], "too_early")

    def test_same_input_same_output(self):
        codes = [code(2, "B", age_days=3), code(1, "A", age_days=8)]
        kw = dict(discount_codes=codes, search_result=search(cand("B7", "150")), signals={"asks_discount": True})
        results = [decide(**kw) for _ in range(5)]
        for r in results[1:]:
            self.assertEqual(r, results[0])

    def test_inputs_are_not_mutated(self):
        known, objs, state = {"category"}, [("price", "A1")], {"sales": {"x": 1}}
        sig, codes = {"asks_discount": True}, [code()]
        snapshot = (copy.deepcopy(known), copy.deepcopy(objs), copy.deepcopy(state), copy.deepcopy(sig))
        decide(known_slots=known, objections=objs, conversation_state=state, signals=sig, discount_codes=codes,
               search_result=search(cand("B", "100")))
        self.assertEqual((known, objs, state, sig), snapshot)
        self.assertEqual(len(codes), 1)

    def test_state_updates_is_a_fresh_dict(self):
        a = decide(search_result=search(cand("B", "100")))
        b = decide(search_result=search(cand("B", "100")))
        a.state_updates["sales"]["alt_first_done"] = False
        self.assertTrue(b.state_updates["sales"]["alt_first_done"])

    def test_garbage_state_is_tolerated(self):
        for bad in ("x", 5, [], {"sales": "oops"}, {"sales": None}):
            s = decide(conversation_state=bad, search_result=search(cand("B", "100")))
            self.assertTrue(s.alternative_first, bad)

    def test_garbage_signals_are_tolerated(self):
        for bad in (None, "ready", 3, []):
            self.assertTrue(decide(signals=bad).discount.allowed)

    def test_now_defaults_to_the_current_time(self):
        live = DiscountCode(id=1, code="A", is_active=True, expires_at=timezone.now() + timedelta(hours=1))
        dead = DiscountCode(id=2, code="B", is_active=True, expires_at=timezone.now() - timedelta(hours=1))
        self.assertTrue(decide(discount_codes=[live], now=None).discount.allowed)
        self.assertEqual(decide(discount_codes=[dead], now=None).discount.reason, "expired")


# ====================================================================== الداشبورد

class DiscountDashboardDatesTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("staff-dc", password="pw", is_staff=True)
        self.client = Client()
        self.client.force_login(user)
        self.merchant = ApiToken.objects.create(label="Shop", image_quota=5)

    def url_for(self):
        return reverse("apitokens:discount-codes", kwargs={"pk": self.merchant.pk})

    def post(self, **fields):
        data = {"action": "add", "code": "SAVE10", "description": "خصم 10% على كل المنتجات"}
        data.update(fields)
        return self.client.post(self.url_for(), data, follow=True)

    def test_existing_workflow_unchanged_without_dates(self):
        resp = self.post()
        obj = DiscountCode.objects.get(code="SAVE10")
        self.assertEqual((obj.starts_at, obj.expires_at, obj.is_active), (None, None, True))
        self.assertContains(resp, "SAVE10")

    def test_datetime_local_values_are_interpreted_in_the_current_timezone(self):
        self.post(starts_at="2026-12-01T09:30", expires_at="2026-12-31T23:59")
        obj = DiscountCode.objects.get()
        tz = timezone.get_current_timezone()
        self.assertEqual(obj.starts_at, timezone.make_aware(datetime(2026, 12, 1, 9, 30), tz))
        self.assertEqual(obj.expires_at, timezone.make_aware(datetime(2026, 12, 31, 23, 59), tz))

    def test_plain_date_is_accepted(self):
        self.post(expires_at="2026-12-31")
        obj = DiscountCode.objects.get()
        self.assertEqual(obj.expires_at, timezone.make_aware(datetime(2026, 12, 31, 0, 0), timezone.get_current_timezone()))
        self.assertIsNone(obj.starts_at)

    def test_explicit_offset_is_respected(self):
        self.post(expires_at="2026-12-31T10:00:00+00:00")
        self.assertEqual(DiscountCode.objects.get().expires_at, datetime(2026, 12, 31, 10, 0, tzinfo=dt_timezone.utc))

    def test_whitespace_only_means_no_limit(self):
        self.post(starts_at="  ", expires_at="")
        obj = DiscountCode.objects.get()
        self.assertEqual((obj.starts_at, obj.expires_at), (None, None))

    def test_invalid_date_is_rejected_with_a_message(self):
        for bad in ("tomorrow", "2026-13-45", "31/12/2026", "2026-12-31T25:00"):
            with self.subTest(bad):
                resp = self.post(expires_at=bad)
                self.assertEqual(DiscountCode.objects.count(), 0)
                self.assertContains(resp, i18n.t("err_discount_date_invalid", "ar"))

    def test_invalid_start_date_is_rejected(self):
        resp = self.post(starts_at="nope")
        self.assertEqual(DiscountCode.objects.count(), 0)
        self.assertContains(resp, i18n.t("err_discount_date_invalid", "ar"))

    def test_expiry_before_start_is_rejected(self):
        resp = self.post(starts_at="2026-12-31T10:00", expires_at="2026-12-01T10:00")
        self.assertEqual(DiscountCode.objects.count(), 0)
        self.assertContains(resp, i18n.t("err_discount_dates_order", "ar"))

    def test_error_message_follows_the_session_language(self):
        session = self.client.session
        session["lang"] = "en"
        session.save()
        resp = self.post(expires_at="garbage")
        self.assertContains(resp, i18n.t("err_discount_date_invalid", "en"))

    def test_code_required_still_checked_first(self):
        resp = self.post(code="", expires_at="garbage")
        self.assertContains(resp, i18n.t("err_discount_code_required", "ar"))
        self.assertEqual(DiscountCode.objects.count(), 0)

    def test_list_shows_dates_and_badges(self):
        now = timezone.now()
        DiscountCode.objects.create(merchant=self.merchant, code="EXPIREDX", expires_at=now - timedelta(days=1))
        DiscountCode.objects.create(merchant=self.merchant, code="FUTUREX", starts_at=now + timedelta(days=3))
        DiscountCode.objects.create(merchant=self.merchant, code="LIVEX", expires_at=now + timedelta(days=3))
        DiscountCode.objects.create(merchant=self.merchant, code="FOREVERX")
        html = self.client.get(self.url_for()).content.decode()
        self.assertEqual(html.count(i18n.t("status_expired", "ar")), 1)
        self.assertEqual(html.count(i18n.t("status_not_started", "ar")), 1)
        self.assertIn(i18n.t("discount_expires_prefix", "ar"), html)
        self.assertIn(i18n.t("discount_starts_prefix", "ar"), html)
        self.assertIn(i18n.t("discount_no_limit", "ar"), html)
        row = html.split("EXPIREDX")[1].split("</tr>")[0]
        self.assertIn(i18n.t("status_expired", "ar"), row)
        row = html.split("LIVEX")[1].split("</tr>")[0]
        self.assertNotIn(i18n.t("status_expired", "ar"), row)
        self.assertNotIn(i18n.t("status_not_started", "ar"), row)

    def test_form_has_datetime_inputs(self):
        html = self.client.get(self.url_for()).content.decode()
        self.assertIn('name="starts_at"', html)
        self.assertIn('name="expires_at"', html)
        self.assertIn('type="datetime-local"', html)

    def test_toggle_still_works(self):
        obj = DiscountCode.objects.create(merchant=self.merchant, code="T1")
        self.client.post(self.url_for(), {"action": "toggle", "code_id": obj.pk})
        obj.refresh_from_db()
        self.assertFalse(obj.is_active)

    def test_toggle_is_scoped_to_the_merchant(self):
        other = ApiToken.objects.create(label="Other", image_quota=1)
        obj = DiscountCode.objects.create(merchant=other, code="T2")
        resp = self.client.post(self.url_for(), {"action": "toggle", "code_id": obj.pk})
        self.assertEqual(resp.status_code, 404)

    def test_requires_staff(self):
        anon = Client()
        self.assertEqual(anon.get(self.url_for()).status_code, 302)

    def test_saved_codes_flow_into_valid_codes(self):
        now = timezone.now()
        DiscountCode.objects.create(merchant=self.merchant, code="OLDEXP", expires_at=now - timedelta(days=1))
        DiscountCode.objects.create(merchant=self.merchant, code="LATER", starts_at=now + timedelta(days=1))
        DiscountCode.objects.create(merchant=self.merchant, code="GOOD1")
        DiscountCode.objects.create(merchant=self.merchant, code="GOOD2", expires_at=now + timedelta(days=1))
        valid = policy.valid_codes(list(self.merchant.discount_codes.all()), now)
        self.assertEqual([c.code for c in valid], ["GOOD1", "GOOD2"])

    def test_migration_is_in_sync_with_models(self):
        call_command("makemigrations", "apitokens", check=True, dry_run=True, verbosity=0)


class DiscountI18nKeysTests(SimpleTestCase):
    KEYS = ("discount_starts_at_label", "discount_expires_at_label", "discount_dates_hint", "th_validity",
            "discount_starts_prefix", "discount_expires_prefix", "discount_no_limit", "status_expired",
            "status_not_started", "err_discount_date_invalid", "err_discount_dates_order")

    def test_every_key_has_ar_and_en(self):
        for key in self.KEYS:
            for lang in ("ar", "en"):
                self.assertTrue(i18n.TRANSLATIONS[key][lang].strip(), (key, lang))

    def test_arabic_is_saudi(self):
        for key in self.KEYS:
            self.assertEqual(find_non_saudi(i18n.TRANSLATIONS[key]["ar"]), [], key)

    def test_expected_wording(self):
        self.assertEqual(i18n.TRANSLATIONS["discount_expires_prefix"]["ar"], "ينتهي في")
        self.assertEqual(i18n.TRANSLATIONS["discount_starts_prefix"]["ar"], "يبدأ من")
        self.assertEqual(i18n.TRANSLATIONS["status_expired"]["ar"], "منتهي")

    def test_directives_pass_the_saudi_check_trivially(self):
        # التوجيهات إنجليزية: ما في عربي أصلاً ← ما في شي شامي
        for line in decide().directives:
            self.assertEqual(find_non_saudi(line), [])
