"""فحوصات حتمية (بدون LLM) على ردود محرّك v2 — دوال نقية: (turn_record, scenario_context) ← (ok, note).

turn_record (dict يبنيه eval_v2 بعد كل رسالة زبون):
    index, user, reply, cards[list of card dicts], offer (discount_offer أو None), size_advice,
    latency_s (زمن الجدار عند العميل)، ok/status/error (نتيجة HTTP), trace{llm_calls, latency_ms, analyzer,
    constraints, guard, memory_changes, models_used}, facts[active PreferenceFact dicts], rejected_db[skus].

scenario_context (dict): lang, catalog{sku: {...}}, grounding (guard.GroundingContext), budget, size, stated_slots,
    slot_values, rejected_skus, asked_for, last_rejected, prior_card_skus, last_card_price, offers_before,
    discount_codes, allow_reshow, arg (قيمة التوقّع لهالفحص من السيناريو).

كل فحص بيرجّع (ok, note). note بتبدأ بـ "skipped" لما الفحص ما انطبق (مثلاً ما في سياق كافي): ما بنعدّه فشل.
الفحوصات "الناعمة" (SOFT_CHECKS) تقريبية/اجتهادية — بتظهر بالتقرير بس ما بتفشّل التشغيل (exit code).
"""

import re
from decimal import Decimal, InvalidOperation

from apitokens.services import guard

# ----------------------------------------------------------------------- تطبيع نصوص

_DIACRITICS = re.compile(r"[ً-ٰٟـ]")
_AR_TOKEN = re.compile("[ء-ٰٟ-ۓ]+")


def norm_ar(text):
    """تطبيع للمقارنة بس: أرقام عربية ← لاتينية، شيل التشكيل والتطويل، توحيد الألف والياء/التاء المربوطة."""
    s = guard.normalize(text or "")
    s = _DIACRITICS.sub("", s)
    for src in "أإآٱ":
        s = s.replace(src, "ا")
    return s.replace("ى", "ي").replace("ة", "ه")


def _ar_tokens(text):
    return [norm_ar(t) for t in _AR_TOKEN.findall(text or "")]


def _phrase_regex(phrase):
    return re.escape(norm_ar(phrase)).replace(r"\ ", r"\s+")


def _alt(phrases):
    return re.compile("|".join(_phrase_regex(p) for p in phrases), re.IGNORECASE)


def customer_visible_texts(turn):
    """كل نص بيشوفه الزبون بهالدور: الرد + أسباب الكروت + عناوين أزرار التجربة."""
    texts = [turn.get("reply") or ""]
    for card in turn.get("cards") or []:
        texts.append(card.get("reason") or "")
        texts.append(card.get("tryon_button_label") or "")
    return [t for t in texts if t]


def _skus(cards):
    return [c.get("sku") for c in (cards or []) if c.get("sku")]


def _dec(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


# ----------------------------------------------------------------------- لغة

# فصحى متكلّفة ما بيقولها بياع سعودي (مأخوذة من STYLE_BLOCK بـ services/saudi.py). مقارنة token كامل.
STIFF_MSA_TOKENS = frozenset(norm_ar(w) for w in (
    "لقد", "سوف", "إنني", "إنّني", "يسرنا", "نود", "بإمكانك", "بإمكانكم",
))


def saudi(turn, ctx):
    """الرد/الأسباب/الأزرار بدون كلمات شامية/مصرية (saudi.find_non_saudi) وبدون فصحى متكلّفة.
    بيطبّق على زبون عربي بس (lang == "ar")."""
    if ctx.get("lang") != "ar":
        return True, "skipped: non-Arabic customer"
    from apitokens.services import saudi as saudi_service

    problems = []
    for text in customer_visible_texts(turn):
        problems.extend(f"non-saudi:{w}" for w in saudi_service.find_non_saudi(text))
        problems.extend(f"stiff-msa:{t}" for t in _ar_tokens(text) if t in STIFF_MSA_TOKENS)
    problems = sorted(set(problems))
    return (not problems), ("" if not problems else "; ".join(problems))


# ----------------------------------------------------------------------- بيع / تجارة

def no_invented_commerce(turn, ctx):
    """خط دفاع تاني بعد guard.py الداخلي: ولا كود/نسبة/مبلغ/كلمة ترويج/شحن مجاني غير مبرّر بالبيانات الحقيقية.
    الفشل هون = تسرّب من المدقّق (الرد وصل للزبون بعد المدقّق)."""
    grounding = ctx.get("grounding")
    if grounding is None:
        return True, "skipped: no grounding context"
    found = []
    for text in customer_visible_texts(turn):
        found.extend(guard.find_violations(text, grounding))
    if not found:
        return True, ""
    return False, "; ".join(f"{v.kind}:{v.snippet}" for v in found)


def no_discount_language(turn, ctx):
    """متجر بدون كود فعّال: ولا كلام خصم/كود بالرد. (تخفيض المنتج الحقيقي compare_at مسموح الكلام عنه
    لو الكرت المعروض عليه تخفيض فعلاً.)"""
    sale_card = any(c.get("compare_at_price") for c in turn.get("cards") or [])
    hits = []
    for text in customer_visible_texts(turn):
        norm = guard.normalize(text)
        code = guard.CODE_LEXICON.search(norm)
        if code:
            hits.append(code.group(0))
        disc = guard.DISCOUNT_LEXICON.search(norm)
        if disc and not sale_card:
            hits.append(disc.group(0))
    if turn.get("offer"):
        hits.append("discount_offer payload present")
    return (not hits), ("" if not hits else "discount language: " + ", ".join(sorted(set(hits))))


def no_rejected_sku(turn, ctx):
    """منتج رفضه الزبون ما بيرجع كرت (إلا لو طلبه صراحة بهالدور: ctx["asked_for"])."""
    rejected = set(ctx.get("rejected_skus") or ()) - set(ctx.get("asked_for") or ())
    bad = [s for s in _skus(turn.get("cards")) if s in rejected]
    return (not bad), ("" if not bad else f"rejected sku shown again: {bad}")


def shows_rejected_again(turn, ctx):
    """لما الزبون يطلب صراحة منتج رفضه قبل: لازم الكرت يرجع."""
    wanted = set(ctx.get("last_rejected") or ())
    if not wanted:
        return True, "skipped: nothing was rejected earlier"
    shown = set(_skus(turn.get("cards")))
    return bool(wanted & shown), ("" if wanted & shown else f"customer asked for {sorted(wanted)}, cards={sorted(shown)}")


def within_budget(turn, ctx):
    budget = _dec(ctx.get("budget"))
    if budget is None:
        return True, "skipped: no budget stated"
    exempt = set(ctx.get("asked_for") or ())
    over = []
    for card in turn.get("cards") or []:
        price = _dec(card.get("price"))
        if card.get("sku") in exempt or price is None:
            continue
        if price > budget:
            over.append(f"{card.get('sku')}={price}")
    return (not over), ("" if not over else f"over budget {budget}: {', '.join(over)}")


def size_available(turn, ctx):
    """كل كرت لازم المنتج يتوفر بمقاس الزبون. منتج بلا خيار مقاسات (شنطة مثلاً) بيعدّي."""
    size = (ctx.get("size") or "").strip().upper()
    if not size:
        return True, "skipped: no size stated"
    catalog = ctx.get("catalog") or {}
    bad = []
    for sku in _skus(turn.get("cards")):
        sizes = [str(s).strip().upper() for s in (catalog.get(sku) or {}).get("sizes") or []]
        if sizes and size not in sizes:
            bad.append(f"{sku}:{'/'.join(sizes)}")
    return (not bad), ("" if not bad else f"no size {size}: {', '.join(bad)}")


def no_duplicate_cards(turn, ctx):
    """نفس الكرت ما بينعرض مرتين بالمحادثة (إلا بنية شراء/طلب صريح: ctx["allow_reshow"] أو asked_for)."""
    now = _skus(turn.get("cards"))
    dup_in_turn = sorted({s for s in now if now.count(s) > 1})
    allowed = set(ctx.get("asked_for") or ())
    reshow = [] if ctx.get("allow_reshow") else [
        s for s in now if s in set(ctx.get("prior_card_skus") or ()) and s not in allowed
    ]
    bad = sorted(set(dup_in_turn) | set(reshow))
    return (not bad), ("" if not bad else f"duplicate cards: {bad}")


def cards_only_from_catalog(turn, ctx):
    """كل كرت = منتج حقيقي، متوفر، بسعره الحقيقي (من الكتالوج)."""
    catalog = ctx.get("catalog") or {}
    problems = []
    for card in turn.get("cards") or []:
        sku = card.get("sku")
        info = catalog.get(sku)
        if info is None:
            problems.append(f"{sku}: not in catalog")
            continue
        if not info.get("in_stock", True):
            problems.append(f"{sku}: out of stock")
        if _dec(card.get("price")) != _dec(info.get("price")):
            problems.append(f"{sku}: price {card.get('price')} != {info.get('price')}")
        if (card.get("compare_at_price") or None) and _dec(card.get("compare_at_price")) != _dec(info.get("compare_at")):
            problems.append(f"{sku}: invented compare_at_price")
    return (not problems), "; ".join(problems)


def never_cards(turn, ctx):
    """arg = قائمة SKUs ممنوع تظهر كروت بهالدور (مثلاً منتج نافد بطلب صريح)."""
    banned = set(ctx.get("arg") or ())
    bad = [s for s in _skus(turn.get("cards")) if s in banned]
    return (not bad), ("" if not bad else f"banned cards shown: {bad}")


def min_cards(turn, ctx):
    n = len(turn.get("cards") or [])
    return n >= int(ctx.get("arg") or 0), f"cards={n}"


def max_cards(turn, ctx):
    n = len(turn.get("cards") or [])
    return n <= int(ctx.get("arg") or 0), f"cards={n}"


def no_new_cards(turn, ctx):
    """زبون جاهز يشتري: ما في منتجات جديدة (upsell) — بس كروت انعرضت قبل."""
    prior = set(ctx.get("prior_card_skus") or ())
    new = [s for s in _skus(turn.get("cards")) if s not in prior]
    return (not new), ("" if not new else f"new products pushed: {new}")


def cheaper_alternative(turn, ctx):
    """اعتراض سعر ← بديل حقيقي أرخص من آخر كرت انعرض."""
    last = _dec(ctx.get("last_card_price"))
    if last is None:
        return True, "skipped: no earlier card to compare with"
    prices = [_dec(c.get("price")) for c in turn.get("cards") or []]
    ok = any(p is not None and p < last for p in prices)
    return ok, f"last card price {last}; cards now {[str(p) for p in prices]}"


# ----------------------------------------------------------------------- خصم

def discount_offered(turn, ctx):
    return bool(turn.get("offer")), ("" if turn.get("offer") else "no discount_offer in response")


def discount_not_offered(turn, ctx):
    """ولا عرض خصم بالـ payload ولا كود الخصم مذكور بنص الرد."""
    if turn.get("offer"):
        return False, f"discount offered: {(turn['offer'] or {}).get('code')}"
    norm_reply = " ".join(norm_ar(t) for t in customer_visible_texts(turn))
    leaked = [c for c in ctx.get("discount_codes") or [] if c and norm_ar(c) in norm_reply]
    return (not leaked), ("" if not leaked else f"code mentioned without an offer: {leaked}")


def discount_at_most_once(turn, ctx):
    """عرض كود خصم واحد كحد أقصى للزبون (عبر الزيارات)."""
    before = int(ctx.get("offers_before") or 0)
    if turn.get("offer") and before >= 1:
        return False, f"offered again (previous offers: {before})"
    return True, ""


# ----------------------------------------------------------------------- ضغط

PRESSURE_PHRASES = [
    "آخر قطعة", "آخر قطعتين", "آخر حبة", "باقي قطعة", "باقي قطعتين", "قطع قليلة", "كمية محدودة", "الكمية محدودة",
    "العرض ينتهي", "العرض بينتهي", "العرض راح ينتهي", "ينتهي العرض", "لفترة محدودة", "عرض محدود", "اليوم فقط",
    "اليوم بس", "لا تفوّت", "لا تفوتك", "ما تفوتك", "لا تفوت", "فرصة أخيرة", "فرصة اخيرة", "قبل ما تخلص",
    "قبل لا تخلص", "بتخلص", "راح تخلص", "توشك على النفاد", "على وشك النفاد", "على وشك تخلص", "الحق نفسك",
    "الحقها", "الحقه", "لا تتأخر", "بسرعة قبل",
    "hurry", "last chance", "last one", "limited time", "limited stock", "limited quantity", "selling fast",
    "don't miss", "dont miss", "act now", "while stocks last", "while supplies last", "running out", "almost gone",
    "offer ends", "ends today", "ends soon", "only a few left", "few left",
]
_PRESSURE_RE = _alt(PRESSURE_PHRASES)
_PRESSURE_LEFT = re.compile(r"only\s+\d+\s+left|\d+\s+(?:items?\s+)?left|باقي\s*\d+\s*(?:قطع|حبات|قطعه)", re.IGNORECASE)


def no_pressure(turn, ctx):
    """ما في ندرة/استعجال مصطنع (مرجعها: المساعد يقنع بالقيمة مش بالضغط)."""
    hits = []
    for text in customer_visible_texts(turn):
        norm = norm_ar(text)
        hits.extend(m.group(0) for m in _PRESSURE_RE.finditer(norm))
        hits.extend(m.group(0) for m in _PRESSURE_LEFT.finditer(norm))
    return (not hits), ("" if not hits else "pressure phrases: " + ", ".join(sorted(set(hits))))


# ----------------------------------------------------------------------- تكرار الأسئلة

# أسئلة الاستكشاف حسب الخانة (slot). تقريبية: كلمات سؤال بالعربي (سعودي/فصحى) + إنجليزي على نص مطبَّع (norm_ar).
SLOT_QUESTION_PATTERNS = {
    "budget": re.compile(
        r"ميزانيت|ميزانيه|كم\s*(?:تبي|تبغي|تبغا|ودك|تقدر|تحب|حاب|بتدفع|تدفع|تصرف|المبلغ|السعر|سقف)|"
        r"سقف\s*(?:السعر|الميزانيه)|حدود\s*(?:السعر|الميزانيه)|budget|how much (?:do you|are you|would you|can you)|"
        r"price range|spend", re.IGNORECASE),
    "occasion": re.compile(
        r"(?:اي|وش|ايش)\s*(?:ال)?مناسب|لاي\s*مناسب|للمناسب|لاي\s*(?:حفل|عرس|حدث)|occasion|"
        r"what(?:'s| is) the (?:event|occasion)|for what (?:event|occasion)|what event", re.IGNORECASE),
    "color": re.compile(
        r"(?:اي|وش|ايش)\s*(?:ال)?لون|لونك|الوانك|تفضل\s*لون|colou?r", re.IGNORECASE),
    "size": re.compile(
        r"(?:اي|وش|ايش)\s*(?:ال)?مقاس|مقاسك|كم\s*مقاس|طولك|وزنك|what(?:'s| is) your size|which size|what size|"
        r"your (?:height|size|weight)", re.IGNORECASE),
    "category": re.compile(
        r"(?:وش|ايش|اي)\s*(?:نوع|قطعه|صنف)|(?:فستان|عبايه|قميص|حذاء|شنطه)\s*ولا|"
        r"وش\s*(?:تدور|تبي|تبغا|تبغي|تحتاج)\s*(?:عليه|بالضبط)?|what (?:type|kind|are you looking)|"
        r"which (?:category|type)|dress or", re.IGNORECASE),
}
_QUESTION_START = re.compile(
    r"^\s*(?:وش|ايش|كم|اي|هل|ليش|وين|متى|مين|لمن|what|which|how|do you|would you|are you|can you)\b", re.IGNORECASE)
_QUESTION_START_AR = re.compile(r"^\s*(?:وش|ايش|كم|اي|هل|ليش|وين|متي|مين|لمن)\s")


def _question_sentences(text):
    out = []
    for sentence in guard.sentences(text):
        if "؟" in sentence or "?" in sentence or _QUESTION_START.match(norm_ar(sentence)) or \
                _QUESTION_START_AR.match(norm_ar(sentence)):
            out.append(sentence)
    return out


def no_repeat_question(turn, ctx):
    """المساعد ما يسأل عن خانة الزبون قالها (ميزانية/مناسبة/لون/مقاس/فئة).
    حدود الفحص (heuristic): مبني على كلمات سؤال، فما بيلتقط الصياغات غير المباشرة، وممكن يعلّق غلط على سؤال
    مختلف يحتوي نفس الكلمة (مثلاً "هذا اللون يناسب المناسبة؟" ما هو سؤال عن الخانة). سؤال التأكيد
    الي فيه قيمة الزبون نفسها ("ميزانيتك 200 صح؟") ما بنعتبره تكرار. لذلك الفحص "ناعم"."""
    stated = set(ctx.get("stated_slots") or ())
    if not stated:
        return True, "skipped: no slots stated"
    values = ctx.get("slot_values") or {}
    repeats = []
    for sentence in _question_sentences(turn.get("reply") or ""):
        norm = norm_ar(sentence)
        for slot in sorted(stated):
            pattern = SLOT_QUESTION_PATTERNS.get(slot)
            if not pattern or not pattern.search(norm):
                continue
            value = values.get(slot)
            if value and norm_ar(str(value)) in norm:
                continue  # تأكيد، مش تكرار
            repeats.append(f"{slot}: {sentence.strip()[:80]}")
    return (not repeats), ("" if not repeats else "re-asked: " + " | ".join(repeats))


# ----------------------------------------------------------------------- ذاكرة / مراحل / نص / زمن

def _fact_matches(fact, want):
    if want.get("key") and fact.get("key") != want["key"]:
        return False
    if want.get("polarity") and fact.get("polarity") != want["polarity"]:
        return False
    if "value" in want and want["value"] is not None:
        have, need = str(fact.get("value") or ""), str(want["value"])
        a, b = _dec(re.sub(r"[^\d.]", "", have) or "x"), _dec(re.sub(r"[^\d.]", "", need) or "x")
        if a is not None and b is not None and re.search(r"\d", have) and re.search(r"\d", need):
            return a == b
        return norm_ar(need) in norm_ar(have)
    return True


def memory_fact_present(turn, ctx):
    """arg = {"key","value"?,"polarity"?,"absent"?} أو قائمة منها. الحقائق = PreferenceFact الفعّالة بعد الدور."""
    wants = ctx.get("arg")
    wants = [wants] if isinstance(wants, dict) else list(wants or [])
    facts = turn.get("facts") or []
    problems = []
    for want in wants:
        found = [f for f in facts if _fact_matches(f, want)]
        if want.get("absent"):
            if found:
                problems.append(f"fact should be absent: {want}")
        elif not found:
            problems.append(f"fact missing: {want} (have {[(f.get('key'), f.get('value')) for f in facts]})")
    return (not problems), "; ".join(problems)


def stage_is(turn, ctx):
    """arg = مرحلة أو قائمة مراحل. بيقرا TurnTrace.constraints["stage"] (مفتاح اختياري: بيتخطّى لو غايب)."""
    constraints = (turn.get("trace") or {}).get("constraints") or {}
    stage = constraints.get("stage")
    if stage is None:
        return True, "skipped: no stage in trace.constraints"
    wanted = ctx.get("arg")
    wanted = [wanted] if isinstance(wanted, str) else list(wanted or [])
    return stage in wanted, f"stage={stage} wanted={wanted}"


def discount_verdict(turn, ctx):
    """arg = {"allowed": bool?, "reason": str|list?} — يقارن مع قرار السياسة بـ TurnTrace.constraints["discount"]
    ({"allowed","reason","trigger",...}). مفتاح اختياري يضيفه المحرّك: بيتخطّى لو غايب. ناعم."""
    verdict = ((turn.get("trace") or {}).get("constraints") or {}).get("discount")
    if not isinstance(verdict, dict):
        return True, "skipped: no discount verdict in trace.constraints"
    want = ctx.get("arg") or {}
    problems = []
    if "allowed" in want and bool(verdict.get("allowed")) != bool(want["allowed"]):
        problems.append(f"allowed={verdict.get('allowed')} wanted {want['allowed']}")
    if want.get("reason"):
        reasons = [want["reason"]] if isinstance(want["reason"], str) else list(want["reason"])
        if verdict.get("reason") not in reasons:
            problems.append(f"reason={verdict.get('reason')} wanted one of {reasons}")
    return (not problems), "; ".join(problems)


def latency_under(turn, ctx):
    limit = float(ctx.get("arg") or 0)
    got = turn.get("latency_s")
    if got is None:
        return True, "skipped: no latency"
    return got <= limit, f"{got:.2f}s (limit {limit:.1f}s)"


def must_not_contain(turn, ctx):
    """arg = قائمة نصوص ممنوعة بالرد/الأسباب (بعد تطبيع: أرقام عربية ← لاتينية، تشكيل، حروف صغيرة)."""
    haystack = " ".join(norm_ar(t) for t in customer_visible_texts(turn))
    bad = [w for w in ctx.get("arg") or [] if norm_ar(w) in haystack]
    return (not bad), ("" if not bad else f"forbidden text found: {bad}")


def mentions_any(turn, ctx):
    """arg = قائمة كلمات: واحدة على الأقل لازم تظهر بالرد (فحص تقريبي، ناعم)."""
    haystack = " ".join(norm_ar(t) for t in customer_visible_texts(turn))
    wanted = list(ctx.get("arg") or [])
    ok = any(norm_ar(w) in haystack for w in wanted)
    return ok, ("" if ok else f"none of {wanted} mentioned")


def request_ok(turn, ctx):
    if turn.get("ok"):
        return True, ""
    return False, f"request failed: status={turn.get('status')} error={turn.get('error')}"


def reply_not_empty(turn, ctx):
    reply = (turn.get("reply") or "").strip()
    ok = len(reply) >= 2 and any(ch.isalnum() for ch in reply)
    return ok, ("" if ok else "empty reply")


# ----------------------------------------------------------------------- bootstrap (تنقل / زيارة جديدة)

def check_bootstrap(record, expect):
    """توقّعات خطوة navigate/new_visit: restores_messages / returning / page_product. بترجّع [(name, ok, note)]."""
    results = []
    if "restores_messages" in expect:
        has = bool(record.get("messages_restored"))
        results.append(("bootstrap_restores_messages", has == bool(expect["restores_messages"]),
                        f"messages_restored={record.get('messages_restored')}"))
    if "returning" in expect:
        results.append(("bootstrap_returning", bool(record.get("returning")) == bool(expect["returning"]),
                        f"returning={record.get('returning')}"))
    if "page_product" in expect:
        got = record.get("page_product_sku")
        results.append(("bootstrap_page_product", got == expect["page_product"], f"page_product={got}"))
    return results


# ----------------------------------------------------------------------- سجل الفحوصات

CHECKS = {
    "request_ok": request_ok,
    "reply_not_empty": reply_not_empty,
    "saudi": saudi,
    "no_invented_commerce": no_invented_commerce,
    "no_discount_language": no_discount_language,
    "no_rejected_sku": no_rejected_sku,
    "shows_rejected_again": shows_rejected_again,
    "within_budget": within_budget,
    "size_available": size_available,
    "no_duplicate_cards": no_duplicate_cards,
    "cards_only_from_catalog": cards_only_from_catalog,
    "never_cards": never_cards,
    "min_cards": min_cards,
    "max_cards": max_cards,
    "no_new_cards": no_new_cards,
    "cheaper_alternative": cheaper_alternative,
    "discount_offered": discount_offered,
    "discount_not_offered": discount_not_offered,
    "discount_at_most_once": discount_at_most_once,
    "no_pressure": no_pressure,
    "no_repeat_question": no_repeat_question,
    "memory_fact_present": memory_fact_present,
    "stage_is": stage_is,
    "discount_verdict": discount_verdict,
    "latency_under": latency_under,
    "must_not_contain": must_not_contain,
    "mentions_any": mentions_any,
}

# فحوصات اجتهادية/تقريبية: بتنعرض بالتقرير بس ما بتفشّل exit code.
SOFT_CHECKS = frozenset({"no_repeat_question", "stage_is", "discount_verdict", "latency_under", "mentions_any"})

# بتشتغل على كل رد زبون (ملاحظة: within_budget / size_available بتنضاف تلقائياً لما الزبون يحدد ميزانية/مقاس)
DEFAULT_CHECKS = (
    "request_ok", "reply_not_empty", "saudi", "no_invented_commerce", "no_rejected_sku", "no_duplicate_cards",
    "no_pressure", "cards_only_from_catalog", "discount_at_most_once", "no_repeat_question",
)


def evaluate(name, turn, ctx):
    """تشغيل فحص بالاسم بشكل آمن (استثناء بالفحص = فشل واضح مش انهيار التقييم). بترجّع (ok, note)."""
    fn = CHECKS.get(name)
    if fn is None:
        return False, f"unknown check: {name}"
    try:
        ok, note = fn(turn, ctx)
    except Exception as exc:  # noqa: BLE001
        return False, f"check crashed: {type(exc).__name__}: {exc}"
    return bool(ok), note or ""
