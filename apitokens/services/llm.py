"""طبقة تجريد فوق مزوّد الذكاء الاصطناعي — حتى نقدر نبدّل Anthropic/OpenAI/
Gemini بتغيير إعداد وحيد (LLM_PROVIDER) بدون لمس أي كود تاني. كل مزوّد
بيطبّق نفس الواجهة: chat(system_prompt, messages, tool) -> LLMResult.

`tool` شكله موحّد بغض النظر عن المزوّد:
    {"name": str, "description": str, "parameters": <JSON schema dict>}
"""

import json
from dataclasses import dataclass
from typing import Optional

from django.conf import settings


class LLMError(Exception):
    pass


@dataclass
class LLMResult:
    reply_text: str
    tool_name: Optional[str] = None
    tool_input: Optional[dict] = None


def get_provider():
    provider = (settings.LLM_PROVIDER or "anthropic").strip().lower()
    if provider == "anthropic":
        return AnthropicProvider(settings.ANTHROPIC_API_KEY, settings.ANTHROPIC_MODEL)
    if provider == "openai":
        return OpenAIProvider(settings.OPENAI_API_KEY, settings.OPENAI_MODEL)
    if provider == "gemini":
        return GeminiProvider(settings.GEMINI_API_KEY, settings.GEMINI_MODEL)
    raise LLMError(f"unknown_llm_provider:{provider}")


class AnthropicProvider:
    def __init__(self, api_key, model):
        self.api_key = api_key
        self.model = model

    def is_configured(self):
        return bool(self.api_key)

    def chat(self, system_prompt, messages, tool):
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
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
    def __init__(self, api_key, model):
        self.api_key = api_key
        self.model = model

    def is_configured(self):
        return bool(self.api_key)

    def chat(self, system_prompt, messages, tool):
        import openai

        client = openai.OpenAI(api_key=self.api_key)
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
        import google.generativeai as genai

        genai.configure(api_key=self.api_key)

        gemini_tool = genai.protos.Tool(
            function_declarations=[
                genai.protos.FunctionDeclaration(
                    name=tool["name"],
                    description=tool["description"],
                    parameters=tool["parameters"],
                )
            ]
        )
        model = genai.GenerativeModel(
            model_name=self.model,
            system_instruction=system_prompt,
            tools=[gemini_tool],
        )
        # Gemini بيستخدم role="model" مش "assistant"
        history = [
            {"role": "user" if m["role"] == "user" else "model", "parts": [m["content"]]}
            for m in messages[:-1]
        ]
        try:
            chat_session = model.start_chat(history=history)
            response = chat_session.send_message(messages[-1]["content"])
        except Exception as exc:  # SDK Google بيرمي أنواع استثناءات متعددة
            raise LLMError(str(exc)) from exc

        text_parts = []
        tool_name = None
        tool_input = None
        for part in response.candidates[0].content.parts:
            if getattr(part, "text", None):
                text_parts.append(part.text)
            fc = getattr(part, "function_call", None)
            if fc and fc.name:
                tool_name = fc.name
                tool_input = dict(fc.args)
        return LLMResult(
            reply_text=" ".join(text_parts).strip(),
            tool_name=tool_name,
            tool_input=tool_input,
        )
