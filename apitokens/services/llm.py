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


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMStep:
    """خطوة وحدة من حلقة الأدوات. `raw` = عناصر المزوّد الأصلية (reasoning items
    عند OpenAI، Content بتواقيع التفكير عند Gemini) — لازم تنرجع حرفياً بالخطوة
    الجاية وإلا المزوّد بيرفض أو بيفقد سياق التفكير."""

    text: str
    tool_calls: list = field(default_factory=list)
    raw: object = None


# تاريخ بشكل موحّد بغض النظر عن المزوّد (chat_step):
#   {"role": "user", "content": str}
#   {"role": "assistant", "content": str, "tool_calls": [{"id","name","input"}], "raw": <LLMStep.raw>}
#   {"role": "tool", "tool_call_id": str, "name": str, "content": str}  # نتيجة الأداة (JSON نص)
# tool_choice: None/"auto" | "none" | {"name": "<tool>"} (إجبار أداة)


def assistant_entry(step):
    return {
        "role": "assistant",
        "content": step.text,
        "tool_calls": [{"id": c.id, "name": c.name, "input": c.input} for c in step.tool_calls],
        "raw": step.raw,
    }


def tool_entry(call, content):
    return {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content}


def _tool_result_object(content):
    """Gemini بدو dict بنتيجة الأداة."""
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return {"output": content}
    return value if isinstance(value, dict) else {"output": value}


def anthropic_messages(transcript):
    out, pending = [], []

    def flush():
        if pending:
            out.append({"role": "user", "content": list(pending)})
            pending.clear()

    for entry in transcript:
        role = entry["role"]
        if role == "tool":
            pending.append(
                {"type": "tool_result", "tool_use_id": entry["tool_call_id"], "content": entry["content"]}
            )
            continue
        flush()
        if role == "user":
            out.append({"role": "user", "content": entry["content"]})
        else:
            blocks = []
            if entry.get("content"):
                blocks.append({"type": "text", "text": entry["content"]})
            for call in entry.get("tool_calls") or []:
                blocks.append(
                    {"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["input"]}
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})
    flush()
    return out


def openai_chat_messages(system_prompt, transcript):
    out = [{"role": "system", "content": system_prompt}]
    for entry in transcript:
        role = entry["role"]
        if role == "user":
            out.append({"role": "user", "content": entry["content"]})
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": entry["tool_call_id"], "content": entry["content"]})
        else:
            message = {"role": "assistant", "content": entry.get("content") or None}
            calls = entry.get("tool_calls") or []
            if calls:
                message["tool_calls"] = [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c["input"], ensure_ascii=False)}}
                    for c in calls
                ]
            out.append(message)
    return out


def responses_input(transcript):
    items = []
    for entry in transcript:
        role = entry["role"]
        if role == "user":
            items.append({"role": "user", "content": entry["content"]})
        elif role == "tool":
            items.append(
                {"type": "function_call_output", "call_id": entry["tool_call_id"], "output": entry["content"]}
            )
        elif entry.get("raw"):
            items.extend(entry["raw"])  # عناصر الخرج الأصلية (فيها reasoning المشفّر)
        else:
            if entry.get("content"):
                items.append({"role": "assistant", "content": entry["content"]})
            for call in entry.get("tool_calls") or []:
                items.append(
                    {"type": "function_call", "call_id": call["id"], "name": call["name"],
                     "arguments": json.dumps(call["input"], ensure_ascii=False)}
                )
    return items


def gemini_contents(transcript, types):
    contents, pending = [], []

    def flush():
        if pending:
            contents.append(types.Content(role="user", parts=list(pending)))
            pending.clear()

    for entry in transcript:
        role = entry["role"]
        if role == "tool":
            pending.append(
                types.Part.from_function_response(
                    name=entry["name"], response=_tool_result_object(entry["content"])
                )
            )
            continue
        flush()
        if role == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=entry["content"])]))
        elif entry.get("raw") is not None:
            contents.append(entry["raw"])  # Content الأصلي بتواقيع التفكير
        else:
            parts = []
            if entry.get("content"):
                parts.append(types.Part(text=entry["content"]))
            for call in entry.get("tool_calls") or []:
                parts.append(
                    types.Part(function_call=types.FunctionCall(name=call["name"], args=call["input"]))
                )
            if parts:
                contents.append(types.Content(role="model", parts=parts))
    flush()
    return contents


def chat_step_with_retry(provider, system_prompt, transcript, tools, tool_choice=None,
                         max_attempts=2, backoff_seconds=1):
    last_error = None
    for attempt in range(max_attempts):
        try:
            return provider.chat_step(system_prompt, transcript, tools, tool_choice=tool_choice)
        except LLMError as exc:
            last_error = exc
            logger.warning(
                "llm_step_failed provider=%s attempt=%s/%s error=%s",
                type(provider).__name__, attempt + 1, max_attempts, exc,
            )
            if attempt < max_attempts - 1:
                time.sleep(backoff_seconds * (attempt + 1))
    raise last_error


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


def _loads(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


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


    def chat_step(self, system_prompt, transcript, tools, tool_choice=None):
        import anthropic

        client = anthropic.Anthropic(
            api_key=self.api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0
        )
        anthropic_tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in tools
        ]
        if tool_choice == "none":
            choice = {"type": "none"}
        elif isinstance(tool_choice, dict):
            choice = {"type": "tool", "name": tool_choice["name"]}
        else:
            choice = {"type": "auto"}
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=1000,
                system=system_prompt,
                messages=anthropic_messages(transcript),
                tools=anthropic_tools,
                tool_choice=choice,
            )
        except anthropic.APIError as exc:
            raise LLMError(str(exc)) from exc
        text = " ".join(b.text for b in response.content if b.type == "text").strip()
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in response.content if b.type == "tool_use"]
        return LLMStep(text=text, tool_calls=calls, raw=None)


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

    def chat_step(self, system_prompt, transcript, tools, tool_choice=None):
        import openai

        client = openai.OpenAI(
            api_key=self.api_key, base_url=self.base_url,
            timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0,
        )
        if tool_choice == "none":
            choice = "none"
        elif isinstance(tool_choice, dict):
            choice = (
                {"type": "function", "name": tool_choice["name"]} if self.base_url is None
                else {"type": "function", "function": {"name": tool_choice["name"]}}
            )
        else:
            choice = "auto"
        try:
            if self.base_url is None:
                extra = {"reasoning": {"effort": self.reasoning_effort}} if self.reasoning_effort else {}
                response = client.responses.create(
                    model=self.model,
                    instructions=system_prompt,
                    input=responses_input(transcript),
                    tools=[
                        {"type": "function", "name": t["name"], "description": t["description"],
                         "parameters": t["parameters"], "strict": False}
                        for t in tools
                    ],
                    tool_choice=choice,
                    max_output_tokens=4000,
                    store=False,
                    # بدون store، تفكير الموديل بين جولات الأدوات ما بينحفظ إلا مشفّراً بالرد.
                    include=["reasoning.encrypted_content"],
                    **extra,
                )
            else:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=openai_chat_messages(system_prompt, transcript),
                    tools=[
                        {"type": "function", "function": {
                            "name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
                        for t in tools
                    ],
                    tool_choice=choice,
                    max_completion_tokens=2000,
                )
        except openai.APIError as exc:
            raise LLMError(str(exc)) from exc

        if self.base_url is None:
            calls = []
            for item in response.output:
                if item.type == "function_call":
                    calls.append(ToolCall(item.call_id, item.name, _loads(item.arguments)))
            raw = [item.model_dump(exclude_none=True) for item in response.output]
            return LLMStep(text=(response.output_text or "").strip(), tool_calls=calls, raw=raw)
        message = response.choices[0].message
        calls = [
            ToolCall(c.id, c.function.name, _loads(c.function.arguments)) for c in (message.tool_calls or [])
        ]
        return LLMStep(text=(message.content or "").strip(), tool_calls=calls, raw=None)

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

    def chat_step(self, system_prompt, transcript, tools, tool_choice=None):
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=self.api_key,
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_SECONDS * 1000),
        )
        gemini_tool = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=t["name"], description=t["description"], parameters_json_schema=t["parameters"],
                )
                for t in tools
            ]
        )
        if tool_choice == "none":
            mode, names = "NONE", None
        elif isinstance(tool_choice, dict):
            mode, names = "ANY", [tool_choice["name"]]
        else:
            mode, names = "AUTO", None
        tool_config = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode=mode, allowed_function_names=names)
        )
        try:
            response = client.models.generate_content(
                model=self.model,
                contents=gemini_contents(transcript, types),
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=[gemini_tool],
                    tool_config=tool_config,
                    max_output_tokens=2000,
                ),
            )
        except Exception as exc:  # SDK Google بيرمي أنواع استثناءات متعددة
            raise LLMError(str(exc)) from exc

        calls = [
            ToolCall(getattr(c, "id", None) or f"call_{i}", c.name, dict(c.args) if c.args else {})
            for i, c in enumerate(response.function_calls or [])
        ]
        try:
            text = (response.text or "").strip()
        except Exception:
            text = ""
        content = response.candidates[0].content if response.candidates else None
        return LLMStep(text=text, tool_calls=calls, raw=content)
