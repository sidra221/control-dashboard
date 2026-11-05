"""فحص تكامل kie.ai الحقيقي (تجربة اللبس) — بيكلّف فلوس حقيقية!

    python manage.py kieai_live_check --person /path/me.jpg --product-image https://cdn.../dress.jpg --confirm-cost
    python manage.py kieai_live_check --person me.jpg --product-image URL --confirm-cost --end-to-end

لازم تشغّله بس بعد ما صاحب المشروع يوافق على الصرف. كل توليد صورة = استدعاء مدفوع لـ kie.ai.
عدد التوليدات المدفوعة المتوقع: 1 (الاستدعاء المباشر لـ apitokens.services.kieai.generate_tryon بنفس برومبت
widget_views.TRYON_PROMPT)، و 2 مع --end-to-end (الثاني عبر endpoint الودجت الحقيقي /widget/tryon/).
بدون --confirm-cost بيرفض (exit code 2) وما بيستدعي kie.ai ولا بيلمس الشبكة.

الاستدعاء المباشر: بيقيس المدّة، بيطبع رابط الصورة الناتجة، وبيعمل HEAD يتأكد إنه بيفتح.
--end-to-end: على تاجر v2 مؤقت (قاعدة بيانات مؤقتة، kie.ai حقيقي بدون mock) بيتحقق من:
  - الرصيد انحجز مرة وما انرجّع،
  - ProductReaction(tried_on) + رسالة الحدث انسجّلوا،
  - conversation.tryons زاد،
  - برومبت أول دور محادثة بعد التجربة (LLM ممثَّل بدون تكلفة) بيذكر التجربة بقسم الذاكرة.

Exit codes: 0 نجح، 1 فشل فحص، 2 رفض/إعداد ناقص (بدون --confirm-cost، بدون مفتاح، ملف مفقود).
"""

import argparse
import mimetypes
import os
import time
from unittest.mock import patch

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apitokens import widget_views
from apitokens.evals import v2_scenarios
from apitokens.management.commands.eval_v2 import Widget, eval_environment
from apitokens.models import Conversation, Message, ProductReaction
from apitokens.services import kieai, llm, memory

PAID_GENERATIONS = 1
PAID_GENERATIONS_END_TO_END = 2
HEAD_TIMEOUT = 15


def refusal_message(end_to_end=False):
    paid = PAID_GENERATIONS_END_TO_END if end_to_end else PAID_GENERATIONS
    return (
        "REFUSING TO RUN: this command makes REAL, PAID kie.ai image generations "
        f"(expected paid generations: {paid}). Run it only after the owner approved the spend, "
        "and pass --confirm-cost to acknowledge it. Nothing was sent to kie.ai."
    )


def check_reachable(url):
    """HEAD على الرابط الناتج: {"ok", "status", "content_type", "bytes", "error"}."""
    try:
        resp = requests.head(url, timeout=HEAD_TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        return {"ok": False, "status": None, "content_type": None, "bytes": None, "error": f"{type(exc).__name__}: {exc}"}
    content_type = resp.headers.get("Content-Type", "")
    return {
        "ok": resp.status_code == 200 and (not content_type or content_type.lower().startswith(("image/", "binary/", "application/octet"))),
        "status": resp.status_code, "content_type": content_type,
        "bytes": resp.headers.get("Content-Length"), "error": None,
    }


def memory_mentions_tryon(system_prompts, sku):
    """هل أي برومبت بائع (قسم الذاكرة) بيذكر إنه الزبون جرّب هالمنتج؟"""
    return any("tried_on" in p and sku in p for p in system_prompts)


class _FakeProvider:
    model = "kieai-live-check-stub"

    def is_configured(self):
        return True


class Command(BaseCommand):
    help = "REAL kie.ai try-on integration check (costs money; requires --confirm-cost)"

    def add_arguments(self, parser):
        parser.add_argument("--person", help="Path to a photo of a person (jpg/png)")
        parser.add_argument("--product-image", help="Public URL of the product image to put on the person")
        parser.add_argument("--confirm-cost", action="store_true", help="Acknowledge that real kie.ai generations are paid")
        parser.add_argument("--end-to-end", action="store_true",
                            help="Also drive the real /widget/tryon/ endpoint of a throwaway v2 merchant (second paid generation)")
        parser.add_argument("--use-current-db", action="store_true", help=argparse.SUPPRESS)

    def handle(self, *args, **opts):
        end_to_end = opts["end_to_end"]
        # الرفض أول شي — قبل ما نقرا أي ملف أو نفحص أي إعداد أو نلمس الشبكة
        if not opts["confirm_cost"]:
            raise CommandError(refusal_message(end_to_end), returncode=2)

        paid = PAID_GENERATIONS_END_TO_END if end_to_end else PAID_GENERATIONS
        person, product_image = opts["person"], opts["product_image"]
        if not person or not os.path.isfile(person):
            raise CommandError(f"--person must be an existing photo file (got {person!r})", returncode=2)
        if not product_image or not product_image.lower().startswith(("http://", "https://")):
            raise CommandError("--product-image must be a public http(s) URL", returncode=2)
        if not settings.KIEAI_API_KEY:
            raise CommandError("KIEAI_API_KEY is not configured: nothing was sent to kie.ai", returncode=2)
        size = os.path.getsize(person)
        if size > widget_views.MAX_PHOTO_BYTES:
            raise CommandError(f"--person is {size} bytes; the widget limit is {widget_views.MAX_PHOTO_BYTES}", returncode=2)

        self.stdout.write(self.style.WARNING(
            f"expected PAID kie.ai generations: {paid}"
            + (" (1 direct + 1 through /widget/tryon/)" if end_to_end else "")
        ))
        problems = []

        # 1) استدعاء مباشر لـ services/kieai.generate_tryon بنفس برومبت الودجت
        with open(person, "rb") as fh:
            started = time.perf_counter()
            try:
                url = kieai.generate_tryon(
                    api_key=settings.KIEAI_API_KEY, person_photo=fh, person_filename=os.path.basename(person),
                    product_image_url=product_image, prompt=widget_views.TRYON_PROMPT,
                )
            except kieai.TryOnError as exc:
                self.stdout.write(self.style.ERROR(f"direct call FAILED after {time.perf_counter() - started:.1f}s: {exc}"))
                raise CommandError("kie.ai direct generation failed", returncode=1) from exc
        duration = time.perf_counter() - started
        self.stdout.write(f"direct call: ok in {duration:.1f}s")
        self.stdout.write(f"result image: {url}")
        head = check_reachable(url)
        if head["ok"]:
            self.stdout.write(self.style.SUCCESS(
                f"reachable: HTTP {head['status']} {head['content_type'] or '-'} bytes={head['bytes'] or '-'}"))
        else:
            problems.append("result URL not reachable")
            self.stdout.write(self.style.ERROR(f"NOT reachable: {head}"))

        # 2) end-to-end عبر endpoint الودجت الحقيقي
        if end_to_end:
            with open(person, "rb") as fh:
                photo = fh.read()
            results = self.end_to_end(photo, os.path.basename(person), product_image, use_current_db=opts["use_current_db"])
            for name, ok, note in results:
                line = f"e2e {name}: {'ok' if ok else 'FAILED'}" + (f" - {note}" if note else "")
                self.stdout.write(self.style.SUCCESS(line) if ok else self.style.ERROR(line))
                if not ok:
                    problems.append(name)

        if problems:
            raise CommandError("kie.ai live check failed: " + ", ".join(problems), returncode=1)
        self.stdout.write(self.style.SUCCESS("kie.ai live check passed"))

    # ------------------------------------------------------------------
    def end_to_end(self, photo, filename, product_image, use_current_db=False):
        """بيرجّع [(name, ok, note)]. kie.ai حقيقي (بدون mock)، والمحادثة التالية LLM ممثَّل بدون تكلفة."""
        content_type = mimetypes.guess_type(filename)[0] or "image/jpeg"
        results = []
        with eval_environment(use_current_db=use_current_db, fake_tryon=False):
            merchant = v2_scenarios.build_merchant(label="kieai-live-e2e", quota=5)
            product = merchant.products.get(sku=v2_scenarios.FALLBACK_SKU)
            product.image_url = product_image[:500]
            product.save(update_fields=["image_url"])
            widget = Widget(merchant, ip="10.7.0.1")
            widget.session()  # جلسة موقّعة (التجربة بتتطلب X-Session-Token لتاجر v2)
            conv = Conversation.objects.get(merchant=merchant, session_id=widget.storage["session_id"])

            started = time.perf_counter()
            status, data = widget.tryon(product.sku, photo_bytes=photo, filename=filename, content_type=content_type)
            seconds = time.perf_counter() - started
            results.append(("tryon_endpoint", bool(data.get("ok")) and bool(data.get("image_url")),
                            f"HTTP {status} in {seconds:.1f}s image_url={data.get('image_url')}" if data.get("ok")
                            else f"HTTP {status} {data}"))
            if data.get("ok"):
                head = check_reachable(data["image_url"])
                results.append(("endpoint_image_reachable", head["ok"], f"HTTP {head['status']}"))

            merchant.refresh_from_db()
            results.append(("quota_reserved_once_not_refunded", merchant.images_used == 1,
                            f"images_used={merchant.images_used}"))
            reaction = ProductReaction.objects.filter(merchant=merchant, reaction="tried_on", sku=product.sku).count()
            results.append(("reaction_tried_on", reaction == 1, f"count={reaction}"))
            event = Message.objects.filter(conversation=conv, role="event", content__startswith=f"tried_on: {product.sku}").exists()
            results.append(("event_message", event, ""))
            conv.refresh_from_db()
            results.append(("conversation_tryons", conv.tryons == 1, f"tryons={conv.tryons}"))

            prompts = []

            def fake_chat(provider, system_prompt, messages, tools, force_tool=None, **kw):
                prompts.append(system_prompt)
                if force_tool:
                    return llm.LLMResult(reply_text="", tool_calls=[{"name": force_tool, "input": {}}])
                return llm.LLMResult(reply_text="تمام، شكلك حلو فيه، وش رايك نكمل الطلب؟", tool_calls=[])

            def fake_step(provider, system_prompt, transcript, tools, tool_choice=None, **kw):
                prompts.append(system_prompt)
                return llm.LLMStep(text="تمام، شكلك حلو فيه، وش رايك نكمل الطلب؟", tool_calls=[])

            with patch.object(llm, "chat_with_retry", fake_chat), patch.object(llm, "chat_step_with_retry", fake_step), \
                    patch.object(llm, "get_provider", lambda role=None: _FakeProvider()):
                reply, _, _ = widget.say("وش رايك فيه؟")
            mentioned = memory_mentions_tryon(prompts, product.sku)
            if not mentioned and reply.get("ok") and not prompts:
                # محرّك ما استدعى الـ LLM الممثَّل: نفحص قسم الذاكرة مباشرة
                ctx = memory.session_from_token(merchant, widget.storage["session_token"])
                section = memory.build_memory_section(ctx, memory.compile_constraints(ctx, merchant.currency), merchant.currency)
                mentioned = memory_mentions_tryon([section], product.sku)
            results.append(("next_turn_memory_mentions_tryon", mentioned, f"prompts_seen={len(prompts)}"))
        return results
