"""طبقة تجريد فوق مزوّد الذكاء الاصطناعي — حتى نقدر نبدّل Anthropic/OpenAI/
Gemini بتغيير إعداد وحيد (LLM_PROVIDER) بدون لمس أي كود تاني. كل مزوّد
بيطبّق نفس الواجهة: chat(system_prompt, messages, tool) -> LLMResult.

`tool` شكله موحّد بغض النظر عن المزوّد:
    {"name": str, "description": str, "parameters": <JSON schema dict>}
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

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
    tool_name: Optional[str] = None
    tool_input: Optional[dict] = None


def chat_with_retry(provider, system_prompt, messages, tool, max_attempts=2, backoff_seconds=1):
    """مزوّدات الذكاء الاصطناعي (خصوصاً الطبقة المجانية) بترجع أخطاء مؤقتة
    (503/overloaded) بشكل شائع — منعيد المحاولة قبل ما نرجّع خطأ حقيقي
    للزبون. هاد مشترك لكل المزوّدات بدل ما يتكرر بكل وحدة لحالها.

    attempt واحد إضافي بس (مش 3): لما الموديل يكون "overloaded" فعلياً عادةً
    بتضل هيك لثواني/دقايق، فثالث محاولة غالباً بتفشل كمان وبس بتزود وقت
    الانتظار قبل ما الزبون ياخد أي رد.
    """
    last_error = None
    for attempt in range(max_attempts):
        try:
            return provider.chat(system_prompt, messages, tool)
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
        return OpenAIProvider(settings.OPENAI_API_KEY, settings.OPENAI_MODEL)
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

    def chat(self, system_prompt, messages, tool):
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key, timeout=REQUEST_TIMEOUT_SECONDS)
        anthropic_tool = {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": tool["parameters"],
        }
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=500,
                system=system_prompt,
                messages=messages,
                tools=[anthropic_tool],
            )
        except anthropic.APIError as exc:
            raise LLMError(str(exc)) from exc

        text_parts = []
        tool_name = None
        tool_input = None
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_name = block.name
                tool_input = block.input
        return LLMResult(
            reply_text=" ".join(text_parts).strip(),
            tool_name=tool_name,
            tool_input=tool_input,
        )


class OpenAIProvider:
    """بتخدم OpenAI، وكمان أي مزوّد متوافق مع OpenAI API (مثل Groq) عبر
    تمرير base_url — الـ endpoint نفسه بالضبط، بس سيرفر مختلف."""

    def __init__(self, api_key, model, base_url=None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    def is_configured(self):
        return bool(self.api_key)

    def chat(self, system_prompt, messages, tool):
        import openai

        client = openai.OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        openai_messages = [{"role": "system", "content": system_prompt}] + list(messages)
        openai_tool = {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=openai_messages,
                tools=[openai_tool],
                max_tokens=500,
            )
        except openai.APIError as exc:
            raise LLMError(str(exc)) from exc

        choice = response.choices[0].message
        tool_name = None
        tool_input = None
        if choice.tool_calls:
            call = choice.tool_calls[0]
            tool_name = call.function.name
            try:
                tool_input = json.loads(call.function.arguments)
            except json.JSONDecodeError:
                tool_input = {}
        return LLMResult(
            reply_text=(choice.content or "").strip(),
            tool_name=tool_name,
            tool_input=tool_input,
        )


class GeminiProvider:
    def __init__(self, api_key, model):
        self.api_key = api_key
        self.model = model

    def is_configured(self):
        return bool(self.api_key)

    def chat(self, system_prompt, messages, tool):
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
                    name=tool["name"],
                    description=tool["description"],
                    parameters_json_schema=tool["parameters"],
                )
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

        try:
            response = client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=[gemini_tool],
                    max_output_tokens=500,
                ),
            )
        except Exception as exc:  # SDK Google بيرمي أنواع استثناءات متعددة
            raise LLMError(str(exc)) from exc

        tool_name = None
        tool_input = None
        calls = response.function_calls
        if calls:
            tool_name = calls[0].name
            tool_input = dict(calls[0].args) if calls[0].args else {}

        try:
            reply_text = (response.text or "").strip()
        except Exception:
            reply_text = ""

        return LLMResult(
            reply_text=reply_text,
            tool_name=tool_name,
            tool_input=tool_input,
        )
