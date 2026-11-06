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
# بوّابات الخصم (بهالترتيب — أول فشل بيحدد reason):
#   G1 في كود صالح (valid_codes)               ← no_codes | expired
#   G2 في منتج الزبون مركّز عليه               ← not_engaged
#   G3 turn_no >= 2                             ← too_early
#   G4 ما انعرض كود بهالمحادثة (ما في تبريد بين المحادثات) ← already_offered
#   G5 المرحلة مش discovery                     ← not_engaged (ما في منتج انعرض فعلياً)
# بعدها المحفّزات (واحد يكفي): price_objection | hesitation | asked، وإلا:
#   ready_no_price_concern (جاهز بدون اعتراض/تردد: منحمي هامش التاجر) أو no_trigger.
# بديل-أولاً: اعتراض سعر + بديل أرخص فعلياً (>=15%) + ما انعمل مرة ← alternative_first
#   (الخصم مرفوض هالدور بسبب alternative_first)؛ بعد ما ينعمل مرة والزبون لسا معترض ← الكود مسموح.
#
# مفاتيح الحالة (Conversation.state["sales"]):
#   alt_first_done: bool — انعرض بديل أرخص مرة بهالمحادثة.
# =====================================================================================

PRICE_OBJECTIONS = frozenset({"price", "competitor_price"})
SOFT_OBJECTIONS = frozenset({"quality", "fit", "trust", "shipping"})   # بتنعالج أولاً، مش محفّز خصم لحالها
SLOT_PRIORITY = ("category", "occasion", "color", "size")
SLOT_LABELS = {
    "category": "what kind of item they are looking for",
    "occasion": "the occasion it is for",
    "color": "the color they prefer",
    "size": "their size",
    "budget": "their budget",
}
MAX_QUESTIONS = 2
ALT_MIN_SAVING = Decimal("0.15")
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
        "hesitation_reason": s.get("hesitation_reason") or "",
        "asks_discount": bool(s.get("asks_discount")),
        "wants_alternative": bool(s.get("wants_alternative")),
    }


def _sales_state(conversation_state):
    state = conversation_state.get("sales") if isinstance(conversation_state, dict) else None
    return state if isinstance(state, dict) else {}


def _price(product):
    try:
        return Decimal(str(product.price))
    except (ArithmeticError, ValueError, AttributeError):
        return None


def cheaper_alternative(engaged_product, search_result, requirements=None):
    """أول مرشّح حقيقي أرخص بوضوح (>= 15%) من منتج الزبون ويحترم المتطلبات، أو None.
    exact أولاً؛ غير exact بنقبله بس لو التساهل مش بالفئة/المقاس/اللون/الميزانية."""
    if engaged_product is None or search_result is None:
        return None
    base = _price(engaged_product)
    if base is None or base <= 0:
        return None
    ceiling = base * (Decimal("1") - ALT_MIN_SAVING)
    excluded = getattr(requirements, "excluded_skus", frozenset())
    budget_max = getattr(requirements, "budget_max", None)
    allow_oos = bool(getattr(requirements, "include_out_of_stock", False))

    def eligible(cand):
        product = cand.product
        price = _price(product)
        if price is None or price > ceiling or product.sku == engaged_product.sku or product.sku in excluded:
            return False
        if budget_max is not None and price > budget_max:
            return False
        if not allow_oos and getattr(product, "in_stock", True) is False:
            return False
        return cand.exact or not (set(cand.relaxed) & {"category", "size", "color", "budget"})

    pool = list(search_result.candidates or [])
    for cand in [c for c in pool if c.exact] + [c for c in pool if not c.exact]:
        if eligible(cand):
            return cand
    return None


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


def _discount_verdict(*, stage, readiness, sig, objections, engaged_product, turn_no, shown_count,
                      discount_codes, offer_history, alt_first_available, alt_first_done, now):
    now = now or timezone.now()
    valid = valid_codes(discount_codes, now)
    types = [t for t, _sku in objections]
    reason = sig["hesitation_reason"]
    price_open = any(t in PRICE_OBJECTIONS for t in types)
    timing_open = "timing" in types
    explained_by_soft = bool(types) and set(types) <= SOFT_OBJECTIONS and reason not in ("price", "other")
    hesitation = shown_count > 0 and (
        reason in ("price", "other")
        or timing_open
        or (readiness == "hesitant" and reason == "" and not explained_by_soft)
    )
    engaged = engaged_product is not None
    asked = (
        sig["asks_discount"] and engaged
        and (readiness in ("interested", "hesitant") or sig["intent"] in ("accept", "compare"))
    )
    trigger = "price_objection" if price_open else "hesitation" if hesitation else "asked" if asked else ""

    trace = {
        "codes_total": len(discount_codes), "codes_valid": len(valid), "stage": stage, "readiness": readiness,
        "engaged_sku": getattr(engaged_product, "sku", None), "turn_no": turn_no, "shown_count": shown_count,
        "offer_history": bool(offer_history), "price_objection": price_open, "hesitation": hesitation,
        "asks_discount": sig["asks_discount"], "alt_first_available": alt_first_available,
        "alt_first_done": alt_first_done, "trigger": trigger, "open_objections": sorted(set(types)),
    }

    def deny(why):
        trace["denied_by"] = why
        return contracts.DiscountVerdict(allowed=False, code=None, reason=why, trigger=trigger, rule_trace=trace)

    if not valid:
        active = [c for c in discount_codes if c.is_active]
        return deny("expired" if any(c.expires_at and c.expires_at <= now for c in active) else "no_codes")
    if not engaged:
        return deny("not_engaged")
    if turn_no < 2:
        return deny("too_early")
    if offer_history:
        return deny("already_offered")
    if stage == "discovery":
        return deny("not_engaged")
    if not trigger:
        return deny("ready_no_price_concern" if readiness == "ready" else "no_trigger")
    if trigger == "price_objection" and alt_first_available and not alt_first_done:
        return deny("alternative_first")
    trace["selected_code_id"] = valid[0].id
    return contracts.DiscountVerdict(allowed=True, code=valid[0], reason="ok", trigger=trigger, rule_trace=trace)


_ALWAYS = (
    "Never pressure or rush the customer; never claim scarcity, urgency or a deadline unless the catalog data says so.",
    "Never invent product facts, prices, stock or promotions: use only what the catalog data shows.",
)


def _stage_directives(stage, *, ask_slot, forbid, search_result, objections, readiness):
    d = []
    if stage == "discovery":
        if "ask_question" in forbid:
            d.append("Do NOT ask any question this turn: enough questions were asked already. Show 1-2 concrete products "
                     "from the catalog instead, each with a short reason taken from catalog data.")
        elif ask_slot:
            d.append(f"Ask at most ONE short, friendly question about {SLOT_LABELS[ask_slot]}; "
                     "nothing else this turn, no interrogation.")
        else:
            d.append("You know enough about what they want: show 1-2 best matches from the catalog "
                     "with a short concrete reason each.")
    elif stage == "recommendation":
        d.append("Show the 1-2 best matches (never more), each with a concrete reason from catalog data "
                 "(color, size availability, category, a real sale price).")
        note = getattr(search_result, "note", "") if search_result else ""
        if note == "no_exact_match":
            d.append("The exact match is not available: say so honestly, then show the closest option and what differs.")
        elif note == "nothing_matches":
            d.append("Nothing matches exactly right now: say so warmly and show the nearest real options.")
    elif stage == "objection_handling":
        d.append("Acknowledge the concern in a few words, with no apology and no long preamble.")
        types = {t for t, _s in objections}
        if types & PRICE_OBJECTIONS:
            d.append("Reframe the value concretely from catalog facts (material, cut, versatility, a real sale price); "
                     "never invent savings.")
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
    elif stage == "closing":
        d.append("Make buying effortless: point to the buy button, confirm the size if needed, "
                 "keep the message short and warm.")
        d.append("No upsell and no extra questions that delay the purchase.")
    if readiness == "hesitant" and stage != "objection_handling":
        d.append("The customer seems unsure: acknowledge it lightly and give one concrete reassurance.")
    return d


def decide(*, stage, signals, readiness, objections, engaged_product, turn_no, shown_count, known_slots,
           questions_asked_in_conversation, requirements, discount_codes, offer_history, search_result=None,
           conversation_state=None, now=None):
    """استراتيجية الدور. دالة نقية: نفس المدخلات ← نفس المخرجات، بدون DB (كل شي بيجي جاهز)."""
    stage = stage if stage in contracts.STAGES else "discovery"
    readiness = readiness if readiness in contracts.READINESS else "browsing"
    sig = _signals(signals)
    objections = [(t, s) for t, s in (objections or ())]
    known_slots = set(known_slots or ())
    alt_done = bool(_sales_state(conversation_state).get("alt_first_done"))

    price_open = any(t in PRICE_OBJECTIONS for t, _s in objections)
    alternative = cheaper_alternative(engaged_product, search_result, requirements) if price_open else None
    alt_first_available = alternative is not None
    alternative_first = alt_first_available and not alt_done

    verdict = _discount_verdict(
        stage=stage, readiness=readiness, sig=sig, objections=objections, engaged_product=engaged_product,
        turn_no=turn_no, shown_count=shown_count, discount_codes=list(discount_codes or ()),
        offer_history=offer_history, alt_first_available=alt_first_available, alt_first_done=alt_done, now=now,
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

    directives = _stage_directives(
        stage, ask_slot=ask_slot, forbid=forbid, search_result=search_result, objections=objections,
        readiness=readiness,
    )
    if stage == "closing" and ask_slot == "size":
        directives.append("If the size is not settled, ask which size they want in the same short message as the buy step.")

    if verdict.allowed:
        directives.append("Present the approved code from the APPROVED OFFER block as a good reason to decide on this piece: "
                          "use only the merchant's own description, no calculated prices, no other promotions, "
                          "no free shipping, no deadline.")
    else:
        directives.append("Do not offer, hint at or promise any discount, code or special price this turn.")
        if alternative_first:
            directives.append("The customer is price-sensitive: first show ONE clearly cheaper real alternative from the "
                              f"catalog (sku {alternative.product.sku}) and say what makes it a good fit; "
                              "do not mention discounts.")
        asks = sig["asks_discount"]
        if asks and verdict.reason in ("not_engaged", "too_early", "no_trigger"):
            directives.append("They asked about discounts before choosing anything: say warmly you will look for the best "
                              "deal once they pick something they like, and mention only REAL sale items (those with a "
                              "catalog compare-at price); never invent a code, percentage or promotion.")
        elif asks and verdict.reason in ("no_codes", "expired"):
            directives.append("There is no extra code to give right now: do not claim otherwise; say so warmly and point "
                              "to the product's real value and any REAL sale items.")
        elif asks and verdict.reason == "already_offered":
            directives.append("A code was already shared in this conversation: do not offer another one; "
                              "remind them of it only if they ask.")
        elif asks and verdict.reason == "ready_no_price_concern":
            directives.append("If they ask about discounts, do not offer a code: keep the answer brief and warm, "
                              "point to the product's value and move to the buy step.")
        price_side = (price_open or asks or sig["hesitation_reason"] in ("price", "other")
                      or readiness == "hesitant" or verdict.reason in ("no_codes", "expired"))
        if price_side:
            directives.append("Keep selling constructively: reframe the value with concrete catalog facts, offer a cheaper "
                              "real alternative if one exists, and reassure them; stay helpful and keep the conversation open.")
    directives.extend(_ALWAYS)

    return contracts.Strategy(
        stage=stage, readiness=readiness, directives=directives, ask_slot=ask_slot, forbid=forbid,
        alternative_first=alternative_first, discount=verdict,
        state_updates={"sales": {"alt_first_done": True}} if alternative_first else {},
    )
