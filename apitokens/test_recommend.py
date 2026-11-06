"""اختبارات محرّك الترشيح (services/recommend.py): فلاتر صلبة، ترتيب، تخفيف،
وحقائق مؤسَّسة. منتجات غير محفوظة (Product(...)) — بدون داتابيس أو شبكة."""

import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from .models import PreferenceFact, Product
from .services import recommend as rec
from .services.contracts import Candidate, Reason, Requirements, SearchResult
from .services.recommend import (
    canonical_category, canonical_color, canonical_size, check_product, compare, describe_candidates,
    explain, product_categories, product_colors, product_sizes_available, requirements_from_facts, search,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def P(sku, title="Item", price="100", *, category="", tags="", description="", options=None,
      in_stock=True, was=None, embedding=None):
    return Product(
        sku=sku, title=title, price=Decimal(str(price)), category=category, tags=tags,
        description=description, options=options or {}, in_stock=in_stock,
        compare_at_price=Decimal(str(was)) if was is not None else None, embedding=embedding,
    )


def F(key, value, *, polarity="like", source="explicit", subject="self", status="active", minutes=0):
    return PreferenceFact(key=key, value=value, polarity=polarity, source=source, subject=subject,
                          status=status, updated_at=T0 + timedelta(minutes=minutes))


def R(**kw):
    return Requirements(**kw)


def dress(sku, color="Black", size=("S", "M", "L"), price="100", title=None, **kw):
    return P(sku, title or f"{color} Dress {sku}", price, category="Dresses",
             options={"Color": [color], "Size": list(size)}, **kw)


def skus(result):
    return [c.product.sku for c in result.candidates]


# ------------------------------------------------------------------ جداول المرادفات

class SynonymTests(SimpleTestCase):
    def test_colors_black_variants(self):
        for text in ("أسود", "اسود", "سوداء", "سودا", "Black", "BLACK", "الاسود", "باللون الأسود"):
            self.assertEqual(canonical_color(text), "black", text)

    def test_colors_misc_arabic(self):
        cases = {"أبيض": "white", "بيضاء": "white", "احمر": "red", "حمراء": "red", "ازرق": "blue", "كحلي": "navy",
                 "كحلية": "navy", "أخضر": "green", "زيتي": "olive", "وردي": "pink", "زهري": "pink",
                 "بنفسجي": "purple", "برتقالي": "orange", "بني": "brown", "بيج": "beige", "رمادي": "grey",
                 "ذهبي": "gold", "فضي": "silver", "عنابي": "burgundy", "كريمي": "cream", "اصفر": "yellow"}
        for text, want in cases.items():
            self.assertEqual(canonical_color(text), want, text)

    def test_colors_english_and_phrases(self):
        self.assertEqual(canonical_color("navy blue"), "navy")
        self.assertEqual(canonical_color("Dark Blue"), "navy")
        self.assertEqual(canonical_color("ازرق غامق"), "navy")
        self.assertEqual(canonical_color("light blue"), "blue")
        self.assertEqual(canonical_color("grey"), "grey")
        self.assertEqual(canonical_color("gray"), "grey")
        self.assertEqual(canonical_color("rose gold"), "gold")

    def test_color_unknown_or_empty(self):
        for text in ("dress", "فستان", "", None, "xyz", "   "):
            self.assertIsNone(canonical_color(text), text)

    def test_category_dress_variants(self):
        for text in ("فستان", "فساتين", "الفستان", "dress", "Dresses", "gown"):
            self.assertEqual(canonical_category(text), "dress", text)

    def test_category_abaya_hamza_variants(self):
        for text in ("عباية", "عباءة", "عبايه", "عبايات", "abaya", "Abayas"):
            self.assertEqual(canonical_category(text), "abaya", text)

    def test_category_misc(self):
        cases = {"قميص": "shirt", "تيشيرت": "shirt", "t-shirt": "shirt", "بلوزة": "blouse", "بنطلون": "pants",
                 "جينز": "pants", "تنورة": "skirt", "جاكيت": "jacket", "Coat": "jacket", "معطف": "jacket",
                 "حذاء": "shoes", "Sneakers": "shoes", "شنطة": "bag", "حقيبة": "bag", "handbag": "bag",
                 "حجاب": "hijab", "طرحة": "hijab", "scarf": "hijab", "اكسسوار": "accessory", "jewelry": "accessory",
                 "طقم": "set", "جمبسوت": "jumpsuit", "قفطان": "kaftan"}
        for text, want in cases.items():
            self.assertEqual(canonical_category(text), want, text)

    def test_category_diacritics_and_unknown(self):
        self.assertEqual(canonical_category("فُسْتَان"), "dress")
        self.assertIsNone(canonical_category("أسود"))
        self.assertIsNone(canonical_category(""))
        self.assertIsNone(canonical_category(None))

    def test_size_letters_and_words(self):
        for text, want in {"m": "M", "M": "M", "Medium": "M", "ميديم": "M", "وسط": "M", "s": "S", "Small": "S",
                           "سمول": "S", "l": "L", "Large": "L", "لارج": "L", "xl": "XL", "Extra Large": "XL",
                           "XXL": "XXL", "2XL": "XXL", "xxxl": "XXXL", "3xl": "XXXL", "XS": "XS",
                           "extra small": "XS"}.items():
            self.assertEqual(canonical_size(text), want, text)

    def test_size_numeric_and_arabic_indic(self):
        self.assertEqual(canonical_size("42"), "42")
        self.assertEqual(canonical_size("٤٢"), "42")
        self.assertEqual(canonical_size("مقاس 38"), "38")
        self.assertEqual(canonical_size("size 46"), "46")
        self.assertIsNone(canonical_size("120"))

    def test_size_unknown(self):
        for text in ("", None, "dress", "أسود"):
            self.assertIsNone(canonical_size(text), text)

    def test_size_does_not_pluralize(self):
        self.assertIsNone(canonical_size("ms"))


# ------------------------------------------------------------------ حقائق المنتج

class ProductFactsTests(SimpleTestCase):
    def test_colors_from_option_keys(self):
        for key in ("Color", "colour", "اللون", "Frame Color"):
            self.assertEqual(product_colors(P("a", options={key: ["Black", "Navy"]})), {"black", "navy"}, key)

    def test_colors_option_slash_values(self):
        self.assertEqual(product_colors(P("a", options={"Color": ["Black/White"]})), {"black", "white"})

    def test_colors_options_beat_title(self):
        # العنوان "Black" بس خيار اللون المتوفر Navy فقط ← الأسود غير متوفر
        p = P("a", "Black Dress", options={"Color": ["Navy"]})
        self.assertEqual(product_colors(p), {"navy"})

    def test_colors_fallback_tags_and_title(self):
        self.assertEqual(product_colors(P("a", "Red Dress")), {"red"})
        self.assertEqual(product_colors(P("a", "Dress", tags="summer, اخضر")), {"green"})

    def test_colors_unrecognized_options_fall_back(self):
        self.assertEqual(product_colors(P("a", "Black Dress", options={"Color": ["Midnight"]})), {"black"})

    def test_colors_none(self):
        self.assertEqual(product_colors(P("a", "Plain Item")), set())

    def test_categories_from_category_tags_title(self):
        self.assertEqual(product_categories(P("a", "Thing", category="Dresses")), {"dress"})
        self.assertEqual(product_categories(P("a", "Thing", tags="abaya, new")), {"abaya"})
        self.assertEqual(product_categories(P("a", "Black Dress")), {"dress"})

    def test_categories_arabic(self):
        self.assertEqual(product_categories(P("a", "عباية سوداء")), {"abaya"})
        self.assertEqual(product_categories(P("a", "x", category="فساتين")), {"dress"})

    def test_categories_title_noise_limited(self):
        # "Wrap dress with belt" ما يصير إكسسوار لأن الفئة معروفة
        p = P("a", "Wrap Dress with Belt", category="Dresses")
        self.assertEqual(product_categories(p), {"dress"})

    def test_categories_title_all_when_nothing_else(self):
        self.assertEqual(product_categories(P("a", "Abaya Set")), {"abaya", "set"})

    def test_sizes_canonical(self):
        p = P("a", options={"Size": ["S", "Medium", "ل", "42"]})
        self.assertEqual(product_sizes_available(p), {"S", "M", "42"})

    def test_sizes_none_without_option(self):
        self.assertEqual(product_sizes_available(P("a")), set())
        self.assertEqual(product_sizes_available(P("a", options={"Color": ["Black"]})), set())

    def test_sizes_arabic_option_key(self):
        self.assertEqual(product_sizes_available(P("a", options={"المقاس": ["M", "L"]})), {"M", "L"})

    def test_sizes_unparseable_values_empty(self):
        self.assertEqual(product_sizes_available(P("a", options={"Size": ["Custom"]})), set())


# ------------------------------------------------------------------ requirements_from_facts

class RequirementsTests(SimpleTestCase):
    def test_empty(self):
        req = requirements_from_facts([])
        self.assertEqual(req, Requirements())

    def test_explicit_color_category_size_budget(self):
        req = requirements_from_facts([
            F("color", "أسود", minutes=1), F("category", "فستان", minutes=2), F("size", "M", minutes=3),
            F("budget_max", "300", minutes=4), F("budget_min", "100", minutes=5),
        ])
        self.assertEqual(req.colors, ("black",))
        self.assertEqual(req.categories, ("dress",))
        self.assertEqual(req.sizes, ("M",))
        self.assertEqual(req.budget_max, Decimal("300"))
        self.assertEqual(req.budget_min, Decimal("100"))

    def test_inferred_constraints_filter(self):
        req = requirements_from_facts([
            F("color", "black", source="inferred"), F("category", "dress", source="inferred"),
            F("size", "M", source="inferred"), F("budget_max", "200", source="inferred"),
        ])
        self.assertEqual(req.colors, ("black",))
        self.assertEqual(req.categories, ("dress",))
        self.assertEqual(req.sizes, ("M",))
        self.assertEqual(req.budget_max, Decimal("200"))

    def test_explicit_constraint_beats_inferred(self):
        req = requirements_from_facts([
            F("budget_max", "200", source="inferred"), F("budget_max", "400", source="explicit", minutes=1),
            F("color", "red", source="explicit"), F("color", "black", source="inferred"),
        ])
        self.assertEqual(req.budget_max, Decimal("400"))
        self.assertEqual(set(req.colors), {"red", "black"})

    def test_inactive_ignored(self):
        req = requirements_from_facts([F("color", "black", status="superseded"), F("size", "L", status="retracted")])
        self.assertEqual(req, Requirements())

    def test_avoid_colors(self):
        req = requirements_from_facts([F("color", "احمر", polarity="avoid"), F("color", "black")])
        self.assertEqual(req.avoid_colors, ("red",))
        self.assertEqual(req.colors, ("black",))

    def test_colors_oldest_first_and_deduped(self):
        req = requirements_from_facts([
            F("color", "navy", minutes=5), F("color", "اسود", minutes=1), F("color", "Black", minutes=2),
        ])
        # black الفائز (دقيقة 2) ثم navy (دقيقة 5)
        self.assertEqual(req.colors, ("black", "navy"))

    def test_newest_wins_like_over_avoid(self):
        req = requirements_from_facts([F("color", "black", polarity="avoid", minutes=1),
                                       F("color", "أسود", polarity="like", minutes=2)])
        self.assertEqual((req.colors, req.avoid_colors), (("black",), ()))

    def test_newest_wins_avoid_over_like(self):
        req = requirements_from_facts([F("color", "black", polarity="like", minutes=5),
                                       F("color", "أسود", polarity="avoid", minutes=2)])
        self.assertEqual((req.colors, req.avoid_colors), (("black",), ()))
        req = requirements_from_facts([F("color", "black", polarity="like", minutes=2),
                                       F("color", "أسود", polarity="avoid", minutes=5)])
        self.assertEqual((req.colors, req.avoid_colors), ((), ("black",)))

    def test_unknown_color_ignored(self):
        req = requirements_from_facts([F("color", "midnight shimmer"), F("color", "black")])
        self.assertEqual(req.colors, ("black",))

    def test_budget_newest_active_wins(self):
        req = requirements_from_facts([F("budget_max", "500", minutes=1), F("budget_max", "300", minutes=9),
                                       F("budget_max", "200", minutes=5)])
        self.assertEqual(req.budget_max, Decimal("300"))

    def test_budget_arabic_indic_digits(self):
        self.assertEqual(requirements_from_facts([F("budget_max", "٣٠٠")]).budget_max, Decimal("300"))
        self.assertEqual(requirements_from_facts([F("budget_min", "١٥٠٫٥")]).budget_min, Decimal("150.5"))

    def test_budget_with_currency_text(self):
        self.assertEqual(requirements_from_facts([F("budget_max", "500 ريال")]).budget_max, Decimal("500"))
        self.assertEqual(requirements_from_facts([F("budget_max", "SAR 250")]).budget_max, Decimal("250"))

    def test_budget_thousands_separator(self):
        self.assertEqual(requirements_from_facts([F("budget_max", "1,500")]).budget_max, Decimal("1500"))

    def test_budget_unparsable_ignored(self):
        for bad in ("كثير", "no budget", "", "0", "200 or 300"):
            self.assertIsNone(requirements_from_facts([F("budget_max", bad)]).budget_max, bad)

    def test_budget_falls_back_to_older_parsable(self):
        req = requirements_from_facts([F("budget_max", "400", minutes=1), F("budget_max", "غير محدد", minutes=9)])
        self.assertEqual(req.budget_max, Decimal("400"))

    def test_budget_any_subject(self):
        self.assertEqual(requirements_from_facts([F("budget_max", "200", subject="gift")]).budget_max, Decimal("200"))

    def test_sizes_self(self):
        req = requirements_from_facts([F("size", "m", minutes=1), F("size", "L", minutes=2), F("size", "medium", minutes=3)])
        self.assertEqual(req.sizes, ("M", "L"))

    def test_gift_size_with_recipient_wins(self):
        req = requirements_from_facts([F("size", "M"), F("size", "XL", subject="gift"), F("recipient", "أمي")])
        self.assertEqual(req.sizes, ("XL",))

    def test_gift_size_without_recipient_uses_self(self):
        req = requirements_from_facts([F("size", "M"), F("size", "XL", subject="gift")])
        self.assertEqual(req.sizes, ("M",))

    def test_recipient_without_gift_size_uses_self(self):
        req = requirements_from_facts([F("size", "M"), F("recipient", "اختي")])
        self.assertEqual(req.sizes, ("M",))

    def test_unrecognized_size_ignored(self):
        self.assertEqual(requirements_from_facts([F("size", "huge-ish")]).sizes, ())

    def test_categories_like_only(self):
        req = requirements_from_facts([F("category", "عباية"), F("category", "dress"), F("category", "حذاء", polarity="avoid"),
                                       F("category", "unknownthing")])
        self.assertEqual(req.categories, ("abaya", "dress"))

    def test_other_keys_ignored(self):
        req = requirements_from_facts([F("occasion", "wedding"), F("style", "classic"), F("material", "silk"),
                                       F("brand", "x"), F("fit", "loose")])
        self.assertEqual(req, Requirements())

    def test_passthrough_sets(self):
        req = requirements_from_facts([], excluded_skus=["a", "b"], exempt_skus=("c",), include_out_of_stock=True)
        self.assertEqual(req.excluded_skus, frozenset({"a", "b"}))
        self.assertEqual(req.exempt_skus, frozenset({"c"}))
        self.assertTrue(req.include_out_of_stock)
        self.assertIsInstance(req.excluded_skus, frozenset)

    def test_works_with_simple_namespace_facts(self):
        fact = SimpleNamespace(key="color", value="black", polarity="like", source="explicit", status="active",
                               subject="self", updated_at=None)
        self.assertEqual(requirements_from_facts([fact]).colors, ("black",))


# ------------------------------------------------------------------ check_product / فلاتر فردية

class CheckProductTests(SimpleTestCase):
    def test_passes_when_empty_req(self):
        self.assertEqual(check_product(dress("a"), R()), [])

    def test_stock(self):
        p = dress("a", in_stock=False)
        self.assertEqual(check_product(p, R()), ["stock"])
        self.assertEqual(check_product(p, R(include_out_of_stock=True)), [])

    def test_rejected_and_exempt(self):
        p = dress("a")
        self.assertEqual(check_product(p, R(excluded_skus=frozenset({"a"}))), ["rejected"])
        self.assertEqual(check_product(p, R(excluded_skus=frozenset({"a"}), exempt_skus=frozenset({"a"}))), [])

    def test_exempt_does_not_bypass_stock(self):
        p = dress("a", in_stock=False)
        self.assertEqual(check_product(p, R(exempt_skus=frozenset({"a"}))), ["stock"])

    def test_exempt_does_not_bypass_color_or_size(self):
        p = dress("a", color="Black", size=("S",))
        req = R(colors=("red",), sizes=("XL",), exempt_skus=frozenset({"a"}))
        self.assertEqual(check_product(p, req), ["color", "size"])

    def test_budget_max_min(self):
        p = dress("a", price="150")
        self.assertEqual(check_product(p, R(budget_max=Decimal("100"))), ["budget"])
        self.assertEqual(check_product(p, R(budget_max=Decimal("150"))), [])
        self.assertEqual(check_product(p, R(budget_min=Decimal("200"))), ["budget"])
        self.assertEqual(check_product(p, R(budget_min=Decimal("150"), budget_max=Decimal("150"))), [])

    def test_budget_exempt(self):
        p = dress("a", price="500")
        req = R(budget_max=Decimal("100"), exempt_skus=frozenset({"a"}))
        self.assertEqual(check_product(p, req), [])

    def test_category_any_of(self):
        p = dress("a")
        self.assertEqual(check_product(p, R(categories=("abaya", "dress"))), [])
        self.assertEqual(check_product(p, R(categories=("abaya",))), ["category"])

    def test_color_any_of(self):
        p = dress("a", color="Navy")
        self.assertEqual(check_product(p, R(colors=("black", "navy"))), [])
        self.assertEqual(check_product(p, R(colors=("black",))), ["color"])

    def test_product_without_known_color_fails_color_req(self):
        self.assertEqual(check_product(P("a", "Plain Item"), R(colors=("black",))), ["color"])

    def test_avoid_color_all_avoided(self):
        p = dress("a", color="Red")
        self.assertEqual(check_product(p, R(avoid_colors=("red",))), ["avoid_color"])

    def test_avoid_color_partial_passes(self):
        p = P("a", "x", options={"Color": ["Red", "Black"]})
        self.assertEqual(check_product(p, R(avoid_colors=("red",))), [])

    def test_avoid_color_unknown_colors_pass(self):
        self.assertEqual(check_product(P("a", "Plain"), R(avoid_colors=("red",))), [])

    def test_size_wrong(self):
        p = dress("a", size=("S", "M"))
        self.assertEqual(check_product(p, R(sizes=("XL",))), ["size"])
        self.assertEqual(check_product(p, R(sizes=("XL", "M"))), [])

    def test_size_no_option_passes(self):
        self.assertEqual(check_product(P("a", "Bag", category="Bags"), R(sizes=("M",))), [])

    def test_violation_order(self):
        p = dress("a", color="Black", size=("S",), price="900", in_stock=False)
        req = R(budget_max=Decimal("100"), colors=("red",), categories=("abaya",), sizes=("XL",),
                excluded_skus=frozenset({"a"}))
        self.assertEqual(check_product(p, req), ["stock", "rejected", "budget", "category", "color", "size"])

    def test_none_req_means_no_requirements(self):
        self.assertEqual(check_product(dress("a"), None), [])


# ------------------------------------------------------------------ search: فلاتر مجتمعة وعدّادات

class SearchFilterTests(SimpleTestCase):
    def test_empty_catalog(self):
        res = search([], R(colors=("black",)))
        self.assertIsInstance(res, SearchResult)
        self.assertEqual((res.candidates, res.considered, res.note), ([], 0, "nothing_matches"))
        self.assertEqual(set(res.dropped), set(rec.DROP_KEYS))

    def test_none_catalog(self):
        self.assertEqual(search(None, R()).considered, 0)

    def test_no_requirements_returns_all_in_stock(self):
        res = search([dress("a"), dress("b", in_stock=False)], R())
        self.assertEqual(skus(res), ["a"])
        self.assertEqual(res.dropped["stock"], 1)
        self.assertEqual(res.note, "")

    def test_all_exact_flag_and_counts(self):
        res = search([dress("a"), dress("b", title="Other Thing")], R(colors=("black",)))
        self.assertTrue(all(c.exact for c in res.candidates))
        self.assertEqual(res.exact_count, 2)
        self.assertEqual(res.relaxed_fields, ())
        self.assertEqual(res.considered, 2)

    def test_color_filter(self):
        res = search([dress("a", "Black"), dress("b", "Navy")], R(colors=("navy",)))
        self.assertEqual(skus(res), ["b"])
        self.assertEqual(res.dropped["color"], 1)

    def test_category_filter(self):
        items = [dress("a"), P("b", "Black Abaya", category="Abayas", options={"Color": ["Black"]})]
        res = search(items, R(categories=("abaya",)))
        self.assertEqual(skus(res), ["b"])
        self.assertEqual(res.dropped["category"], 1)

    def test_budget_filter(self):
        items = [dress("a", price="80"), dress("b", price="250"), dress("c", price="20")]
        res = search(items, R(budget_max=Decimal("100"), budget_min=Decimal("50")))
        self.assertEqual(skus(res), ["a"])
        self.assertEqual(res.dropped["budget"], 2)

    def test_stock_filter_and_include_out_of_stock(self):
        items = [dress("a"), dress("b", in_stock=False)]
        self.assertEqual(skus(search(items, R())), ["a"])
        self.assertEqual(sorted(skus(search(items, R(include_out_of_stock=True)))), ["a", "b"])

    def test_rejected_filter(self):
        res = search([dress("a"), dress("b")], R(excluded_skus=frozenset({"a"})))
        self.assertEqual(skus(res), ["b"])
        self.assertEqual(res.dropped["rejected"], 1)

    def test_exempt_brings_back_rejected_and_over_budget(self):
        items = [dress("a", price="500"), dress("b", price="50")]
        req = R(budget_max=Decimal("100"), excluded_skus=frozenset({"a", "b"}), exempt_skus=frozenset({"a", "b"}))
        self.assertEqual(sorted(skus(search(items, req))), ["a", "b"])

    def test_avoid_color_filter(self):
        items = [dress("a", "Red"), dress("b", "Black"), P("c", "Plain Thing")]
        res = search(items, R(avoid_colors=("red",)))
        self.assertEqual(sorted(skus(res)), ["b", "c"])
        self.assertEqual(res.dropped["avoid_color"], 1)

    def test_size_filter_and_unverifiable_warning(self):
        items = [dress("a", size=("S",)), dress("b", size=("M", "L")), P("c", "Black Bag", category="Bags")]
        res = search(items, R(sizes=("M",)))
        self.assertEqual(sorted(skus(res)), ["b", "c"])
        self.assertEqual(res.dropped["size"], 1)
        by = {c.product.sku: c for c in res.candidates}
        self.assertIn("size not verifiable", by["c"].warnings)
        self.assertEqual(by["b"].warnings, [])

    def test_one_size_passes_with_warning(self):
        items = [P("a", "Free Abaya", category="Abayas", options={"Size": ["One Size"]})]
        res = search(items, R(sizes=("M",)))
        self.assertEqual(skus(res), ["a"])
        self.assertTrue(res.candidates[0].warnings)

    def test_combined_filters(self):
        items = [
            dress("ok1", "Black", ("M",), "150"),
            dress("wrongcolor", "Navy", ("M",), "150"),
            dress("wrongsize", "Black", ("S",), "150"),
            dress("pricey", "Black", ("M",), "900"),
            dress("oos", "Black", ("M",), "150", in_stock=False),
            dress("rej", "Black", ("M",), "150"),
            P("shirt", "Black Shirt", "150", category="Shirts", options={"Color": ["Black"], "Size": ["M"]}),
        ]
        req = R(colors=("black",), categories=("dress",), sizes=("M",), budget_max=Decimal("200"),
                excluded_skus=frozenset({"rej"}))
        res = search(items, req, relax=False)
        self.assertEqual(skus(res), ["ok1"])
        self.assertEqual(res.dropped, {"stock": 1, "rejected": 1, "budget": 1, "occasion": 0, "category": 1, "color": 1,
                                       "avoid_color": 0, "size": 1})
        self.assertEqual(res.considered, 7)

    def test_dropped_counts_first_violation_only(self):
        p = dress("a", price="900", in_stock=False)
        res = search([p], R(budget_max=Decimal("100")), relax=False)
        self.assertEqual((res.dropped["stock"], res.dropped["budget"]), (1, 0))

    def test_limit(self):
        items = [dress(f"s{i:02d}", title=f"Dress Style{i} v{i}") for i in range(20)]
        self.assertEqual(len(search(items, R(), limit=5).candidates), 5)
        self.assertEqual(search(items, R(), limit=0).candidates, [])

    def test_accepts_generators(self):
        res = search((dress(f"a{i}", title=f"T{i} X{i}") for i in range(3)), R())
        self.assertEqual(res.considered, 3)


# ------------------------------------------------------------------ search: ترتيب

class RankingTests(SimpleTestCase):
    def test_deterministic_and_tiebreak_by_sku(self):
        items = [P(s, f"Plain{i} Thing{i}", "100") for i, s in enumerate(("c", "a", "b"))]
        first = skus(search(items, R()))
        self.assertEqual(first, ["a", "b", "c"])
        self.assertEqual(skus(search(list(reversed(items)), R())), first)

    def test_query_title_beats_description(self):
        items = [P("a", "Linen Shirt", description="great for summer floral"),
                 P("b", "Floral Maxi Gown", description="long")]
        self.assertEqual(skus(search(items, R(), query="floral"))[0], "b")

    def test_query_tags_beat_description(self):
        items = [P("a", "Item One", description="wedding guest"), P("b", "Item Two", tags="wedding")]
        self.assertEqual(skus(search(items, R(), query="wedding"))[0], "b")

    def test_query_arabic_normalization(self):
        items = [P("a", "فستان سهرة", category="فساتين"), P("b", "عباية يومية", category="عبايات")]
        self.assertEqual(skus(search(items, R(), query="ابغى عباءة"))[0], "b")

    def test_query_stopwords_ignored(self):
        items = [P("a", "Alpha Gown"), P("b", "Beta Gown")]
        res = search(items, R(), query="i want a the for")
        self.assertEqual(res.candidates[0].score, res.candidates[1].score)

    def test_query_canonical_cross_language(self):
        items = [P("a", "Red Top Alpha", category="Tops"), P("b", "Item Beta", category="Dresses",
                 options={"Color": ["Red"]})]
        # "فستان احمر" لازم يطلع المنتج الأحمر من فئة dress حتى لو العنوان ما فيه كلمات عربية
        self.assertEqual(skus(search(items, R(), query="فستان احمر"))[0], "b")

    def test_novelty_ranks_shown_lower_but_keeps_them(self):
        items = [P("a", "Alpha One"), P("b", "Beta Two"), P("c", "Gamma Three")]
        res = search(items, R(), shown_skus=["a"])
        self.assertEqual(skus(res)[-1], "a")
        self.assertEqual(len(res.candidates), 3)

    def test_sale_bonus(self):
        items = [P("a", "Alpha One", "100"), P("b", "Beta Two", "100", was="150")]
        self.assertEqual(skus(search(items, R()))[0], "b")

    def test_fake_sale_ignored(self):
        items = [P("a", "Alpha One", "100"), P("b", "Beta Two", "100", was="100")]
        self.assertEqual(skus(search(items, R())), ["a", "b"])

    def test_price_fit_bonus(self):
        items = [P("a", "Alpha One", "20"), P("b", "Beta Two", "90")]
        self.assertEqual(skus(search(items, R(budget_max=Decimal("100"))))[0], "b")

    def test_no_budget_price_neutral(self):
        items = [P("a", "Alpha One", "20"), P("b", "Beta Two", "90")]
        self.assertEqual(skus(search(items, R())), ["a", "b"])

    def test_embedding_cosine(self):
        items = [P("a", "Alpha One", embedding=[1.0, 0.0]), P("b", "Beta Two", embedding=[0.0, 1.0]),
                 P("c", "Gamma Three")]
        self.assertEqual(skus(search(items, R(), query_vector=[0.0, 1.0]))[0], "b")
        self.assertEqual(skus(search(items, R(), query_vector=[1.0, 0.0]))[0], "a")

    def test_weak_semantic_match_is_a_close_alternative(self):
        items = [P("a", "Alpha One", embedding=[1.0, 0.0])]
        res = search(items, R(), query="evening gown", query_vector=[0.2, 0.98])
        self.assertIn("close alternative", " ".join(res.candidates[0].warnings))

    def test_embedding_ignored_without_vector(self):
        items = [P("a", "Alpha One", embedding=[1.0, 0.0]), P("b", "Beta Two", embedding=[0.0, 1.0])]
        self.assertEqual(skus(search(items, R())), ["a", "b"])

    def test_scores_are_numbers_rounded(self):
        res = search([P("a", "Alpha One")], R(), query="alpha")
        self.assertIsInstance(res.candidates[0].score, float)

    def test_diversify_caps_same_prefix(self):
        same = [P(f"x{i}", f"Satin Dress {i}", "100", description="floral") for i in range(5)]
        other = [P("y1", "Linen Abaya A"), P("y2", "Cotton Shirt B")]
        res = search(same + other, R(), query="satin floral", limit=4)
        got = skus(res)
        self.assertEqual(sum(1 for s in got if s.startswith("x")), 2)
        self.assertEqual(len(got), 4)
        self.assertIn("y1", got)
        self.assertIn("y2", got)

    def test_diversify_fills_when_not_enough_variety(self):
        same = [P(f"x{i}", f"Satin Dress {i}") for i in range(5)]
        self.assertEqual(len(search(same, R(), limit=4).candidates), 4)

    def test_diversify_prefix_uses_arabic_normalization(self):
        items = [P(f"x{i}", f"عباية سوداء {i}") for i in range(4)] + [P("z", "فستان ازرق")]
        got = skus(search(items, R(), limit=3))
        self.assertIn("z", got)


# ------------------------------------------------------------------ search: تخفيف

class RelaxationTests(SimpleTestCase):
    def test_color_relaxation(self):
        items = [dress("a", "Navy", ("M",)), dress("b", "Red", ("M",))]
        res = search(items, R(colors=("black",), categories=("dress",), sizes=("M",)))
        self.assertEqual(sorted(skus(res)), ["a", "b"])
        self.assertEqual(res.note, "no_exact_match")
        self.assertEqual(res.relaxed_fields, ("color",))
        self.assertEqual(res.exact_count, 0)
        for cand in res.candidates:
            self.assertFalse(cand.exact)
            self.assertEqual(cand.relaxed, ("color",))
            self.assertTrue(cand.warnings[0].startswith("requested color black not available; this one is "))
        by = {c.product.sku: c for c in res.candidates}
        self.assertIn("this one is navy", by["a"].warnings[0])

    def test_color_relaxation_keeps_size_budget_stock_rejected(self):
        items = [
            dress("ok", "Navy", ("M",), "100"),
            dress("size", "Navy", ("S",), "100"),
            dress("budget", "Navy", ("M",), "999"),
            dress("stock", "Navy", ("M",), "100", in_stock=False),
            dress("rej", "Navy", ("M",), "100"),
        ]
        req = R(colors=("black",), sizes=("M",), budget_max=Decimal("200"), excluded_skus=frozenset({"rej"}))
        self.assertEqual(skus(search(items, req)), ["ok"])

    def test_color_relaxation_keeps_category(self):
        items = [dress("d", "Navy"), P("s", "Navy Shirt", category="Shirts", options={"Color": ["Navy"]})]
        res = search(items, R(colors=("black",), categories=("dress",)))
        self.assertEqual(skus(res), ["d"])

    def test_color_relaxation_keeps_avoid_color(self):
        items = [dress("a", "Red"), dress("b", "Navy")]
        res = search(items, R(colors=("black",), avoid_colors=("red",)))
        self.assertEqual(skus(res), ["b"])

    def test_colorless_product_in_relaxation_has_warning(self):
        res = search([P("a", "Plain Dress", category="Dresses")], R(colors=("black",)))
        self.assertEqual(res.note, "no_exact_match")
        self.assertIn("no listed color", res.candidates[0].warnings[0])

    def test_category_relaxation(self):
        items = [P("ab", "Black Abaya", category="Abayas", options={"Color": ["Black"], "Size": ["M"]}),
                 P("sh", "Black Shoes", category="Shoes", options={"Color": ["Black"]})]
        res = search(items, R(categories=("dress",), colors=("black",), sizes=("M",)))
        self.assertEqual(skus(res), ["ab"])
        self.assertEqual(res.relaxed_fields, ("category",))
        self.assertEqual(res.note, "no_exact_match")
        self.assertFalse(res.candidates[0].exact)
        self.assertEqual(res.candidates[0].relaxed, ("category",))
        self.assertIn("requested category dress not available", res.candidates[0].warnings[0])

    def test_category_relaxation_respects_color(self):
        items = [P("ab", "Navy Abaya", category="Abayas", options={"Color": ["Navy"]})]
        res = search(items, R(categories=("dress",), colors=("black",)))
        # لا الفئة ولا اللون يطابقوا: ما في تخفيف مزدوج
        self.assertEqual((res.candidates, res.note), ([], "nothing_matches"))

    def test_category_relaxation_respects_budget_stock_rejected_size(self):
        base = dict(category="Abayas", options={"Color": ["Black"], "Size": ["M"]})
        items = [P("price", "Black Abaya A", "900", **base), P("oos", "Black Abaya B", "100", in_stock=False, **base),
                 P("rej", "Black Abaya C", "100", **base),
                 P("size", "Black Abaya D", "100", category="Abayas", options={"Color": ["Black"], "Size": ["S"]})]
        req = R(categories=("dress",), sizes=("M",), budget_max=Decimal("200"), excluded_skus=frozenset({"rej"}))
        self.assertEqual(search(items, req).note, "nothing_matches")

    def test_tops_family(self):
        items = [P("bl", "Silk Blouse", category="Blouses")]
        res = search(items, R(categories=("shirt",)))
        self.assertEqual(skus(res), ["bl"])

    def test_unrelated_category_not_suggested(self):
        res = search([P("sh", "Black Shoes", category="Shoes")], R(categories=("dress",)))
        self.assertEqual(res.note, "nothing_matches")

    def test_relax_false(self):
        res = search([dress("a", "Navy")], R(colors=("black",)), relax=False)
        self.assertEqual((res.candidates, res.note), ([], "nothing_matches"))

    def test_nothing_matches_when_all_dropped_by_hard_filters(self):
        res = search([dress("a", price="900")], R(budget_max=Decimal("100")))
        self.assertEqual((res.candidates, res.note, res.dropped["budget"]), ([], "nothing_matches", 1))

    def test_exact_results_prevent_relaxation(self):
        items = [dress("a", "Black"), dress("b", "Navy")]
        res = search(items, R(colors=("black",)))
        self.assertEqual(skus(res), ["a"])
        self.assertEqual((res.note, res.relaxed_fields), ("", ()))

    def test_never_relax_wrong_size(self):
        res = search([dress("a", "Navy", ("S",))], R(colors=("black",), sizes=("XL",)))
        self.assertEqual((res.candidates, res.note), ([], "nothing_matches"))

    def test_never_relax_rejected_even_if_only_color_differs(self):
        res = search([dress("a", "Navy")], R(colors=("black",), excluded_skus=frozenset({"a"})))
        self.assertEqual(res.candidates, [])

    def test_exempt_still_allowed_in_relaxation(self):
        res = search([dress("a", "Navy", price="900")],
                     R(colors=("black",), budget_max=Decimal("100"), exempt_skus=frozenset({"a"})))
        self.assertEqual(skus(res), ["a"])

    def test_relaxed_candidates_still_have_reasons(self):
        res = search([dress("a", "Navy")], R(colors=("black",), categories=("dress",)))
        self.assertTrue(any(r.kind == "category" for r in res.candidates[0].reasons))
        self.assertFalse(any(r.kind == "color" for r in res.candidates[0].reasons))


# ------------------------------------------------------------------ explain / describe / compare

def _words(text):
    return set(re.findall(r"[A-Za-z]+", text.lower()))


TEMPLATE_WORDS = _words("comes in option tags title category size sizes available price is within the budget minimum "
                        "above real sale was now in stock tagged one")


def allowed_vocab(product, req, query=""):
    text = " ".join([product.title, product.description, product.category, product.tags,
                     " ".join(f"{k} " + " ".join(map(str, v)) for k, v in (product.options or {}).items()),
                     " ".join(req.colors), " ".join(req.categories), " ".join(req.sizes), query])
    return TEMPLATE_WORDS | _words(text)


def allowed_numbers(product, req):
    nums = {rec._money(product.price)}
    if product.compare_at_price is not None:
        nums.add(rec._money(product.compare_at_price))
    for d in (req.budget_max, req.budget_min):
        if d is not None:
            nums.add(rec._money(d))
    for size in (product.options or {}).get("Size", []):
        nums.add(str(size))
    nums.update(req.sizes)
    return nums


class ExplainTests(SimpleTestCase):
    def setUp(self):
        self.p = P("d1", "Black Satin Dress", "120", category="Dresses", tags="evening, satin",
                   description="Long dress for evenings", options={"Color": ["Black", "Navy"], "Size": ["S", "M", "L"]},
                   was="150")
        self.req = R(colors=("black",), categories=("dress",), sizes=("M",), budget_max=Decimal("200"))

    def texts(self, product=None, req=None, query=""):
        return {r.kind: r.text for r in explain(product or self.p, req or self.req, query)}

    def test_all_kinds(self):
        got = self.texts()
        self.assertEqual(got["color"], "comes in black (Color option)")
        self.assertEqual(got["size"], "size M available (sizes: S, M, L)")
        self.assertEqual(got["budget"], "price 120 is within the 200 budget")
        self.assertEqual(got["sale"], "real sale: was 150, now 120")
        self.assertEqual(got["category"], "category: dress")
        self.assertEqual(got["stock"], "in stock")

    def test_reasons_are_Reason_objects(self):
        self.assertTrue(all(isinstance(r, Reason) for r in explain(self.p, self.req)))

    def test_every_reason_grounded(self):
        vocab, nums = allowed_vocab(self.p, self.req, "satin evening"), allowed_numbers(self.p, self.req)
        for reason in explain(self.p, self.req, "satin evening"):
            self.assertTrue(_words(reason.text) <= vocab, (reason.text, _words(reason.text) - vocab))
            for number in re.findall(r"\d+(?:\.\d+)?", reason.text):
                self.assertIn(number, nums, reason.text)

    def test_nothing_invented_for_minimal_product(self):
        p = P("m1", "Thing", "10")
        reasons = explain(p, R())
        self.assertEqual([r.kind for r in reasons], ["stock"])

    def test_no_material_claims(self):
        joined = " ".join(r.text for r in explain(self.p, self.req, "silk cotton waterproof")).lower()
        for word in ("silk", "cotton", "waterproof", "breathable", "premium"):
            self.assertNotIn(word, joined)

    def test_no_sale_reason_without_real_sale(self):
        p = P("a", "Dress", "100", was="100")
        self.assertNotIn("sale", self.texts(p, R()))
        p2 = P("a", "Dress", "100")
        self.assertNotIn("sale", self.texts(p2, R()))

    def test_out_of_stock_no_stock_reason(self):
        self.assertNotIn("stock", self.texts(P("a", "Dress", in_stock=False), R(include_out_of_stock=True)))

    def test_budget_over_has_no_budget_reason(self):
        self.assertNotIn("budget", self.texts(self.p, R(budget_max=Decimal("100"), exempt_skus=frozenset({"d1"}))))

    def test_budget_min_only(self):
        got = self.texts(self.p, R(budget_min=Decimal("100")))
        self.assertEqual(got["budget"], "price 120 is above the 100 minimum")

    def test_budget_range(self):
        got = self.texts(self.p, R(budget_min=Decimal("100"), budget_max=Decimal("200")))
        self.assertEqual(got["budget"], "price 120 is within the 100-200 budget")

    def test_color_source_tags_and_title(self):
        p = P("a", "Red Dress", tags="Green")
        self.assertEqual(self.texts(p, R(colors=("green",)))["color"], "comes in green (tags)")
        p = P("a", "Red Dress")
        self.assertEqual(self.texts(p, R(colors=("red",)))["color"], "comes in red (title)")

    def test_color_reason_absent_when_not_available(self):
        self.assertNotIn("color", self.texts(self.p, R(colors=("red",))))

    def test_query_driven_color_and_category_without_req(self):
        got = self.texts(self.p, R(), query="فستان اسود")
        self.assertIn("color", got)
        self.assertIn("category", got)

    def test_tag_reason_from_query(self):
        got = {r.kind: r.text for r in explain(self.p, R(), "satin")}
        self.assertEqual(got["tag"], "tagged: satin")

    def test_size_reason_absent_when_no_size_option(self):
        self.assertNotIn("size", self.texts(P("a", "Bag"), R(sizes=("M",))))

    def test_multiple_sizes_reason(self):
        got = self.texts(self.p, R(sizes=("S", "M")))
        self.assertEqual(got["size"], "sizes S, M available (sizes: S, M, L)")


class DescribeTests(SimpleTestCase):
    KEYS = {"sku", "title", "price", "currency", "category", "colors", "sizes", "description", "tryon", "exact",
            "why", "warnings"}

    def cands(self):
        p1 = P("d1", "Black Dress", "120.00", category="Dresses", description="x" * 300,
               options={"Color": ["Navy", "Black"], "Size": ["L", "S", "M"]}, was="150.00")
        p2 = P("g1", "Gift Card", "50", category="Cards")
        return [Candidate(product=p1, score=1.0, reasons=[Reason("stock", "in stock")], exact=True),
                Candidate(product=p2, exact=False, relaxed=("color",), warnings=["w"])]

    def test_shape_and_keys(self):
        rows = describe_candidates(self.cands(), "SAR")
        self.assertEqual(len(rows), 2)
        self.assertEqual(set(rows[0]), self.KEYS | {"was_price"})
        self.assertEqual(set(rows[1]), self.KEYS)

    def test_values(self):
        row = describe_candidates(self.cands(), "SAR")[0]
        self.assertEqual(row["price"], "120")
        self.assertEqual(row["was_price"], "150")
        self.assertEqual(row["currency"], "SAR")
        self.assertEqual(row["colors"], ["black", "navy"])
        self.assertEqual(row["sizes"], ["S", "M", "L"])
        self.assertEqual(row["why"], ["in stock"])
        self.assertTrue(row["tryon"])
        self.assertTrue(row["exact"])

    def test_description_truncated(self):
        row = describe_candidates(self.cands())[0]
        self.assertEqual(row["description"], "x" * 220 + "…")
        short = describe_candidates([Candidate(product=P("a", "T", description="short text"))])[0]
        self.assertEqual(short["description"], "short text")

    def test_non_tryon_and_warnings(self):
        row = describe_candidates(self.cands())[1]
        self.assertFalse(row["tryon"])
        self.assertFalse(row["exact"])
        self.assertEqual(row["warnings"], ["w"])
        self.assertNotIn("was_price", row)

    def test_json_serializable(self):
        import json
        json.dumps(describe_candidates(self.cands(), "SAR"))

    def test_empty(self):
        self.assertEqual(describe_candidates([]), [])
        self.assertEqual(describe_candidates(None), [])

    def test_sizes_as_written_with_numeric(self):
        p = P("a", "Heels", options={"Size": ["42", "38", "40"]})
        self.assertEqual(describe_candidates([Candidate(product=p)])[0]["sizes"], ["38", "40", "42"])

    def test_fake_sale_has_no_was_price(self):
        p = P("a", "Dress", "100", was="90")
        self.assertNotIn("was_price", describe_candidates([Candidate(product=p)])[0])


class CompareTests(SimpleTestCase):
    def setUp(self):
        self.a = P("A", "Black Dress", "120", category="Dresses", options={"Color": ["Black"], "Size": ["S", "M"]})
        self.b = P("B", "Navy Dress", "95", category="Dresses", options={"Color": ["Navy"], "Size": ["M"]}, was="120")

    def test_structure(self):
        out = compare([self.a, self.b])
        self.assertEqual(set(out), {"items", "differences", "cheapest_sku"})
        self.assertEqual([i["sku"] for i in out["items"]], ["A", "B"])
        self.assertEqual(out["cheapest_sku"], "B")

    def test_differences_facts(self):
        diffs = compare([self.a, self.b])["differences"]
        self.assertIn("price: A 120 vs B 95", diffs)
        self.assertIn("colors: A black vs B navy", diffs)
        self.assertIn("sizes: A S, M vs B M", diffs)
        self.assertIn("sale: A no sale vs B was 120", diffs)
        self.assertFalse(any(d.startswith("category") for d in diffs))

    def test_identical_has_no_differences(self):
        self.assertEqual(compare([self.a, self.a])["differences"], [])

    def test_with_req_adds_reasons(self):
        out = compare([self.a, self.b], R(colors=("black",)))
        self.assertIn("comes in black (Color option)", out["items"][0]["why"])

    def test_three_products_and_tie(self):
        c = P("C", "Third Dress", "95", category="Dresses")
        out = compare([self.a, self.b, c])
        self.assertEqual(out["cheapest_sku"], "B")
        self.assertTrue(any(d.startswith("price: A 120 vs B 95 vs C 95") for d in out["differences"]))

    def test_empty_and_single(self):
        self.assertEqual(compare([]), {"items": [], "differences": [], "cheapest_sku": None})
        self.assertEqual(compare([self.a])["differences"], [])
        self.assertEqual(compare([self.a])["cheapest_sku"], "A")


# ------------------------------------------------------------------ تكامل وأداء

class IntegrationTests(SimpleTestCase):
    def test_facts_to_search_end_to_end(self):
        facts = [F("category", "فستان", minutes=1), F("color", "اسود", minutes=2), F("size", "ميديم", minutes=3),
                 F("budget_max", "٢٠٠", minutes=4), F("color", "احمر", polarity="avoid", minutes=5)]
        items = [dress("ok", "Black", ("M", "L"), "150"), dress("big", "Black", ("S",), "150"),
                 dress("pricey", "Black", ("M",), "400"), dress("rej", "Black", ("M",), "100")]
        req = requirements_from_facts(facts, excluded_skus=["rej"])
        res = search(items, req, query="فستان اسود")
        self.assertEqual(skus(res), ["ok"])
        rows = describe_candidates(res.candidates, "SAR")
        self.assertEqual(rows[0]["sku"], "ok")
        self.assertTrue(rows[0]["why"])

    def test_inferred_constraints_filter_search(self):
        facts = [F("color", "black", source="inferred"), F("budget_max", "50", source="inferred")]
        res = search([dress("a", "Navy", price="500")], requirements_from_facts(facts))
        self.assertEqual(skus(res), [])
        self.assertEqual(res.note, "nothing_matches")

    def test_large_catalog_performance(self):
        colors, cats = ["Black", "Navy", "Red", "Green", "Beige"], ["Dresses", "Abayas", "Shirts", "Pants"]
        items = [P(f"s{i:04d}", f"{colors[i % 5]} Style{i} {cats[i % 4]}", 50 + (i % 200), category=cats[i % 4],
                   tags="summer, new", description="a nice item " * 5,
                   options={"Color": [colors[i % 5]], "Size": ["S", "M", "L"]}) for i in range(500)]
        req = R(colors=("black",), categories=("dress",), sizes=("M",), budget_max=Decimal("200"))
        start = time.perf_counter()
        res = search(items, req, query="black dress summer")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)
        self.assertEqual(res.considered, 500)
        self.assertLessEqual(len(res.candidates), 8)
        self.assertTrue(res.candidates)

    def test_search_does_not_mutate_inputs(self):
        items = [dress("a"), dress("b", "Navy")]
        before = [(p.sku, p.price, dict(p.options)) for p in items]
        search(items, R(colors=("red",)))
        self.assertEqual(before, [(p.sku, p.price, dict(p.options)) for p in items])


def _wear(sku, title, category, description, tags=""):
    product = P(sku, title, category=category, tags=tags, description=description)
    product.image_url = "https://x/%s.jpg" % sku.lower()
    product.product_url = "https://shop.example.com/p/%s" % sku.lower()
    return product


class OccasionRequestTests(SimpleTestCase):
    def setUp(self):
        self.gown = _wear(
            "GOWN", "فستان سهرة أسود بنقشة زهور", "فساتين سهرة",
            "فستان سهرة مناسب للأعراس والمناسبات الرسمية.", "أسود,سهرة,زواجات,رسمي",
        )
        self.day = _wear(
            "DAY", "فستان نهاري أزرق مورّد", "فساتين كاجوال",
            "فستان نهاري للطلعات والجمعات.", "أزرق,نهاري",
        )
        self.shirt = _wear(
            "SHIRT", "قميص كاروهات أزرق وأسود", "قمصان رجالية",
            "قميص رجالي قطني. يُلبس مفتوحاً فوق تيشيرت أو مغلقاً للطلعات.",
            "كاروهات,رجالي,كاجوال",
        )
        self.office = _wear(
            "OFFICE", "فستان رمادي محبوك", "فساتين كاجوال",
            "يناسب الدوام والطلعات الشتوية.", "رمادي,عمل",
        )

    def test_wedding_word_matches_a_dress_that_says_weddings_and_not_a_shirt(self):
        self.assertEqual(rec.named_occasion("عرس"), "wedding")
        self.assertEqual(rec.named_occasion("للعرس"), "wedding")
        self.assertIsNone(rec.named_occasion("مرحبا"))
        self.assertTrue(rec.product_matches_occasion(self.gown, "wedding"))
        self.assertFalse(rec.product_matches_occasion(self.shirt, "wedding"))
        self.assertFalse(rec.product_matches_occasion(self.day, "wedding"))

    def test_wedding_search_drops_the_old_shirt_and_the_day_dress(self):
        res = search([self.shirt, self.day, self.gown], R(occasion="wedding", categories=("dress", "set", "abaya", "kaftan")), query="عرس")
        self.assertEqual(skus(res), ["GOWN"])
        self.assertEqual(res.note, "")

    def test_the_demo_catalog_wedding_is_an_evening_dress_not_the_check_shirt(self):
        from .demo_catalog import PRODUCTS

        products = [
            _wear(item["external_id"], item["title"], item["category"], item["description"], item["tags"])
            for item in PRODUCTS
        ]
        res = search(products, R(occasion="wedding", categories=("dress", "set", "abaya", "kaftan")), query="عرس")
        self.assertTrue(res.candidates)
        self.assertTrue(all(rec.product_matches_occasion(c.product, "wedding") for c in res.candidates))
        self.assertNotIn("demo-83", skus(res))
        self.assertTrue(any("فستان" in c.product.title or "طقم" in c.product.title for c in res.candidates))

    def test_a_mans_wedding_does_not_borrow_a_womens_dress(self):
        res = search([self.gown, self.shirt], R(occasion="wedding", categories=("thobe",)), query="عرس")
        self.assertEqual(res.candidates, [])
        self.assertEqual(res.note, "nothing_matches")

    def test_work_does_not_return_the_wedding_gown(self):
        res = search([self.gown, self.shirt, self.office], R(occasion="work"), query="دوام")
        self.assertIn("OFFICE", skus(res))
        self.assertNotIn("GOWN", skus(res))

    def test_saying_a_shirt_for_the_wedding_does_not_pretend_the_casual_shirt_fits(self):
        planned = rec.plan_request("قميص للعرس", (), "female", 2)
        self.assertEqual(planned["occasion"], "wedding")
        self.assertEqual(planned["categories"], ("shirt",))
        res = search([self.gown, self.shirt], R(occasion="wedding", categories=("shirt",)), query="قميص للعرس")
        self.assertEqual(res.candidates, [])

    def test_current_occasion_replaces_a_saved_shirt_category(self):
        planned = rec.plan_request("عرس", [F("category", "قميص")], "female", 9)
        self.assertEqual(planned["occasion"], "wedding")
        self.assertFalse(planned["needs_gender"])
        self.assertIn("dress", planned["categories"])
        self.assertNotIn("shirt", planned["categories"])

    def test_unknown_gender_does_not_guess_a_dress_or_a_shirt(self):
        planned = rec.plan_request("عرس", (), "", 2)
        self.assertTrue(planned["needs_gender"])
        self.assertEqual(planned["categories"], ())

    def test_naming_a_dress_keeps_the_wedding(self):
        remembered = F("occasion", "wedding")
        remembered.conversation_id = 2
        planned = rec.plan_request("بدي فستان", [remembered], "female", 2)
        self.assertEqual(planned["occasion"], "wedding")
        self.assertEqual(planned["categories"], ("dress",))
        self.assertFalse(planned["drop_occasion"])

    def test_a_new_conversation_does_not_keep_the_old_category(self):
        old = F("category", "قميص")
        old.conversation_id = 1
        self.assertIsNone(rec.plan_request("ورجيني", [old], "female", 2)["categories"])
        self.assertEqual(rec.facts_for_request([old], 2, None), [])
        self.assertEqual(len(rec.facts_for_request([old], 1, None)), 1)

    def test_an_occasion_stays_for_later_turns_until_they_name_an_item(self):
        remembered = F("occasion", "wedding")
        remembered.conversation_id = 2
        later = rec.plan_request("ورجيني شو عندك", [remembered], "female", 2)
        self.assertEqual(later["occasion"], "wedding")
        self.assertIn("dress", later["categories"])
        switched = rec.plan_request("بدي قميص", [remembered], "female", 2)
        self.assertEqual(switched["occasion"], "")
        self.assertEqual(switched["categories"], ("shirt",))
        self.assertTrue(switched["drop_occasion"])

    def test_a_wrong_card_is_replaced_by_the_matching_product(self):
        from types import SimpleNamespace

        from .services.contracts import Candidate, SearchResult, Strategy
        from .services.v2_turn import _enforce_occasion

        out = SimpleNamespace(
            recommendations=[{"sku": "SHIRT", "title": self.shirt.title}],
            reply="هالقميص الرجالي تقدرين تلبسينه مغلق للعرس.",
        )
        plan = SimpleNamespace(
            requirements=R(occasion="wedding", categories=("dress",)),
            strategy=Strategy(),
            search=SearchResult(candidates=[Candidate(product=self.gown, exact=True)], exact_count=1),
        )
        _enforce_occasion(out, plan, [self.gown, self.shirt], SimpleNamespace(currency="SAR"), "ar", "female")
        self.assertEqual([c["sku"] for c in out.recommendations], ["GOWN"])
        self.assertIn(self.gown.title, out.reply)
        self.assertNotIn("قميص", out.reply)

    def test_no_match_asks_instead_of_forcing_a_product(self):
        from types import SimpleNamespace

        from .services.contracts import SearchResult, Strategy
        from .services.v2_turn import _enforce_occasion

        out = SimpleNamespace(recommendations=[{"sku": "SHIRT"}], reply="هذا القميص للعرس.")
        plan = SimpleNamespace(
            requirements=R(occasion="wedding", categories=("thobe",)),
            strategy=Strategy(),
            search=SearchResult(candidates=[], note="nothing_matches"),
        )
        _enforce_occasion(out, plan, [self.gown, self.shirt], SimpleNamespace(currency="SAR"), "ar", "male")
        self.assertEqual(out.recommendations, [])
        self.assertIn("ما لقيت", out.reply)


class OccasionMemoryTests(TestCase):
    def test_saying_wedding_retracts_the_saved_shirt(self):
        from datetime import timedelta

        from django.utils import timezone

        from .models import ApiToken, Conversation, CustomerProfile
        from .services.v2_turn import _apply_request

        merchant = ApiToken.objects.create(
            label="متجر", token="OCCASIONTOKEN01", image_quota=5, currency="SAR", engine="v2",
        )
        profile = CustomerProfile.objects.create(
            merchant=merchant, gender_signal="female", expires_at=timezone.now() + timedelta(days=30),
        )
        conversation = Conversation.objects.create(
            merchant=merchant, session_id="occasion-1", profile=profile, engine_version=2,
        )
        PreferenceFact.objects.create(
            merchant=merchant, profile=profile, conversation=conversation, key="category", value="قميص",
            polarity="like", source="explicit", status="active",
        )
        facts, constraint = _apply_request(SimpleNamespace(profile=profile, conversation=conversation), "عرس", 2)
        self.assertEqual(constraint["occasion"], "wedding")
        self.assertIn("dress", constraint["categories"])
        self.assertFalse(any(f.key == "category" and f.status == "active" for f in facts))
        self.assertFalse(PreferenceFact.objects.filter(profile=profile, key="category", status="active").exists())
        self.assertTrue(PreferenceFact.objects.filter(
            profile=profile, key="occasion", value="wedding", status="active", conversation=conversation,
        ).exists())
