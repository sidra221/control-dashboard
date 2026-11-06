"""إشارات المحلّل (intent / readiness / hesitation_reason / asks_discount / wants_alternative):
تعقيم بالكود، شكل ثابت دايماً، وتوافق رجعي لو الموديل ما رجّعها."""

from django.test import SimpleTestCase

from .services import understanding

KEYS = {"intent", "readiness", "hesitation_reason", "asks_discount", "wants_alternative"}
DEFAULTS = {"intent": "other", "readiness": "", "hesitation_reason": "", "asks_discount": False, "wants_alternative": False}


def validate(raw):
    return understanding.validate(raw, user_texts=["ميزانيتي 200 ريال"], products_by_sku={"A": object()},
                                  merchant_currency="SAR")


class SignalsToolSchemaTests(SimpleTestCase):
    def setUp(self):
        self.schema = understanding.ANALYZE_TOOL["parameters"]["properties"]["signals"]
        self.props = self.schema["properties"]

    def test_schema_has_the_five_signals_with_the_right_enums(self):
        self.assertEqual(set(self.props), KEYS)
        self.assertEqual(
            self.props["intent"]["enum"],
            ["browse", "search", "ask_info", "compare", "object", "reject", "accept", "buy", "ask_discount", "chitchat", "other"],
        )
        self.assertEqual(self.props["readiness"]["enum"], ["browsing", "interested", "hesitant", "ready"])
        self.assertEqual(self.props["hesitation_reason"]["enum"], ["price", "style", "fit", "trust", "timing", "other", ""])
        self.assertEqual(self.props["asks_discount"]["type"], "boolean")
        self.assertEqual(self.props["wants_alternative"]["type"], "boolean")

    def test_signals_are_optional_in_the_schema(self):
        params = understanding.ANALYZE_TOOL["parameters"]
        self.assertNotIn("signals", params.get("required", []))
        self.assertNotIn("required", self.schema)

    def test_prompt_explains_readiness_and_dialect_coverage(self):
        text = understanding.ANALYZER_SYSTEM
        for needle in ('"ready" ONLY when', '"hesitant"', '"interested"', '"browsing"', "Arabizi", "Saudi", "Egyptian", "MEANING"):
            self.assertIn(needle, text)
        self.assertIn('source="inferred" and polarity="like"', text)
        self.assertIn("Never infer a dislike", text)


class SignalsValidationTests(SimpleTestCase):
    def test_absent_signals_give_the_defaults(self):
        for raw in ({}, {"signals": None}, {"signals": "ready"}, {"signals": []}, None):
            self.assertEqual(validate(raw).signals, DEFAULTS, raw)

    def test_signals_dict_always_has_exactly_the_five_keys(self):
        self.assertEqual(set(validate({"signals": {"intent": "buy"}}).signals), KEYS)

    def test_valid_values_pass_through(self):
        out = validate({"signals": {"intent": "ask_discount", "readiness": "hesitant", "hesitation_reason": "price",
                                    "asks_discount": True, "wants_alternative": True}})
        self.assertEqual(out.signals, {"intent": "ask_discount", "readiness": "hesitant", "hesitation_reason": "price",
                                       "asks_discount": True, "wants_alternative": True})

    def test_every_enum_value_is_accepted(self):
        for intent in understanding.INTENTS:
            self.assertEqual(validate({"signals": {"intent": intent}}).signals["intent"], intent)
        for readiness in ("browsing", "interested", "hesitant", "ready"):
            self.assertEqual(validate({"signals": {"readiness": readiness}}).signals["readiness"], readiness)
        for reason in ("price", "style", "fit", "trust", "timing", "other", ""):
            self.assertEqual(validate({"signals": {"hesitation_reason": reason}}).signals["hesitation_reason"], reason)

    def test_invalid_values_are_dropped_to_defaults(self):
        out = validate({"signals": {"intent": "teleport", "readiness": "super_ready", "hesitation_reason": "mood",
                                    "asks_discount": "maybe", "wants_alternative": 7}})
        self.assertEqual(out.signals, DEFAULTS)
        self.assertIn("signals:bad_intent", out.dropped)
        self.assertIn("signals:bad_readiness", out.dropped)
        self.assertIn("signals:bad_hesitation_reason", out.dropped)

    def test_one_bad_field_does_not_poison_the_others(self):
        out = validate({"signals": {"intent": "nope", "readiness": "ready", "asks_discount": True}})
        self.assertEqual((out.signals["intent"], out.signals["readiness"], out.signals["asks_discount"]),
                         ("other", "ready", True))

    def test_non_string_enum_values_do_not_crash(self):
        out = validate({"signals": {"intent": ["buy"], "readiness": 3, "hesitation_reason": {"a": 1}}})
        self.assertEqual(out.signals, DEFAULTS)

    def test_case_and_whitespace_are_normalized(self):
        out = validate({"signals": {"intent": " BUY ", "readiness": "Ready"}})
        self.assertEqual((out.signals["intent"], out.signals["readiness"]), ("buy", "ready"))

    def test_boolean_strings_from_a_sloppy_model(self):
        out = validate({"signals": {"asks_discount": "true", "wants_alternative": "false"}})
        self.assertEqual((out.signals["asks_discount"], out.signals["wants_alternative"]), (True, False))

    def test_extra_keys_are_ignored(self):
        out = validate({"signals": {"intent": "buy", "discount_percent": 50, "code": "SAVE"}})
        self.assertEqual(set(out.signals), KEYS)

    def test_signals_do_not_share_state_between_instances(self):
        a, b = validate({}), validate({})
        a.signals["intent"] = "buy"
        self.assertEqual(b.signals["intent"], "other")

    def test_signals_do_not_touch_the_rest_of_the_validation(self):
        out = validate({
            "preferences": [{"key": "budget_max", "value": "200", "source": "explicit", "evidence": "ميزانيتي 200"}],
            "signals": {"intent": "search"},
        })
        self.assertEqual(out.preferences[0]["value"], "200")
        self.assertEqual(out.signals["intent"], "search")


class SignalsSummaryTests(SimpleTestCase):
    def test_summary_includes_signals(self):
        out = validate({"signals": {"intent": "buy", "readiness": "ready"}})
        summary = out.summary()
        self.assertEqual(summary["signals"]["intent"], "buy")
        self.assertEqual(summary["signals"]["readiness"], "ready")
        self.assertEqual(set(summary["signals"]), KEYS)

    def test_summary_signals_is_a_copy(self):
        out = validate({})
        out.summary()["signals"]["intent"] = "buy"
        self.assertEqual(out.signals["intent"], "other")

    def test_summary_backward_compatible_keys_remain(self):
        summary = validate({}).summary()
        for key in ("preferences", "retractions", "reactions", "objections", "resolved", "topics", "gender", "pending", "dropped"):
            self.assertIn(key, summary)

    def test_default_validated_has_defaults(self):
        self.assertEqual(understanding.Validated().signals, DEFAULTS)
