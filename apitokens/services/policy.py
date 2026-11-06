"""سياسة العروض — الكود هو اللي بيقرر إذا مسموح نعرض كود خصم، مش الموديل.

هاد النسخة الأولى (v1) بتعتمد على اللي بيوصل بالطلب فقط (تاريخ المحادثة
وsku المعروضة من الودجت، وكلها من العميل فبتقدر تقلّل العروض بس ما بتحمي
من تزوير). النسخة الكاملة بحالة سيرفر ومراحل البيع بتيجي بمرحلة لاحقة وبتحل
محل هالدالة.
"""

from decimal import Decimal

from django.utils import timezone

from . import contracts

MIN_USER_TURNS = 2


def history_mentions_code(messages, codes):
    """هل ذكر المساعد أي كود خصم بالمحادثة؟ (بيمنع تكرار العرض)"""
    wanted = [c.strip().lower() for c in codes if c and c.strip()]
    if not wanted:
        return False
    for m in messages:
        if m["role"] != "assistant":
            continue
        content = m["content"].lower()
        if any(code in content for code in wanted):
            return True
    return False


def basic_offer_gate(*, user_turns, engaged, already_offered):
    """(مسموح؟، السبب). مبكر = قبل ما الزبون يتفاعل مع منتج محدد؛ متكرر = عُرض
    كود سابقاً بنفس المحادثة."""
    if already_offered:
        return False, "already_offered"
    if not engaged:
        return False, "no_engaged_product"
    if user_turns < MIN_USER_TURNS:
        return False, "too_early"
    return True, "ok"


# =====================================================================================
# v2 — الاستراتيجية وسياسة الخصم (حتمية، بدون DB، بدون موديل)
#
# decide() بتاخد بيانات جاهزة وبترجّع contracts.Strategy: شو يعمل البائع هالدور (directives
# إنجليزية) + قرار الخصم. الموديل ما بيقرر خصم أبداً: الكود بيختار كود موجود وصالح، والنص
# اللي بيشوفه الزبون هو وصف التاجر حرفياً (يتعامل معه seller_v2). الأكواد القديمة بدون حقول
# نسبة/قيمة ← ما منستنتج أي رقم منها، ولا منختار كود حسب نسبة بوصفه.
#
# بوّابات الخصم (بهالترتيب — أول فشل بيحدد reason). القاعدة: زبون مهتم فعلاً بمنتج + متردد بإتمام
# الشراء (مهما كان السبب) ← الكود الوحيد المتاح. التمييز بين تردد حقيقي وسؤال عادي مسؤولية المحلّل
# (فهم سياق الرسالة والمحادثة)، مش قوائم كلمات ولا أنواع اعتراضات هون.
#   G1 في كود صالح (valid_codes)               ← no_codes | expired
#   G2 اهتمام حقيقي بمنتج محدد                  ← not_engaged (engaged_product = المنتج يلي عليه اهتمام؛ مش صفحة/معروض/مُشاهَد)
#   G3 turn_no >= 3 (الدورين الأول والتاني ممنوعين) ← too_early
#   G4 ما انعرض كود بهالمحادثة (ما في تبريد بين المحادثات) ← already_offered
#   G5 الزبون متردد بهالرسالة                    ← no_hesitation
#        متردد = readiness hesitant، أو سبب تردد (أي سبب)، أو اعتراض سعري بنفس الدور ("تمام آخذه بس غالي").
#        رفض منتج ("مو حلو") بدون سبب سعري مش تردد. اعتراض قديم مفتوح لحاله مش تردد.
#   G6 بديل أرخص أولاً — بس لو التردد سعري ويوجد بديل أرخص مناسب (>=15%) لسا ما انعرض ولا تأكّد إنه انعرض
#        ← alternative_first. الأسباب التانية للتردد ما بتمرّ بمسار البديل.
#   وإلا: مسموح بأقدم كود صالح. ما في ربط بين سبب التردد وصلاحية الكود.
#
# مفاتيح الحالة (Conversation.state["sales"]):
#   alt_first_done: bool     — انعرض بطاقة بديل أرخص فعلاً بالرد يلي وصل للزبون (بيسجّلها v2_turn بعد حفظ الرد، مش decide).
#   alt_first_attempts: int  — كم مرة طلبنا من البائع يعرض بديل: للمراقبة (trace) فقط، ما بيفتح الخصم أبداً.
# =====================================================================================

PRICE_OBJECTIONS = frozenset({"price", "competitor_price"})
SLOT_PRIORITY = ("category", "occasion", "color", "size")
SLOT_LABELS = {
    "category": "what kind of item they are looking for",
    "occasion": "the occasion it is for",
    "color": "the color they prefer",
    "size": "their size",
    "budget": "their budget",
}
MAX_QUESTIONS = 2
FIRST_DISCOUNT_TURN = 3   # أول رسالة زبون ممكن يتعرض فيها كود (الأول والتاني ممنوعين)
ALT_MIN_SAVING = Decimal("0.10")
# نوايا ما بتطلب منتج جديد: الزبون عم يحكي عن القطعة يلي عجبته (مقاسه، سؤال عنها، "آخذه")
STAY_INTENTS = frozenset({"accept", "buy", "ask_info", "chitchat", "other"})
WIDE_SPREAD_RATIO = Decimal("2.5")


def valid_codes(codes, now=None):
    """الأكواد الفعّالة وضمن فترتها، بترتيب ثابت (الأقدم أولاً). هاد هو الاختيار الوحيد المسموح
    لما يكون في أكثر من كود — ما في ترتيب حسب نسبة الخصم."""
    now = now or timezone.now()
    ok = [
        c for c in codes
        if c.is_active
        and (c.starts_at is None or c.starts_at <= now)
        and (c.expires_at is None or c.expires_at > now)
    ]
    return sorted(ok, key=lambda c: (c.created_at is None, c.created_at.timestamp() if c.created_at else 0, c.id or 0))


def _signals(signals):
    s = signals if isinstance(signals, dict) else {}
    return {
        "intent": s.get("intent") or "other",
        "readiness": s.get("readiness") or "",
        "hesitation_reason": s.get("hesitation_reason") or "",
        "asks_discount": bool(s.get("asks_discount")),
        "wants_alternative": bool(s.get("wants_alternative")),
    }


def _sales_state(conversation_state):
    state = conversation_state.get("sales") if isinstance(conversation_state, dict) else None
    return state if isinstance(state, dict) else {}


def _sale_prices(product):
    """(السعر القديم، السعر الحالي) إذا في تخفيض حقيقي بالكتالوج، وإلا None."""
    now = _price(product)
    if product is None or now is None:
        return None
    raw = getattr(product, "compare_at_price", None)
    if raw is None:
        return None
    try:
        was = Decimal(str(raw))
    except (ArithmeticError, ValueError):
        return None
    if was <= now:
        return None
    return was, now


def _price(product):
    try:
        return Decimal(str(product.price))
    except (ArithmeticError, ValueError, AttributeError):
        return None


def _alt_eligible(engaged_product, requirements):
    """شرط البديل الأرخص: >= 10% أرخص من منتج الزبون، مش منتجه، مش مرفوض، ضمن الميزانية، متوفر
    (إلا إذا المتطلبات بتسمح)، وما انتساهل فيه بالفئة/المقاس/اللون/الميزانية."""
    base = _price(engaged_product)
    if base is None or base <= 0:
        return None
    ceiling = base * (Decimal("1") - ALT_MIN_SAVING)
    excluded = set(getattr(requirements, "excluded_skus", frozenset()))
    budget_max = getattr(requirements, "budget_max", None)
    allow_oos = bool(getattr(requirements, "include_out_of_stock", False))
    # نفس قسم المتجر: بديل فستان سهرة لازم يكون سهرة، مش كاجوال لمجرد إنه أرخص
    base_category = (getattr(engaged_product, "category", "") or "").strip()

    def eligible(cand):
        product = cand.product
        price = _price(product)
        if price is None or price > ceiling or product.sku == engaged_product.sku or product.sku in excluded:
            return False
        category = (getattr(product, "category", "") or "").strip()
        if base_category and category and category != base_category:
            return False
        if budget_max is not None and price > budget_max:
            return False
        if not allow_oos and getattr(product, "in_stock", True) is False:
            return False
        return cand.exact or not (set(cand.relaxed) & {"category", "size", "color", "budget"})

    return eligible


def cheaper_alternative(engaged_product, search_result, requirements=None, shown_skus=frozenset()):
    """أول مرشّح حقيقي أرخص بوضوح ولسا ما انعرض للزبون، أو None. exact أولاً. اللي انعرض أصلاً
    مستبعد هون (شوف cheaper_presented): البديل لازم يكون شي بيقدر البائع يعرضه فعلاً."""
    if engaged_product is None or search_result is None:
        return None
    eligible = _alt_eligible(engaged_product, requirements)
    if eligible is None:
        return None
    shown = set(shown_skus or ())
    pool = [c for c in (search_result.candidates or []) if c.product.sku not in shown]
    for cand in [c for c in pool if c.exact] + [c for c in pool if not c.exact]:
        if eligible(cand):
            return cand
    return None


def cheaper_presented(engaged_product, search_result, requirements=None, shown_skus=frozenset()):
    """هل في بديل أرخص مناسب انعرض للزبون من قبل وما زال مناسب (متوفر، ضمن قيوده، مش مرفوض)؟
    إذا إي ← بنعتبره مُقدَّم وما منعرضه مرة تانية بلا داعي."""
    if engaged_product is None or search_result is None:
        return False
    eligible = _alt_eligible(engaged_product, requirements)
    if eligible is None:
        return False
    shown = set(shown_skus or ())
    return any(c.product.sku in shown and eligible(c) for c in (search_result.candidates or []))


def alternative_shown(engaged_product, shown_products):
    """هل في بين المنتجات يلي انعرضت فعلاً هالدور بديل أرخص (>= 10%) من منتج الزبون؟"""
    base = _price(engaged_product) if engaged_product is not None else None
    if base is None or base <= 0:
        return False
    ceiling = base * (Decimal("1") - ALT_MIN_SAVING)
    for product in shown_products:
        price = _price(product)
        if price is not None and price <= ceiling and product.sku != engaged_product.sku:
            return True
    return False


def _pick_ask_slot(known_slots, *, price_hint, search_result):
    for slot in SLOT_PRIORITY:
        if slot not in known_slots:
            return slot
    if "budget" not in known_slots:
        candidates = search_result.candidates if search_result else []
        prices = [p for p in (_price(c.product) for c in candidates) if p and p > 0]
        wide = len(prices) >= 2 and max(prices) / min(prices) >= WIDE_SPREAD_RATIO
        if wide or price_hint:
            return "budget"
    return None


def _is_hesitating(sig, price_now):
    """تردد بإتمام الشراء بهالرسالة (إشارات المحلّل لهالدور، مش حالة قديمة). رفض المنتج لحاله مش تردد."""
    price_related = price_now or sig["hesitation_reason"] == "price"
    if sig["intent"] == "reject" and not price_related:
        return False
    return bool(sig["readiness"] == "hesitant" or sig["hesitation_reason"] or price_now)


def _discount_verdict(*, stage, readiness, sig, objections, engaged_product, turn_no, shown_count,
                      discount_codes, offer_history, alternative_first, alt_first_available, alt_first_done,
                      price_related, hesitating, now):
    now = now or timezone.now()
    valid = valid_codes(discount_codes, now)
    engaged = engaged_product is not None
    trigger = ("price_hesitation" if price_related else "hesitation") if hesitating else ""

    trace = {
        "codes_total": len(discount_codes), "codes_valid": len(valid), "stage": stage, "readiness": readiness,
        "engaged_sku": getattr(engaged_product, "sku", None), "turn_no": turn_no, "shown_count": shown_count,
        "offer_history": bool(offer_history), "hesitating": hesitating, "price_related": price_related,
        "asks_discount": sig["asks_discount"], "alt_first_available": alt_first_available,
        "alt_first_done": alt_first_done, "trigger": trigger,
        "open_objections": sorted({t for t, _sku in objections}),
    }

    def deny(why):
        trace["denied_by"] = why
        return contracts.DiscountVerdict(allowed=False, code=None, reason=why, trigger=trigger, rule_trace=trace)

    if not valid:
        active = [c for c in discount_codes if c.is_active]
        return deny("expired" if any(c.expires_at and c.expires_at <= now for c in active) else "no_codes")
    if not engaged:
        return deny("not_engaged")
    if turn_no < FIRST_DISCOUNT_TURN:
        return deny("too_early")
    if offer_history:
        return deny("already_offered")
    if not hesitating:
        return deny("no_hesitation")
    if alternative_first:
        return deny("alternative_first")
    trace["selected_code_id"] = valid[0].id
    return contracts.DiscountVerdict(allowed=True, code=valid[0], reason="ok", trigger=trigger, rule_trace=trace)


_ALWAYS = (
    "Never pressure or rush the customer; never claim scarcity, urgency or a deadline unless the catalog data says so.",
    "Never invent product facts, prices, stock or promotions: use only what the catalog data shows.",
)


def _featured_sku(search_result):
    """أول مرشّح مطابق، وإلا أول مرشّح. الكود يسمّي القطعة حتى البائع ما يختار لحاله."""
    candidates = list(getattr(search_result, "candidates", None) or [])
    exact = [c for c in candidates if getattr(c, "exact", False)]
    for cand in exact or candidates:
        sku = getattr(getattr(cand, "product", None), "sku", None)
        if sku:
            return sku
    return None


def _one_card_line(search_result, *, ask_slot, forbid):
    """أمر عرض واحد: كرت واحد، والمنتج مسمّى لما البحث لقيه. السؤال ييجي بعد الكرت، مش بداله."""
    note = getattr(search_result, "note", "") if search_result else ""
    questions_forbidden = "ask_question" in forbid
    if note == "nothing_matches":
        line = "Nothing matches exactly right now: say so in one short warm line and show no card."
        if questions_forbidden:
            return line + " Do not ask any question this turn."
        return line + " Ask ONE short question about what to adjust."
    sku = _featured_sku(search_result)
    if sku:
        line = (
            f"Show exactly ONE card this turn: sku {sku}, using show_product. "
            "In one or two sentences say why it fits THIS customer, using only that product's real facts. "
            "Do not show a second product."
        )
    else:
        line = (
            "Show exactly ONE card this turn: the single best candidate listed below, using show_product. "
            "In one or two sentences say why it fits THIS customer, using only that product's real facts. "
            "Do not show a second product."
        )
    if note == "no_exact_match":
        line = "The exact match is not available: say so honestly in one short phrase, then show this closest option and what differs. " + line
    if questions_forbidden:
        return line + " Do not ask any question this turn."
    if ask_slot:
        return line + (
            f" After the product, you may add ONE short question about {SLOT_LABELS[ask_slot]}, "
            "and only if that detail would change the choice. Skip the question if this product already fits."
        )
    return line


def _stay_line(sku):
    return (f"Stay on sku {sku}: it is the product in front of the customer. Answer what they said about it "
            "(size, fit, try-on or buying) and show no other product.")


def narrow_before_cards(*, intent, has_category, is_gift, page_open, wants_products):
    """هدية أو تصفّح واسع بلا فئة: نسأل قبل أي كرت. صفحة منتج، أو طلب يشوف الموجود، أو فئة معروفة، ما تنحجب."""
    if page_open or wants_products or has_category:
        return False
    return bool(is_gift or intent == "browse")


def _stage_directives(stage, *, ask_slot, forbid, search_result, objections, readiness, stay_sku=None,
                      hold_cards=False, hold_kind=""):
    d = []
    if hold_cards:
        if hold_kind == "narrow":
            d.append(
                "This is a gift or a wide browse and no category is known. Do NOT call show_product this turn. "
                "Ask exactly ONE short gentle question that offers two kinds of options, for example clothes or accessories. "
                "Do not name a specific product from the catalog."
            )
        elif hold_kind == "occasion":
            d.append(
                "They named an occasion but not which piece, and you do not know who it is for. "
                "Do NOT call show_product. Ask exactly ONE short question: a dress or a thobe. "
                "Do not name a specific product and do not describe one as if it fits."
            )
        else:
            d.append(
                "The customer has not said what they want this turn. Ask ONE short question about what they are looking for. "
                "Show no product card and do not name a specific product from the catalog."
            )
        return d
    if stay_sku and stage in ("discovery", "recommendation"):
        d.append(_stay_line(stay_sku))
    elif stage == "discovery":
        d.append(_one_card_line(search_result, ask_slot=ask_slot, forbid=forbid))
    elif stage == "recommendation":
        d.append(_one_card_line(search_result, ask_slot=None, forbid=forbid))
    elif stage == "objection_handling":
        d.append("Acknowledge the concern in a few words, with no apology and no long preamble.")
        types = {t for t, _s in objections}
        if types & PRICE_OBJECTIONS:
            d.append("On price, use only this product's was_price (if present) or a detail from its description or why tags. "
                     "Do not invent durability, lifespan, versatility, or cost per wear. "
                     "If nothing distinctive is there, go straight to the cheaper real alternative.")
        if "timing" in types:
            d.append("Respect that they want to think: give one concrete reason this piece suits them and keep the door "
                     "open while offering help with size or try-on.")
        if types & {"quality", "trust"}:
            d.append("Reassure with real facts only (materials, store policies from the store info); do not exaggerate.")
        if "fit" in types:
            d.append("Offer size help: the size guide or the try-on feature.")
        if "shipping" in types:
            d.append("Answer from the store info only; if it is not there, say you will not guess.")
        if not types:
            d.append("Find out gently what is holding them back (one short question at most) and address it.")
        d.append("Offer a real alternative from the catalog, or the approved code when the discount rule below allows it, "
                 "and keep the conversation going.")
    elif stage == "persuasion":
        d.append("Confirm briefly why it fits THEM (use what they told you), give one benefit that matters to them, "
                 "and make the next step easy: try-on, size help or the buy button. One next step only.")
        if stay_sku:
            d.append(_stay_line(stay_sku))
    elif stage == "closing":
        d.append("Make buying effortless: point to the buy button, confirm the size if needed, "
                 "keep the message short and warm.")
        d.append("No upsell and no extra questions that delay the purchase.")
    if readiness == "hesitant" and stage != "objection_handling":
        d.append("The customer seems unsure: acknowledge it lightly and give one concrete reassurance.")
    return d


def decide(*, stage, signals, readiness, objections, engaged_product, turn_no, shown_count, known_slots,
           questions_asked_in_conversation, requirements, discount_codes, offer_history, search_result=None,
           conversation_state=None, now=None, price_now=False, shown_skus=frozenset(), page_sku="",
           hold_cards=False, hold_kind=""):
    """استراتيجية الدور. دالة نقية: نفس المدخلات ← نفس المخرجات، بدون DB (كل شي بيجي جاهز).
    engaged_product = المنتج يلي عليه اهتمام حقيقي (None إذا ما في). price_now = اعتراض سعري انذكر بهالدور."""
    stage = stage if stage in contracts.STAGES else "discovery"
    readiness = readiness if readiness in contracts.READINESS else "browsing"
    sig = _signals(signals)
    objections = [(t, s) for t, s in (objections or ())]
    known_slots = set(known_slots or ())
    sales = _sales_state(conversation_state)
    attempts = sales.get("alt_first_attempts")
    attempts = attempts if isinstance(attempts, int) else 0
    alt_done = bool(sales.get("alt_first_done"))

    price_open = any(t in PRICE_OBJECTIONS for t, _s in objections)
    hesitating = engaged_product is not None and _is_hesitating(sig, price_now)
    price_related = hesitating and (price_now or sig["hesitation_reason"] == "price")
    # بديل أرخص أولاً: بس للتردد السعري. إذا في بديل مناسب انعرض من قبل بنعتبره مُقدَّم.
    alternative = None
    presented = False
    if price_related:
        presented = cheaper_presented(engaged_product, search_result, requirements, shown_skus)
        if not presented:
            alternative = cheaper_alternative(engaged_product, search_result, requirements, shown_skus)
    alt_first_available = alternative is not None
    alternative_first = alt_first_available and not alt_done

    verdict = _discount_verdict(
        stage=stage, readiness=readiness, sig=sig, objections=objections, engaged_product=engaged_product,
        turn_no=turn_no, shown_count=shown_count, discount_codes=list(discount_codes or ()),
        offer_history=offer_history, alternative_first=alternative_first, alt_first_available=alt_first_available,
        alt_first_done=alt_done or presented, price_related=price_related, hesitating=hesitating, now=now,
    )

    forbid = []
    can_ask = questions_asked_in_conversation < MAX_QUESTIONS
    if not can_ask:
        forbid.append("ask_question")
    if not verdict.allowed:
        forbid.append("push_discount")
    if stage in ("objection_handling", "closing"):
        forbid.append("upsell")

    ask_slot = None
    if can_ask:
        if stage == "discovery":
            price_hint = price_open or sig["asks_discount"] or sig["hesitation_reason"] == "price"
            ask_slot = _pick_ask_slot(known_slots, price_hint=price_hint, search_result=search_result)
        elif stage == "closing" and "size" not in known_slots:
            ask_slot = "size"

    # صفحة منتج مفتوحة، أو قطعة عجبت الزبون، وهو مش طالب غيرها ولا بديل أرخص ← منضل عليها.
    # الصفحة بتغلب القطعة القديمة: اللي فاتح قدامه هو موضوع الحكي. مشاهدة الصفحة ما بتعدّ اهتمام للخصم.
    stay_sku = ""
    page_sku = (page_sku or "").strip()
    holding = not alternative_first and not sig["wants_alternative"] and sig["intent"] in STAY_INTENTS
    if hold_cards:
        stay_sku = ""
    elif holding and page_sku:
        stay_sku = page_sku
    elif holding and engaged_product is not None:
        stay_sku = getattr(engaged_product, "sku", None) or ""
    directives = _stage_directives(
        stage, ask_slot=ask_slot, forbid=forbid, search_result=search_result, objections=objections,
        readiness=readiness, stay_sku=stay_sku, hold_cards=hold_cards, hold_kind=hold_kind,
    )
    if stage == "closing" and ask_slot == "size":
        directives.append("If the size is not settled, ask which size they want in the same short message as the buy step.")

    if verdict.allowed:
        directives.append("Present the approved code from the APPROVED OFFER block as a good reason to decide on this piece: "
                          "use only the merchant's own description, no calculated prices, no other promotions, "
                          "no free shipping, no deadline.")
        if sig["asks_discount"]:
            directives.append("They asked about a discount or for a bigger one: say naturally that this code, exactly as the "
                              "merchant describes it, is the maximum discount the store has approved; do not negotiate, do not repeat the percentage "
                              "or amount they asked for, and do not invent any other offer.")
    else:
        directives.append("Do not offer, hint at or promise any discount, code or special price this turn.")
        if alternative_first:
            directives.append("The customer is price-sensitive: first show ONE clearly cheaper real alternative from the "
                              f"catalog (sku {alternative.product.sku}) and say what makes it a good fit; "
                              "do not bring up discounts yourself.")
        asks = sig["asks_discount"]
        if asks and verdict.reason == "already_offered":
            directives.append("This customer already received the store's code earlier in this conversation (see DISCOUNT). "
                              "If they ask for more, for a bigger discount or for the code again, say naturally that this code, "
                              "as the merchant describes it, is the maximum discount the store has approved; do not offer another one, do not "
                              "negotiate, do not repeat the percentage or amount they asked for, do not invent other offers.")
        elif asks and verdict.reason in ("no_codes", "expired"):
            directives.append("They asked about discounts and the store has no discount code available right now: say so "
                              "plainly and naturally in ONE short sentence, with no long apology. Do not repeat the percentage or "
                              "amount they asked for, never promise one for later, never invent an alternative offer. Then keep "
                              "helping: the product's real value, REAL sale items (those with a catalog compare-at price) or a "
                              "cheaper real alternative.")
        elif asks:
            directives.append("They asked about discounts: say naturally in ONE short sentence that you cannot offer a discount "
                              "right now. Do NOT say that no codes exist, never reveal or hint at any code, amount or percentage, "
                              "do not repeat the percentage or amount they asked for, never promise one for later, never invent an "
                              "alternative offer. Then keep helping: the product's real value, REAL sale items (those with a "
                              "catalog compare-at price) or a cheaper real alternative.")
        price_side = (price_open or asks or sig["hesitation_reason"] == "price" or price_now
                      or readiness == "hesitant" or verdict.reason in ("no_codes", "expired"))
        if price_side:
            directives.append("Keep selling constructively: reframe the value with concrete catalog facts, offer a cheaper "
                              "real alternative if one exists, and reassure them; stay helpful and keep the conversation open.")
    if price_related:
        directives.append(
            "Do not mention a budget, 'your budget', or any budget amount unless the customer stated one."
        )
        sale = _sale_prices(engaged_product)
        if sale is not None:
            was, now = sale
            directives.append(
                f"The product they find expensive is already on sale: it was {was} and it is {now} now. "
                "Say those two catalog prices. Do not calculate a new percentage."
            )
    if (getattr(requirements, "occasion", "") or "") and not hold_cards:
        directives.append(
            "They named an occasion. Show a product only when its own title, tags or description match that occasion. "
            "Never rewrite another product so it sounds suitable."
        )
    directives.extend(_ALWAYS)

    return contracts.Strategy(
        stage=stage, readiness=readiness, directives=directives, ask_slot=ask_slot, forbid=forbid,
        alternative_first=alternative_first, stay_sku=stay_sku, hold_cards=bool(hold_cards),
        hold_kind=hold_kind or "", discount=verdict,
        state_updates={"sales": {"alt_first_attempts": attempts + 1}} if alternative_first else {},
    )
