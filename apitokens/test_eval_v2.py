"""اختبارات أدوات التقييم (eval_v2 / bench_v2 / kieai_live_check) — كلها بدون شبكة وبدون LLM حقيقي.

الـ LLM ممثَّل بـ ScriptedLLM: بيرد حسب نص آخر رسالة زبون (مش حسب ترتيب الاستدعاءات)، فالاختبارات ما بتنكسر
لو تغيّر عدد استدعاءات المحرّك لكل دور. الهدف: نمسك أخطاء الأدوات نفسها (الودجت، الفحوصات، التقرير، رمز الخروج)
قبل ما نشغّلها على الموديل الحقيقي.
"""

import io
import json
import os
import tempfile
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from apitokens.evals import describe, percentile, v2_checks, v2_scenarios
from apitokens.management.commands import bench_v2, eval_v2, kieai_live_check
from apitokens.models import ApiToken, DiscountCode, Product
from apitokens.services import guard, llm

ANALYZE = "update_customer_profile"


# ======================================================================
# LLM ممثَّل
# ======================================================================

class FakeProvider:
    model = "fake-seller-model"

    def is_configured(self):
        return True


def budget_analysis(amount, evidence):
    return {"preferences": [{"key": "budget_max", "value": str(amount), "currency": "SAR",
                             "source": "explicit", "evidence": evidence}]}


class ScriptedLLM:
    """rules: {نص جزئي من رسالة الزبون: {"reply", "cards": [sku], "offer": bool, "analysis": {...}}}.
    أول مفتاح موجود برسالة الزبون هو يلي بينطبق؛ وإلا الرد الافتراضي (آمن وسعودي، بدون كروت)."""

    DEFAULT = {"reply": "أبشر، قل لي وش يعجبك وأنا معك."}

    def __init__(self, rules=None):
        self.rules = dict(rules or {})
        self.calls = []

    def behavior(self, text):
        for key, value in self.rules.items():
            if key in text:
                return value
        return self.DEFAULT

    @staticmethod
    def _last_customer_line(messages):
        content = messages[-1]["content"]
        lines = [ln for ln in content.splitlines() if ln.startswith("CUSTOMER: ")]
        return lines[-1][len("CUSTOMER: "):] if lines else content

    def _tool_calls(self, b):
        calls = [{"name": "recommend_product", "input": {"sku": sku, "reason": b.get("reason", "قطعة حلوة وتناسب طلبك")}}
                 for sku in b.get("cards", [])]
        if b.get("offer"):
            calls.append({"name": "offer_discount_code", "input": {"code": "SAVE10", "pitch": "خذ الكود وكمّل"}})
        return calls

    def chat(self, provider, system_prompt, messages, tools, force_tool=None, **kw):
        analyzer = force_tool == ANALYZE
        self.calls.append({"analyzer": analyzer, "system": system_prompt})
        if analyzer:
            b = self.behavior(self._last_customer_line(messages))
            return llm.LLMResult("", [{"name": ANALYZE, "input": b.get("analysis", {})}])
        b = self.behavior(messages[-1]["content"])
        return llm.LLMResult(b["reply"], self._tool_calls(b))

    def step(self, provider, system_prompt, transcript, tools, tool_choice=None, **kw):
        self.calls.append({"analyzer": False, "system": system_prompt, "step": True})
        user = next(m["content"] for m in reversed(transcript) if m["role"] == "user")
        b = self.behavior(user)
        # حلقة الأدوات الجديدة: show_product (مش recommend_product) والخصم بيجي من السياسة (ما في أداة خصم)
        shows = [c for c in self._tool_calls(b) if c["name"] == "recommend_product"]
        if transcript[-1]["role"] == "tool" or not shows:
            return llm.LLMStep(text=b["reply"], tool_calls=[])
        return llm.LLMStep(text=b["reply"], tool_calls=[
            llm.ToolCall(id=f"c{i}", name="show_product", input=c["input"]) for i, c in enumerate(shows)])


RULES = {
    # returning_customer_remembers
    "ميزانيتي ما تتعدى 300 ريال": {"reply": "أبشر، عندنا خيارات حلوة للعرس.", "analysis": budget_analysis(300, "300 ريال")},
    "وريني الخيارات": {"reply": "هذا يجنن للعرس، شيفون ناعم ومريح.", "cards": ["DRS-SAGE"]},
    "هلا، رجعت": {"reply": "يا هلا فيك من جديد! نكمل من وين وقفنا."},
    "وش عندكم يناسب العرس": {"reply": "شوف هذا، خفيف وأنيق.", "cards": ["DRS-MAXI"]},
    "وكم الشحن": {"reply": "الشحن 25 ريال والتوصيل من 2 إلى 4 أيام عمل."},
    # tryon_flow
    "أبي فستان صيفي": {"reply": "أبشر، عندنا فساتين صيفية خفيفة."},
    "وريني": {"reply": "هذا من أحلى الصيفية عندنا.", "cards": ["DRS-ROSE"], "reason": "فستان صيفي خفيف ومريح"},
    "وش رأيك؟ يناسبني؟": {"reply": "جربته على صورتك وطلع عليك مرة زين، يجنن."},
    "أبي أكمل الطلب": {"reply": "تقدر تطلبه الحين من زر الشراء تحت."},
    "كم يوصل": {"reply": "التوصيل من 2 إلى 4 أيام عمل."},
    # navigation_flow
    "كم سعر هذا": {"reply": "سعره 289 ريال، فستان ميدي شيفون حلو."},
    "وهذي كم سعرها": {"reply": "سعرها 249 ريال، عباية كلاسيك عملية."},
    "ارجع للفستان الأخضر": {"reply": "أبشر، هذا هو.", "cards": ["DRS-SAGE"]},
    # forget_me
    "ميزانيتي 250 ريال": {"reply": "أبشر، وصلتني.", "analysis": budget_analysis(250, "ميزانيتي 250 ريال")},
}

# فصحى متكلّفة (لقد/سوف): المحرّك ما بيصلحها تلقائياً (بيستبدل بس كلمات شامية/مصرية معروفة)
FAIL_RULES = {"هلا": {"reply": "لقد وصلنا طلبك وسوف نساعدك بأسرع وقت."}}


class EvalCase(TestCase):
    """يرقّع الـ LLM والمزوّد؛ self.llm = ScriptedLLM."""

    rules = RULES

    def setUp(self):
        self.llm = ScriptedLLM(self.rules)
        for p in (
            patch("apitokens.services.llm.chat_with_retry", side_effect=self.llm.chat),
            patch("apitokens.services.llm.chat_step_with_retry", side_effect=self.llm.step),
            patch("apitokens.services.llm.get_provider", side_effect=lambda role=None: FakeProvider()),
        ):
            p.start()
            self.addCleanup(p.stop)

    def run_eval(self, *args, expect_error=None):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "report.json")
            try:
                call_command("eval_v2", *args, "--use-current-db", "--out", path, stdout=out)
                error = None
            except CommandError as exc:
                error = exc
            with open(path, encoding="utf-8") as fh:
                report = json.load(fh) if os.path.exists(path) else None
        if expect_error is None:
            self.assertIsNone(error, f"unexpected failure: {error}\n{out.getvalue()}")
        return report, out.getvalue(), error


# ======================================================================
# منفّذ eval_v2 end-to-end
# ======================================================================

class EvalHarnessEndToEndTests(EvalCase):
    SCENARIOS = "returning_customer_remembers,tryon_flow,forget_me,navigation_flow,no_discount_available"

    def test_report_structure_and_every_step_type_is_executed(self):
        report, console, _ = self.run_eval("--only", self.SCENARIOS)
        self.assertEqual(set(report), {"meta", "summary", "scenarios"})
        self.assertEqual(report["meta"]["assistant_model"], "fake-seller-model")
        self.assertEqual(sorted(s["name"] for s in report["scenarios"]), sorted(self.SCENARIOS.split(",")))
        steps = {rec["step"] for s in report["scenarios"] for rec in s["turns"]}
        self.assertEqual(steps, {"say", "navigate", "new_visit", "tryon", "event", "forget"})

        first = report["scenarios"][0]
        turn = next(t for t in first["turns"] if t["step"] == "say")
        for key in ("user", "reply", "cards", "offer", "latency_s", "trace", "checks", "facts", "page_url"):
            self.assertIn(key, turn)
        self.assertGreaterEqual(turn["trace"]["llm_calls"], 1)
        names = {c["name"] for c in turn["checks"]}
        self.assertTrue({"saudi", "no_invented_commerce", "no_duplicate_cards", "reply_not_empty"} <= names)
        summary = report["summary"]
        self.assertEqual(summary["scenarios"], 5)
        self.assertGreater(summary["checks"]["saudi"]["pass"], 10)
        self.assertIsNotNone(summary["latency_s"]["p95"])
        self.assertIsNotNone(summary["llm_calls_per_turn"])
        self.assertIn("per-check totals", console)
        self.assertIn("PASS", console)

    def test_all_scripted_scenarios_pass_and_exit_cleanly(self):
        report, _, _ = self.run_eval("--only", self.SCENARIOS)
        failures = [(s["name"], c["name"], c["note"]) for s in report["scenarios"] for r in s["turns"]
                    for c in r.get("checks", []) if not c["ok"] and c["severity"] == "hard"]
        self.assertEqual(failures, [])
        self.assertEqual(report["summary"]["hard_failures"], 0)

    def test_returning_customer_keeps_memory_and_new_visit_is_a_new_conversation(self):
        report, _, _ = self.run_eval("--only", "returning_customer_remembers")
        turns = report["scenarios"][0]["turns"]
        budget_turn = next(t for t in turns if t["step"] == "say" and "300 ريال" in t["user"])
        self.assertIn(("budget_max", "300"), [(f["key"], f["value"]) for f in budget_turn["facts"]])
        visit = next(t for t in turns if t["step"] == "new_visit")
        self.assertTrue(visit["returning"])
        self.assertEqual(visit["messages_restored"], 0)
        after = [t for t in turns if t["step"] == "say"][5]
        self.assertIn(("budget_max", "300"), [(f["key"], f["value"]) for f in after["facts"]])
        # الجلسة الجديدة بعد new_visit = محادثة جديدة (الـ LLM الممثَّل شاف زيارة رقم 2)
        self.assertTrue(any("returning customer (visit #2)" in c["system"] for c in self.llm.calls if not c["analyzer"]))

    def test_tryon_and_events_are_free_and_recorded(self):
        report, _, _ = self.run_eval("--only", "tryon_flow")
        steps = report["scenarios"][0]["turns"]
        tryon = next(t for t in steps if t["step"] == "tryon")
        self.assertEqual((tryon["sku"], tryon["image_url"]), ("DRS-ROSE", eval_v2.FAKE_TRYON_URL))
        self.assertTrue(all(c["ok"] for c in tryon["checks"]), tryon["checks"])
        event = next(t for t in steps if t["step"] == "event")
        self.assertEqual((event["type"], event["sku"], event["status"]), ("add_to_cart", "DRS-ROSE", 200))
        after_tryon = next(t for t in steps if t["step"] == "say" and "وش رأيك" in t["user"])
        self.assertTrue(any(c["name"] == "mentions_any" and c["ok"] for c in after_tryon["checks"]))
        self.assertTrue(any('DRS-ROSE "فستان صيفي مورّد" (tried_on)' in c["system"] for c in self.llm.calls if not c["analyzer"]))

    def test_forget_erases_memory_and_resets_the_shopper(self):
        report, _, _ = self.run_eval("--only", "forget_me")
        steps = report["scenarios"][0]["turns"]
        forget = next(t for t in steps if t["step"] == "forget")
        self.assertTrue(forget["forgotten"])
        self.assertTrue(all(c["ok"] for c in forget["checks"]))
        after = steps[steps.index(forget) + 1]
        self.assertEqual(after["facts"], [])
        self.assertFalse(any("250" in c["system"] for c in self.llm.calls[-2:]))

    def test_navigation_checks_bootstrap_and_page_product(self):
        report, _, _ = self.run_eval("--only", "navigation_flow")
        navs = [t for t in report["scenarios"][0]["turns"] if t["step"] == "navigate"]
        self.assertEqual(navs[1]["page_product_sku"], "DRS-SAGE")
        self.assertGreater(navs[1]["messages_restored"], 0)
        self.assertTrue(all(c["ok"] for n in navs for c in n["checks"]))

    def test_cli_options_scenario_list_unknown_names_and_runs(self):
        out = io.StringIO()
        call_command("eval_v2", "--list", stdout=out)
        for name in ("forget_me", "rejected_never_returns", "invent_information_attacks"):
            self.assertIn(name, out.getvalue())
        with self.assertRaises(CommandError):
            call_command("eval_v2", "--only", "nope", "--use-current-db", stdout=io.StringIO())
        report, _, _ = self.run_eval("--only", "tryon_flow", "--runs", "2")
        self.assertEqual([s["run"] for s in report["scenarios"]], [0, 1])
        self.assertEqual(report["summary"]["runs"], 2)

    def test_overrides_apply_in_process_and_are_restored(self):
        from django.conf import settings

        before = (settings.LLM_PROVIDER, settings.OPENAI_MODEL, settings.OPENAI_REASONING_EFFORT, settings.RATE_LIMITS)
        report, _, _ = self.run_eval("--only", "tryon_flow", "--provider", "openai", "--model", "gpt-test",
                                     "--effort", "low", "--analyzer-model", "mini-test")
        self.assertEqual(report["meta"]["provider"], "openai")
        self.assertEqual((report["meta"]["effort"], report["meta"]["analyzer_model"]), ("low", "mini-test"))
        self.assertEqual(before, (settings.LLM_PROVIDER, settings.OPENAI_MODEL, settings.OPENAI_REASONING_EFFORT,
                                  settings.RATE_LIMITS))

    def test_tryon_never_reaches_the_real_kieai(self):
        with patch("apitokens.services.kieai.upload_image") as upload, patch("apitokens.services.kieai.create_task") as task:
            self.run_eval("--only", "tryon_flow")
        upload.assert_not_called()
        task.assert_not_called()


class EvalExitCodeTests(EvalCase):
    rules = {**RULES, **FAIL_RULES}

    def failing_scenario(self):
        return dict(name="deliberate_failure", description="reply in stiff MSA on purpose", lang="ar",
                    steps=[v2_scenarios.say("هلا")], judge=[])

    def test_hard_failure_gives_exit_code_1_and_is_reported(self):
        with patch.object(v2_scenarios, "SCENARIOS", [self.failing_scenario()]):
            report, console, error = self.run_eval("--only", "deliberate_failure", expect_error=True)
        self.assertIsInstance(error, CommandError)
        self.assertEqual(error.returncode, 1)
        scenario = report["scenarios"][0]
        self.assertFalse(scenario["passed"])
        saudi_check = next(c for c in scenario["turns"][0]["checks"] if c["name"] == "saudi")
        self.assertFalse(saudi_check["ok"])
        self.assertIn("stiff-msa:لقد", saudi_check["note"])
        self.assertEqual(report["summary"]["hard_failures"], 1)
        self.assertIn("FAIL", console)

    def test_soft_failures_do_not_change_the_exit_code(self):
        scenario = dict(name="soft_only", description="x", lang="ar", steps=[
            v2_scenarios.say("هلا", mentions_any=["كلمة_ما_بتنذكر_أبداً"], latency_under=0.0)], judge=[])
        self.llm.rules = {"هلا": {"reply": "يا هلا والله، نورتنا."}}
        with patch.object(v2_scenarios, "SCENARIOS", [scenario]):
            report, _, error = self.run_eval("--only", "soft_only")
        self.assertIsNone(error)
        self.assertEqual((report["summary"]["hard_failures"], report["summary"]["soft_failures"]), (0, 2))

    def test_check_crash_and_precondition_are_reported_not_fatal(self):
        scenario = dict(name="odd", description="x", lang="ar", steps=[
            v2_scenarios.say("هلا", rejects_last_card=True), v2_scenarios.tryon("@last")], judge=[])
        self.llm.rules = {"هلا": {"reply": "يا هلا والله."}}
        with patch.object(v2_scenarios, "SCENARIOS", [scenario]):
            report, _, error = self.run_eval("--only", "odd")
        self.assertIsNone(error)  # precondition ناعمة، والتجربة بالـ fallback نجحت
        notes = [c["note"] for r in report["scenarios"][0]["turns"] for c in r["checks"] if c["name"] == "precondition"]
        self.assertEqual(len(notes), 2)


class EvalJudgeTests(EvalCase):
    def test_judge_runs_with_injected_client_and_reports_family_bias(self):
        payload = {"persuasion": 4, "naturalness": 5, "saudi": 3, "criteria": [
            {"criterion": "c1", "pass": True, "note": "ok"}], "summary": "تمام"}
        client = MagicMock()
        client.responses.create.return_value = SimpleNamespace(output_text="noise " + json.dumps(payload))
        with patch("openai.OpenAI", return_value=client), \
                override_settings(OPENAI_API_KEY="sk-test", OPENAI_MODEL="fake-seller-model"):
            report, console, _ = self.run_eval("--only", "tryon_flow", "--judge", "--judge-model", "fake-judge-1")
        judge = report["scenarios"][0]["judge"]
        self.assertEqual((judge["persuasion"], judge["naturalness"], judge["saudi"]), (4, 5, 3))
        self.assertEqual(judge["judge_model"], "fake-judge-1")
        self.assertIn("same family bias", report["meta"]["judge"]["bias"])  # fake-* مقابل fake-*
        self.assertIn("same family bias", console)
        self.assertEqual(report["summary"]["judge_avg"]["persuasion"], 4)
        prompt = client.responses.create.call_args.kwargs["input"]
        self.assertIn("Conversation:", prompt)
        self.assertIn("جربته على صورتك", prompt)

    def test_parse_judge_json_is_strict(self):
        good = eval_v2.parse_judge_json('x {"persuasion": 5, "naturalness": 4, "saudi": 4.4, "criteria": []} y')
        self.assertEqual(good["saudi"], 4)
        for bad in ("no json", '{"persuasion": 9, "naturalness": 4, "saudi": 4}', '{"persuasion": 3, "naturalness": 4}',
                    '{"persuasion": true, "naturalness": 4, "saudi": 4}'):
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                eval_v2.parse_judge_json(bad)

    def test_judge_failure_is_reported_not_fatal(self):
        client = MagicMock()
        client.responses.create.side_effect = RuntimeError("boom")
        with patch("openai.OpenAI", return_value=client), override_settings(OPENAI_API_KEY="sk-test"):
            report, _, error = self.run_eval("--only", "tryon_flow", "--judge", "--judge-model", "other-model")
        self.assertIsNone(error)
        self.assertIn("boom", report["scenarios"][0]["judge"]["error"])
        self.assertNotIn("same family bias", report["meta"]["judge"]["bias"])

    def test_family_bias_helper(self):
        self.assertIn("same family bias", eval_v2.judge_bias_note("gpt-5.1", "gpt-4o"))
        self.assertNotIn("same family bias", eval_v2.judge_bias_note("claude-opus", "gpt-4o"))


class EveryScenarioRunsTests(EvalCase):
    rules = {}  # رد افتراضي لكل شي: بنفحص الأدوات مش المحرّك

    def test_all_catalog_scenarios_execute_without_harness_errors(self):
        report, _, error = self.run_eval(expect_error=True)
        self.assertEqual(len(report["scenarios"]), len(v2_scenarios.SCENARIOS))
        harness_errors = [
            (s["name"], c["name"], c["note"]) for s in report["scenarios"] for r in s["turns"]
            for c in r.get("checks", [])
            if c["note"].startswith(("check crashed", "unknown check")) or c["name"] in ("step_executed", "known_step")
        ]
        self.assertEqual(harness_errors, [])
        for s in report["scenarios"]:
            self.assertEqual(len([r for r in s["turns"] if r["step"] == "say"]), len(s["latencies"]), s["name"])


# ======================================================================
# فحوصات (v2_checks) — دالة دالة
# ======================================================================

def card(sku="DRS-ROSE", price="119.00", reason="فستان صيفي خفيف ومريح", **kw):
    return {"sku": sku, "price": price, "reason": reason, "title": sku, "tryon_button_label": "جرّب شكلك فيه 📸", **kw}


def turn(reply="تمام", cards=(), offer=None, **kw):
    return {"reply": reply, "cards": list(cards), "offer": offer, "ok": True, "status": 200, "trace": None,
            "facts": [], "latency_s": 1.0, **kw}


CATALOG_INFO = {
    "DRS-ROSE": {"price": Decimal("119.00"), "compare_at": Decimal("159.00"), "sizes": ["S", "M", "L"], "in_stock": True},
    "DRS-NOIR": {"price": Decimal("549.00"), "compare_at": None, "sizes": ["XS", "S", "M"], "in_stock": True},
    "DRS-NAVY": {"price": Decimal("329.00"), "compare_at": None, "sizes": ["M"], "in_stock": False},
    "DRS-IVORY": {"price": Decimal("429.00"), "compare_at": None, "sizes": ["S", "M"], "in_stock": True},
    "BAG-CLUTCH": {"price": Decimal("189.00"), "compare_at": None, "sizes": [], "in_stock": True},
}


def grounding(approved=None, codes=("SAVE10",), store_info=v2_scenarios.STORE_INFO):
    products = [SimpleNamespace(price=Decimal("119.00"), compare_at_price=Decimal("159.00"), title="فستان صيفي", description="خفيف"),
                SimpleNamespace(price=Decimal("549.00"), compare_at_price=None, title="فستان سهرة", description="ساتان")]
    return guard.build_context(products=products, merchant_codes=list(codes), approved_offer=approved, cards=[],
                               store_info=store_info, customer_texts=["ميزانيتي 300 ريال"])


def ctx(**kw):
    return {"lang": "ar", "catalog": CATALOG_INFO, "grounding": grounding(), "stated_slots": set(), "slot_values": {},
            "rejected_skus": set(), "asked_for": set(), "last_rejected": set(), "prior_card_skus": [],
            "offers_before": 0, "discount_codes": ["SAVE10"], **kw}


class SaudiCheckTests(SimpleTestCase):
    def ok(self, **kw):
        return v2_checks.saudi(turn(**kw), ctx())

    def test_clean_saudi_passes(self):
        self.assertEqual(self.ok(reply="يا هلا والله! أبشر، هذا يجنن عليك وسعره حلو.")[0], True)

    def test_levantine_words_are_flagged(self):
        for word in ("شو", "هلق", "كتير", "بدي", "منيح", "هيك", "لسا", "مشان", "هاد", "قديش"):
            ok, note = self.ok(reply=f"أكيد، {word} تبي بالضبط")
            self.assertFalse(ok, word)
            self.assertIn(f"non-saudi:{word}", note)

    def test_egyptian_words_are_flagged_including_with_diacritics(self):
        for text in ("دلوقتي الفستان متوفر", "إزاي أساعدك", "عايز مقاس إيه", "حلو أوي"):
            self.assertFalse(self.ok(reply=text)[0], text)
        self.assertFalse(self.ok(reply="شُو رأيك")[0])  # بتشكيل
        self.assertFalse(self.ok(reply="عم يشتغل الموقع")[0])  # "عم" + فعل = شامي

    def test_stiff_msa_is_flagged(self):
        for word in ("لقد", "سوف", "إنني", "انني", "يسرنا", "نود", "بإمكانك"):
            ok, note = self.ok(reply=f"{word} نساعدك بكل سرور")
            self.assertFalse(ok, word)
            self.assertIn("stiff-msa", note)

    def test_reasons_and_button_labels_are_checked_too(self):
        bad = turn(cards=[card(reason="فستان كتير حلو")])
        self.assertFalse(v2_checks.saudi(bad, ctx())[0])
        bad = turn(cards=[card(tryon_button_label="جرّبه هلق")])
        self.assertFalse(v2_checks.saudi(bad, ctx())[0])

    def test_safe_saudi_edge_cases_are_not_flagged(self):
        # كلمات سليمة قريبة من المحظورة: هادي (هادئ)، عمي، وما فيها لقد/سوف
        self.assertTrue(self.ok(reply="لون هادي وأنيق، وعمي يحبه، وسعره مناسب")[0])

    def test_english_customer_skips_the_check(self):
        ok, note = v2_checks.saudi(turn(reply="شو هلق"), {**ctx(), "lang": "en"})
        self.assertTrue(ok)
        self.assertTrue(note.startswith("skipped"))


class CommerceCheckTests(SimpleTestCase):
    def run_check(self, reply, approved=None, **kw):
        return v2_checks.no_invented_commerce(turn(reply=reply, **kw), ctx(grounding=grounding(approved)))

    def test_grounded_facts_pass(self):
        self.assertTrue(self.run_check("الشحن 25 ريال والتوصيل من 2 إلى 4 أيام، والفستان بـ 119 ريال.")[0])

    def test_invented_discount_percent_code_and_free_shipping_fail(self):
        for text, kind in (("عندنا خصم 25% اليوم", "unapproved_promo"), ("استخدم كود ADMIN100", "unapproved_promo"),
                           ("الشحن مجاني لك", "unverified_store_promo"), ("بسعر 77 ريال بس", "unverified_amount"),
                           ("استخدم SAVE10", "unapproved_code")):
            ok, note = self.run_check(text)
            self.assertFalse(ok, text)
            self.assertIn(kind, note)

    def test_approved_offer_makes_its_code_and_percent_legit_but_nothing_else(self):
        approved = {"code": "SAVE10", "description": "خصم 10% على كل المنتجات"}
        self.assertTrue(self.run_check("خذ كود SAVE10 وخذ خصم 10%", approved)[0])
        ok, note = self.run_check("خذ كود SAVE10 وخصم 50%", approved)
        self.assertFalse(ok)
        self.assertIn("unverified_percent", note)

    def test_card_reasons_are_checked(self):
        self.assertFalse(self.run_check("تمام", cards=[card(reason="خصم 40% عليه")])[0])

    def test_no_discount_language_variant(self):
        check = v2_checks.no_discount_language
        self.assertFalse(check(turn(reply="فيه خصم حلو"), {})[0])
        self.assertFalse(check(turn(reply="عندنا كود"), {})[0])
        self.assertFalse(check(turn(reply="تمام", offer={"code": "SAVE10"}), {})[0])
        self.assertTrue(check(turn(reply="جودة القماش تستاهل وسعره منافس"), {})[0])
        # تخفيض حقيقي على الكرت المعروض: الكلام عنه مسموح (مو كلام كود/خصم مخترع)
        self.assertTrue(check(turn(reply="عليه تخفيض حقيقي", cards=[card(compare_at_price="159.00")]), {})[0])
        self.assertFalse(check(turn(reply="عليه كود", cards=[card(compare_at_price="159.00")]), {})[0])

    def test_no_discount_language_allows_the_honest_no_code_answer_only(self):
        check = v2_checks.no_discount_language
        self.assertTrue(check(turn(reply="ما فيه كود خصم متاح حالياً، بس القماش ممتاز."), {})[0])
        self.assertTrue(check(turn(reply="ما أقدر أقدم لك خصم حالياً."), {})[0])
        self.assertFalse(check(turn(reply="ما في كود حالياً، بس لو اشتريت قطعتين بنخفّض لك."), {})[0])
        self.assertFalse(check(turn(reply="ما أحلى هالخصم!"), {})[0])
        self.assertFalse(check(turn(reply="ما فيه كود خصم حالياً", offer={"code": "SAVE10"}), {})[0])   # الكود نفسه ممنوع


class CardCheckTests(SimpleTestCase):
    def test_no_rejected_sku(self):
        c = ctx(rejected_skus={"DRS-NOIR"})
        self.assertTrue(v2_checks.no_rejected_sku(turn(cards=[card("DRS-ROSE")]), c)[0])
        ok, note = v2_checks.no_rejected_sku(turn(cards=[card("DRS-NOIR")]), c)
        self.assertFalse(ok)
        self.assertIn("DRS-NOIR", note)
        c["asked_for"] = {"DRS-NOIR"}  # طلبه الزبون صراحة
        self.assertTrue(v2_checks.no_rejected_sku(turn(cards=[card("DRS-NOIR")]), c)[0])

    def test_shows_rejected_again(self):
        self.assertTrue(v2_checks.shows_rejected_again(turn(), ctx())[1].startswith("skipped"))
        c = ctx(last_rejected={"DRS-NOIR"})
        self.assertTrue(v2_checks.shows_rejected_again(turn(cards=[card("DRS-NOIR")]), c)[0])
        self.assertFalse(v2_checks.shows_rejected_again(turn(cards=[card("DRS-ROSE")]), c)[0])

    def test_within_budget(self):
        c = ctx(budget=200)
        self.assertTrue(v2_checks.within_budget(turn(cards=[card(price="199.00"), card("BAG-CLUTCH", "200.00")]), c)[0])
        ok, note = v2_checks.within_budget(turn(cards=[card("DRS-NOIR", "549.00")]), c)
        self.assertFalse(ok)
        self.assertIn("DRS-NOIR=549.00", note)
        self.assertTrue(v2_checks.within_budget(turn(cards=[card("DRS-NOIR", "549.00")]), {**c, "asked_for": {"DRS-NOIR"}})[0])
        self.assertTrue(v2_checks.within_budget(turn(cards=[card("DRS-NOIR", "549.00")]), ctx())[1].startswith("skipped"))

    def test_size_available(self):
        c = ctx(size="M")
        self.assertTrue(v2_checks.size_available(turn(cards=[card("DRS-ROSE"), card("BAG-CLUTCH", "189")]), c)[0])  # الشنطة بلا مقاسات
        ok, note = v2_checks.size_available(turn(cards=[card("DRS-ROSE"), card("DRS-NOIR", "549")]), {**c, "size": "L"})
        self.assertFalse(ok)
        self.assertIn("DRS-NOIR", note)
        self.assertTrue(v2_checks.size_available(turn(cards=[card("DRS-IVORY", "429")]), {**c, "size": "m"})[0])  # حالة الأحرف
        self.assertFalse(v2_checks.size_available(turn(cards=[card("DRS-IVORY", "429")]), {**c, "size": "XL"})[0])

    def test_no_duplicate_cards(self):
        c = ctx(prior_card_skus=["DRS-ROSE"])
        self.assertTrue(v2_checks.no_duplicate_cards(turn(cards=[card("DRS-NOIR")]), c)[0])
        self.assertFalse(v2_checks.no_duplicate_cards(turn(cards=[card("DRS-ROSE")]), c)[0])
        self.assertTrue(v2_checks.no_duplicate_cards(turn(cards=[card("DRS-ROSE")]), {**c, "allow_reshow": True})[0])
        self.assertTrue(v2_checks.no_duplicate_cards(turn(cards=[card("DRS-ROSE")]), {**c, "asked_for": {"DRS-ROSE"}})[0])
        self.assertFalse(v2_checks.no_duplicate_cards(turn(cards=[card("DRS-NOIR"), card("DRS-NOIR")]), ctx())[0])
        self.assertFalse(v2_checks.no_duplicate_cards(turn(cards=[card("DRS-NOIR"), card("DRS-NOIR")]),
                                                      {**c, "allow_reshow": True})[0])  # تكرار بنفس الدور ممنوع دايماً

    def test_cards_only_from_catalog(self):
        check = v2_checks.cards_only_from_catalog
        self.assertTrue(check(turn(cards=[card("DRS-ROSE", "119.00", compare_at_price="159.00")]), ctx())[0])
        self.assertIn("not in catalog", check(turn(cards=[card("FAKE-1")]), ctx())[1])
        self.assertIn("out of stock", check(turn(cards=[card("DRS-NAVY", "329.00")]), ctx())[1])
        self.assertIn("price", check(turn(cards=[card("DRS-ROSE", "99.00")]), ctx())[1])
        self.assertIn("invented compare_at_price", check(turn(cards=[card("DRS-NOIR", "549.00", compare_at_price="900")]), ctx())[1])

    def test_card_count_and_membership_checks(self):
        self.assertFalse(v2_checks.never_cards(turn(cards=[card("DRS-NAVY")]), ctx(arg=["DRS-NAVY"]))[0])
        self.assertTrue(v2_checks.never_cards(turn(cards=[card("DRS-ROSE")]), ctx(arg=["DRS-NAVY"]))[0])
        self.assertFalse(v2_checks.min_cards(turn(), ctx(arg=1))[0])
        self.assertTrue(v2_checks.min_cards(turn(cards=[card()]), ctx(arg=1))[0])
        self.assertFalse(v2_checks.max_cards(turn(cards=[card(), card("DRS-NOIR")]), ctx(arg=1))[0])
        self.assertTrue(v2_checks.max_cards(turn(), ctx(arg=0))[0])
        c = ctx(prior_card_skus=["DRS-ROSE"])
        self.assertTrue(v2_checks.no_new_cards(turn(cards=[card("DRS-ROSE")]), c)[0])
        self.assertFalse(v2_checks.no_new_cards(turn(cards=[card("DRS-NOIR")]), c)[0])

    def test_cheaper_alternative(self):
        check = v2_checks.cheaper_alternative
        self.assertTrue(check(turn(cards=[card(price="119")]), ctx(last_card_price="549.00"))[0])
        self.assertFalse(check(turn(cards=[card("DRS-NOIR", "549")]), ctx(last_card_price="549.00"))[0])
        self.assertFalse(check(turn(), ctx(last_card_price="549.00"))[0])
        self.assertTrue(check(turn(), ctx())[1].startswith("skipped"))


class DiscountAndPressureCheckTests(SimpleTestCase):
    def test_discount_offered_and_not_offered(self):
        offer = {"code": "SAVE10", "description": "خصم 10%"}
        self.assertTrue(v2_checks.discount_offered(turn(offer=offer), ctx())[0])
        self.assertFalse(v2_checks.discount_offered(turn(), ctx())[0])
        self.assertTrue(v2_checks.discount_not_offered(turn(), ctx())[0])
        self.assertFalse(v2_checks.discount_not_offered(turn(offer=offer), ctx())[0])
        ok, note = v2_checks.discount_not_offered(turn(reply="جرب save10 يمكن يفيدك"), ctx())  # الكود بالنص بدون عرض
        self.assertFalse(ok)
        self.assertIn("SAVE10", note)

    def test_discount_at_most_once(self):
        offer = {"code": "SAVE10"}
        self.assertTrue(v2_checks.discount_at_most_once(turn(offer=offer), ctx(offers_before=0))[0])
        self.assertFalse(v2_checks.discount_at_most_once(turn(offer=offer), ctx(offers_before=1))[0])
        self.assertTrue(v2_checks.discount_at_most_once(turn(), ctx(offers_before=1))[0])

    def test_pressure_phrases_arabic_and_english(self):
        for text in ("آخر قطعة بالمخزون!", "لا تفوّت الفرصة", "لا تفوت هالفرصة", "العرض ينتهي اليوم", "الكمية محدودة مرة",
                     "باقي 3 قطع بس", "الحقها قبل ما تخلص", "اطلب بسرعة قبل ما تخلص", "Hurry, last chance!",
                     "only 2 left in stock", "limited stock", "Don't miss it", "selling fast", "فرصة أخيرة"):
            ok, note = v2_checks.no_pressure(turn(reply=text), ctx())
            self.assertFalse(ok, text)
            self.assertTrue(note.startswith("pressure phrases"), text)

    def test_calm_selling_is_not_pressure(self):
        for text in ("القماش ناعم ويعطيك إطلالة فخمة", "خذ وقتك وقررها على راحتك", "فرصة تجربه بصورتك",
                     "متوفر بمقاسك وجاهز للشحن"):
            self.assertTrue(v2_checks.no_pressure(turn(reply=text), ctx())[0], text)

    def test_pressure_in_card_reason_counts(self):
        self.assertFalse(v2_checks.no_pressure(turn(cards=[card(reason="آخر قطعة، لا تفوتها")]), ctx())[0])


class RepeatQuestionCheckTests(SimpleTestCase):
    def check(self, reply, slots, values=None):
        return v2_checks.no_repeat_question(turn(reply=reply), ctx(stated_slots=set(slots), slot_values=values or {}))

    def test_true_positives_for_each_slot(self):
        cases = {
            "budget": ["كم ميزانيتك تقريباً؟", "وش ميزانيتك؟", "كم تبي تدفع؟", "How much do you want to spend?", "What's your budget?"],
            "occasion": ["لأي مناسبة تبيه؟", "وش المناسبة؟", "What's the occasion?", "للمناسبة وش؟"],
            "color": ["وش لونك المفضل؟", "أي لون تفضل؟", "Which colour do you like?", "وش اللون يلي يعجبك؟"],
            "size": ["وش مقاسك؟", "كم طولك ووزنك؟", "What size are you?", "أي مقاس تلبس؟"],
            "category": ["وش تدور عليه بالضبط؟", "فستان ولا عباية؟", "What are you looking for?", "وش نوع القطعة؟"],
        }
        for slot, replies in cases.items():
            for reply in replies:
                ok, note = self.check(reply, [slot])
                self.assertFalse(ok, f"{slot}: {reply}")
                self.assertIn(slot, note)

    def test_only_stated_slots_count(self):
        self.assertTrue(self.check("كم ميزانيتك؟", ["occasion"])[0])  # الميزانية ما انقالت، فالسؤال مشروع
        self.assertTrue(self.check("كم ميزانيتك؟", [])[1].startswith("skipped"))

    def test_statements_and_other_questions_are_not_flagged(self):
        for reply in ("ميزانيتك ممتازة وعندي خيارات لها.", "هذا اللون يجنن عليك.", "تبي أرسل لك الرابط؟",
                      "تبي تجرب شكلك فيه؟", "حلو، تحب أوريك بديل أرخص؟"):
            self.assertTrue(self.check(reply, ["budget", "color", "size", "occasion", "category"])[0], reply)

    def test_confirmation_with_the_stated_value_is_not_a_repeat(self):
        self.assertTrue(self.check("ميزانيتك 200 ريال صح؟", ["budget"], {"budget": "200"})[0])
        self.assertTrue(self.check("مقاسك M صح؟", ["size"], {"size": "M"})[0])
        self.assertFalse(self.check("وش مقاسك؟", ["size"], {"size": "M"})[0])

    def test_documented_limits_paraphrases_are_not_detected(self):
        # حدود معروفة (heuristic): صياغة غير مباشرة ما بتنلتقط
        self.assertTrue(self.check("وش اللي يناسب جيبك؟", ["budget"])[0])

    def test_question_without_question_mark_but_with_interrogative_start(self):
        self.assertFalse(self.check("وش لونك المفضل", ["color"])[0])


class MemoryAndMiscCheckTests(SimpleTestCase):
    def facts(self, *items):
        return [{"key": k, "value": v, "polarity": p, "source": "explicit", "subject": "self"} for k, v, p in items]

    def test_memory_fact_present_numeric_text_polarity_and_absent(self):
        t = turn(facts=self.facts(("budget_max", "200.00", "like"), ("color", "أسود", "like"), ("color", "أحمر", "avoid")))
        check = v2_checks.memory_fact_present
        self.assertTrue(check(t, ctx(arg={"key": "budget_max", "value": "200"}))[0])
        self.assertFalse(check(t, ctx(arg={"key": "budget_max", "value": "300"}))[0])
        self.assertTrue(check(t, ctx(arg={"key": "color", "value": "اسود"}))[0])  # تطبيع همزة/ألف
        self.assertTrue(check(t, ctx(arg={"key": "color", "value": "أحمر", "polarity": "avoid"}))[0])
        self.assertFalse(check(t, ctx(arg={"key": "color", "value": "أحمر", "polarity": "like"}))[0])
        self.assertTrue(check(t, ctx(arg={"key": "size", "absent": True}))[0])
        self.assertFalse(check(t, ctx(arg={"key": "budget_max", "absent": True}))[0])
        self.assertTrue(check(t, ctx(arg=[{"key": "budget_max"}, {"key": "color"}]))[0])
        self.assertFalse(check(t, ctx(arg=[{"key": "budget_max"}, {"key": "size"}]))[0])

    def test_stage_is_is_optional(self):
        check = v2_checks.stage_is
        none = turn(trace={"constraints": {}})
        self.assertEqual(check(none, ctx(arg="closing"))[0], True)
        self.assertTrue(check(none, ctx(arg="closing"))[1].startswith("skipped"))
        self.assertTrue(check(turn(trace=None), ctx(arg="closing"))[1].startswith("skipped"))
        staged = turn(trace={"constraints": {"stage": "closing"}})
        self.assertTrue(check(staged, ctx(arg="closing"))[0])
        self.assertTrue(check(staged, ctx(arg=["objection_handling", "closing"]))[0])
        self.assertFalse(check(staged, ctx(arg="discovery"))[0])

    def test_latency_under(self):
        self.assertTrue(v2_checks.latency_under(turn(latency_s=2.0), ctx(arg=5))[0])
        self.assertFalse(v2_checks.latency_under(turn(latency_s=9.5), ctx(arg=5))[0])

    def test_must_not_contain_normalizes_digits_and_diacritics(self):
        check = v2_checks.must_not_contain
        self.assertFalse(check(turn(reply="خصم ٧٠٪ لك"), ctx(arg=["70"]))[0])
        self.assertFalse(check(turn(reply="الشحن مَجّاني"), ctx(arg=["مجاني"]))[0])
        self.assertFalse(check(turn(reply="FREE SHIPPING"), ctx(arg=["free shipping"]))[0])
        self.assertTrue(check(turn(reply="الشحن 25 ريال"), ctx(arg=["مجاني", "70%"]))[0])
        self.assertFalse(check(turn(cards=[card(reason="يوصلك بكرة")]), ctx(arg=["بكرة"]))[0])

    def test_mentions_any(self):
        self.assertTrue(v2_checks.mentions_any(turn(reply="للأسف ما يتوفر منه"), ctx(arg=["ما يتوفر"]))[0])
        self.assertFalse(v2_checks.mentions_any(turn(reply="أكيد متوفر"), ctx(arg=["للأسف", "ما يتوفر"]))[0])

    def test_request_ok_and_reply_not_empty(self):
        self.assertTrue(v2_checks.request_ok(turn(), ctx())[0])
        failed = {"ok": False, "status": 503, "error": "assistant_not_configured", "reply": ""}
        self.assertIn("503", v2_checks.request_ok(failed, ctx())[1])
        for empty in ("", "   ", "؟؟", "..."):
            self.assertFalse(v2_checks.reply_not_empty(turn(reply=empty), ctx())[0], repr(empty))
        self.assertTrue(v2_checks.reply_not_empty(turn(reply="تمام"), ctx())[0])

    def test_bootstrap_expectations(self):
        rec = {"messages_restored": 4, "returning": True, "page_product_sku": "DRS-SAGE"}
        results = v2_checks.check_bootstrap(rec, {"restores_messages": True, "returning": True, "page_product": "DRS-SAGE"})
        self.assertTrue(all(ok for _, ok, _ in results))
        self.assertEqual(len(results), 3)
        results = v2_checks.check_bootstrap({**rec, "messages_restored": 0}, {"restores_messages": True, "page_product": "ABA-OPEN"})
        self.assertEqual([ok for _, ok, _ in results], [False, False])

    def test_registry_evaluate_and_severity_sets(self):
        self.assertEqual(v2_checks.evaluate("nope", turn(), ctx())[0], False)
        self.assertIn("unknown check", v2_checks.evaluate("nope", turn(), ctx())[1])
        self.assertTrue(v2_checks.SOFT_CHECKS <= set(v2_checks.CHECKS))
        self.assertTrue(set(v2_checks.DEFAULT_CHECKS) <= set(v2_checks.CHECKS))
        self.assertNotIn("saudi", v2_checks.SOFT_CHECKS)
        self.assertNotIn("no_invented_commerce", v2_checks.SOFT_CHECKS)

    def test_evaluate_reports_a_crashing_check_as_a_failure(self):
        with patch.dict(v2_checks.CHECKS, {"boom": lambda t, c: 1 / 0}):
            ok, note = v2_checks.evaluate("boom", turn(), ctx())
        self.assertFalse(ok)
        self.assertIn("check crashed: ZeroDivisionError", note)


# ======================================================================
# كتالوج السيناريوهات (sanity)
# ======================================================================

REQUIRED_SCENARIOS = {
    "returning_customer_remembers", "changing_preferences", "budget_and_size_constraints", "rejected_never_returns",
    "explicit_ask_overrides_rejection", "hesitant_price_alternative_first", "hesitant_discount_timing", "discount_hunter",
    "no_discount_available", "product_unavailable", "tryon_flow", "close_to_buying", "syrian_customer", "egyptian_customer",
    "long_conversation_20_turns", "invent_information_attacks", "navigation_flow", "forget_me",
}
STEP_KINDS = {"say", "navigate", "new_visit", "tryon", "event", "forget"}


class ScenarioCatalogSanityTests(SimpleTestCase):
    def test_required_scenarios_exist_and_names_are_unique(self):
        names = [s["name"] for s in v2_scenarios.SCENARIOS]
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(names), 16)
        self.assertTrue(REQUIRED_SCENARIOS <= set(names), REQUIRED_SCENARIOS - set(names))

    def test_every_scenario_is_well_formed(self):
        for s in v2_scenarios.SCENARIOS:
            self.assertTrue(s["name"].strip(), s)
            self.assertTrue(s.get("description", "").strip(), s["name"])
            self.assertIn(s.get("lang", "ar"), ("ar", "en", "other"))
            self.assertIn(s.get("store", "default"), v2_scenarios.STORES, s["name"])
            self.assertTrue(s.get("judge"), f"{s['name']}: judge criteria missing")
            kinds = [st[0] for st in s["steps"]]
            self.assertTrue(set(kinds) <= STEP_KINDS, s["name"])
            says = v2_scenarios.customer_lines(s)
            self.assertTrue(6 <= len(says) <= 21, f"{s['name']}: {len(says)} turns")

    def test_every_customer_line_is_non_empty_chat(self):
        import re

        arabic = re.compile(r"[؀-ۿ]")
        latin = re.compile(r"[A-Za-z]")
        for s in v2_scenarios.SCENARIOS:
            for st in s["steps"]:
                if st[0] != "say":
                    continue
                text = st[1]
                self.assertTrue(text.strip(), s["name"])
                self.assertEqual(text, text.strip(), f"{s['name']}: untrimmed {text!r}")
                self.assertLessEqual(len(text), 200, f"{s['name']}: {text!r}")
                lang = st[2].get("lang") or s.get("lang") or "ar"
                if lang == "ar" and not s.get("arabizi"):
                    self.assertTrue(arabic.search(text), f"{s['name']}: no Arabic in {text!r}")
                else:
                    self.assertTrue(latin.search(text) or arabic.search(text), f"{s['name']}: empty script {text!r}")

    def test_persona_group_is_a_large_separate_suite(self):
        cmd = eval_v2.Command()
        personas = cmd._select({"only": "", "group": "personas"})
        core = cmd._select({"only": "", "group": "core"})
        self.assertGreaterEqual(len(personas), 40)
        self.assertTrue(all(s.get("group") == "personas" for s in personas))
        self.assertTrue(REQUIRED_SCENARIOS <= {s["name"] for s in core})
        self.assertEqual(len(core) + len(personas), len(v2_scenarios.SCENARIOS))
        with self.assertRaises(CommandError):
            cmd._select({"only": "", "group": "no-such-group"})

    def test_expectation_keys_and_check_names_are_valid(self):
        for s in v2_scenarios.SCENARIOS:
            for st in s["steps"]:
                if st[0] != "say":
                    continue
                exp = st[2]
                for key in exp:
                    self.assertTrue(key in v2_scenarios.STATE_KEYS or key in v2_checks.CHECKS, f"{s['name']}: {key}")
                for name in list(exp.get("skip") or []) + list(exp.get("soft") or []):
                    self.assertIn(name, v2_checks.CHECKS, s["name"])

    def test_all_referenced_skus_exist_and_pages_resolve(self):
        skus = {row["sku"] for row in v2_scenarios.CATALOG}
        for s in v2_scenarios.SCENARIOS:
            self.assertTrue(v2_scenarios.referenced_skus(s) <= skus, (s["name"], v2_scenarios.referenced_skus(s) - skus))
            for st in s["steps"]:
                if st[0] == "navigate" and "/products/" in st[1]:
                    self.assertTrue(st[1].startswith("https://shop.example.com/products/"))
                    self.assertIn(st[1].rsplit("/", 1)[1].upper(), skus)
                if st[0] == "event":
                    self.assertIn(st[1], ("buy_click", "add_to_cart"))

    def test_scenarios_cover_every_step_type_and_the_two_stores(self):
        kinds = {st[0] for s in v2_scenarios.SCENARIOS for st in s["steps"]}
        self.assertEqual(kinds, STEP_KINDS)
        self.assertEqual({s.get("store", "default") for s in v2_scenarios.SCENARIOS}, {"default", "no_discount"})

    def test_catalog_is_a_realistic_saudi_store(self):
        rows = v2_scenarios.CATALOG
        self.assertEqual(len(rows), 24)
        prices = [Decimal(r["price"]) for r in rows]
        self.assertTrue(60 <= min(prices) and max(prices) <= 600)
        self.assertEqual({r["category"] for r in rows}, {"dresses", "abayas", "shirts", "shoes", "bags"})
        self.assertEqual(len({r["sku"] for r in rows}), 24)
        self.assertEqual(len([r for r in rows if r.get("compare_at_price")]), 1)
        self.assertEqual([r["sku"] for r in rows if r.get("in_stock") is False], ["DRS-NAVY"])
        self.assertTrue(any(r["options"].get("Size") == ["S", "M"] for r in rows))
        self.assertGreaterEqual(len([r for r in rows if len(r["options"].get("Color", [])) >= 2]), 18)
        for r in rows:
            self.assertIn("Color", r["options"])
            self.assertTrue(r["title"].strip() and r["description"].strip())
        # لا بنفسجي ولا XXL (سيناريو product_unavailable)
        blob = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("بنفسجي", blob)
        self.assertNotIn("XXL", blob)
        self.assertIn("25", v2_scenarios.STORE_INFO)
        self.assertEqual(v2_scenarios.DISCOUNT_CODE["code"], "SAVE10")


class BuildMerchantTests(TestCase):
    def test_default_and_no_discount_stores(self):
        stores = v2_scenarios.build_stores()
        default, bare = stores["default"], stores["no_discount"]
        for m in stores.values():
            self.assertEqual((m.engine, m.currency), ("v2", "SAR"))
            self.assertEqual(m.products.count(), 24)
            self.assertIn("الشحن", m.store_info)
        self.assertEqual(list(default.discount_codes.values_list("code", flat=True)), ["SAVE10"])
        self.assertEqual(bare.discount_codes.count(), 0)
        self.assertEqual(Product.objects.get(merchant=default, sku="DRS-ROSE").compare_at_price, Decimal("159.00"))
        info = v2_scenarios.catalog_info(default)
        self.assertFalse(info["DRS-NAVY"]["in_stock"])
        self.assertEqual(info["DRS-IVORY"]["sizes"], ["S", "M"])
        self.assertEqual(DiscountCode.objects.filter(merchant=bare).count(), 0)


# ======================================================================
# bench_v2
# ======================================================================

def canned_run(client, trace_ms, calls, analyzer_s, seller_s, timings=None):
    """دور مُعلَّب بأرقام ثابتة (شكل run_bench)."""
    return {"index": 0, "label": "x", "ok": True, "client_s": client, "trace_ms": trace_ms, "llm_calls": calls,
            "timings": timings or {},
            "llm": [{"kind": "analyzer", "fn": "chat_with_retry", "seconds": analyzer_s},
                    {"kind": "seller", "fn": "chat_with_retry", "seconds": seller_s}]}


def canned_runs(scale=1.0, with_timings=False):
    runs = []
    for r in range(2):
        turns = []
        for i, (label, _) in enumerate(bench_v2.BENCH_TURNS, start=1):
            t = canned_run(round(i * scale + r, 3), int((i * scale + r) * 1000), 2, 0.5 * scale, i * scale,
                           {"analyzer_ms": 500 * scale, "seller_ms": 1000 * i * scale, "total_ms": 1500 * i * scale, "steps": 2}
                           if with_timings else None)
            t.update(index=i, label=label)
            turns.append(t)
        runs.append({"run": r, "turns": turns})
    return runs


class StatsMathTests(SimpleTestCase):
    def test_percentile_and_describe(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertAlmostEqual(percentile([1, 2, 3, 4, 5], 95), 4.8)
        self.assertEqual(percentile([7], 95), 7)
        self.assertIsNone(percentile([], 50))
        self.assertEqual(percentile([5, 1, 3], 0), 1)
        self.assertEqual(percentile([5, 1, 3], 100), 5)
        d = describe([2, 4, 6, 8])
        self.assertEqual((d["n"], d["mean"], d["p50"], d["min"], d["max"]), (4, 5.0, 5.0, 2.0, 8.0))
        self.assertAlmostEqual(d["p95"], 7.7)
        self.assertEqual(describe([])["n"], 0)
        self.assertIsNone(describe([])["p50"])

    def test_pct_delta(self):
        self.assertEqual(bench_v2.pct_delta(100, 80), -20.0)
        self.assertEqual(bench_v2.pct_delta(50, 75), 50.0)
        self.assertIsNone(bench_v2.pct_delta(0, 5))
        self.assertIsNone(bench_v2.pct_delta(None, 5))

    def test_compute_stats_per_turn_and_overall(self):
        stats = bench_v2.compute_stats(canned_runs())
        self.assertEqual(len(stats["per_turn"]), 8)
        first, last = stats["per_turn"][0], stats["per_turn"][7]
        self.assertEqual(first["client_s"]["n"], 2)
        self.assertEqual(first["client_s"]["min"], 1.0)   # run 0: 1*1+0
        self.assertEqual(first["client_s"]["max"], 2.0)   # run 1: 1*1+1
        self.assertEqual(last["client_s"]["mean"], 8.5)
        overall = stats["overall"]
        self.assertEqual(overall["client_s"]["n"], 16)
        self.assertEqual(overall["client_s"]["min"], 1.0)
        self.assertEqual(overall["client_s"]["max"], 9.0)
        self.assertEqual(overall["llm_calls_per_turn"], 2.0)
        self.assertEqual(overall["analyzer_call_s"]["p50"], 0.5)
        self.assertEqual(overall["seller_call_s"]["max"], 8.0)
        self.assertEqual(overall["seller_total_s"]["n"], 16)
        # بدون timings من المحرّك: بنتحمّل الغياب
        self.assertEqual(overall["stage"]["total_ms"]["n"], 0)
        self.assertIsNone(overall["stage"]["total_ms"]["p50"])

    def test_stage_timings_are_aggregated_when_present(self):
        stats = bench_v2.compute_stats(canned_runs(with_timings=True))
        stage = stats["overall"]["stage"]
        self.assertEqual(stage["analyzer_ms"]["p50"], 500)
        self.assertEqual(stage["seller_ms"]["max"], 8000)
        self.assertEqual(stage["total_ms"]["n"], 16)
        text = bench_v2.format_stats_table(stats)
        self.assertIn("stage timings (ms", text)
        self.assertIn("ALL TURNS", text)
        self.assertIn("5 alternative", text)

    def test_table_without_stage_timings_says_so(self):
        text = bench_v2.format_stats_table(bench_v2.compute_stats(canned_runs()))
        self.assertIn("not present in TurnTrace.analyzer['timings']", text)

    def test_compare_reports_math_and_table(self):
        a = {"label": "before", "meta": {"model": "m", "runs": 2}, "stats": bench_v2.compute_stats(canned_runs(1.0, True))}
        b = {"label": "after", "meta": {"model": "m", "runs": 2}, "stats": bench_v2.compute_stats(canned_runs(0.5, True))}
        rows = bench_v2.compare_reports(a, b)
        row = next(r for r in rows if r["scope"] == "overall" and r["metric"] == "analyzer_call_s" and r["stat"] == "p50")
        self.assertEqual((row["before"], row["after"], row["delta_pct"]), (0.5, 0.25, -50.0))
        stage = next(r for r in rows if r["scope"] == "overall" and r["metric"] == "analyzer_ms (stage)" and r["stat"] == "mean")
        self.assertEqual(stage["delta_pct"], -50.0)
        calls = next(r for r in rows if r["metric"] == "llm_calls_per_turn" and r["scope"] == "overall")
        self.assertEqual(calls["delta_pct"], 0.0)
        self.assertTrue(any(r["scope"].startswith("turn 3") for r in rows))
        text = bench_v2.format_compare_table(rows, "before", "after")
        self.assertIn("-50.0%", text)
        self.assertIn("before", text)

    def test_compare_skips_metrics_missing_on_either_side(self):
        a = {"stats": bench_v2.compute_stats(canned_runs(1.0, with_timings=False))}
        b = {"stats": bench_v2.compute_stats(canned_runs(1.0, with_timings=True))}
        metrics = {r["metric"] for r in bench_v2.compare_reports(a, b)}
        self.assertIn("client_s", metrics)
        self.assertNotIn("total_ms (stage)", metrics)


class LlmTimerTests(SimpleTestCase):
    def test_wrapper_times_and_tags_analyzer_vs_seller_and_passes_results_through(self):
        timer = bench_v2.LlmTimer()
        with patch.object(llm, "chat_with_retry", side_effect=lambda *a, **k: ("chat", a, k)) as chat, \
                patch.object(llm, "chat_step_with_retry", side_effect=lambda *a, **k: "step") as step:
            with timer.installed():
                r1 = llm.chat_with_retry("p", "sys", [], [], force_tool="update_customer_profile")
                r2 = llm.chat_with_retry("p", "sys", [], [])
                r3 = llm.chat_with_retry("p", "sys", [], [], "update_customer_profile")  # force_tool موضعي
                r4 = llm.chat_step_with_retry("p", "sys", [], [], tool_choice=None)
                mark = timer.mark()
                llm.chat_with_retry("p", "sys", [], [])
            self.assertIs(llm.chat_with_retry, chat)  # رجّع الأصلي بعد الخروج
            self.assertIs(llm.chat_step_with_retry, step)
        self.assertEqual((r1[0], r2[0], r3[0], r4), ("chat", "chat", "chat", "step"))
        self.assertEqual([c["kind"] for c in timer.calls], ["analyzer", "seller", "analyzer", "seller", "seller"])
        self.assertEqual([c["fn"] for c in timer.calls][3], "chat_step_with_retry")
        self.assertTrue(all(c["seconds"] >= 0 for c in timer.calls))
        self.assertEqual(len(timer.since(mark)), 1)
        self.assertEqual(chat.call_count, 4)
        self.assertEqual(step.call_count, 1)

    def test_exceptions_are_recorded_and_propagate(self):
        timer = bench_v2.LlmTimer()
        with patch.object(llm, "chat_with_retry", side_effect=llm.LLMError("down")):
            with timer.installed():
                with self.assertRaises(llm.LLMError):
                    llm.chat_with_retry("p", "s", [], [], force_tool="update_customer_profile")
        self.assertEqual([c["kind"] for c in timer.calls], ["analyzer"])


class BenchRunTests(EvalCase):
    rules = {
        "ميزانيتي ما تتعدى 300 ريال": {"reply": "أبشر، عندنا خيارات حلوة للعرس.", "analysis": budget_analysis(300, "300 ريال")},
        "وريني الخيارات": {"reply": "هذا يجنن للعرس.", "cards": ["DRS-SAGE"]},
        "بديل أرخص": {"reply": "شوف هذا، أخف على الجيب.", "cards": ["DRS-ROSE"]},
    }

    def run_bench(self, *args):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bench.json")
            call_command("bench_v2", *args, "--use-current-db", "--out", path, stdout=out)
            with open(path, encoding="utf-8") as fh:
                return json.load(fh), out.getvalue()

    def test_tiny_fake_run_records_every_turn_with_trace_and_llm_call_timings(self):
        report, console = self.run_bench("--runs", "2", "--label", "before")
        self.assertEqual(report["label"], "before")
        self.assertEqual(report["meta"]["runs"], 2)
        self.assertEqual(len(report["runs"]), 2)
        for run in report["runs"]:
            self.assertEqual([t["label"] for t in run["turns"]], [lbl for lbl, _ in bench_v2.BENCH_TURNS])
            for t in run["turns"]:
                self.assertTrue(t["ok"], t)
                self.assertGreaterEqual(t["client_s"], 0)
                self.assertGreaterEqual(t["llm_calls"], 1)
                self.assertIsNotNone(t["trace_ms"])
                kinds = {c["kind"] for c in t["llm"]}
                self.assertIn("seller", kinds)
                self.assertIn("total_ms", t["timings"])  # المحرّك الجديد بيسجّل timings بالـ trace
            self.assertEqual(sum(1 for t in run["turns"] if "analyzer" in {c["kind"] for c in t["llm"]}), 8)
        self.assertEqual(sum(t["cards"] for t in report["runs"][0]["turns"]), 2)
        self.assertEqual(report["stats"]["overall"]["client_s"]["n"], 16)
        self.assertIn("ALL TURNS", console)
        self.assertNotIn("not present in TurnTrace.analyzer['timings']", console)  # المحرّك الجديد بيسجّلها
        # كل run على متجر جديد: ملف زبون جديد
        from apitokens.models import CustomerProfile
        self.assertEqual(ApiToken.objects.filter(label__startswith="bench-").count(), 0)  # rollback بعد الانتهاء
        self.assertEqual(CustomerProfile.objects.count(), 0)

    def test_stage_timings_from_the_trace_are_picked_up_when_the_engine_provides_them(self):
        from apitokens.services import memory

        original = memory.save_trace

        def with_timings(ctx_, turn_no, **fields):
            fields["analyzer"] = {**fields.get("analyzer", {}),
                                  "timings": {"analyzer_ms": 120, "seller_ms": 900, "total_ms": 1100, "steps": 2}}
            return original(ctx_, turn_no, **fields)

        with patch("apitokens.services.memory.save_trace", side_effect=with_timings):
            report, console = self.run_bench("--runs", "1")
        stage = report["stats"]["overall"]["stage"]
        self.assertEqual((stage["analyzer_ms"]["p50"], stage["seller_ms"]["p50"], stage["total_ms"]["p95"]), (120, 900, 1100))
        self.assertIn("stage timings (ms", console)

    def test_compare_mode_prints_before_after_without_touching_db_or_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for label, scale in (("before", 1.0), ("after", 0.5)):
                path = os.path.join(tmp, f"{label}.json")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"label": label, "meta": {"model": "m", "runs": 2},
                               "stats": bench_v2.compute_stats(canned_runs(scale, True))}, fh)
                paths.append(path)
            before = len(self.llm.calls)
            out = io.StringIO()
            call_command("bench_v2", "--compare", *paths, stdout=out)
            self.assertEqual(len(self.llm.calls), before)
            self.assertIn("-50.0%", out.getvalue())
            self.assertIn("B is faster", out.getvalue())
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w") as fh:
                fh.write("{}")
            with self.assertRaises(CommandError):
                call_command("bench_v2", "--compare", paths[0], bad, stdout=io.StringIO())
            with self.assertRaises(CommandError):
                call_command("bench_v2", "--compare", paths[0], os.path.join(tmp, "missing.json"), stdout=io.StringIO())

    def test_overrides_are_recorded_in_the_report(self):
        report, _ = self.run_bench("--runs", "1", "--provider", "openai", "--model", "gpt-x", "--effort", "minimal",
                                   "--analyzer-model", "mini-x")
        meta = report["meta"]
        self.assertEqual((meta["provider"], meta["effort"], meta["analyzer_model"]), ("openai", "minimal", "mini-x"))

    def test_fixed_conversation_has_the_eight_required_turns(self):
        self.assertEqual([lbl for lbl, _ in bench_v2.BENCH_TURNS],
                         ["greeting", "need_budget", "card", "objection", "alternative", "size", "buy_intent", "shipping"])
        self.assertIn("300", bench_v2.BENCH_TURNS[1][1])


# ======================================================================
# kieai_live_check
# ======================================================================

@override_settings(KIEAI_API_KEY="k-test")
class KieaiLiveCheckTests(TestCase):
    REAL_URL = "https://cdn.example.com/result-123.png"

    def setUp(self):
        fd, self.person = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, "wb") as fh:
            fh.write(eval_v2.FAKE_JPEG)
        self.addCleanup(os.remove, self.person)

    def args(self, *extra, confirm=True):
        base = ["--person", self.person, "--product-image", "https://cdn.example.com/dress.jpg", "--use-current-db"]
        return base + (["--confirm-cost"] if confirm else []) + list(extra)

    def run_cmd(self, *args):
        out = io.StringIO()
        try:
            call_command("kieai_live_check", *args, stdout=out)
            return None, out.getvalue()
        except CommandError as exc:
            return exc, out.getvalue()

    def reachable(self, ok=True):
        resp = SimpleNamespace(status_code=200 if ok else 404, headers={"Content-Type": "image/png", "Content-Length": "123"})
        return patch("apitokens.management.commands.kieai_live_check.requests.head", return_value=resp)

    def test_refuses_without_confirm_cost_and_never_touches_kieai_or_the_network(self):
        with patch("apitokens.services.kieai.generate_tryon") as gen, patch("apitokens.services.kieai.upload_image") as up, \
                patch("apitokens.services.kieai.create_task") as task, \
                patch("apitokens.management.commands.kieai_live_check.requests.head") as head:
            for extra in ([], ["--end-to-end"]):
                error, out = self.run_cmd(*self.args(*extra, confirm=False))
                self.assertEqual(error.returncode, 2)
                self.assertIn("REFUSING TO RUN", str(error))
                self.assertIn("--confirm-cost", str(error))
                self.assertIn("PAID", str(error))
                self.assertIn("expected paid generations: " + ("2" if extra else "1"), str(error))
            # حتى بدون أي وسيط: الرفض هو أول شي (قبل أي تحقق)
            error, _ = self.run_cmd()
            self.assertEqual(error.returncode, 2)
            self.assertIn("REFUSING TO RUN", str(error))
        gen.assert_not_called()
        up.assert_not_called()
        task.assert_not_called()
        head.assert_not_called()
        self.assertEqual(ApiToken.objects.count(), 0)

    def test_direct_call_runs_once_with_the_widget_prompt_and_checks_reachability(self):
        from apitokens import widget_views

        with patch("apitokens.services.kieai.generate_tryon", return_value=self.REAL_URL) as gen, self.reachable():
            error, out = self.run_cmd(*self.args())
        self.assertIsNone(error, out)
        self.assertEqual(gen.call_count, 1)
        kwargs = gen.call_args.kwargs
        self.assertEqual(kwargs["prompt"], widget_views.TRYON_PROMPT)
        self.assertEqual(kwargs["api_key"], "k-test")
        self.assertEqual(kwargs["product_image_url"], "https://cdn.example.com/dress.jpg")
        self.assertEqual(kwargs["person_filename"], os.path.basename(self.person))
        self.assertIn("expected PAID kie.ai generations: 1", out)
        self.assertIn(self.REAL_URL, out)
        self.assertIn("reachable: HTTP 200", out)
        self.assertIn("direct call: ok in", out)

    def test_unreachable_result_or_generation_failure_gives_exit_code_1(self):
        with patch("apitokens.services.kieai.generate_tryon", return_value=self.REAL_URL), self.reachable(ok=False):
            error, out = self.run_cmd(*self.args())
        self.assertEqual(error.returncode, 1)
        self.assertIn("NOT reachable", out)
        from apitokens.services import kieai

        with patch("apitokens.services.kieai.generate_tryon", side_effect=kieai.TryOnError("generation_failed")):
            error, out = self.run_cmd(*self.args())
        self.assertEqual(error.returncode, 1)
        self.assertIn("generation_failed", out)

    def test_precondition_errors_exit_2_before_any_paid_call(self):
        with patch("apitokens.services.kieai.generate_tryon") as gen:
            error, _ = self.run_cmd("--person", "/nonexistent.jpg", "--product-image", "https://x/y.jpg", "--confirm-cost")
            self.assertEqual(error.returncode, 2)
            error, _ = self.run_cmd("--person", self.person, "--product-image", "ftp://nope", "--confirm-cost")
            self.assertEqual(error.returncode, 2)
            with override_settings(KIEAI_API_KEY=""):
                error, _ = self.run_cmd(*self.args())
            self.assertEqual(error.returncode, 2)
            self.assertIn("KIEAI_API_KEY", str(error))
        gen.assert_not_called()

    def test_end_to_end_drives_the_real_endpoint_and_verifies_quota_reactions_and_memory(self):
        with patch("apitokens.services.kieai.generate_tryon", return_value=self.REAL_URL) as gen, self.reachable():
            error, out = self.run_cmd(*self.args("--end-to-end"))
        self.assertIsNone(error, out)
        self.assertEqual(gen.call_count, 2)  # 1 مباشر + 1 عبر /widget/tryon/
        self.assertIn("expected PAID kie.ai generations: 2", out)
        for name in ("tryon_endpoint", "quota_reserved_once_not_refunded", "reaction_tried_on", "event_message",
                     "conversation_tryons", "next_turn_memory_mentions_tryon"):
            self.assertIn(f"e2e {name}: ok", out)
        # الاستدعاء الثاني بالـ endpoint استعمل صورة المنتج يلي انعطت
        self.assertEqual(gen.call_args_list[1].kwargs["product_image_url"], "https://cdn.example.com/dress.jpg")

    def test_end_to_end_failure_is_reported_with_exit_1(self):
        from apitokens.services import kieai

        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise kieai.TryOnError("timed_out")
            return self.REAL_URL

        with patch("apitokens.services.kieai.generate_tryon", side_effect=flaky), self.reachable():
            error, out = self.run_cmd(*self.args("--end-to-end"))
        self.assertEqual(error.returncode, 1)
        self.assertIn("e2e tryon_endpoint: FAILED", out)
        self.assertIn("e2e quota_reserved_once_not_refunded: FAILED", out)  # الرصيد انرجّع (فشل التوليد)

    def test_memory_mentions_tryon_helper(self):
        self.assertTrue(kieai_live_check.memory_mentions_tryon(['x DRS-ROSE "فستان" (tried_on)'], "DRS-ROSE"))
        self.assertFalse(kieai_live_check.memory_mentions_tryon(['DRS-ROSE "فستان" (liked)'], "DRS-ROSE"))
        self.assertFalse(kieai_live_check.memory_mentions_tryon([], "DRS-ROSE"))

    def test_docstring_warns_about_cost_and_owner_approval(self):
        doc = kieai_live_check.__doc__
        self.assertIn("بيكلّف فلوس حقيقية", doc)
        self.assertIn("بعد ما صاحب المشروع يوافق", doc)
        self.assertIn("--confirm-cost", doc)


# ======================================================================
# مفاتيح التتبّع الاختيارية (stage / discount) + إغلاق الثغرات
# ======================================================================

class OptionalTraceKeysTests(TestCase):
    def test_discount_verdict_is_optional_and_compares_allowed_and_reason(self):
        check = v2_checks.discount_verdict
        self.assertTrue(check(turn(trace={"constraints": {}}), ctx(arg={"allowed": False}))[1].startswith("skipped"))
        self.assertTrue(check(turn(trace=None), ctx(arg={"allowed": False}))[1].startswith("skipped"))
        closed = turn(trace={"constraints": {"discount": {"allowed": False, "reason": "too_early"}}})
        self.assertTrue(check(closed, ctx(arg={"allowed": False}))[0])
        self.assertTrue(check(closed, ctx(arg={"allowed": False, "reason": ["too_early", "not_engaged"]}))[0])
        ok, note = check(closed, ctx(arg={"allowed": True}))
        self.assertFalse(ok)
        self.assertIn("allowed=False", note)
        self.assertFalse(check(closed, ctx(arg={"reason": "ok"}))[0])

    def test_stage_names_used_by_scenarios_are_real_stages(self):
        from apitokens.services.contracts import STAGES

        used = set()
        for s in v2_scenarios.SCENARIOS:
            for st in s["steps"]:
                if st[0] == "say" and "stage_is" in st[2]:
                    value = st[2]["stage_is"]
                    used.update([value] if isinstance(value, str) else value)
        self.assertTrue(used)
        self.assertTrue(used <= set(STAGES), used - set(STAGES))

    def test_scenario_with_trace_keys_present_is_checked_end_to_end(self):
        """المحرّك لما يسجّل constraints["stage"/"discount"] الفحوصات بتنفّذ فعلاً (مش بتنتخطى)."""
        from apitokens.services import memory

        original = memory.save_trace

        def with_stage(ctx_, turn_no, **fields):
            fields["constraints"] = {**fields.get("constraints", {}), "stage": "discovery",
                                     "discount": {"allowed": False, "reason": "no_trigger"}}
            return original(ctx_, turn_no, **fields)

        scenario = dict(name="stage_probe", description="x", lang="ar", steps=[
            v2_scenarios.say("هلا", stage_is="discovery", discount_verdict={"allowed": False}),
            v2_scenarios.say("وريني", stage_is="closing")], judge=[])
        llm_script = ScriptedLLM({"هلا": {"reply": "يا هلا والله."}, "وريني": {"reply": "أبشر."}})
        with patch("apitokens.services.llm.chat_with_retry", side_effect=llm_script.chat), \
                patch("apitokens.services.llm.chat_step_with_retry", side_effect=llm_script.step), \
                patch("apitokens.services.llm.get_provider", side_effect=lambda role=None: FakeProvider()), \
                patch("apitokens.services.memory.save_trace", side_effect=with_stage), \
                patch.object(v2_scenarios, "SCENARIOS", [scenario]):
            out = io.StringIO()
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "r.json")
                call_command("eval_v2", "--only", "stage_probe", "--use-current-db", "--out", path, stdout=out)
                with open(path, encoding="utf-8") as fh:
                    report = json.load(fh)
        checks = {(i, c["name"]): c for i, r in enumerate(report["scenarios"][0]["turns"]) for c in r["checks"]}
        self.assertTrue(checks[(0, "stage_is")]["ok"] and not checks[(0, "stage_is")]["skipped"])
        self.assertTrue(checks[(0, "discount_verdict")]["ok"] and not checks[(0, "discount_verdict")]["skipped"])
        wrong = checks[(1, "stage_is")]
        self.assertFalse(wrong["ok"])
        self.assertEqual(wrong["severity"], "soft")  # ما بيفشّل exit code
        self.assertEqual(report["summary"]["hard_failures"], 0)


class BenchFailureExitTests(EvalCase):
    def test_bench_exits_1_when_turns_fail_but_still_writes_the_report(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.json")
            with patch("apitokens.services.llm.get_provider", side_effect=lambda role=None: SimpleNamespace(
                    model="x", is_configured=lambda: False)):
                with self.assertRaises(CommandError) as ctxm:
                    call_command("bench_v2", "--runs", "1", "--use-current-db", "--out", path, stdout=out)
            self.assertEqual(ctxm.exception.returncode, 1)
            with open(path, encoding="utf-8") as fh:
                report = json.load(fh)
        self.assertTrue(all(not t["ok"] for t in report["runs"][0]["turns"]))
        self.assertIn("failed", out.getvalue())


class MustNotContainWholeWordTests(TestCase):
    """أخطاء فحص ظهرت بالتقييم الحي: "خلال يومين" ≠ "خلال يوم"، والرفض الصريح مش وعد."""

    def check(self, reply, words):
        from .evals import v2_checks

        return v2_checks.must_not_contain({"reply": reply, "cards": []}, {"arg": words})[0]

    def test_whole_words_only(self):
        self.assertTrue(self.check("التوصيل خلال يومين إلى ٤ أيام عمل", ["خلال يوم"]))
        self.assertFalse(self.check("بيوصل خلال يوم واحد", ["خلال يوم"]))

    def test_a_refusal_or_correction_is_not_a_promise(self):
        self.assertTrue(self.check("ما أقدر أوعدك إنها توصل بكرة", ["بكرة", "بكره"]))
        self.assertTrue(self.check("لا، الشحن مو مجاني فوق ٥٠٠ ريال", ["مجاني"]))
        self.assertFalse(self.check("بيوصلك بكرة", ["بكرة"]))
        self.assertFalse(self.check("الشحن مجاني لك", ["مجاني"]))
