"""اختبارات البائع v2 (seller_v2 + prompts_v2): حلقة الأدوات المحدودة، التحقق من الكروت بالكود،
المدقّق، واللهجة السعودية. بدون شبكة وبدون مفاتيح: `llm.chat_step_with_retry` ممثَّل بسكربت
(Script) بيسجّل كل استدعاء (البرومبت، التاريخ، tool_choice)، ومحرّك التوصيات ممثَّل بوحدة وهمية
بتنحقن بـ sys.modules (الوحدة الحقيقية بتنكتب بالتوازي من وكيل تاني).
"""

import itertools
import json
import sys
import types
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from . import prompts_v2
from . import widget_views as wv
from .models import ApiToken, DiscountCode, Product
from .services import contracts, guard, llm, saudi, seller_v2
from .services.contracts import Candidate, Reason, Requirements, SearchResult, SellerInput, Strategy

# ---------------------------------------------------------------- نصوص الاختبار (سعودي سليم)
GOOD_REPLY = "يا هلا! هالفستان الأخضر يجنّن عليك، وخامته مرّة مريحة. ودّك تجرّبه؟"
GOOD_REPLY_2 = "أبشر، هذا الأنسب لك الحين. وش رايك فيه؟"
GOOD_REASON = "لونه هادي ويناسب مناسبتك"
GOOD_LABEL = "جرّبه على صورتك 📸"
SYRIAN_REPLY = "شو رأيك فيه؟ كتير حلو وبدك تجربه هيك."
SYRIAN_REASON = "هيك بيكون لوقتك أحلى"
SYRIAN_LABEL = "جرّبه هلق 📸"
CUSTOMER_AR = "أبي فستان للعيد"

COUNTER = itertools.count(1)


def S(text="", *calls):
    return llm.LLMStep(text=text, tool_calls=list(calls), raw=None)


def C(name, **kwargs):
    return llm.ToolCall(f"call_{next(COUNTER)}", name, kwargs)


def show(sku, reason=GOOD_REASON, **kw):
    return C("show_product", sku=sku, reason=reason, **kw)


class Script:
    """بديل chat_step_with_retry: كل عنصر LLMStep أو استثناء أو دالة. طابور فاضي = فشل صريح
    (ما في استدعاء LLM إضافي مسموح بالاختبار)."""

    def __init__(self, *items):
        self.items = list(items)
        self.calls = []

    def __call__(self, provider, system_prompt, transcript, tools, tool_choice=None, **kw):
        self.calls.append(
            {"system": system_prompt, "transcript": [dict(e) for e in transcript], "tools": tools,
             "tool_choice": tool_choice}
        )
        if not self.items:
            raise AssertionError(f"unexpected extra LLM call #{len(self.calls)} (tool_choice={tool_choice})")
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item() if callable(item) else item

    def tool_results(self, call_index):
        """نتائج الأدوات (JSON مفكوك) يلي شافها الموديل بالاستدعاء رقم call_index."""
        return [json.loads(e["content"]) for e in self.calls[call_index]["transcript"] if e["role"] == "tool"]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- محرّك توصيات وهمي بنفس واجهة recommend.py
def make_fake_recommend():
    mod = types.ModuleType("apitokens.services.recommend")
    mod.search_calls = []
    mod.explode_on = set()

    def check_product(product, req):
        if product.sku in mod.explode_on:
            raise RuntimeError("boom")
        out = []
        exempt = product.sku in req.exempt_skus
        if not product.in_stock:
            out.append("stock")
        if product.sku in req.excluded_skus and not exempt:
            out.append("rejected")
        if req.budget_max is not None and product.price > req.budget_max and not exempt:
            out.append("budget")
        if req.categories and product.category.lower() not in {c.lower() for c in req.categories}:
            out.append("category")
        return out

    def search(products, req, *, query="", limit=8, shown_skus=(), relax=True, query_vector=None):
        mod.search_calls.append({"req": req, "query": query, "limit": limit, "shown": tuple(shown_skus)})
        ok = [p for p in products if not check_product(p, req)]
        return SearchResult(
            candidates=[Candidate(product=p) for p in ok][:limit], exact_count=len(ok),
            note="" if ok else "nothing_matches",
        )

    def describe_candidates(candidates, currency=""):
        rows = []
        for c in candidates:
            p, options = c.product, c.product.options or {}
            rows.append({
                "sku": p.sku, "title": p.title, "price": str(p.price), "currency": currency,
                "category": p.category, "colors": options.get("Color", []), "sizes": options.get("Size", []),
                "description": (p.description or "")[:200], "tryon": p.supports_tryon, "exact": c.exact,
                "why": [r.text for r in c.reasons], "warnings": list(c.warnings),
            })
        return rows

    def compare(products, req=None):
        return {
            "items": [{"sku": p.sku, "price": str(p.price)} for p in products],
            "differences": ["price"], "cheapest_sku": min(products, key=lambda p: p.price).sku,
        }

    def explain(product, req, query=""):
        return [Reason("category", f"it is a {product.category}")]

    mod.check_product, mod.search, mod.describe_candidates = check_product, search, describe_candidates
    mod.compare, mod.explain = compare, explain
    return mod


class SellerBase(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="متجر", token="SELLERV2TOKEN01", image_quota=5, currency="SAR")
        mk = lambda sku, title, price, **kw: Product.objects.create(
            merchant=self.merchant, sku=sku, title=title, price=price, image_url=f"https://x/{sku}.jpg",
            category=kw.pop("category", "dress"), description=kw.pop("description", "Soft flowy fabric"),
            product_url=f"https://shop.example.com/products/{sku.lower()}", **kw,
        )
        self.gown = mk("GOWN", "Black Evening Gown", "350.00", description="Floor-length satin gown",
                       options={"Size": ["S", "M", "L"], "Color": ["Black"]})
        self.green = mk("GREEN", "Green Midi Dress", "180.00", options={"Size": ["M"], "Color": ["Green"]})
        self.red = mk("RED", "Red Maxi Dress", "120.00", compare_at_price="160.00")
        self.shirt = mk("SHIRT", "Cotton Shirt", "90.00", category="shirt")
        self.pricey = mk("PRICEY", "Luxury Dress", "900.00")
        self.abaya = mk("ABAYA", "Rejected Abaya", "100.00", category="abaya")
        self.sold_out = mk("SOLDOUT", "Sold Out Dress", "80.00", in_stock=False)
        DiscountCode.objects.create(merchant=self.merchant, code="SAVE10", description="خصم 10% على كل المنتجات")
        DiscountCode.objects.create(merchant=self.merchant, code="OLD50", description="خصم 50%", is_active=False)
        self.fake = make_fake_recommend()
        patcher = patch.dict(sys.modules, {"apitokens.services.recommend": self.fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_input(self, **over):
        base = dict(
            merchant=self.merchant, window=[{"role": "user", "content": CUSTOMER_AR}], lang="ar",
            memory_section="", strategy=Strategy(), requirements=Requirements(),
            prefetched=SearchResult(
                candidates=[Candidate(product=self.green, reasons=[Reason("color", "color green")]),
                            Candidate(product=self.red)],
                exact_count=2,
            ),
            shown_skus=[], currency="SAR", store_info="", customer_texts=[CUSTOMER_AR], deadline_seconds=25.0,
        )
        base.update(over)
        return SellerInput(**base)

    def run_seller(self, script, inp=None, **over):
        inp = inp or self.make_input(**over)
        self.script = script
        provider = SimpleNamespace(is_configured=lambda: True)
        with patch.object(llm, "chat_step_with_retry", script), patch.object(llm, "get_provider", lambda role=None: provider):
            return seller_v2.run_seller(inp)

    def assertSaudi(self, out):
        texts = [out.reply] + [c["reason"] for c in out.recommendations] + [c["tryon_button_label"] for c in out.recommendations]
        for text in texts:
            self.assertEqual(saudi.find_non_saudi(text), [], text)


# ================================================================ الحلقة
class LoopTests(SellerBase):
    def test_happy_path_is_one_llm_call_with_card_via_terminal_shortcut(self):
        script = Script(S(GOOD_REPLY, show("GREEN", tryon_button_label=GOOD_LABEL)))
        out = self.run_seller(script)
        self.assertEqual((out.llm_calls, out.steps, len(script.calls)), (1, 1, 1))
        self.assertEqual(out.reply, GOOD_REPLY)
        self.assertEqual([c["sku"] for c in out.recommendations], ["GREEN"])
        card = out.recommendations[0]
        self.assertEqual((card["reason"], card["tryon_button_label"], card["price"]), (GOOD_REASON, GOOD_LABEL, "180.00"))
        self.assertTrue(card["tryon_supported"])
        self.assertEqual(out.tool_events, [{"name": "show_product", "input": {"sku": "GREEN", "reason": GOOD_REASON,
                                                                              "tryon_button_label": GOOD_LABEL},
                                            "status": "shown"}])
        self.assertEqual(out.guard, {})
        self.assertIsNone(script.calls[0]["tool_choice"])
        self.assertEqual(len(out.timings["steps_ms"]), 1)
        self.assertIn("seller_ms", out.timings)
        self.assertSaudi(out)

    def test_plain_text_reply_without_tools_is_one_call(self):
        out = self.run_seller(Script(S(GOOD_REPLY_2)))
        self.assertEqual((out.llm_calls, out.reply, out.recommendations), (1, GOOD_REPLY_2, []))

    def test_info_tool_round_trip_feeds_results_into_next_step(self):
        script = Script(
            S("", C("search_products", query="green dress")),
            S(GOOD_REPLY, show("GREEN")),
        )
        out = self.run_seller(script)
        self.assertEqual((out.llm_calls, out.steps), (2, 2))
        self.assertEqual(script.tool_results(0), [])
        results = script.tool_results(1)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertIn("GREEN", [r["sku"] for r in results[0]["results"]])
        second = script.calls[1]["transcript"]
        self.assertEqual([e["role"] for e in second], ["user", "assistant", "tool"])
        self.assertEqual(second[1]["tool_calls"][0]["name"], "search_products")
        self.assertEqual(second[2]["name"], "search_products")
        self.assertEqual([c["sku"] for c in out.recommendations], ["GREEN"])
        self.assertEqual(self.fake.search_calls[0]["query"], "green dress")

    def test_tool_only_show_step_uses_the_first_card_reason_as_the_reply_in_one_call(self):
        # OpenAI يرسل show_product بلا نص كثير: بدل جولة كاملة (~2 ث) الـ reason هو الرد
        script = Script(S("", show("GREEN", reason=GOOD_REASON)))
        out = self.run_seller(script)
        self.assertEqual(out.llm_calls, 1)
        self.assertEqual((out.reply, [c["sku"] for c in out.recommendations]), (GOOD_REASON, ["GREEN"]))
        self.assertIn({"name": "reason_as_reply", "input": {}, "status": "used"}, out.tool_events)

    def test_tool_only_step_continues_when_there_is_no_usable_reason_or_a_tool_failed(self):
        script = Script(S("", show("GREEN", reason="")), S(GOOD_REPLY))
        out = self.run_seller(script)
        self.assertEqual(out.llm_calls, 2)
        self.assertEqual(out.reply, GOOD_REPLY)
        inp = self.make_input(requirements=Requirements(budget_max=Decimal("200")))
        script = Script(S("", show("PRICEY", reason=GOOD_REASON)), S(GOOD_REPLY_2, show("GREEN")))
        out = self.run_seller(script, inp)
        self.assertEqual(out.llm_calls, 2)

    def test_show_error_then_self_correction(self):
        inp = self.make_input(requirements=Requirements(budget_max=Decimal("200")))
        script = Script(
            S(GOOD_REPLY, show("PRICEY")),
            S(GOOD_REPLY_2, show("GREEN")),
        )
        out = self.run_seller(script, inp)
        self.assertEqual(out.llm_calls, 2)
        result = script.tool_results(1)[0]
        self.assertEqual((result["status"], result["violations"]), ("violates_requirement", ["budget"]))
        self.assertEqual([c["sku"] for c in out.recommendations], ["GREEN"])
        self.assertEqual(out.reply, GOOD_REPLY_2)
        self.assertEqual([e["status"] for e in out.tool_events], ["violates_requirement", "shown"])

    def test_identical_call_is_cached_and_not_counted_twice(self):
        script = Script(
            S("", C("get_product", sku="GREEN")),
            S("", C("get_product", sku=" green ".upper().strip()), C("get_product", sku="GOWN")),
            S(GOOD_REPLY_2),
        )
        out = self.run_seller(script)
        events = out.tool_events
        self.assertEqual([e.get("cached") for e in events], [None, True, None])
        again = script.tool_results(2)[1]
        self.assertEqual(again["note"], "already_executed")
        self.assertTrue(again["ok"])
        # الكاش ما انحسب: بقي 4 استدعاءات من الـ6 (مجموع الفعلي 2)
        self.assertEqual(sum(1 for e in events if not e.get("cached")), 2)

    def test_cached_calls_do_not_consume_the_tool_limit(self):
        calls = [C("get_product", sku="GREEN") for _ in range(8)]
        script = Script(S("", *calls), S(GOOD_REPLY_2))
        out = self.run_seller(script)
        self.assertEqual(sum(1 for e in out.tool_events if e["status"] == "tool_limit_reached"), 0)
        self.assertEqual(sum(1 for e in out.tool_events if e.get("cached")), 7)

    def test_same_sku_shown_twice_in_one_step_is_duplicate(self):
        script = Script(
            S(GOOD_REPLY, show("GREEN", reason="الأول"), show("GREEN", reason="الثاني")),
            S(GOOD_REPLY_2),
        )
        out = self.run_seller(script)
        self.assertEqual([c["sku"] for c in out.recommendations], ["GREEN"])
        self.assertEqual(out.recommendations[0]["reason"], "الأول")
        self.assertEqual([r["status"] for r in script.tool_results(1)], ["shown", "duplicate"])
        self.assertEqual(out.llm_calls, 2)

    def test_already_shown_sku_from_input_is_duplicate(self):
        script = Script(S(GOOD_REPLY, show("GREEN")), S(GOOD_REPLY_2))
        out = self.run_seller(script, shown_skus=["GREEN"])
        self.assertEqual(out.recommendations, [])
        self.assertEqual(script.tool_results(1)[0]["status"], "duplicate")
        self.assertEqual(out.reply, GOOD_REPLY_2)

    def test_duplicate_card_reason_is_kept_as_reply_when_model_writes_no_text(self):
        script = Script(S("", show("GREEN", reason=GOOD_REPLY_2)), S(""), S(""))
        out = self.run_seller(script, shown_skus=["GREEN"])
        self.assertEqual(out.reply, GOOD_REPLY_2)

    def test_card_cap_is_three(self):
        script = Script(
            S(GOOD_REPLY, show("GREEN"), show("RED"), show("GOWN"), show("SHIRT")),
            S(GOOD_REPLY_2),
        )
        out = self.run_seller(script)
        self.assertEqual([c["sku"] for c in out.recommendations], ["GREEN", "RED", "GOWN"])
        self.assertEqual([r["status"] for r in script.tool_results(1)], ["shown", "shown", "shown", "card_limit"])

    def test_max_three_steps_and_forced_final_step_ignores_tools(self):
        script = Script(
            S("", C("get_product", sku="GREEN")),
            S("", C("get_product", sku="RED")),
            S(GOOD_REPLY_2, show("GREEN")),   # الخطوة الأخيرة: الأداة بتنتجاهل
        )
        out = self.run_seller(script)
        self.assertEqual((out.llm_calls, out.steps), (3, 3))
        self.assertEqual([c["tool_choice"] for c in script.calls], [None, None, "none"])
        self.assertEqual(out.recommendations, [])
        self.assertEqual(out.reply, GOOD_REPLY_2)
        self.assertEqual(out.tool_events[-1]["status"], "ignored_final_step")

    def test_max_six_tool_calls_and_exhausted_budget_forces_final(self):
        calls = [C("get_product", sku=s) for s in ("GREEN", "RED", "GOWN", "SHIRT")]
        calls += [C("search_products", query="a"), C("search_products", query="b"), C("search_products", query="c")]
        script = Script(S("", *calls), S(GOOD_REPLY_2))
        out = self.run_seller(script)
        statuses = [e["status"] for e in out.tool_events]
        self.assertEqual(statuses.count("tool_limit_reached"), 1)
        self.assertEqual(sum(1 for s in statuses if s != "tool_limit_reached"), 6)
        # الميزانية خلصت ← الخطوة التانية نهائية بـ tool_choice="none" حتى لو لسا ما وصلنا للخطوة 3
        self.assertEqual([c["tool_choice"] for c in script.calls], [None, "none"])
        self.assertEqual(script.tool_results(1)[-1]["error"], "tool_limit_reached")
        self.assertEqual(out.reply, GOOD_REPLY_2)

    def test_deadline_exceeded_between_steps_falls_back_gracefully(self):
        clock = Clock()

        def slow_info():
            clock.t += 30
            return S("", C("get_product", sku="GREEN"))

        inp = self.make_input(deadline_seconds=10)
        with patch.object(seller_v2, "_now", clock):
            out = self.run_seller(Script(slow_info), inp)
        self.assertEqual((out.llm_calls, out.steps), (1, 1))
        self.assertEqual(out.reply, wv.FALLBACK_REPHRASE_REPLY["ar"])
        self.assertSaudi(out)

    def test_deadline_exceeded_before_first_step_makes_no_llm_call(self):
        script = Script()
        out = self.run_seller(script, deadline_seconds=0)
        self.assertEqual((out.llm_calls, len(script.calls)), (0, 0))
        self.assertEqual(out.reply, wv.FALLBACK_REPHRASE_REPLY["ar"])

    def test_deadline_skips_regeneration(self):
        clock = Clock()

        def slow_bad_reply():
            clock.t += 30
            return S("عندنا كود SAVE10 خصم 20%", show("GREEN"))

        with patch.object(seller_v2, "_now", clock):
            out = self.run_seller(Script(slow_bad_reply), self.make_input(deadline_seconds=10))
        self.assertEqual(out.llm_calls, 1)
        self.assertNotIn("SAVE10", out.reply)
        self.assertIn("guard_strip", out.guard)

    def test_tool_exception_returns_internal_error_and_turn_survives(self):
        self.fake.explode_on.add("GREEN")
        script = Script(S(GOOD_REPLY, show("GREEN")), S(GOOD_REPLY_2, show("RED")))
        with self.assertLogs("apitokens.services.seller_v2", level="ERROR"):
            out = self.run_seller(script)
        self.assertEqual(script.tool_results(1)[0], {"ok": False, "error": "internal"})
        self.assertEqual([c["sku"] for c in out.recommendations], ["RED"])
        self.assertEqual(out.tool_events[0]["status"], "internal")

    def test_unknown_tool_name_gets_an_error_result(self):
        script = Script(S("", C("send_email", to="x")), S(GOOD_REPLY_2))
        out = self.run_seller(script)
        self.assertEqual(script.tool_results(1)[0], {"ok": False, "error": "unknown_tool"})
        self.assertEqual(out.reply, GOOD_REPLY_2)

    def test_provider_failure_on_first_step_raises(self):
        with self.assertRaises(llm.LLMError):
            self.run_seller(Script(llm.LLMError("down")))

    def test_provider_failure_on_second_step_degrades_to_fallback(self):
        out = self.run_seller(Script(S("", C("get_product", sku="GREEN")), llm.LLMError("down")))
        self.assertEqual((out.llm_calls, out.reply), (1, wv.FALLBACK_REPHRASE_REPLY["ar"]))

    def test_provider_failure_after_card_uses_card_reason_as_reply(self):
        out = self.run_seller(Script(S("", show("GREEN", reason=GOOD_REPLY_2)), llm.LLMError("down")))
        self.assertEqual(out.reply, GOOD_REPLY_2)
        self.assertEqual([c["sku"] for c in out.recommendations], ["GREEN"])

    def test_regeneration_failure_keeps_going_with_deterministic_strip(self):
        out = self.run_seller(Script(S("عندنا خصم 20% اليوم. فستان حلو.", show("GREEN")), llm.LLMError("down")))
        self.assertEqual(out.reply, "فستان حلو.")
        self.assertEqual(out.llm_calls, 2)


# ================================================================ الأدوات
class ToolTests(SellerBase):
    def test_search_can_only_tighten_requirements(self):
        req = Requirements(
            budget_max=Decimal("200"), categories=("dress",), sizes=("M",), excluded_skus=frozenset({"ABAYA"}),
            avoid_colors=("red",),
        )
        script = Script(
            S("", C("search_products", max_price=5000, category="shoes", size="XL", color="red", query="x")),
            S("", C("search_products", max_price=150, category="dress", size="m", limit=99)),
            S(GOOD_REPLY_2),
        )
        out = self.run_seller(script, self.make_input(requirements=req))
        loose, tight = self.fake.search_calls
        self.assertEqual(loose["req"], req)   # ما تغيّر شي
        self.assertEqual(tight["req"].budget_max, Decimal("150"))
        self.assertEqual((tight["req"].categories, tight["req"].sizes), (("dress",), ("M",)))
        self.assertEqual(tight["req"].excluded_skus, frozenset({"ABAYA"}))
        self.assertEqual(tight["limit"], 5)
        ignored = script.tool_results(1)[0]["ignored_filters"]
        self.assertEqual(sorted(ignored), ["category", "color", "max_price", "size"])
        self.assertNotIn("ignored_filters", script.tool_results(2)[1])
        self.assertEqual(out.reply, GOOD_REPLY_2)

    def test_search_cannot_add_price_when_no_budget_but_can_set_one(self):
        self.run_seller(Script(S("", C("search_products", max_price="120.5")), S(GOOD_REPLY_2)))
        self.assertEqual(self.fake.search_calls[0]["req"].budget_max, Decimal("120.5"))

    def test_search_rows_never_include_out_of_stock_products(self):
        script = Script(S("", C("search_products", query="dress")), S(GOOD_REPLY_2))
        self.run_seller(script)
        skus = [r["sku"] for r in script.tool_results(1)[0]["results"]]
        self.assertNotIn("SOLDOUT", skus)
        self.assertIn("GREEN", skus)

    def test_get_product_facts_and_errors(self):
        req = Requirements(budget_max=Decimal("200"), excluded_skus=frozenset({"ABAYA"}))
        script = Script(
            S("", C("get_product", sku="GOWN"), C("get_product", sku="NOPE"), C("get_product", sku="SOLDOUT"),
              C("get_product", sku="ABAYA")),
            S(GOOD_REPLY_2),
        )
        self.run_seller(script, self.make_input(requirements=req))
        gown, nope, sold, abaya = script.tool_results(1)
        self.assertTrue(gown["ok"])
        row = gown["product"]
        self.assertEqual((row["sku"], row["full_description"], row["options"]["Size"]), ("GOWN", "Floor-length satin gown", ["S", "M", "L"]))
        self.assertEqual((row["can_show"], row["violations"]), (False, ["budget"]))
        self.assertEqual(nope["error"], "unknown_sku")
        self.assertEqual(sold["error"], "out_of_stock")
        self.assertEqual(abaya["error"], "hidden_by_customer_rejection")

    def test_get_product_truncates_description(self):
        self.green.description = "x" * 2000
        self.green.save()
        script = Script(S("", C("get_product", sku="GREEN")), S(GOOD_REPLY_2))
        self.run_seller(script)
        self.assertEqual(len(script.tool_results(1)[0]["product"]["full_description"]), 800)

    def test_rejected_but_exempt_product_is_visible(self):
        req = Requirements(excluded_skus=frozenset({"ABAYA"}), exempt_skus=frozenset({"ABAYA"}))
        script = Script(S("", C("get_product", sku="ABAYA")), S(GOOD_REPLY_2))
        self.run_seller(script, self.make_input(requirements=req))
        self.assertTrue(script.tool_results(1)[0]["ok"])

    def test_compare_products(self):
        script = Script(
            S("", C("compare_products", skus=["GREEN", "RED"]), C("compare_products", skus=["GREEN"]),
              C("compare_products", skus=["GREEN", "NOPE"])),
            S(GOOD_REPLY_2),
        )
        self.run_seller(script)
        ok, too_few, bad = script.tool_results(1)
        self.assertEqual((ok["ok"], ok["cheapest_sku"], [i["sku"] for i in ok["items"]]), (True, "RED", ["GREEN", "RED"]))
        self.assertEqual(too_few["error"], "invalid_skus")
        self.assertEqual(bad["problems"], {"NOPE": "unknown_sku"})

    def test_show_product_blocks_rejected_over_budget_out_of_stock_and_unknown(self):
        req = Requirements(budget_max=Decimal("200"), excluded_skus=frozenset({"ABAYA"}))
        script = Script(
            S(GOOD_REPLY, show("ABAYA"), show("PRICEY"), show("SOLDOUT"), show("NOPE")),
            S(GOOD_REPLY_2),
        )
        out = self.run_seller(script, self.make_input(requirements=req))
        results = script.tool_results(1)
        self.assertEqual([r["status"] for r in results],
                         ["violates_requirement", "violates_requirement", "out_of_stock", "unknown_sku"])
        self.assertEqual(results[0]["violations"], ["rejected"])
        self.assertEqual(results[1]["violations"], ["budget"])
        self.assertEqual(out.recommendations, [])

    def test_show_product_allows_explicitly_requested_rejected_product(self):
        req = Requirements(excluded_skus=frozenset({"ABAYA"}), exempt_skus=frozenset({"ABAYA"}))
        out = self.run_seller(Script(S(GOOD_REPLY, show("ABAYA"))), self.make_input(requirements=req))
        self.assertEqual([c["sku"] for c in out.recommendations], ["ABAYA"])

    def test_show_product_card_fields_and_label_language(self):
        script = Script(S("Great pick!", show("GREEN", reason="Soft and easy", tryon_button_label="جرّبه 📸",
                                                for_someone_else=True)))
        out = self.run_seller(script, lang="en", window=[{"role": "user", "content": "I want a dress"}],
                              customer_texts=["I want a dress"])
        card = out.recommendations[0]
        # عنوان بحروف غير لغة الزبون ← النص الثابت بلغته (نسخة الهدية)
        self.assertEqual(card["tryon_button_label"], wv.TRYON_BUTTON_LABELS["en"]["gift"])
        self.assertEqual({"sku", "title", "price", "compare_at_price", "currency", "image_url", "product_url",
                          "variant_id", "product_id", "has_options", "reason", "tryon_supported",
                          "tryon_button_label"}, set(card))
        self.assertEqual(card["currency"], "SAR")

    def test_card_reason_in_wrong_script_is_blanked(self):
        out = self.run_seller(Script(S(GOOD_REPLY, show("GREEN", reason="Lovely dress for you"))))
        self.assertEqual(out.recommendations[0]["reason"], "")
        self.assertEqual(out.llm_calls, 1)

    def test_size_tool_appends_computed_sentence(self):
        script = Script(
            S("", C("recommend_size", sku="GOWN", height_cm=165, weight_kg=60)),
            S("أبشر، القصة راح تجلس عليك حلو."),
        )
        out = self.run_seller(script)
        result = script.tool_results(1)[0]
        self.assertEqual((result["ok"], result["size"], result["available"]), (True, "M", True))
        self.assertIn("Do NOT state", result["note"])
        expected = wv.SIZE_REPLY["ar"]["fit"].format(title="Black Evening Gown", size="M")
        self.assertIn(expected, out.reply)
        self.assertEqual(out.reply.count(expected), 1)
        self.assertTrue(out.reply.startswith("أبشر، القصة راح تجلس عليك حلو."))
        self.assertEqual((out.size_advice["size"], out.size_advice["sku"]), ("M", "GOWN"))
        self.assertNotIn("text", out.size_advice)
        self.assertSaudi(out)

    def test_size_tool_without_measurements_and_with_unknown_sku(self):
        script = Script(
            S("", C("recommend_size", sku="GOWN"), C("recommend_size", sku="NOPE", height_cm=170)),
            S(""),
            S(""),
        )
        out = self.run_seller(script)
        no_measure, unknown = script.tool_results(1)
        self.assertEqual(no_measure["error"], "no_size_advice")
        self.assertEqual(unknown["error"], "unknown_sku")
        # بلا نص وبلا كرت وطلب مقاس ← بنسأل عن القياسات
        self.assertEqual(out.reply, wv.SIZE_REPLY["ar"]["ask"])
        self.assertIsNone(out.size_advice)


# ================================================================ الخصم والمدقّق
class GuardTests(SellerBase):
    OFFER = {"code": "SAVE10", "description": "خصم 10% على كل المنتجات"}

    def test_no_offer_model_mentions_discount_regenerates_then_clean(self):
        script = Script(
            S("فستان حلو عليك. عندنا خصم 20% بكود SAVE10 الحين!", show("GREEN")),
            S(GOOD_REPLY_2),
        )
        out = self.run_seller(script)
        self.assertEqual((out.llm_calls, out.steps), (2, 1))
        self.assertEqual(out.reply, GOOD_REPLY_2)
        self.assertIn("unapproved_code", out.guard["guard_first"])
        self.assertNotIn("guard_strip", out.guard)
        self.assertIsNone(out.discount_offer)
        regen = script.calls[1]
        self.assertEqual(regen["tool_choice"], "none")
        self.assertIn("CORRECTION", regen["system"])
        self.assertIn("GREEN", regen["system"])

    def test_no_offer_regeneration_still_violating_gets_stripped(self):
        script = Script(
            S("فستان حلو عليك. عندنا خصم 20% الحين!", show("GREEN")),
            S("هذا مرّة يناسبك. خذه بكود SAVE10 بسعر 99 ريال!"),
        )
        out = self.run_seller(script)
        self.assertEqual(out.llm_calls, 2)
        self.assertEqual(out.reply, "هذا مرّة يناسبك.")
        self.assertTrue({"unapproved_code", "unapproved_promo"} <= set(out.guard["guard_strip"]))
        self.assertEqual(guard.find_violations(out.reply, seller_v2.guard.build_context(products=[], merchant_codes=["SAVE10"])), [])

    def test_everything_stripped_falls_to_safe_reply(self):
        script = Script(S("خصم 30% لك!", show("GREEN")), S("خصم 40% لك!"))
        out = self.run_seller(script)
        self.assertEqual(out.reply, guard.SAFE_REPLY["ar"])

    def test_card_reason_with_promo_is_stripped(self):
        script = Script(
            S(GOOD_REPLY, show("GREEN", reason="فستان حلو. خذه بخصم 25% اليوم")),
            S(GOOD_REPLY_2),
        )
        out = self.run_seller(script)
        self.assertEqual(out.recommendations[0]["reason"], "فستان حلو.")
        self.assertEqual(out.llm_calls, 2)

    def test_approved_offer_code_added_exactly_once_when_model_omits_it(self):
        out = self.run_seller(Script(S(GOOD_REPLY, show("GREEN"))), approved_offer=dict(self.OFFER))
        self.assertEqual(out.reply.count("SAVE10"), 1)
        self.assertTrue(out.reply.startswith(GOOD_REPLY))
        self.assertIn(wv.DISCOUNT_APPENDIX["ar"].format(**self.OFFER), out.reply)
        self.assertEqual(out.discount_offer, self.OFFER)
        self.assertEqual(out.llm_calls, 1)
        self.assertEqual(out.guard, {})
        self.assertSaudi(out)

    def test_approved_offer_code_not_duplicated_when_model_includes_it_in_any_case(self):
        out = self.run_seller(Script(S("خذ كود save10 وتحصل خصم 10%، وش رايك؟", show("GREEN"))),
                              approved_offer=dict(self.OFFER))
        self.assertEqual(out.reply.lower().count("save10"), 1)
        self.assertEqual(out.llm_calls, 1)

    def test_approved_offer_with_invented_numbers_is_blocked(self):
        script = Script(
            S("خذ كود SAVE10 وتحصل خصم 33% على الفستان.", show("GREEN")),
            S("يستاهل كل ريال، وكود SAVE10 يسهّلها عليك."),
        )
        out = self.run_seller(script, approved_offer=dict(self.OFFER))
        self.assertEqual(out.llm_calls, 2)
        self.assertIn("unverified_percent", out.guard["guard_first"])
        self.assertEqual(out.reply.count("SAVE10"), 1)
        self.assertNotIn("33", out.reply)

    def test_model_computed_discounted_price_is_blocked(self):
        script = Script(
            S("السعر بعد الخصم 162 ريال بس. يجنّن عليك.", show("GREEN")),
            S("السعر بعد الخصم 162 ريال بس. يجنّن عليك."),
        )
        out = self.run_seller(script, approved_offer=dict(self.OFFER))
        self.assertEqual(out.llm_calls, 2)
        self.assertNotIn("162", out.reply)
        self.assertIn("unverified_amount", out.guard["guard_strip"])
        # الجملة المحذوفة ما شالت الكود: بينلزق بجملة ثابتة
        self.assertEqual(out.reply.count("SAVE10"), 1)

    def test_inactive_merchant_code_is_never_allowed(self):
        script = Script(S(GOOD_REPLY + " كود OLD50 يخدمك.", show("GREEN")), S(GOOD_REPLY_2))
        out = self.run_seller(script, approved_offer=dict(self.OFFER))
        self.assertIn("unapproved_code", out.guard["guard_first"])
        self.assertNotIn("OLD50", out.reply)

    def test_real_product_price_is_allowed(self):
        out = self.run_seller(Script(S("سعره 180 ريال بس وهو مرّة يستاهل.", show("GREEN"))))
        self.assertEqual(out.llm_calls, 1)
        self.assertEqual(out.guard, {})

    def test_only_one_regeneration_when_dialect_and_guard_both_fire(self):
        script = Script(
            S("شو رأيك؟ خذه بخصم 20% هلق!", show("GREEN", reason=SYRIAN_REASON, tryon_button_label=SYRIAN_LABEL)),
            S("شو رأيك؟ خذه بخصم 20% هلق وكتير حلو!"),
        )
        out = self.run_seller(script)   # أي استدعاء تالت = AssertionError
        self.assertEqual(out.llm_calls, 2)
        self.assertEqual(len(script.calls), 2)
        self.assertIn("guard_strip", out.guard)
        self.assertIn("dialect_substituted", out.guard)
        self.assertNotIn("20", out.reply)
        self.assertSaudi(out)

    def test_only_one_regeneration_when_mismatch_and_guard_both_fire(self):
        script = Script(
            S("Great choice! 20% off for you today.", show("GREEN")),
            S("Great choice! 20% off for you today."),
        )
        out = self.run_seller(script)
        self.assertEqual(out.llm_calls, 2)
        self.assertNotIn("20%", out.reply)


# ================================================================ اللهجة واللغة
class DialectAndLanguageTests(SellerBase):
    def test_syrian_reply_regenerates_then_substitutes_card_texts(self):
        script = Script(
            S(SYRIAN_REPLY, show("GREEN", reason=SYRIAN_REASON, tryon_button_label=SYRIAN_LABEL)),
            S(GOOD_REPLY_2),
        )
        out = self.run_seller(script)
        self.assertEqual(out.llm_calls, 2)
        self.assertEqual(out.reply, GOOD_REPLY_2)   # التجديد نظّف الرد
        self.assertSaudi(out)                        # الكرت والعنوان انصلحوا بالاستبدال الحتمي
        self.assertEqual(out.recommendations[0]["tryon_button_label"], "جرّبه الحين 📸")
        self.assertEqual(out.recommendations[0]["reason"], "كذا بيكون لوقتك أحلى")
        self.assertEqual(sorted(out.guard["dialect_substituted"]), ["هلق", "هيك"])
        self.assertIn("dialect", out.guard["guard_first"])
        self.assertIn("not Saudi", script.calls[1]["system"])

    def test_syrian_reply_after_regeneration_is_substituted_deterministically(self):
        script = Script(S(SYRIAN_REPLY, show("GREEN")), S("شو رأيك؟ كتير حلو، بدك تجربه هيك."))
        out = self.run_seller(script)
        self.assertEqual(out.llm_calls, 2)
        self.assertEqual(out.reply, "وش رأيك؟ مرّة حلو، تبي تجربه كذا.")
        self.assertEqual(sorted(out.guard["dialect_substituted"]), ["بدك", "شو", "كتير", "هيك"])
        self.assertNotIn("guard_strip", out.guard)
        self.assertSaudi(out)

    def test_words_without_a_safe_substitute_are_dropped_not_shown(self):
        bad = "هالفستان حلو. حابب تجربه؟ بدو يعجبك."
        script = Script(S(bad, show("GREEN")), S(bad))
        out = self.run_seller(script)
        self.assertEqual(saudi.find_non_saudi(out.reply), [])
        self.assertIn("هالفستان حلو.", out.reply)
        self.assertSaudi(out)

    def test_dialect_not_enforced_for_english(self):
        out = self.run_seller(Script(S("Great pick!", show("GREEN", reason="Soft and easy"))), lang="en",
                              window=[{"role": "user", "content": "I want a dress"}], customer_texts=["I want a dress"])
        self.assertEqual((out.llm_calls, out.reply, out.guard), (1, "Great pick!", {}))

    def test_english_reply_to_arabic_customer_regenerates_in_arabic(self):
        script = Script(
            S("Great choice, you'll love it!", show("GREEN")),
            S(GOOD_REPLY_2),
        )
        out = self.run_seller(script)
        self.assertEqual((out.llm_calls, out.reply), (2, GOOD_REPLY_2))
        self.assertIn("wrong language", script.calls[1]["system"])
        self.assertIn(CUSTOMER_AR, script.calls[1]["system"])
        self.assertEqual(script.calls[1]["tool_choice"], "none")

    def test_failed_language_correction_keeps_original_reply(self):
        script = Script(S("Great choice!", show("GREEN")), S("Still English!"))
        out = self.run_seller(script)
        self.assertEqual((out.llm_calls, out.reply), (2, "Great choice!"))

    def test_arabic_reply_to_english_customer_regenerates(self):
        script = Script(S(GOOD_REPLY, show("GREEN", reason="Soft and easy")), S("Lovely pick, a great fit for you!"))
        out = self.run_seller(script, lang="en", window=[{"role": "user", "content": "I want a dress"}],
                              customer_texts=["I want a dress"])
        self.assertEqual((out.llm_calls, out.reply), (2, "Lovely pick, a great fit for you!"))

    def test_fixed_arabic_literals_are_saudi(self):
        strings = [GOOD_REPLY, GOOD_REPLY_2, GOOD_REASON, GOOD_LABEL, guard.SAFE_REPLY["ar"],
                   wv.FALLBACK_REPHRASE_REPLY["ar"], wv.PURCHASE_REPLY["ar"],
                   wv.DISCOUNT_APPENDIX["ar"].format(code="X", description="خصم 10%"),
                   wv.FALLBACK_DISCOUNT_REPLY["ar"].format(code="X", description="خصم 10%"),
                   *wv.TRYON_BUTTON_LABELS["ar"].values(), *wv.SIZE_REPLY["ar"].values(),
                   *seller_v2._EXTRA_SUBSTITUTIONS.values(), *saudi._SUBSTITUTIONS.values()]
        for text in strings:
            self.assertEqual(saudi.find_non_saudi(text), [], text)
        self.assertNotEqual(saudi.find_non_saudi(SYRIAN_REPLY), [])   # الفحص نفسه شغّال

    def test_fallback_texts_are_composed_when_no_text(self):
        # كرت بسبب فاضي + نية شراء + بلا نص ← جملة الشراء الثابتة
        out = self.run_seller(Script(S("", show("GREEN", reason="")), S("")),
                              window=[{"role": "user", "content": "كيف اطلبه؟"}], customer_texts=["كيف اطلبه؟"])
        self.assertEqual(out.reply, wv.PURCHASE_REPLY["ar"])

    def test_offer_only_fallback_reply_contains_code_once(self):
        out = self.run_seller(Script(S(""), S(""), S("")), approved_offer={"code": "SAVE10", "description": "خصم 10%"})
        self.assertEqual(out.reply.count("SAVE10"), 1)
        self.assertEqual(out.discount_offer["code"], "SAVE10")


# ================================================================ البرومبت
class PromptTests(SellerBase):
    def prompt(self, **over):
        return prompts_v2.build_system_prompt(self.make_input(**over))

    def test_strategy_directives_ask_slot_and_forbids_are_rendered(self):
        strategy = Strategy(
            stage="objection_handling", readiness="hesitant",
            directives=["Acknowledge the price concern in a few words.", "Reframe the value in one sentence."],
            ask_slot="color", forbid=["push_discount", "upsell"], alternative_first=True,
        )
        prompt = self.prompt(strategy=strategy)
        for needle in ("objection_handling", "hesitant", "Acknowledge the price concern", "Reframe the value",
                       "only about: color", "Do not push or hint at a discount", "Do not suggest extra",
                       "Alternative first"):
            self.assertIn(needle, prompt)

    def test_ask_question_forbidden_means_no_question(self):
        prompt = self.prompt(strategy=Strategy(ask_slot="size", forbid=["ask_question"]))
        self.assertIn("ask no question this turn", prompt)
        self.assertNotIn("only about: size", prompt)
        self.assertIn("ask no question this turn", self.prompt(strategy=Strategy()))

    def test_prefetched_candidates_json_memory_store_and_shown_present(self):
        prompt = self.prompt(memory_section="\nCUSTOMER MEMORY — budget 200 SAR\n", store_info="Shipping takes 3 days.",
                             shown_skus=["GOWN", "RED"])
        marker = "CANDIDATES"
        section = prompt[prompt.index(marker):]
        payload = next(line for line in section.splitlines() if line.startswith("[{"))
        rows = json.loads(payload)
        self.assertEqual([r["sku"] for r in rows], ["GREEN", "RED"])
        self.assertEqual(rows[0]["why"], ["color green"])
        self.assertTrue(rows[1].get("already_shown"))      # RED على الشاشة: معلَّم
        self.assertNotIn("already_shown", rows[0])
        self.assertIn("already_shown are already on the customer's screen", section)
        self.assertIn("CUSTOMER MEMORY — budget 200 SAR", prompt)
        self.assertIn("Shipping takes 3 days.", prompt)
        self.assertIn("don't show these cards again): GOWN, RED", prompt)

    def test_store_info_is_capped(self):
        prompt = self.prompt(store_info="A" * 9000 + "ZZZ")
        self.assertIn("A" * 6000, prompt)
        self.assertNotIn("A" * 6001, prompt)
        self.assertNotIn("ZZZ", prompt)

    def test_no_discount_sentence_when_no_approved_offer(self):
        prompt = self.prompt()
        self.assertIn(prompts_v2.NO_DISCOUNT_SENTENCE, prompt)
        self.assertNotIn("SAVE10", prompt)

    def test_approved_offer_is_rendered_with_description_and_rules(self):
        prompt = self.prompt(approved_offer={"code": "SAVE10", "description": "خصم 10% على كل المنتجات"})
        self.assertNotIn(prompts_v2.NO_DISCOUNT_SENTENCE, prompt)
        self.assertIn("SAVE10", prompt)
        self.assertIn("خصم 10% على كل المنتجات", prompt)
        self.assertIn("NEVER compute a discounted price", prompt)

    def test_prefetched_notes(self):
        no_exact = SearchResult(candidates=[Candidate(product=self.green, exact=False, warnings=["color blue not available"])],
                                exact_count=0, note="no_exact_match")
        prompt = self.prompt(prefetched=no_exact)
        self.assertIn("No candidate matches everything exactly", prompt)
        self.assertIn("color blue not available", prompt)
        nothing = self.prompt(prefetched=SearchResult(note="nothing_matches"))
        self.assertIn("Nothing in the catalog fits right now", nothing)
        self.assertIn("ONE question", nothing)
        self.assertNotIn("Nothing in the catalog fits", self.prompt())

    def test_style_block_only_for_arabic(self):
        self.assertIn(saudi.STYLE_BLOCK, self.prompt(lang="ar"))
        self.assertNotIn("SAUDI ARABIC — mandatory", self.prompt(lang="en"))

    def test_static_text_comes_first_and_dynamic_last(self):
        prompt = self.prompt(
            memory_section="CUSTOMER MEMORY X", store_info="STORE INFO X", shown_skus=["GOWN"],
            strategy=Strategy(directives=["DIRECTIVE X"]), page_note="PAGE NOTE X",
            approved_offer={"code": "SAVE10", "description": "خصم 10%"},
        )
        marker = prompt.index(prompts_v2.THIS_TURN_MARKER)
        for static in ("LANGUAGE", saudi.STYLE_BLOCK, "HONESTY", "HOW YOU SELL", "TOOLS"):
            self.assertLess(prompt.index(static), marker, static)
        for dynamic in ("SALES STRATEGY FOR THIS TURN", "DIRECTIVE X", "SAVE10", "STORE INFO X", "CUSTOMER MEMORY X",
                        "PAGE NOTE X", "don't show these cards again", "CANDIDATES"):
            self.assertGreater(prompt.index(dynamic), marker, dynamic)
        # الجزء الثابت متطابق حرفياً بين دورين مختلفين (يفيد الكاش)
        other = self.prompt(memory_section="different", strategy=Strategy(stage="closing"))
        self.assertEqual(prompt[:marker], other[:other.index(prompts_v2.THIS_TURN_MARKER)])

    def test_static_prompt_is_lean(self):
        words = len(prompts_v2.static_prompt("en").split())
        self.assertLess(words, 900)
        self.assertGreater(words, 300)

    def test_honesty_rules_are_in_the_prompt(self):
        prompt = self.prompt()
        for needle in ("Never invent materials", "ONLY from \"Store information\"", "Never compute prices",
                       "recommend_size", "show_product", "ONE short, confident nudge", "ONE question per reply"):
            self.assertIn(needle, prompt)

    def test_page_product_default_text(self):
        prompt = self.prompt(page_product=self.gown)
        self.assertIn("GOWN (Black Evening Gown)", prompt)

    def test_run_seller_sends_the_built_prompt_and_all_tools(self):
        script = Script(S(GOOD_REPLY))
        self.run_seller(script, strategy=Strategy(directives=["DIRECTIVE X"]))
        self.assertIn("DIRECTIVE X", script.calls[0]["system"])
        self.assertEqual({t["name"] for t in script.calls[0]["tools"]},
                         {"search_products", "get_product", "compare_products", "show_product", "recommend_size"}
                         - ({"search_products"} if len(script.calls[0]["tools"]) == 4 else set()))
        for tool in script.calls[0]["tools"]:
            self.assertEqual(tool["parameters"]["type"], "object")


class SearchToolVisibilityTests(SellerBase):
    """مرشّحين كفاية جاهزين ← أداة البحث مخفية (بتوفّر جولة LLM كاملة)."""

    def tools_sent(self, **over):
        script = Script(S(GOOD_REPLY))
        self.run_seller(script, **over)
        return [t["name"] for t in script.calls[0]["tools"]]

    def test_search_is_hidden_with_two_unshown_prefetched_candidates(self):
        names = self.tools_sent()   # الافتراضي بالـ fixture: مرشّحين اثنين غير معروضين
        self.assertNotIn("search_products", names)
        self.assertIn("show_product", names)

    def test_search_stays_when_candidates_are_few_or_already_shown(self):
        shown = [c.product.sku for c in self.make_input().prefetched.candidates]
        self.assertIn("search_products", self.tools_sent(shown_skus=shown))
        self.assertIn("search_products", self.tools_sent(prefetched=SearchResult()))
