"""اختبارات القاعدة المبسّطة للخصم، خارج حلقة المحادثة الكاملة:
- دليل الاهتمام الحقيقي (memory.interest_skus / last_shown_sku)
- اعتراض انرفع بهالدور ما بينحل لما الزبون يقول "آخذه" (resolve_open_objections(keep_current=True))
- المدقّق: جملة صادقة "ما في كود/خصم" مسموحة، وأي وعد/رقم/كود مخترع ممنوع
- تعريف التردد بنص المحلّل (التمييز بين تردد حقيقي وأسئلة عادية)
"""

from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from .models import ApiToken, DiscountCode, Objection, Product
from .services import guard, memory, saudi, understanding


class InterestEvidenceTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="m", token="DRTOKEN0000001", image_quota=5, engine="v2")
        for sku in ("A", "B", "C", "D"):
            Product.objects.create(merchant=self.merchant, sku=sku, title=sku, price=Decimal("100"))
        self.ctx = memory.resolve_session(self.merchant, None, None)

    def react(self, sku, kind):
        memory.record_reaction(self.ctx, sku, kind, 1)

    def interest(self):
        return memory.interest_skus(self.ctx.conversation, self.ctx.profile)

    def test_shown_and_viewed_products_are_not_interest(self):
        self.react("A", "shown")
        self.react("B", "viewed")
        self.assertEqual(self.interest(), [])

    def test_every_real_action_is_interest_most_recent_first(self):
        for sku, kind in (("A", "liked"), ("B", "asked"), ("C", "tried_on"), ("D", "buy_click")):
            self.react(sku, kind)
        self.react("A", "add_to_cart")
        self.assertEqual(self.interest(), ["A", "D", "C", "B"])

    def test_a_later_rejection_removes_the_interest(self):
        self.react("A", "liked")
        self.react("A", "rejected")
        self.assertEqual(self.interest(), [])
        self.react("A", "asked")                     # سؤال صريح بيرجّعه
        self.assertEqual(self.interest(), ["A"])

    def test_shown_after_liked_does_not_erase_interest(self):
        self.react("A", "liked")
        self.react("A", "shown")
        self.assertEqual(self.interest(), ["A"])

    def test_neutral_is_not_interest(self):
        self.react("A", "neutral")
        self.assertEqual(self.interest(), [])

    def test_a_product_rejected_in_an_earlier_visit_is_not_interest_now(self):
        old = memory.resolve_session(self.merchant, None, None)
        memory.record_reaction(old, "A", "rejected", 1)
        # نفس الملف، محادثة تانية: الإعجاب الجديد بيلغي الرفض (آخر ردّ فعل صريح)، بس الرفض لحاله مش اهتمام
        same_profile = type(self.ctx)(self.merchant, old.profile, old.conversation)
        self.assertEqual(memory.interest_skus(old.conversation, old.profile), [])
        self.assertEqual(memory.interest_skus(same_profile.conversation, same_profile.profile), [])

    def test_interest_is_per_conversation(self):
        self.react("A", "liked")
        other = memory.resolve_session(self.merchant, None, None)
        self.assertEqual(memory.interest_skus(other.conversation, other.profile), [])

    def test_last_shown_sku_skips_rejected_products(self):
        self.react("A", "shown")
        self.react("B", "shown")
        self.react("B", "rejected")
        self.assertEqual(memory.last_shown_sku(self.ctx.conversation, self.ctx.profile), "A")
        self.assertIsNone(memory.last_shown_sku(memory.resolve_session(self.merchant, None, None).conversation))


class ObjectionResolutionTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="m", token="DRTOKEN0000002", image_quota=5, engine="v2")
        self.ctx = memory.resolve_session(self.merchant, None, None)
        for kind, turn in (("price", 2), ("fit", 3)):
            Objection.objects.create(merchant=self.merchant, profile=self.ctx.profile, conversation=self.ctx.conversation,
                                     type=kind, text=kind, raised_turn=turn)

    def open_types(self):
        return sorted(o.type for o in memory.open_objections(self.ctx.conversation))

    def test_default_resolves_everything(self):
        memory.resolve_open_objections(self.ctx.conversation, 3)
        self.assertEqual(self.open_types(), [])

    def test_keep_current_leaves_the_objection_raised_this_turn_open(self):
        memory.resolve_open_objections(self.ctx.conversation, 3, keep_current=True)
        self.assertEqual(self.open_types(), ["fit"])           # "تمام آخذه بس غالي": اللي قاله هالدور بيضل
        done = Objection.objects.get(type="price")
        self.assertEqual((done.status, done.resolved_turn), ("resolved", 3))


class TruthfulNoDiscountGuardTests(SimpleTestCase):
    """ما في كود معتمد: الجملة الصادقة بتمرّ، أي ترويج/وعد/رقم بيتمنع."""

    def violations(self, text, **kw):
        return guard.find_violations(text, guard.build_context(**kw))

    def test_truthful_sentences_pass_when_no_code_is_approved(self):
        for text in (
            "ما فيه كود خصم متاح حالياً.",
            "ما أقدر أقدم لك خصم حالياً.",
            "حالياً ما فيه كود أو عرض متاح لك.",
            "ما عندي كود خصم الحين، بس القماش ممتاز ويستاهل.",
            "ما فيه أي خصم الحين، لكن أقدر أرشح لك بديل أرخص.",
            "There is no discount code available right now.",
            "Sorry, I don't have any coupon at the moment, but this dress is great value.",
            "No discount is available right now.",
        ):
            with self.subTest(text):
                self.assertEqual(self.violations(text, merchant_codes=["SAVE10"]), [], text)

    def test_disguised_or_conditional_promotions_are_blocked(self):
        for text in (
            "ما أحلى هالخصم!",
            "ما في كود، بس لو اشتريت قطعتين بنخفّض لك.",
            "ما في كود حالياً، والخصم الحين متاح لك.",
            "ما عندي كود ثاني بس الخصم متاح لك.",
            "No code now, but a discount is available for you.",
            "عندي لك خصم حصري.",
            "ما في كود حالياً بس بعطيك خصم لو قررت اليوم.",
        ):
            with self.subTest(text):
                self.assertTrue(self.violations(text, merchant_codes=["SAVE10"]), text)

    def test_numbers_percentages_and_real_codes_stay_blocked_even_inside_a_negation(self):
        for text in (
            "ما في خصم 20% حالياً.",
            "ما فيه كود خصم، بس عندنا 15 بالمية على الفساتين.",
            "ما عندي كود SAVE10 حالياً.",
            "ما أقدر أقدم لك خصم 50 ريال حالياً.",
        ):
            with self.subTest(text):
                self.assertTrue(self.violations(text, merchant_codes=["SAVE10"]), text)

    def test_a_negation_far_from_the_discount_word_does_not_count(self):
        self.assertTrue(self.violations("ما شاء الله الفستان يجنن وعليه خصم مميز اليوم.", merchant_codes=["X1"]))

    def test_the_approved_code_flow_is_unchanged(self):
        offer = {"code": "SAVE10", "description": "خصم 10% على كل المنتجات"}
        self.assertEqual(self.violations("خذ كود SAVE10، خصم 10% على كل المنتجات.", merchant_codes=["SAVE10"], approved_offer=offer), [])


class InventedCodeWithApprovedOfferTests(SimpleTestCase):
    OFFER = {"code": "SAVE10", "description": "خصم 10%"}

    def kinds(self, text, **kw):
        ctx = guard.build_context(merchant_codes=["SAVE10"], approved_offer=self.OFFER, **kw)
        return guard.kinds(guard.find_violations(text, ctx))

    def test_a_different_code_next_to_the_word_code_is_invented(self):
        self.assertEqual(self.kinds("استخدم كود WELCOME20 الحين."), ["unapproved_code"])
        self.assertEqual(self.kinds("use code VIP2024 now"), ["unapproved_code"])
        self.assertEqual(self.kinds("الكود: NEWYEAR"), ["unapproved_code"])

    def test_the_approved_code_any_case_and_plain_words_are_fine(self):
        self.assertEqual(self.kinds("استخدم الكود SAVE10"), [])
        self.assertEqual(self.kinds("استخدم كود save10"), [])
        self.assertEqual(self.kinds("the code and a smile"), [])
        self.assertEqual(self.kinds("الكود ينفع على كل المنتجات"), [])

    def test_a_catalog_sku_after_the_word_code_is_not_a_discount_code(self):
        product = Product(sku="DRS-SAGE", title="x", price=Decimal("100"))
        self.assertEqual(self.kinds("كود DRS-SAGE متوفر بمقاسك", products=[product]), [])
        self.assertEqual(self.kinds("كود DRS-OTHER متوفر بمقاسك", products=[product]), ["unapproved_code"])
        self.assertEqual(self.kinds("كود المنتج DRS-SAGE متوفر", products=[product]), [])


class AnalyzerHesitationDefinitionTests(SimpleTestCase):
    """تعريف التردد بنص المحلّل (فهم المعنى، مش كلمات): النص نفسه بيتحقق منه الـ eval الحقيقي."""

    def test_genuine_hesitation_examples_are_listed(self):
        text = understanding.ANALYZER_SYSTEM
        for needle in ("بفكر", "مترددة", "مو متأكدة إذا أشتريه", "عجبني بس محتارة", "تمام آخذه بس غالي"):
            self.assertIn(needle, text)

    def test_plain_questions_are_explicitly_not_hesitation(self):
        text = understanding.ANALYZER_SYSTEM
        for needle in ("وش أحسن واحد؟", "قارن لي بين هذا وهذا", "وش القماش؟", "متى يوصل؟", "NOT hesitation"):
            self.assertIn(needle, text)

    def test_hesitation_reason_stays_empty_for_questions(self):
        self.assertIn("leave it empty for plain questions", understanding.ANALYZER_SYSTEM)


# ====================================================================== 1) ملاحظة السعر مش رفض
def _validate(raw, texts, current=None, products=None):
    return understanding.validate(raw, user_texts=texts, products_by_sku=products or {"A": Product(sku="A", title="فستان", price=Decimal("100")),
                                  "B": Product(sku="B", title="عباية", price=Decimal("90"))},
                                  merchant_currency="SAR", current_text=current)


class PriceRemarkIsNotARejectionTests(SimpleTestCase):
    def reaction(self, signals, kind="rejected", reason="price", evidence="غالي"):
        raw = {"reaction": {"sku": "A", "type": kind, "reason_type": reason, "evidence": evidence}, "signals": signals}
        return _validate(raw, ["حلو بس غالي علي شوي"]).reactions

    def test_a_price_rejection_of_a_hesitant_customer_becomes_real_interest(self):
        for signals in ({"intent": "object", "readiness": "hesitant", "hesitation_reason": "price"},
                        {"intent": "compare", "readiness": "hesitant", "hesitation_reason": "price"}):
            got = self.reaction(signals)
            self.assertEqual([(r["type"], r["reason_type"]) for r in got], [("liked", "")], signals)

    def test_a_price_complaint_that_is_not_hesitation_is_neutral_not_a_rejection(self):
        got = self.reaction({"intent": "ask_discount", "readiness": "browsing", "hesitation_reason": "price"})
        self.assertEqual([(r["type"], r["reason_type"]) for r in got], [("neutral", "")])

    def test_the_price_objection_is_kept(self):
        out = _validate({"reaction": {"sku": "A", "type": "rejected", "reason_type": "price", "evidence": "غالي"},
                         "signals": {"intent": "object", "readiness": "hesitant", "hesitation_reason": "price"}},
                        ["حلو بس غالي علي شوي"])
        self.assertEqual([o["type"] for o in out.objections], ["price"])
        self.assertIn("reaction:price_remark_is_not_a_rejection:A", out.dropped)

    def test_a_real_rejection_is_untouched_even_when_the_price_is_the_reason(self):
        got = self.reaction({"intent": "reject", "readiness": "", "hesitation_reason": ""})
        self.assertEqual([(r["type"], r["reason_type"]) for r in got], [("rejected", "price")])

    def test_without_analyzer_signals_the_rejection_stays(self):
        raw = {"reaction": {"sku": "A", "type": "rejected", "reason_type": "price", "evidence": "غالي"}}
        self.assertEqual([r["type"] for r in _validate(raw, ["غالي مرة"]).reactions], ["rejected"])

    def test_non_price_rejections_are_never_touched(self):
        for reason in ("style", "color", "fit", "quality", "other"):
            raw = {"reaction": {"sku": "A", "type": "rejected", "reason_type": reason, "evidence": "ما عجبني"},
                   "signals": {"intent": "object", "readiness": "hesitant", "hesitation_reason": "other"}}
            self.assertEqual([r["type"] for r in _validate(raw, ["ما عجبني"]).reactions], ["rejected"], reason)

    def test_the_analyzer_instructions_define_it_by_meaning(self):
        text = understanding.ANALYZER_SYSTEM
        for needle in ("A price remark is NOT a rejection", "حلو بس غالي", "عجبني بس سعره مرتفع", "حلو بس مو متأكد بسبب السعر",
                       "reaction type liked", "ما أبيه", "غالي وما أبيه", "غالية! عندكم أرخص؟"):
            self.assertIn(needle, text)


# ====================================================================== 3) اعتراض قديم أعاده المحلّل
class CurrentObjectionTests(SimpleTestCase):
    def objections(self, current, texts):
        raw = {"objections": [{"type": "price", "evidence": "غالي"}]}
        return _validate(raw, texts, current=current).objections

    def test_an_objection_quoted_in_the_current_message_is_current(self):
        got = self.objections("لسا غالي علي", ["حلو بس غالي", "لسا غالي علي"])
        self.assertEqual([o["current"] for o in got], [True])

    def test_an_old_objection_re_emitted_on_a_plain_question_is_not_current(self):
        got = self.objections("وش ميزته عن الثاني؟", ["حلو بس غالي", "وش ميزته عن الثاني؟"])
        self.assertEqual([(o["type"], o["current"]) for o in got], [("price", False)])

    def test_without_a_current_text_the_legacy_behaviour_is_kept(self):
        got = self.objections(None, ["حلو بس غالي", "وش ميزته؟"])
        self.assertEqual([o["current"] for o in got], [True])


# ====================================================================== 4) صيغة المذكر/المؤنث
class GenderEvidenceTests(SimpleTestCase):
    def gender(self, gender, evidence, texts):
        return _validate({"gender": gender, "gender_evidence": evidence}, texts).gender

    def test_the_customers_own_gendered_words_count(self):
        self.assertEqual(self.gender("female", "مترددة", ["عجبني بس مترددة"]), "female")
        self.assertEqual(self.gender("female", "محتارة", ["عجبني بس محتارة"]), "female")
        self.assertEqual(self.gender("male", "متردد", ["عجبني بس متردد"]), "male")
        self.assertEqual(self.gender("female", "مقتنعة", ["أنا مقتنعة بالفستان"]), "female")

    def test_no_quote_no_gender(self):
        self.assertEqual(self.gender("female", "", ["هلا، أبي فستان للعرس"]), "")
        self.assertEqual(self.gender("female", "مبسوطة", ["هلا، أبي فستان للعرس"]), "")     # الكلمة مش بالرسائل

    def test_what_they_shop_for_or_neutral_verbs_are_not_a_signal(self):
        texts = ["هلا، أبي فستان للعرس"]
        for evidence in ("فستان", "أبي فستان", "هلا", "للعرس", "أبي"):
            self.assertEqual(self.gender("female", evidence, texts), "", evidence)

    def test_the_analyzer_instructions_explain_gender(self):
        text = understanding.ANALYZER_SYSTEM
        for needle in ("gender_evidence", "مترددة", "متردد", "أبي / أبغى", "what they shop for"):
            self.assertIn(needle, text)


class GenderFormsTests(SimpleTestCase):
    def test_unknown_or_male_customers_never_get_feminine_forms(self):
        for gender in ("", "male"):
            fixed, changes = saudi.fix_gender("ودّك تختارينه؟ وتقدرين تجرّبينه وشوفي القماش، وخذي وقتك.", gender)
            self.assertEqual(fixed, "ودّك تختاره؟ وتقدر تجربه وشوف القماش، وخذ وقتك.")
            self.assertEqual(len(changes), 5)

    def test_a_female_customer_gets_feminine_forms_consistently(self):
        fixed, _ = saudi.fix_gender("تقدر تجرّبه وتختاره، شوف القماش وخذ وقتك، وتقدر تكمل، وجرّبها", "female")
        self.assertEqual(fixed, "تقدرين تجربينه وتختارينه، شوفي القماش وخذي وقتك، وتقدرين تكملين، وجربيها")

    def test_already_correct_text_is_untouched(self):
        for text, gender in (("تقدر تجرّب وشوف القماش", ""), ("تقدرين تجرّبين وشوفي القماش", "female"),
                             ("هذا الفستان خفيف ومريح وسعره ١١٩ ريال", "female"), ("ودّك تشوف أكثر؟", "")):
            self.assertEqual(saudi.fix_gender(text, gender), (text, []))

    def test_only_listed_address_forms_change(self):
        # كلمات عامة ما بتتغير: ما في تخمين صرفي
        fixed, changes = saudi.fix_gender("القطعة تناسبك والتوصيل يوصلك وخذ نظرة", "female")
        self.assertEqual(fixed, "القطعة تناسبك والتوصيل يوصلك وخذي نظرة")
        self.assertEqual(len(changes), 1)

    def test_non_arabic_text_is_untouched(self):
        self.assertEqual(saudi.fix_gender("You can try it", "female"), ("You can try it", []))


# ====================================================================== جولة أخيرة
class SameMessageGenderTests(SimpleTestCase):
    def test_clear_forms_in_the_current_message_are_detected(self):
        for text, expected in (("عجبني بس مترددة", "female"), ("عجبني بس متردد", "male"), ("وأنا محتارة بين الفستانين", "female"),
                               ("انا متأكدة من المقاس", "female"), ("أنا مقتنع", "male"), ("طيب أنا بنت وأبي مقاس M", "female")):
            self.assertEqual(understanding.detect_self_gender(text)[0], expected, text)

    def test_questions_to_the_assistant_neutral_text_and_mixed_forms_are_ignored(self):
        for text in ("هل أنت متأكد؟", "متأكد؟", "الطلب جاهز؟", "أبي فستان للعرس", "أنا مقتنعة ومتردد", "هلا"):
            self.assertIsNone(understanding.detect_self_gender(text), text)

    def test_validate_records_it_without_waiting_for_the_analyzer(self):
        out = _validate({"signals": {"intent": "object"}}, ["عجبني بس مترددة"], current="عجبني بس مترددة")
        self.assertEqual(out.gender, "female")
        self.assertTrue(any(d.startswith("gender:detected_in_message:female") for d in out.dropped))

    def test_the_explicit_form_beats_a_wrong_or_missing_analyzer_label(self):
        wrong = _validate({"gender": "male", "gender_evidence": "متردد"}, ["عجبني بس مترددة"], current="عجبني بس مترددة")
        self.assertEqual(wrong.gender, "female")
        none = _validate({}, ["أبي فستان"], current="أبي فستان")
        self.assertEqual(none.gender, "")

    def test_the_legacy_path_without_a_current_text_is_unchanged(self):
        self.assertEqual(_validate({}, ["عجبني بس مترددة"]).gender, "")


class StrayTextTests(SimpleTestCase):
    def test_foreign_script_fragments_are_removed(self):
        text, removed = saudi.strip_stray("التوصيل خلال ٢ إلى ٤ أيام عمل.оставка")
        self.assertEqual(text, "التوصيل خلال ٢ إلى ٤ أيام عمل.")
        self.assertEqual(removed, ["оставка"])
        self.assertEqual(saudi.strip_stray("مرحبا 你好 كيف")[0], "مرحبا كيف")
        self.assertEqual(saudi.strip_stray("שלום هلا")[0], "هلا")

    def test_leaked_tool_calls_and_garbage_characters_are_removed(self):
        self.assertEqual(saudi.strip_stray('هذا فستان حلو show_product(sku="DRS-IVORY")')[0], "هذا فستان حلو")
        self.assertEqual(saudi.strip_stray("وصلك الطلب\ufffd\u200b")[0], "وصلك الطلب")

    def test_glued_latin_leftovers_go_but_legitimate_latin_stays(self):
        self.assertEqual(saudi.strip_stray("نص عادي.ostavka")[0], "نص عادي.")
        for ok in ("خذ كود:SAVE10 الحين", "عندنا مقاس XL و M", "فستان DRS-NOIR متوفر", "السعر 119 SAR"):
            self.assertEqual(saudi.strip_stray(ok)[0], ok)
        self.assertEqual(saudi.strip_stray("فستان.noir متوفر", allowed_latin={"noir"})[0], "فستان.noir متوفر")

    def test_a_customer_who_writes_in_that_script_is_not_touched(self):
        text = "привет كيف الحال"
        self.assertEqual(saudi.strip_stray(text, customer_text="привет")[0], text)

    def test_clean_text_is_returned_untouched(self):
        text = "هذا الفستان خفيف ومريح وسعره ١١٩ ريال."
        self.assertEqual(saudi.strip_stray(text), (text, []))
