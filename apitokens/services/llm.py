"""طبقة تجريد فوق مزوّد الذكاء الاصطناعي — حتى نقدر نبدّل Anthropic/OpenAI/
Gemini بتغيير إعداد وحيد (LLM_PROVIDER) بدون لمس أي كود تاني. كل مزوّد
بيطبّق نفس الواجهة: chat(system_prompt, messages, tools) -> LLMResult.

`tools` قائمة بشكل موحّد بغض النظر عن المزوّد، كل عنصر:
    {"name": str, "description": str, "parameters": <JSON schema dict>}

الموديل ممكن يستدعي أكتر من أداة بنفس الرد (مثلاً recommend_product و
offer_discount_code مع بعض) — LLMResult.tool_calls قائمة بكل الاستدعاءات.
"""

import json
import logging
import time
from dataclasses import dataclass, field

from django.conf import settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    pass


# بدون timeout صريح، أي SDK ممكن يعلّق الـ worker بدون نهاية على شبكة
# بطيئة/معلّقة — صار فينا هيك فعلياً مع Gemini (علقت 9+ دقايق بلا استثناء).
# 45 ثانية لأنه كتالوج كبير (30+ منتج) مع الطبقة المجانية بياخد وقت أطول
# من المعتاد — جرّبنا 20 وكانت قصيرة، طلعت 504 DEADLINE_EXCEEDED بالضبط
# عند الثانية 20.
REQUEST_TIMEOUT_SECONDS = 45


@dataclass
class LLMResult:
    reply_text: str
    tool_calls: list = field(default_factory=list)  # [{"name": str, "input": dict}, ...]


def chat_with_retry(
    provider, system_prompt, messages, tools, force_tool=None, max_attempts=2, backoff_seconds=1
):
    """مزوّدات الذكاء الاصطناعي (خصوصاً الطبقة المجانية) بترجع أخطاء مؤقتة
    (503/overloaded) بشكل شائع — منعيد المحاولة قبل ما نرجّع خطأ حقيقي
    للزبون. هاد مشترك لكل المزوّدات بدل ما يتكرر بكل وحدة لحالها.

    attempt واحد إضافي بس (مش 3): لما الموديل يكون "overloaded" فعلياً عادةً
    بتضل هيك لثواني/دقايق، فثالث محاولة غالباً بتفشل كمان وبس بتزود وقت
    الانتظار قبل ما الزبون ياخد أي رد.

    force_tool: اسم أداة لازم الموديل يستدعيها هالمرة (بدل ما يترك له
    الخيار) — مستخدم لما نكتشف إنه رد بنص عادي بدل ما يستخدم الأداة
    (صار فعلياً مع OpenAI رغم تعليمات واضحة بالبرومبت).
    """
    last_error = None
    for attempt in range(max_attempts):
        try:
            return provider.chat(system_prompt, messages, tools, force_tool=force_tool)
        except LLMError as exc:
            last_error = exc
            logger.warning(
                "llm_attempt_failed provider=%s attempt=%s/%s error=%s",
                type(provider).__name__, attempt + 1, max_attempts, exc,
            )
            if attempt < max_attempts - 1:
                time.sleep(backoff_seconds * (attempt + 1))
    logger.error("llm_all_attempts_failed provider=%s error=%s", type(provider).__name__, last_error)
    raise last_error


def get_provider():
    provider = (settings.LLM_PROVIDER or "anthropic").strip().lower()
    if provider == "anthropic":
        return AnthropicProvider(settings.ANTHROPIC_API_KEY, settings.ANTHROPIC_MODEL)
    if provider == "openai":
        return OpenAIProvider(
            settings.OPENAI_API_KEY,
            settings.OPENAI_MODEL,
            reasoning_effort=settings.OPENAI_REASONING_EFFORT,
        )
    if provider == "gemini":
        return GeminiProvider(settings.GEMINI_API_KEY, settings.GEMINI_MODEL)
    if provider == "groq":
        return OpenAIProvider(
            settings.GROQ_API_KEY,
            settings.GROQ_MODEL,
            base_url="https://api.groq.com/openai/v1",
        )
    raise LLMError(f"unknown_llm_provider:{provider}")


class AnthropicProvider:
    def __init__(self, api_key, model):
        self.api_key = api_key
        self.model = model

    def is_configured(self):
        return bool(self.api_key)

    def chat(self, system_prompt, messages, tools, force_tool=None):
        import anthropic

        # max_retries=0: عنا retry loop خاص فينا (chat_with_retry) فوق كل
        # المزوّدات بالتساوي — لو تركنا الـ SDK يعيد المحاولة لحاله كمان،
        # ممكن يحترم Retry-After طويل من المزوّد ويعلّق لدقايق بدل ثواني،
        # بعيد كلياً عن أي timeout حاططينه إحنا (صار فعلياً مع OpenAI).
        client = anthropic.Anthropic(
            api_key=self.api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0
        )
        anthropic_tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in tools
        ]
        tool_choice = {"type": "tool", "name": force_tool} if force_tool else None
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=500,
                system=system_prompt,
                messages=messages,
                tools=anthropic_tools,
                **({"tool_choice": tool_choice} if tool_choice else {}),
            )
        except anthropic.APIError as exc:
            raise LLMError(str(exc)) from exc

        text_parts = []
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({"name": block.name, "input": block.input})
        return LLMResult(reply_text=" ".join(text_parts).strip(), tool_calls=tool_calls)


class OpenAIProvider:
    """بتخدم OpenAI، وكمان أي مزوّد متوافق مع OpenAI API (مثل Groq) عبر
    تمرير base_url — الـ endpoint نفسه بالضبط، بس سيرفر مختلف."""

    def __init__(self, api_key, model, base_url=None, reasoning_effort=""):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.reasoning_effort = reasoning_effort

    def is_configured(self):
        return bool(self.api_key)

    def chat(self, system_prompt, messages, tools, force_tool=None):
        import openai

        # max_retries=0: نفس السبب المذكور بـ AnthropicProvider — عنا retry
        # loop خاص فينا (chat_with_retry) وما بدنا الـ SDK يعيد المحاولة لحاله.
        client = openai.OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=0,
        )
        # OpenAI نفسه: Responses API — الوحيدة اللي بتسمح للموديلات الحديثة
        # (gpt-5+/luna) تفكّر قبل ما ترد وتستخدم الأدوات بنفس الوقت. المزوّدات
        # المتوافقة (Groq) ما عندها هالـ API، فبتضل على chat/completions.
        if self.base_url is None:
            return self._chat_responses(client, openai, system_prompt, messages, tools, force_tool)

        openai_messages = [{"role": "system", "content": system_prompt}] + list(messages)
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in tools
        ]
        tool_choice = {"type": "function", "function": {"name": force_tool}} if force_tool else "auto"
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=openai_messages,
                tools=openai_tools,
                tool_choice=tool_choice,
                max_completion_tokens=2000,
            )
        except openai.APIError as exc:
            raise LLMError(str(exc)) from exc

        choice = response.choices[0].message
        tool_calls = []
        for call in choice.tool_calls or []:
            try:
                args = json.loads(call.function.arguments)
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({"name": call.function.name, "input": args})
        return LLMResult(reply_text=(choice.content or "").strip(), tool_calls=tool_calls)

    def _chat_responses(self, client, openai, system_prompt, messages, tools, force_tool):
        response_tools = [
            {
                "type": "function",
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
                "strict": False,
            }
            for t in tools
        ]
        tool_choice = {"type": "function", "name": force_tool} if force_tool else "auto"
        extra = {"reasoning": {"effort": self.reasoning_effort}} if self.reasoning_effort else {}
        try:
            response = client.responses.create(
                model=self.model,
                instructions=system_prompt,
                input=[{"role": m["role"], "content": m["content"]} for m in messages],
                tools=response_tools,
                tool_choice=tool_choice,
                # بيشمل توكنات التفكير الداخلي، لهيك أكبر بكتير من حجم الرد نفسه.
                max_output_tokens=4000,
                store=False,
                **extra,
            )
        except openai.APIError as exc:
            raise LLMError(str(exc)) from exc

        tool_calls = []
        for item in response.output:
            if item.type == "function_call":
                try:
                    args = json.loads(item.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append({"name": item.name, "input": args})
        return LLMResult(reply_text=(response.output_text or "").strip(), tool_calls=tool_calls)


class GeminiProvider:
    def __init__(self, api_key, model):
        self.api_key = api_key
        self.model = model

    def is_configured(self):
        return bool(self.api_key)

    def chat(self, system_prompt, messages, tools, force_tool=None):
        # الباكج القديم google-generativeai صار deprecated رسمياً (ولا
        # تحديثات/إصلاحات جايّة)، فمبنيين على الـ SDK الموحّد الجديد google-genai.
        from google import genai
        from google.genai import types

        # بدون timeout صريح، طلب عالق (مش حتى خطأ) بيعلّق الـ worker كامل
        # بدون نهاية — صار فينا هيك فعلياً (علقت 9+ دقايق بدون أي استثناء).
        client = genai.Client(
            api_key=self.api_key,
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_SECONDS * 1000),
        )

        gemini_tool = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=t["name"],
                    description=t["description"],
                    parameters_json_schema=t["parameters"],
                )
                for t in tools
            ]
        )
        # Gemini بيستخدم role="model" مش "assistant"
        contents = [
            {
                "role": "user" if m["role"] == "user" else "model",
                "parts": [{"text": m["content"]}],
            }
            for m in messages
        ]

        tool_config = None
        if force_tool:
            tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="ANY", allowed_function_names=[force_tool],
                )
            )

        try:
            response = client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=[gemini_tool],
                    tool_config=tool_config,
                    max_output_tokens=500,
                ),
            )
        except Exception as exc:  # SDK Google بيرمي أنواع استثناءات متعددة
            raise LLMError(str(exc)) from exc

        tool_calls = [
            {"name": c.name, "input": dict(c.args) if c.args else {}}
            for c in (response.function_calls or [])
        ]

        try:
            reply_text = (response.text or "").strip()
        except Exception:
            reply_text = ""

        return LLMResult(reply_text=reply_text, tool_calls=tool_calls)
