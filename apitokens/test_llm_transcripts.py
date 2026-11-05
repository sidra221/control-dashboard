"""اختبارات (بدون شبكة) لطبقة الأدوات الجديدة بـ llm.py: تحويل التاريخ الموحّد
لشكل كل مزوّد، وبناء طلب chat_step وقراءة الرد. الاختبار الحي الفعلي هو
`manage.py llm_spike` (نتائجه بتقرير المرحلة)."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from .services import llm

TOOLS = [{"name": "get_price", "description": "d", "parameters": {"type": "object", "properties": {}}}]


def transcript_with_two_calls(raw=None):
    step = llm.LLMStep(
        text="", raw=raw,
        tool_calls=[llm.ToolCall("c1", "get_price", {"sku": "A"}), llm.ToolCall("c2", "get_price", {"sku": "B"})],
    )
    t = [{"role": "user", "content": "prices?"}, llm.assistant_entry(step)]
    for call, price in zip(step.tool_calls, ("10", "20")):
        t.append(llm.tool_entry(call, json.dumps({"price": price})))
    return t


class ConverterTests(SimpleTestCase):
    def test_anthropic_batches_parallel_tool_results_into_one_user_message(self):
        msgs = llm.anthropic_messages(transcript_with_two_calls() + [{"role": "user", "content": "thanks"}])
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user", "user"])
        self.assertEqual([b["type"] for b in msgs[1]["content"]], ["tool_use", "tool_use"])
        results = msgs[2]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], ["c1", "c2"])
        self.assertEqual(json.loads(results[1]["content"]), {"price": "20"})
        self.assertEqual(msgs[3]["content"], "thanks")

    def test_anthropic_skips_empty_assistant_entries(self):
        t = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "", "tool_calls": []},
             {"role": "user", "content": "hello?"}]
        self.assertEqual([m["role"] for m in llm.anthropic_messages(t)], ["user", "user"])

    def test_openai_chat_shape(self):
        msgs = llm.openai_chat_messages("SYS", transcript_with_two_calls())
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "tool", "tool"])
        self.assertIsNone(msgs[2]["content"])
        call = msgs[2]["tool_calls"][0]
        self.assertEqual(call["type"], "function")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"sku": "A"})
        self.assertEqual(msgs[3]["tool_call_id"], "c1")

    def test_responses_replays_raw_items_verbatim_and_falls_back_without_them(self):
        raw = [{"type": "reasoning", "id": "rs_1", "encrypted_content": "abc", "summary": []},
               {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "get_price", "arguments": "{}"}]
        with_raw = llm.responses_input(transcript_with_two_calls(raw=raw))
        self.assertEqual(with_raw[1:3], raw)
        self.assertEqual([i.get("type") for i in with_raw[3:]], ["function_call_output"] * 2)
        without = llm.responses_input(transcript_with_two_calls())
        self.assertEqual([i.get("type") for i in without], [None, "function_call", "function_call",
                                                            "function_call_output", "function_call_output"])
        self.assertEqual(without[1]["call_id"], "c1")

    def test_gemini_contents_batch_function_responses_and_reuse_raw_content(self):
        from google.genai import types

        raw = types.Content(role="model", parts=[types.Part(text="signed")])
        contents = llm.gemini_contents(transcript_with_two_calls(raw=raw), types)
        self.assertEqual([c.role for c in contents], ["user", "model", "user"])
        self.assertIs(contents[1], raw)
        self.assertEqual(len(contents[2].parts), 2)
        self.assertEqual(contents[2].parts[0].function_response.response, {"price": "10"})
        # بدون raw: بنبني function_call parts
        built = llm.gemini_contents(transcript_with_two_calls(), types)
        self.assertEqual([p.function_call.name for p in built[1].parts], ["get_price", "get_price"])

    def test_non_dict_tool_results_are_wrapped_for_gemini(self):
        self.assertEqual(llm._tool_result_object("[1,2]"), {"output": [1, 2]})
        self.assertEqual(llm._tool_result_object("plain text"), {"output": "plain text"})


class Dumpable(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return {k: v for k, v in vars(self).items() if not (exclude_none and v is None)}


class ChatStepRequestTests(SimpleTestCase):
    def test_anthropic_request_and_parsing(self):
        captured = {}

        class FakeMessages:
            def create(self, **kw):
                captured.update(kw)
                return SimpleNamespace(content=[
                    SimpleNamespace(type="text", text="checking"),
                    SimpleNamespace(type="tool_use", id="tu1", name="get_price", input={"sku": "A"}),
                ])

        fake_module = SimpleNamespace(
            Anthropic=lambda **kw: SimpleNamespace(messages=FakeMessages()), APIError=Exception
        )
        provider = llm.AnthropicProvider("k", "m")
        with patch.dict("sys.modules", {"anthropic": fake_module}):
            step = provider.chat_step("SYS", [{"role": "user", "content": "hi"}], TOOLS, tool_choice="none")
            self.assertEqual(captured["tool_choice"], {"type": "none"})
            provider.chat_step("SYS", [{"role": "user", "content": "hi"}], TOOLS, tool_choice={"name": "get_price"})
            self.assertEqual(captured["tool_choice"], {"type": "tool", "name": "get_price"})
            provider.chat_step("SYS", [{"role": "user", "content": "hi"}], TOOLS)
            self.assertEqual(captured["tool_choice"], {"type": "auto"})
        self.assertEqual(step.text, "checking")
        self.assertEqual(step.tool_calls, [llm.ToolCall("tu1", "get_price", {"sku": "A"})])
        self.assertEqual(captured["tools"][0]["input_schema"], TOOLS[0]["parameters"])

    def test_openai_responses_request_is_stateless_and_carries_reasoning(self):
        captured = {}
        output = [
            Dumpable(type="reasoning", id="rs_1", encrypted_content="enc"),
            Dumpable(type="function_call", id="fc_1", call_id="call_9", name="get_price", arguments='{"sku": "A"}'),
        ]

        class FakeResponses:
            def create(self, **kw):
                captured.update(kw)
                return SimpleNamespace(output=output, output_text="")

        fake_module = SimpleNamespace(
            OpenAI=lambda **kw: SimpleNamespace(responses=FakeResponses()), APIError=Exception
        )
        provider = llm.OpenAIProvider("k", "m", reasoning_effort="low")
        with patch.dict("sys.modules", {"openai": fake_module}):
            step = provider.chat_step("SYS", [{"role": "user", "content": "hi"}], TOOLS, tool_choice="none")
        self.assertFalse(captured["store"])
        self.assertEqual(captured["include"], ["reasoning.encrypted_content"])
        self.assertEqual(captured["tool_choice"], "none")
        self.assertEqual(captured["reasoning"], {"effort": "low"})
        self.assertEqual(step.tool_calls, [llm.ToolCall("call_9", "get_price", {"sku": "A"})])
        self.assertEqual([i["type"] for i in step.raw], ["reasoning", "function_call"])
        # والـ raw بيرجع كما هو بالخطوة الجاية
        replay = llm.responses_input([{"role": "user", "content": "x"}, llm.assistant_entry(step)])
        self.assertEqual(replay[1:], step.raw)

    def test_openai_compatible_chat_completions_path(self):
        captured = {}
        message = SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(id="t1", function=SimpleNamespace(name="get_price", arguments='{"sku":"B"}'))],
        )

        class FakeCompletions:
            def create(self, **kw):
                captured.update(kw)
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        fake_module = SimpleNamespace(
            OpenAI=lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())),
            APIError=Exception,
        )
        provider = llm.OpenAIProvider("k", "m", base_url="https://api.groq.com/openai/v1")
        with patch.dict("sys.modules", {"openai": fake_module}):
            step = provider.chat_step("SYS", [{"role": "user", "content": "hi"}], TOOLS,
                                      tool_choice={"name": "get_price"})
        self.assertEqual(captured["tool_choice"], {"type": "function", "function": {"name": "get_price"}})
        self.assertEqual(step.tool_calls, [llm.ToolCall("t1", "get_price", {"sku": "B"})])

    def test_gemini_tool_config_modes_and_synthetic_call_ids(self):
        from google.genai import types

        captured = {}

        class FakeModels:
            def generate_content(self, **kw):
                captured.update(kw)
                content = types.Content(role="model", parts=[types.Part(text="x")])
                return SimpleNamespace(
                    function_calls=[SimpleNamespace(name="get_price", args={"sku": "A"}, id=None)],
                    text="", candidates=[SimpleNamespace(content=content)],
                )

        fake_genai = SimpleNamespace(Client=lambda **kw: SimpleNamespace(models=FakeModels()))
        provider = llm.GeminiProvider("k", "m")
        with patch("google.genai.Client", fake_genai.Client):
            step = provider.chat_step("SYS", [{"role": "user", "content": "hi"}], TOOLS, tool_choice="none")
            mode = captured["config"].tool_config.function_calling_config.mode
            self.assertEqual(str(mode).split(".")[-1], "NONE")
            provider.chat_step("SYS", [{"role": "user", "content": "hi"}], TOOLS, tool_choice={"name": "get_price"})
            cfg = captured["config"].tool_config.function_calling_config
            self.assertEqual(str(cfg.mode).split(".")[-1], "ANY")
            self.assertEqual(list(cfg.allowed_function_names), ["get_price"])
        self.assertEqual(step.tool_calls, [llm.ToolCall("call_0", "get_price", {"sku": "A"})])
        self.assertIsNotNone(step.raw)


class StepRetryTests(SimpleTestCase):
    def test_retries_once_then_succeeds(self):
        calls = {"n": 0}

        class Flaky:
            def chat_step(self, *a, **k):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise llm.LLMError("overloaded")
                return llm.LLMStep(text="ok")

        with patch("apitokens.services.llm.time.sleep"):
            step = llm.chat_step_with_retry(Flaky(), "s", [], [])
        self.assertEqual((step.text, calls["n"]), ("ok", 2))

    def test_raises_after_max_attempts(self):
        class Down:
            def chat_step(self, *a, **k):
                raise llm.LLMError("down")

        with patch("apitokens.services.llm.time.sleep"), self.assertRaises(llm.LLMError):
            llm.chat_step_with_retry(Down(), "s", [], [])
