"""تقييم حيّ لمحرّك المبيعات v2 (ذاكرة دائمة) ضد الموديل الحقيقي المضبوط بالإعدادات.

    python manage.py eval_v2
    python manage.py eval_v2 --group personas --judge --out qa_reports/persona_eval.json
    python manage.py eval_v2 --only returning_customer_remembers,forget_me --runs 3
    python manage.py eval_v2 --provider openai --model gpt-5.1 --effort low --judge --out report.json

بيشغّل سيناريوهات طويلة (apitokens/evals/v2_scenarios.py) عبر endpoints v2 الحقيقية بـ Django test Client
بنفس طريقة الودجت: التوكنات من الردود، وآخر رسالة بس بتنرسل (التاريخ على السيرفر). كل شي بقاعدة بيانات
مؤقتة (نفس تقنية eval_sales) فما بيلمس بيانات حقيقية. تجربة اللبس (kie.ai) ممثَّلة بالكامل = بلا تكلفة؛
فحص kie.ai الحقيقي أمر منفصل: kieai_live_check.

الفحوصات حتمية بالكود (apitokens/evals/v2_checks.py)؛ الحكم النوعي بموديل (--judge) اختياري وبيحمل
"تحيّز نفس العائلة" إذا الحكم والمساعد من نفس العائلة — وهالشي بيتذكر بالتقرير.

Exit code 1 لو أي فحص "صلب" (hard) فشل. الفحوصات الناعمة (heuristic) بتنعرض بس ما بتفشّل التشغيل.
"""

import argparse
import json
import re
import time
import uuid
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.test import Client
from django.test.utils import setup_databases, setup_test_environment, teardown_databases

from apitokens.evals import describe, v2_checks, v2_scenarios
from apitokens.models import ApiToken, Conversation, CustomerProfile, Message, PreferenceFact, ProductReaction, TurnTrace
from apitokens.services import guard, llm, memory, ratelimit

FAKE_TRYON_URL = "https://cdn.example.com/eval-fake-tryon-result.png"
FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"

PROVIDER_MODEL_SETTING = {
    "openai": "OPENAI_MODEL", "anthropic": "ANTHROPIC_MODEL", "gemini": "GEMINI_MODEL", "groq": "GROQ_MODEL",
}
_MISSING = object()


# ======================================================================
# إعدادات: override داخل العملية + قاعدة بيانات مؤقتة
# ======================================================================

@contextmanager
def llm_overrides(provider=None, model=None, effort=None, analyzer_model=None, analyzer_provider=None):
    """بيغيّر `settings` داخل العملية (settings.py بيحمّل .env بدون شرط فما في env var بتنفع) ويرجّعها بالآخر."""
    changes = {}
    if provider:
        changes["LLM_PROVIDER"] = provider
    name = (changes.get("LLM_PROVIDER") or settings.LLM_PROVIDER or "anthropic").strip().lower()
    if model:
        changes[PROVIDER_MODEL_SETTING.get(name, "OPENAI_MODEL")] = model
    if effort is not None:
        changes["OPENAI_REASONING_EFFORT"] = effort
    if analyzer_model:
        changes["ANALYZER_MODEL"] = analyzer_model
    if analyzer_provider:
        changes["ANALYZER_PROVIDER"] = analyzer_provider
    saved = {k: getattr(settings, k, _MISSING) for k in changes}
    for key, value in changes.items():
        setattr(settings, key, value)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is _MISSING:
                delattr(settings, key)
            else:
                setattr(settings, key, value)


@contextmanager
def eval_environment(use_current_db=False, fake_tryon=True):
    """بيئة التقييم: ALLOWED_HOSTS للـ test Client، حدود المعدّل مرفوعة، مفتاح kie.ai وهمي (التوليد ممثَّل)،
    وقاعدة بيانات مؤقتة. use_current_db=True (للاختبارات): ما بينشئ قاعدة جديدة.
    fake_tryon=False (kieai_live_check --end-to-end بس): التوليد حقيقي ومفتاح kie.ai الحقيقي بيضل."""
    try:
        setup_test_environment()
    except RuntimeError:  # مستدعاة قبل (داخل اختبارات Django)
        pass
    saved = {k: getattr(settings, k, _MISSING) for k in ("RATE_LIMITS", "KIEAI_API_KEY")}
    settings.RATE_LIMITS = {rule: (10**9, 60) for rule in ratelimit.DEFAULT_LIMITS}
    settings.KIEAI_API_KEY = settings.KIEAI_API_KEY or "eval-fake-key-never-sent"
    old_config = None
    if not use_current_db and connection.vendor == "sqlite":
        # SQLite: قاعدة مؤقتة (transaction طويلة كانت بتقفل الملف وبتوقّف السيرفر الشغّال). Postgres: rollback.
        if connection.is_in_memory_db() and connection.connection is not None:
            # settings_test: NAME=":memory:" وDjango بيتجاهل close() لقاعدة الذاكرة، فالاتصال المفتوح (من فحوصات
            # manage.py) بيضل ماسك قاعدة الذاكرة بدل قاعدة الاختبار الجديدة — منسكّره بالقوة قبل setup_databases.
            connection.connection.close()
            connection.connection = None
        old_config = setup_databases(verbosity=0, interactive=False)
    try:
        fake = patch("apitokens.widget_views.kieai.generate_tryon", return_value=FAKE_TRYON_URL) if fake_tryon \
            else nullcontext()
        with fake:
            with transaction.atomic():
                yield
                transaction.set_rollback(True)
    finally:
        if old_config is not None:
            teardown_databases(old_config, verbosity=0)
        for key, value in saved.items():
            if value is _MISSING:
                delattr(settings, key)
            else:
                setattr(settings, key, value)


# ======================================================================
# الودجت (نفس embed.js بوضع v2)
# ======================================================================

class Widget:
    """بيحاكي الودجت: توكنات بالتخزين، رسالة وحدة بالطلب (بدون تاريخ)، ساعة قابلة للتقديم (new_visit)."""

    def __init__(self, merchant, ip="10.9.0.1", storage=None):
        self.merchant = merchant
        self.ip = ip
        self.storage = storage if storage is not None else {}
        self.offset_days = 0

    def _clock(self):
        if not self.offset_days:
            return nullcontext()
        later = time.time() + self.offset_days * 86400
        return patch("django.core.signing.time", SimpleNamespace(time=lambda: later))

    def _headers(self, session=False):
        extra = {"HTTP_X_API_TOKEN": self.merchant.token, "REMOTE_ADDR": self.ip}
        if session and self.storage.get("session_token"):
            extra["HTTP_X_SESSION_TOKEN"] = self.storage["session_token"]
        return extra

    def _store(self, data):
        for key in ("session_token", "shopper_token", "session_id"):
            if isinstance(data, dict) and data.get(key):
                self.storage[key] = data[key]

    def _tokens(self):
        return {k: self.storage.get(k) for k in ("session_token", "shopper_token")}

    def post_json(self, path, body, session=False):
        with self._clock():
            resp = Client().post(path, data=json.dumps(body), content_type="application/json",
                                 **self._headers(session))
        try:
            data = resp.json()
        except ValueError:
            data = {"ok": False, "error": f"non_json_response_{resp.status_code}"}
        return resp.status_code, data

    def bootstrap(self, page_url=""):
        _, data = self.post_json("/widget/v2/bootstrap/", {**self._tokens(), "page_url": page_url})
        self._store(data)
        return data

    def session(self):
        _, data = self.post_json("/widget/v2/session/", self._tokens())
        self._store(data)
        return data

    def say(self, text, page_url=""):
        """(data, status, seconds) — الثواني = زمن الجدار حول طلب HTTP."""
        body = {"message": text, "client_msg_id": uuid.uuid4().hex, "page_url": page_url, **self._tokens()}
        started = time.perf_counter()
        status, data = self.post_json("/widget/v2/chat/", body)
        elapsed = time.perf_counter() - started
        self._store(data)
        return data, status, elapsed

    def tryon(self, sku, photo_bytes=None, filename="me.jpg", content_type="image/jpeg"):
        photo = SimpleUploadedFile(filename, photo_bytes or FAKE_JPEG, content_type=content_type)
        with self._clock():
            resp = Client().post("/widget/tryon/", {"sku": sku, "photo": photo}, **self._headers(session=True))
        return resp.status_code, resp.json()

    def event(self, kind, sku):
        return self.post_json("/widget/event/", {"type": kind, "sku": sku}, session=True)

    def forget(self):
        status, data = self.post_json("/widget/v2/forget/", self._tokens())
        return status, data

    def memory(self, action="list", **extra):
        return self.post_json("/widget/v2/memory/", {"shopper_token": self.storage.get("shopper_token"),
                                                      "action": action, **extra})[1]


# ======================================================================
# منفّذ السيناريوهات
# ======================================================================

def _new_state(lang):
    return {
        "lang": lang, "budget": None, "size": None, "stated_slots": set(), "slot_values": {},
        "rejected": set(), "last_rejected": set(), "prior_skus": [], "last_card_sku": None,
        "last_card_price": None, "offers_total": 0, "customer_texts": [],
    }


def _result(name, ok, note="", soft=False):
    return {
        "name": name, "ok": bool(ok), "note": note or "",
        "severity": "soft" if soft else "hard",
        "skipped": bool(ok) and (note or "").startswith("skipped"),
    }


class ScenarioRunner:
    def __init__(self, stores, judge=None, ip_prefix="10.9"):
        self.stores = stores
        self.catalogs = {name: v2_scenarios.catalog_info(m) for name, m in stores.items()}
        self.judge = judge
        self.ip_prefix = ip_prefix
        self._counter = 0

    # ---------------------------------------------------------------- تشغيل سيناريو
    def run(self, scenario, run_index=0):
        store_name = scenario.get("store", "default")
        merchant = self.stores[store_name]
        self._counter += 1
        widget = Widget(merchant, ip=f"{self.ip_prefix}.{run_index % 250}.{self._counter % 250 + 1}")
        run = SimpleNamespace(
            scenario=scenario, merchant=merchant, widget=widget, catalog=self.catalogs[store_name],
            state=_new_state(scenario.get("lang", "ar")), page="", records=[], transcript=[], turn_no=0,
            codes=[d.code for d in merchant.discount_codes.all()],
        )
        started = time.time()
        for step in scenario["steps"]:
            kind = step[0]
            try:
                handler = getattr(self, f"_step_{kind}")
            except AttributeError:
                run.records.append({"step": kind, "checks": [_result("known_step", False, f"unknown step {kind}")]})
                continue
            try:
                run.records.append(handler(run, step))
            except Exception as exc:  # noqa: BLE001 — خطأ بخطوة = فشل واضح، مش انهيار التقييم كله
                run.records.append({"step": kind, "error": f"{type(exc).__name__}: {exc}",
                                    "checks": [_result("step_executed", False, f"{type(exc).__name__}: {exc}")]})
        return self._finish(run, run_index, time.time() - started)

    def _finish(self, run, run_index, seconds):
        checks = [c for rec in run.records for c in rec.get("checks", [])]
        hard = [c for c in checks if not c["ok"] and c["severity"] == "hard"]
        soft = [c for c in checks if not c["ok"] and c["severity"] == "soft"]
        says = [r for r in run.records if r.get("step") == "say"]
        result = {
            "name": run.scenario["name"], "description": run.scenario.get("description", ""), "run": run_index,
            "store": run.scenario.get("store", "default"), "passed": not hard, "hard_failures": len(hard),
            "soft_failures": len(soft), "seconds": round(seconds, 2), "turns": run.records,
            "latencies": [r["latency_s"] for r in says if r.get("latency_s") is not None],
            "llm_calls": [r["trace"]["llm_calls"] for r in says if r.get("trace")],
            "judge": None,
        }
        if self.judge is not None and run.scenario.get("judge"):
            try:
                result["judge"] = self.judge(run.scenario, run.transcript, run.merchant)
            except Exception as exc:  # noqa: BLE001
                result["judge"] = {"error": f"{type(exc).__name__}: {exc}"}
        return result

    # ---------------------------------------------------------------- say
    def _step_say(self, run, step):
        text = step[1]
        exp = dict(step[2]) if len(step) > 2 and step[2] else {}
        state = run.state
        extra = []
        state["lang"] = exp.get("lang", run.scenario.get("lang", "ar"))
        if "budget" in exp:
            state["budget"] = exp["budget"]
        if "size" in exp:
            state["size"] = exp["size"]
        states = exp.get("states") or {}
        if not isinstance(states, dict):
            states = {s: None for s in states}
        for slot, value in states.items():
            state["stated_slots"].add(slot)
            if value is not None:
                state["slot_values"][slot] = value
        state["customer_texts"].append(text)
        asked_for = set()
        if exp.get("rejects_last_card"):
            if state["last_card_sku"]:
                state["rejected"].add(state["last_card_sku"])
            else:
                extra.append(_result("precondition", False, "no earlier card to reject: scenario inconclusive", soft=True))
        if exp.get("asks_for_rejected"):
            asked_for = set(state["rejected"])
            state["last_rejected"] = set(asked_for)
            if not asked_for:
                extra.append(_result("precondition", False, "nothing was rejected earlier: scenario inconclusive", soft=True))

        data, status, seconds = run.widget.say(text, run.page)
        turn = self._turn_record(run, text, data, status, seconds)
        run.transcript.append({"role": "user", "content": text})
        run.transcript.append({"role": "assistant", "content": turn["reply"]})

        ctx = self._context(run, turn, asked_for, bool(exp.get("allow_reshow")))
        names = list(v2_checks.DEFAULT_CHECKS)
        if state["budget"] is not None:
            names.append("within_budget")
        if state["size"]:
            names.append("size_available")
        for key in exp:
            if key in v2_checks.CHECKS and key not in names:
                names.append(key)
        names = [n for n in names if n not in set(exp.get("skip") or ())]
        soft_names = set(exp.get("soft") or ())
        checks = list(extra)
        for name in names:
            arg = exp.get(name)
            ok, note = v2_checks.evaluate(name, turn, {**ctx, "arg": arg})
            checks.append(_result(name, ok, note, soft=name in v2_checks.SOFT_CHECKS or name in soft_names))

        # تحديث الحالة بعد الفحص
        cards = turn["cards"]
        state["rejected"] -= asked_for
        if cards:
            state["last_card_sku"] = cards[-1]["sku"]
            try:
                state["last_card_price"] = cards[-1]["price"]
            except KeyError:
                pass
        state["prior_skus"].extend(c["sku"] for c in cards)
        if turn["offer"]:
            state["offers_total"] += 1
        turn["checks"] = checks
        turn["step"] = "say"
        return turn

    def _turn_record(self, run, text, data, status, seconds):
        merchant = run.merchant
        data = data if isinstance(data, dict) else {}
        conv = None
        if data.get("session_id"):
            conv = Conversation.objects.filter(merchant=merchant, session_id=data["session_id"]).first()
        trace = None
        if conv is not None and data.get("turn"):
            trace = TurnTrace.objects.filter(conversation=conv, turn=data["turn"]).order_by("-id").first()
        profile = conv.profile if conv is not None else None
        facts = []
        rejected_db = []
        if profile is not None:
            facts = [
                {"key": f.key, "value": f.value, "polarity": f.polarity, "source": f.source, "subject": f.subject}
                for f in PreferenceFact.objects.filter(profile=profile, status="active").order_by("id")
            ]
            rejected_db = sorted(memory.excluded_skus(profile))
        run.turn_no += 1
        return {
            "index": run.turn_no, "user": text, "reply": data.get("reply") or "",
            "cards": data.get("recommendations") or [], "offer": data.get("discount_offer"),
            "size_advice": data.get("size_advice"), "latency_s": round(seconds, 3),
            "ok": bool(data.get("ok")), "status": status, "error": data.get("error"),
            "page_url": run.page, "turn_no": data.get("turn"), "facts": facts, "rejected_db": rejected_db,
            "trace": None if trace is None else {
                "llm_calls": trace.llm_calls, "latency_ms": trace.latency_ms, "analyzer": trace.analyzer,
                "constraints": trace.constraints, "guard": trace.guard, "memory_changes": trace.memory_changes,
                "models_used": trace.models_used, "error": trace.error,
            },
        }

    def _context(self, run, turn, asked_for, allow_reshow):
        state, merchant = run.state, run.merchant
        grounding = guard.build_context(
            products=list(merchant.products.all()), merchant_codes=run.codes, approved_offer=turn["offer"],
            cards=turn["cards"], store_info=merchant.store_info, customer_texts=state["customer_texts"],
        )
        return {
            "lang": state["lang"], "catalog": run.catalog, "grounding": grounding, "budget": state["budget"],
            "size": state["size"], "stated_slots": set(state["stated_slots"]), "slot_values": dict(state["slot_values"]),
            "rejected_skus": set(state["rejected"]), "asked_for": asked_for, "last_rejected": set(state["last_rejected"]),
            "prior_card_skus": list(state["prior_skus"]), "last_card_price": state["last_card_price"],
            "offers_before": state["offers_total"], "discount_codes": run.codes, "allow_reshow": allow_reshow,
        }

    # ---------------------------------------------------------------- بقية الخطوات
    def _bootstrap_record(self, run, step_name, data, expect, extra=None):
        record = {
            "step": step_name, "page_url": run.page, "returning": bool(data.get("returning")),
            "messages_restored": len(data.get("messages") or []),
            "page_product_sku": (data.get("page_product") or {}).get("sku"), **(extra or {}),
        }
        record["checks"] = [_result(n, ok, note) for n, ok, note in v2_checks.check_bootstrap(record, expect or {})]
        return record

    def _step_navigate(self, run, step):
        run.page = step[1]
        expect = step[2] if len(step) > 2 else {}
        data = run.widget.bootstrap(run.page)
        return self._bootstrap_record(run, "navigate", data, expect)

    def _step_new_visit(self, run, step):
        days = step[1]
        expect = step[2] if len(step) > 2 else {}
        run.widget.offset_days += days
        run.state["prior_skus"] = []  # محادثة جديدة: الكروت السابقة مش "معروضة" بالمحادثة الجديدة
        run.state["last_card_sku"] = None
        run.state["last_card_price"] = None
        data = run.widget.bootstrap(run.page)
        return self._bootstrap_record(run, "new_visit", data, expect, {"days": days})

    def _resolve_sku(self, run, sku):
        if sku == "@last":
            return run.state["last_card_sku"] or v2_scenarios.FALLBACK_SKU, not run.state["last_card_sku"]
        return sku, False

    def _conversation(self, run):
        sid = run.widget.storage.get("session_id")
        return Conversation.objects.filter(merchant=run.merchant, session_id=sid).first() if sid else None

    def _step_tryon(self, run, step):
        sku, fell_back = self._resolve_sku(run, step[1])
        if not run.widget.storage.get("session_token"):
            run.widget.session()
        merchant_before = ApiToken.objects.get(pk=run.merchant.pk).images_used
        conv_before = self._conversation(run)
        tryons_before = conv_before.tryons if conv_before else 0
        reacts_before = ProductReaction.objects.filter(merchant=run.merchant, reaction="tried_on", sku=sku).count()
        status, data = run.widget.tryon(sku)
        merchant_after = ApiToken.objects.get(pk=run.merchant.pk).images_used
        conv_after = self._conversation(run)
        problems = []
        if not data.get("ok"):
            problems.append(f"tryon failed: {data}")
        else:
            if data.get("image_url") != FAKE_TRYON_URL:
                problems.append("unexpected image_url")
            if merchant_after != merchant_before + 1:
                problems.append(f"quota not reserved once ({merchant_before} -> {merchant_after})")
            if conv_after is None or conv_after.tryons != tryons_before + 1:
                problems.append("conversation.tryons not incremented")
            if ProductReaction.objects.filter(merchant=run.merchant, reaction="tried_on", sku=sku).count() != reacts_before + 1:
                problems.append("ProductReaction(tried_on) not recorded")
            if conv_after is None or not Message.objects.filter(
                    conversation=conv_after, role="event", content__startswith=f"tried_on: {sku}").exists():
                problems.append("event message not recorded")
        checks = [_result("tryon_recorded", not problems, "; ".join(problems))]
        if fell_back:
            checks.append(_result("precondition", False, f"no earlier card; used fallback {sku}", soft=True))
        return {"step": "tryon", "sku": sku, "status": status, "image_url": data.get("image_url"), "checks": checks}

    def _step_event(self, run, step):
        kind, (sku, fell_back) = step[1], self._resolve_sku(run, step[2])
        field = {"buy_click": "buy_clicks", "add_to_cart": "add_to_carts"}[kind]
        conv_before = self._conversation(run)
        before = getattr(conv_before, field) if conv_before else 0
        status, data = run.widget.event(kind, sku)
        conv_after = self._conversation(run)
        problems = []
        if status != 200 or not data.get("ok"):
            problems.append(f"event failed: status={status} {data}")
        else:
            if conv_after is None or getattr(conv_after, field) != before + 1:
                problems.append(f"conversation.{field} not incremented")
            if not ProductReaction.objects.filter(merchant=run.merchant, reaction=kind, sku=sku).exists():
                problems.append(f"ProductReaction({kind}) not recorded")
        checks = [_result("event_recorded", not problems, "; ".join(problems))]
        if fell_back:
            checks.append(_result("precondition", False, f"no earlier card; used fallback {sku}", soft=True))
        return {"step": "event", "type": kind, "sku": sku, "status": status, "checks": checks}

    def _step_forget(self, run, step):
        expect = step[1] if len(step) > 1 and isinstance(step[1], dict) else {}
        profile_id = None
        conv = self._conversation(run)
        if conv is not None and conv.profile is not None:
            profile_id = conv.profile.public_id
        status, data = run.widget.forget()
        problems = []
        if status != 200 or not data.get("ok"):
            problems.append(f"forget failed: {status} {data}")
        if expect.get("forgotten", True) and not data.get("forgotten"):
            problems.append("forgotten flag false")
        if profile_id and CustomerProfile.objects.filter(public_id=profile_id).exists():
            problems.append("profile still exists")
        run.widget.storage.clear()  # الودجت بيمسح تخزينه بعد النسيان
        run.state = _new_state(run.state["lang"])
        return {"step": "forget", "status": status, "forgotten": data.get("forgotten"),
                "checks": [_result("forget_erased", not problems, "; ".join(problems))]}


# ======================================================================
# الحكم النوعي (--judge)
# ======================================================================

JUDGE_PROMPT = """You are a strict QA reviewer of an e-commerce sales chatbot for a Saudi fashion store. \
The chatbot must sound like a professional Saudi salesperson texting on WhatsApp (natural Saudi/Gulf dialect, \
never Levantine, Egyptian or stiff Modern Standard Arabic), be persuasive without pressure, never invent \
prices, discounts, codes, shipping promises, colours, sizes or materials, and remember what the customer said. \
Each product card has a real try-on button and a real buy button, so mentioning them is fine. \
Read the whole conversation and answer with JSON ONLY, no prose: \
{"persuasion": <1-5>, "naturalness": <1-5>, "saudi": <1-5>, \
"criteria": [{"criterion": "...", "pass": true|false, "note": "<12 words"}], "summary": "<20 words"} \
Scores: 5 = excellent, 1 = unacceptable. Be strict."""


def model_family(model):
    match = re.match(r"[a-z]+", (model or "").lower())
    return match.group(0) if match else ""


def parse_judge_json(text):
    """JSON صارم: لازم الأرقام 1..5 والمفاتيح موجودة، وإلا ValueError."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("judge returned no JSON object")
    data = json.loads(text[start:end + 1])
    for key in ("persuasion", "naturalness", "saudi"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 5:
            raise ValueError(f"judge score '{key}' missing or outside 1-5: {value!r}")
        data[key] = int(round(value))
    criteria = data.get("criteria") or []
    if not isinstance(criteria, list):
        raise ValueError("judge 'criteria' must be a list")
    data["criteria"] = [
        {"criterion": str(c.get("criterion", "")), "pass": bool(c.get("pass")), "note": str(c.get("note", ""))}
        for c in criteria if isinstance(c, dict)
    ]
    data["summary"] = str(data.get("summary", ""))
    return data


def make_judge(model, effort="medium", client=None):
    """judge(scenario, transcript, merchant) ← dict. client قابل للحقن (اختبارات)؛ الافتراضي OpenAI Responses."""

    def judge(scenario, transcript, merchant):
        if client is None and not settings.OPENAI_API_KEY:
            return {"error": "OPENAI_API_KEY not configured for the judge"}
        api = client
        if api is None:
            import openai

            api = openai.OpenAI(api_key=settings.OPENAI_API_KEY, timeout=90, max_retries=1)
        convo = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in transcript)
        criteria = "\n".join(f"- {c}" for c in scenario.get("judge") or [])
        catalog = json.dumps(v2_scenarios.CATALOG, ensure_ascii=False)
        codes = ", ".join(f"{d.code} ({d.description})" for d in merchant.discount_codes.filter(is_active=True)) or "none"
        kwargs = {"reasoning": {"effort": effort}} if effort and effort != "none" else {}
        resp = api.responses.create(
            model=model, instructions=JUDGE_PROMPT,
            input=(f"Store information:\n{merchant.store_info}\n\nActive discount codes: {codes}\n\n"
                   f"Catalog (out of stock: DRS-NAVY):\n{catalog}\n\nScenario: {scenario['name']} - "
                   f"{scenario.get('description', '')}\n\nConversation:\n{convo}\n\nCriteria:\n{criteria}"),
            max_output_tokens=3000, store=False, **kwargs,
        )
        data = parse_judge_json(resp.output_text.strip())
        data["judge_model"] = model
        return data

    return judge


def judge_bias_note(judge_model, assistant_model):
    jf, af = model_family(judge_model), model_family(assistant_model)
    if jf and jf == af:
        return (f"same family bias: the judge ({judge_model}) and the assistant ({assistant_model}) are the same model "
                f"family ('{jf}'); judge scores are likely inflated and should be read as relative, not absolute.")
    return (f"judge ({judge_model}) and assistant ({assistant_model}) look like different model families; "
            "LLM-judge scores are still subjective: read them as a trend, not as ground truth.")


# ======================================================================
# تلخيص وطباعة
# ======================================================================

def summarize(results):
    checks = {}
    latencies, llm_calls = [], []
    for res in results:
        latencies.extend(res.get("latencies") or [])
        llm_calls.extend(res.get("llm_calls") or [])
        for rec in res["turns"]:
            for c in rec.get("checks", []):
                row = checks.setdefault(c["name"], {"pass": 0, "fail_hard": 0, "fail_soft": 0, "skipped": 0})
                if c.get("skipped"):
                    row["skipped"] += 1
                elif c["ok"]:
                    row["pass"] += 1
                elif c["severity"] == "soft":
                    row["fail_soft"] += 1
                else:
                    row["fail_hard"] += 1
    by_scenario = {}
    for res in results:
        by_scenario.setdefault(res["name"], []).append(res)
    scores = {k: [r["judge"][k] for r in results if r.get("judge") and isinstance(r["judge"].get(k), int)]
              for k in ("persuasion", "naturalness", "saudi")}
    return {
        "scenarios": len(by_scenario),
        "scenarios_passed": sum(all(r["passed"] for r in runs) for runs in by_scenario.values()),
        "runs": len(results), "runs_passed": sum(r["passed"] for r in results),
        "hard_failures": sum(r["hard_failures"] for r in results),
        "soft_failures": sum(r["soft_failures"] for r in results),
        "checks": checks, "latency_s": describe(latencies),
        "llm_calls_per_turn": (sum(llm_calls) / len(llm_calls)) if llm_calls else None,
        "judge_avg": {k: (sum(v) / len(v) if v else None) for k, v in scores.items()},
    }


def _fmt(value, spec=".2f"):
    return "-" if value is None else format(value, spec)


class Command(BaseCommand):
    help = "Live evaluation suite for the v2 sales engine (persistent memory), run against the configured LLM"

    def add_arguments(self, parser):
        parser.add_argument("--only", help="Comma-separated scenario names")
        parser.add_argument("--group", help="Only this group: core (المحرك) or personas (شخصيات الزباين)")
        parser.add_argument("--list", action="store_true", help="List scenarios and exit")
        parser.add_argument("--provider", help="Override LLM_PROVIDER for this run (openai|anthropic|gemini|groq)")
        parser.add_argument("--model", help="Override the model of the (possibly overridden) provider")
        parser.add_argument("--effort", help="Override OPENAI_REASONING_EFFORT")
        parser.add_argument("--analyzer-model", help="Override ANALYZER_MODEL")
        parser.add_argument("--judge", action="store_true", help="Add the LLM judge (persuasion/naturalness/Saudi-ness)")
        parser.add_argument("--judge-model", help="Judge model (default: OPENAI_MODEL). Same family as the assistant = bias")
        parser.add_argument("--judge-effort", default="medium", help="Judge reasoning effort ('none' to omit)")
        parser.add_argument("--out", help="Write the full JSON report (transcripts, traces, checks) here")
        parser.add_argument("--runs", type=int, default=1, help="Run each scenario N times (fresh shopper each time)")
        parser.add_argument("--transcripts", action="store_true", help="Print every transcript (default: failures only)")
        parser.add_argument("--use-current-db", action="store_true", help=argparse.SUPPRESS)

    def handle(self, *args, **opts):
        scenarios = self._select(opts)
        if opts["list"]:
            for s in scenarios:
                says = len(v2_scenarios.customer_lines(s))
                self.stdout.write(
                    f"{s.get('group', 'core'):<9} {s['name']:<36} {says:>2} says  {s['description']}"
                )
            return
        runs = max(1, opts["runs"])
        with llm_overrides(opts["provider"], opts["model"], opts["effort"], opts["analyzer_model"]):
            assistant_model = self._assistant_model()
            judge, judge_meta = None, None
            if opts["judge"]:
                judge_model = opts["judge_model"] or settings.OPENAI_MODEL
                judge = make_judge(judge_model, opts["judge_effort"])
                judge_meta = {"model": judge_model, "bias": judge_bias_note(judge_model, assistant_model)}
            meta = {
                "provider": settings.LLM_PROVIDER, "assistant_model": assistant_model,
                "analyzer_model": settings.ANALYZER_MODEL or None, "effort": settings.OPENAI_REASONING_EFFORT or None,
                "runs": runs, "scenarios": [s["name"] for s in scenarios], "judge": judge_meta,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self.stdout.write(
                f"provider={meta['provider']} model={assistant_model} effort={meta['effort'] or '-'} "
                f"scenarios={len(scenarios)} runs={runs} judge={'on' if judge else 'off'}\n"
            )
            results = []
            with eval_environment(use_current_db=opts["use_current_db"]):
                stores = v2_scenarios.build_stores(sorted({s.get("store", "default") for s in scenarios}))
                runner = ScenarioRunner(stores, judge=judge)
                for scenario in scenarios:
                    for run_index in range(runs):
                        result = runner.run(scenario, run_index)
                        results.append(result)
                        self._print_scenario(result, show=opts["transcripts"], runs=runs)
            report = {"meta": meta, "summary": summarize(results), "scenarios": results}
        self._print_summary(report)
        if opts["out"]:
            with open(opts["out"], "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2, default=_json_default)
            self.stdout.write(f"report written: {opts['out']}")
        if report["summary"]["hard_failures"]:
            raise CommandError(f"{report['summary']['hard_failures']} hard check(s) failed", returncode=1)

    # ------------------------------------------------------------------
    def _select(self, opts):
        pool = v2_scenarios.SCENARIOS
        group = (opts.get("group") or "").strip()
        if group:
            pool = [s for s in pool if s.get("group", "core") == group]
            if not pool:
                groups = sorted({s.get("group", "core") for s in v2_scenarios.SCENARIOS})
                raise CommandError(f"unknown group: {group}. Available: {', '.join(groups)}")
        only = [n.strip() for n in (opts.get("only") or "").split(",") if n.strip()]
        names = {s["name"] for s in pool}
        unknown = [n for n in only if n not in names]
        if unknown:
            raise CommandError(f"unknown scenario(s): {', '.join(unknown)}. Available: {', '.join(sorted(names))}")
        wanted = set(only)
        return [s for s in pool if not wanted or s["name"] in wanted]

    @staticmethod
    def _assistant_model():
        try:
            return getattr(llm.get_provider(), "model", "") or ""
        except Exception:  # noqa: BLE001 — مزوّد بدون مفتاح/اسم غير معروف: التقرير ما بيوقف
            return ""

    def _print_scenario(self, result, show, runs):
        ok = result["passed"]
        tag = self.style.SUCCESS("PASS") if ok else self.style.ERROR("FAIL")
        suffix = f" run {result['run'] + 1}/{runs}" if runs > 1 else ""
        lat = describe(result["latencies"])
        self.stdout.write(
            f"[{tag}] {result['name']}{suffix}  turns={len(result['latencies'])} hard_fail={result['hard_failures']} "
            f"soft_fail={result['soft_failures']} p50={_fmt(lat['p50'], '.1f')}s max={_fmt(lat['max'], '.1f')}s"
        )
        for rec in result["turns"]:
            failed = [c for c in rec.get("checks", []) if not c["ok"]]
            if not (show or failed):
                continue
            if rec["step"] == "say":
                self.stdout.write("  U: " + rec["user"].replace("\n", " "))
                self.stdout.write("  A: " + rec["reply"].replace("\n", " "))
                for card in rec["cards"]:
                    self.stdout.write(f"     card {card.get('sku')} {card.get('price')} | {card.get('reason', '')[:70]}")
                if rec["offer"]:
                    self.stdout.write(f"     offer {rec['offer'].get('code')}")
            else:
                detail = {k: v for k, v in rec.items() if k not in ("checks", "step")}
                self.stdout.write(f"  [{rec['step']}] {json.dumps(detail, ensure_ascii=False, default=_json_default)[:140]}")
            for c in failed:
                style = self.style.ERROR if c["severity"] == "hard" else self.style.WARNING
                self.stdout.write(style(f"     x {c['severity']} {c['name']}: {c['note']}"))
        judge = result.get("judge")
        if judge:
            if judge.get("error"):
                self.stdout.write(self.style.WARNING(f"     judge failed: {judge['error']}"))
            else:
                self.stdout.write(f"     judge persuasion={judge['persuasion']} naturalness={judge['naturalness']} "
                                  f"saudi={judge['saudi']} - {judge.get('summary', '')}")

    def _print_summary(self, report):
        s = report["summary"]
        self.stdout.write("\n" + "=" * 70)
        self.stdout.write(f"scenarios : {s['scenarios_passed']}/{s['scenarios']} passed "
                          f"(runs {s['runs_passed']}/{s['runs']})")
        self.stdout.write(f"hard fails: {s['hard_failures']}   soft fails: {s['soft_failures']}")
        self.stdout.write("per-check totals (pass / hard-fail / soft-fail / skipped):")
        for name, row in sorted(s["checks"].items()):
            self.stdout.write(f"  {name:<26} {row['pass']:>4} / {row['fail_hard']:>3} / {row['fail_soft']:>3} / {row['skipped']:>3}")
        lat = s["latency_s"]
        self.stdout.write(f"latency   : p50={_fmt(lat['p50'])}s p95={_fmt(lat['p95'])}s mean={_fmt(lat['mean'])}s "
                          f"(n={lat['n']}, client wall-clock per turn)")
        self.stdout.write(f"llm calls : {_fmt(s['llm_calls_per_turn'])} per turn (from TurnTrace)")
        judge = report["meta"].get("judge")
        if judge:
            avg = s["judge_avg"]
            self.stdout.write(f"judge     : persuasion={_fmt(avg['persuasion'], '.1f')} naturalness={_fmt(avg['naturalness'], '.1f')} "
                              f"saudi={_fmt(avg['saudi'], '.1f')} (1-5)")
            self.stdout.write(f"judge note: {judge['bias']}")


def _json_default(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)
