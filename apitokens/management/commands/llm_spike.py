"""Spike: يتأكد إنه المزوّد المضبوط بيقدر ينفّذ حلقة أدوات حقيقية — الموديل
بينادي أداة، بنرجّعله النتيجة، وبيكمل — ويكمل بدور تاني على نفس التاريخ بدون
ما نخسر سياق التفكير (reasoning items عند OpenAI، توقيعات Gemini).

    python manage.py llm_spike
    python manage.py llm_spike --provider openai --model gpt-4o
    python manage.py llm_spike --provider openai --model gpt-6-luna --effort low

بيعمل بضع طلبات صغيرة (تكلفة رمزية). ما بيلمس أي قاعدة بيانات.
"""

import json
import sys

from django.conf import settings
from django.core.management.base import BaseCommand

from apitokens.services import llm

PRICES = {"SKU-1": "129.00", "SKU-2": "49.00"}

TOOLS = [
    {
        "name": "get_price",
        "description": "Get the current price of a product by sku. Always use it; never guess prices.",
        "parameters": {
            "type": "object",
            "properties": {"sku": {"type": "string", "description": "Exact sku, e.g. SKU-1."}},
            "required": ["sku"],
        },
    }
]

SYSTEM = (
    "You are a shop assistant. Product prices are only available through the get_price tool: "
    "call it for every sku the customer asks about, then answer briefly using the tool results."
)


def execute(call):
    sku = str(call.input.get("sku", ""))
    if sku in PRICES:
        return {"ok": True, "sku": sku, "price": PRICES[sku], "currency": "USD"}
    return {"ok": False, "error": "unknown_sku", "sku": sku}


def run_turn(provider, transcript, user_text, max_steps=3, log=None):
    """حلقة محدودة: ≤ max_steps خطوات، آخر وحدة بلا أدوات. بترجع (نص نهائي، عدد النداءات)."""
    transcript.append({"role": "user", "content": user_text})
    total_calls = 0
    for step_no in range(max_steps):
        last = step_no == max_steps - 1
        step = llm.chat_step_with_retry(
            provider, SYSTEM, transcript, TOOLS, tool_choice="none" if last else None
        )
        transcript.append(llm.assistant_entry(step))
        if log:
            log(f"    step {step_no + 1}: text={step.text[:70]!r} calls={[c.name + str(c.input) for c in step.tool_calls]}")
        if not step.tool_calls:
            return step.text, total_calls
        for call in step.tool_calls:
            total_calls += 1
            transcript.append(llm.tool_entry(call, json.dumps(execute(call))))
    return "", total_calls


class Command(BaseCommand):
    help = "Live spike: 2-round tool transcript, multi-turn replay, tool_choice none/forced"

    def add_arguments(self, parser):
        parser.add_argument("--provider")
        parser.add_argument("--model")
        parser.add_argument("--effort")

    def handle(self, *args, **opts):
        if opts["provider"]:
            settings.LLM_PROVIDER = opts["provider"]
        provider_name = settings.LLM_PROVIDER
        if opts["model"]:
            setattr(settings, {"openai": "OPENAI_MODEL", "anthropic": "ANTHROPIC_MODEL",
                               "gemini": "GEMINI_MODEL", "groq": "GROQ_MODEL"}[provider_name], opts["model"])
        if opts["effort"] is not None:
            settings.OPENAI_REASONING_EFFORT = opts["effort"]
        provider = llm.get_provider()
        if not provider.is_configured():
            self.stdout.write(self.style.ERROR(f"{provider_name}: no API key configured"))
            sys.exit(2)
        self.stdout.write(f"provider={provider_name} model={provider.model}\n")

        results = []

        def check(name, ok, detail=""):
            results.append(ok)
            tag = self.style.SUCCESS("PASS") if ok else self.style.ERROR("FAIL")
            self.stdout.write(f"  [{tag}] {name} {detail}")

        log = self.stdout.write
        transcript = []
        try:
            self.stdout.write("1) tool round-trip, two skus")
            text, calls = run_turn(provider, transcript, "How much are SKU-1 and SKU-2?", log=log)
            check("model called the tool", calls >= 1, f"(calls={calls})")
            check("final text uses the tool results (129 and 49)", "129" in text and "49" in text, repr(text[:90]))

            self.stdout.write("2) second turn on the same transcript (replay of raw items / signatures)")
            text, calls = run_turn(provider, transcript, "Which one is cheaper? Answer in one sentence.", log=log)
            check("second turn succeeded with replayed history", bool(text), repr(text[:90]))
            check("answer is grounded (mentions SKU-2 or 49)", "SKU-2" in text or "49" in text)

            self.stdout.write("3) tool_choice=none must produce text and no tool calls")
            fresh = [{"role": "user", "content": "What does SKU-1 cost? Just answer in words."}]
            step = llm.chat_step_with_retry(provider, SYSTEM, fresh, TOOLS, tool_choice="none")
            check("no tool calls when disabled", not step.tool_calls, f"(calls={len(step.tool_calls)})")

            self.stdout.write("4) forced tool call")
            fresh = [{"role": "user", "content": "hello there"}]
            step = llm.chat_step_with_retry(provider, SYSTEM, fresh, TOOLS, tool_choice={"name": "get_price"})
            check("forced tool is called", any(c.name == "get_price" for c in step.tool_calls),
                  f"(calls={[c.name for c in step.tool_calls]})")

            self.stdout.write("5) tool error result is understood")
            transcript = []
            text, calls = run_turn(provider, transcript, "What is the price of SKU-999?", log=log)
            check("model reports the unknown sku instead of inventing a price",
                  not any(ch.isdigit() for ch in text.replace("999", "")), repr(text[:90]))
        except llm.LLMError as exc:
            check("provider call", False, f"LLMError: {exc}")

        ok = all(results) and results
        self.stdout.write(
            (self.style.SUCCESS if ok else self.style.ERROR)(f"\n{sum(results)}/{len(results)} checks passed")
        )
        if not ok:
            sys.exit(1)
