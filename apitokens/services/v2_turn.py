"""دور محادثة v2 كامل: جلسة موقّعة ← رسالة محفوظة ← تحليل + تحقق ← ذاكرة ← قيود ← إحاطة ←
رد (نفس محرّك v1 `_run_turn` بالمنطق والتدقيق) ← حفظ.

السيرفر هو مصدر الحقيقة: التاريخ، المنتجات المعروضة، التفضيلات، الرفض، وحالة الخصم كلها
من قاعدة البيانات — ما بنصدّق ولا بنقرا أي تاريخ/هوية من العميل.
"""

import dataclasses
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .. import widget_views as wv
from ..models import Message, ProductReaction
from . import contracts, llm, memory, understanding
from . import policy as policy_mod

# خيط واحد يكفي: المحلّل استدعاء شبكة بس (ما بيلمس القاعدة)، والبائع بيشتغل بالخيط الرئيسي.
_ANALYZER_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="v2-analyzer")

logger = logging.getLogger(__name__)

CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def _worth_analyzing(text):
    return len(text.strip()) >= 2 and any(ch.isalnum() for ch in text)


def run_chat(merchant, body):
    """(payload, None, None) أو (None, (error_code, status), extra). `extra` = توكنات الجلسة
    حتى لو فشل الدور (مثلاً LLM واقع) — بهالشكل إعادة المحاولة بتكمل بنفس الجلسة والرسالة
    المحفوظة بدل ما تبدأ جلسة جديدة."""
    started = time.monotonic()
    text = body.get("message")
    if not isinstance(text, str) or not text.strip():
        return None, ("message_required", 400), None
    text = text.strip()[: wv.MAX_MESSAGE_CHARS]
    raw_id = str(body.get("client_msg_id") or "")
    client_msg_id = raw_id if CLIENT_ID_RE.match(raw_id) else ""
    page_url = str(body.get("page_url") or "")[:2000]

    provider = llm.get_provider()
    if not provider.is_configured():
        return None, ("assistant_not_configured", 503), None

    ctx = memory.resolve_session(merchant, body.get("session_token"), body.get("shopper_token"), create=True)
    conversation = ctx.conversation

    cached = memory.stored_reply(conversation, client_msg_id)
    if cached is not None:  # إعادة إرسال بعد انقطاع: نفس الرد، بدون LLM
        return {**cached, **memory.issue_tokens(ctx), "replayed": True}, None, None

    if not memory.acquire_turn(conversation):
        return None, ("turn_in_progress", 409), memory.issue_tokens(ctx)
    try:
        process = _process_legacy if settings.V2_SELLER_MODE == "legacy" else _process_loop
        payload, error = process(ctx, merchant, text, client_msg_id, page_url, started)
        return payload, error, (memory.issue_tokens(ctx) if error else None)
    finally:
        memory.release_turn(conversation)


def _process_legacy(ctx, merchant, text, client_msg_id, page_url, started):
    conversation = ctx.conversation
    now = timezone.now()

    user_message = memory.find_user_message(conversation, client_msg_id)
    if user_message is None:
        memory.add_message(conversation, "user", text, client_msg_id=client_msg_id)
        from ..models import Conversation

        Conversation.objects.filter(pk=conversation.pk).update(
            turn_count=F("turn_count") + 1, last_activity_at=now
        )
        conversation.turn_count += 1
    turn_no = conversation.turn_count

    page_product = wv._find_page_product(merchant, page_url)
    if page_product is not None:
        memory.record_view(ctx, page_product, turn_no)

    window = memory.recent_window(conversation)
    before = memory.compile_constraints(ctx, merchant.currency)
    refs = memory.recent_product_refs(ctx, page_product)
    products_by_sku = {p.sku: p for p in merchant.products.filter(sku__in=list(refs))}

    viewing_line = ""
    if page_product is not None:
        last_reply = memory.last_assistant_time(conversation)
        opened_after = memory.viewed_after(conversation, page_product.sku, last_reply)
        viewing_line = (
            f"{page_product.sku} — {page_product.title} — {page_product.price}"
            + (" (OPENED AFTER the assistant's last message)" if opened_after else " (already open during the assistant's last message)")
        )

    trace = {"analyzer_called": False}
    validated = understanding.Validated()
    llm_calls = 0
    if _worth_analyzing(text):
        trace["analyzer_called"] = True
        llm_calls += 1
        raw = understanding.analyze(
            llm.get_provider(role="analyzer"), window, memory.memory_lines_for_analyzer(ctx, before),
            "\n".join(f"{sku} — {title} — {price}" for sku, (title, price) in refs.items()),
            viewing_line,
        )
        trace["analyzer_ok"] = raw is not None
        if raw is not None:
            validated = understanding.validate(
                raw,
                user_texts=[m["content"] for m in window if m["role"] == "user"],
                products_by_sku=products_by_sku,
                merchant_currency=merchant.currency,
            )
    changes = memory.apply_validated(ctx, validated, products_by_sku, turn_no)
    conversation.save(update_fields=["state"])

    constraints = memory.compile_constraints(ctx, merchant.currency)
    exempt = frozenset(r["sku"] for r in validated.reactions if r["type"] in ("asked", "liked"))
    shown = memory.shown_skus(conversation)
    if page_product is not None and turn_no == 1 and page_product.sku not in shown:
        # الودجت فتح المحادثة بكرت هالمنتج (ترحيب صفحة المنتج): معروض للزبون، ما نكرّره.
        memory.record_reaction(ctx, page_product, "shown", turn_no)
        shown = shown + [page_product.sku]
    engaged = (
        bool(shown) or page_product is not None
        or ProductReaction.objects.filter(
            conversation=conversation, reaction__in=("asked", "liked", "tried_on", "viewed")
        ).exists()
    )
    page_note = None
    if page_product is not None and not ctx.new_conversation and turn_no > 1:
        page_note = (
            f"\nThe customer is now viewing the product page of {page_product.sku} ({page_product.title}). "
            "It may not be the item you were discussing: treat \"it\"/\"this\" as this product only when their "
            f"message points to it. Don't recommend {page_product.sku} again unless they want to buy it.\n"
        )

    turn = wv.TurnInput(
        merchant=merchant, window=window, full_messages=window, session_id=conversation.session_id,
        shown_skus=shown, page_url=page_url,
        memory_section=memory.build_memory_section(ctx, constraints, merchant.currency),
        page_note=page_note,
        excluded_skus=frozenset(constraints.excluded), exempt_skus=exempt,
        budget_max=float(constraints.budget_max) if constraints.budget_max is not None else None,
        use_memory_budget=True, strict_budget=True,
        gate={"user_turns": turn_no, "engaged": engaged,
              "already_offered": memory.offer_history(ctx.profile, conversation)},
        enforce_saudi=True, persist_log=False, trace=trace,
        # رفض صريح ← ما بنعتبر كلمات الشراء إشارة شراء (مثلاً "ما أبيه")
        suppress_purchase_intent=any(r["type"] == "rejected" for r in validated.reactions),
    )
    payload, error = wv._run_turn(turn)
    if error:
        return None, error

    product_map = {p.sku: p for p in merchant.products.filter(sku__in=[c["sku"] for c in payload["recommendations"]])}
    for card in payload["recommendations"]:
        memory.record_reaction(ctx, product_map.get(card["sku"]) or card["sku"], "shown", turn_no,
                               title=card.get("title", ""))
    stored = {k: payload[k] for k in ("reply", "recommendation", "recommendations", "discount_offer", "size_advice")}
    memory.add_message(
        conversation, "assistant", payload["reply"], meta={"reply_to": client_msg_id, "response": stored}
    )
    skus = list(conversation.recommended_skus)
    skus.extend(c["sku"] for c in payload["recommendations"] if c["sku"] not in skus)
    conversation.recommended_skus = skus
    fields = ["recommended_skus"]
    if payload["discount_offer"] and not conversation.discount_code:
        conversation.discount_code = payload["discount_offer"]["code"]
        fields.append("discount_code")
    conversation.save(update_fields=fields)

    memory.save_trace(
        ctx, turn_no,
        analyzer={"summary": validated.summary(), **{k: v for k, v in trace.items() if k.startswith("analyzer")}},
        memory_changes=changes,
        constraints={
            "budget_max": str(constraints.budget_max) if constraints.budget_max is not None else None,
            "excluded": sorted(constraints.excluded), "pending": constraints.pending,
            "exempt": sorted(exempt), "gate": turn.gate,
        },
        guard={k: v for k, v in trace.items() if k.startswith(("guard", "dialect"))},
        llm_calls=llm_calls + trace.get("llm_calls", 0),
        latency_ms=int((time.monotonic() - started) * 1000),
        models_used={"seller": getattr(llm.get_provider(), "model", ""),
                     "analyzer": getattr(llm.get_provider(role="analyzer"), "model", "")},
    )
    return {**payload, **memory.issue_tokens(ctx), "turn": turn_no}, None


# ======================================================================
# المسار الجديد: فهم ← متطلبات صلبة ← مرشّحين ← مرحلة بيع وسياسة ← بائع بحلقة أدوات
# ======================================================================

CLOSE_EVENTS = ("tried_on", "buy_click", "add_to_cart")
SLOT_KEYS = {"category": "category", "occasion": "occasion", "color": "color", "size": "size", "budget_max": "budget"}


ALT_FIRST_WARN_AFTER = 2   # بعد كم محاولة فاشلة لعرض البديل بنسجّل تحذير (مراقبة فقط، ما بيفتح الخصم)


@dataclasses.dataclass
class Plan:
    """كل شي بيعتمد عليه الرد. بنبنيه قبل التحليل (speculative) وبعده؛ لو تطابقوا بنستخدم
    رد البائع المبكر بدون إعادة (النتيجة نفسها بالضبط، أسرع)."""

    requirements: object
    strategy: object
    search: object
    memory_section: str
    approved_offer: object
    stage: str
    readiness: str
    engaged: object = None   # منتج الزبون؛ بيحتاجه التحقق من إنه انعرض بديل أرخص فعلاً
    shared_offer: object = None   # كود انعرض أصلاً بهالمحادثة (تذكير بس، مش عرض جديد)
    asks_discount: bool = False

    def same_as(self, other):
        def sig(p):
            return (
                p.requirements,
                dataclasses.replace(p.strategy, state_updates={}),
                [(c.product.sku, c.exact) for c in p.search.candidates],
                p.search.note,
                p.memory_section,
                p.approved_offer,
                p.shared_offer,
            )

        return sig(self) == sig(other)


def _engaged_product(ctx, page_product, merchant):
    if page_product is not None:
        return page_product
    last = (
        ProductReaction.objects.filter(
            conversation=ctx.conversation, reaction__in=("liked", "asked", "tried_on", "shown", "viewed")
        ).order_by("-id").first()
    )
    return merchant.products.filter(sku=last.sku).first() if last else None


def _interested_product(ctx, merchant, signals):
    """المنتج يلي عليه اهتمام حقيقي (للخصم): إعجاب/سؤال/تجربة/ضغطة شراء/سلة، أو "آخذه" على آخر منتج
    انعرض. منتج الصفحة أو المُشاهَد أو المعروض لحاله ما بيعدّ."""
    skus = memory.interest_skus(ctx.conversation, ctx.profile)
    sku = skus[0] if skus else None
    if sku is None and (signals or {}).get("intent") in ("buy", "accept"):
        sku = memory.last_shown_sku(ctx.conversation, ctx.profile)
    return merchant.products.filter(sku=sku).first() if sku else None


def _build_plan(ctx, merchant, *, validated, window, text, page_product, shown, turn_no, products):
    from . import policy, recommend, stage as stage_mod

    conversation = ctx.conversation
    facts = memory.active_facts(ctx.profile)
    excluded = memory.excluded_skus(ctx.profile)
    exempt = frozenset(r["sku"] for r in validated.reactions if r["type"] in ("asked", "liked")) if validated else frozenset()
    requirements = recommend.requirements_from_facts(facts, excluded_skus=excluded, exempt_skus=exempt)

    reactions = list(ProductReaction.objects.filter(conversation=conversation).values_list("reaction", flat=True))
    events = {r for r in reactions if r in ("tried_on", "buy_click", "add_to_cart", "liked", "asked", "rejected")}
    shown_count = len(set(shown))
    liked_or_asked = sum(1 for r in reactions if r in ("liked", "asked"))
    objections = memory.open_objections(conversation)
    signals = validated.signals if validated else {
        "intent": "other", "readiness": "", "hesitation_reason": "", "asks_discount": False, "wants_alternative": False,
    }

    state = dict(conversation.state or {})
    stage = stage_mod.next_stage(
        previous=state.get("stage"), signals=signals, open_objections=[o.type for o in objections],
        shown_count=shown_count, liked_or_asked_count=liked_or_asked, events=events, turn_no=turn_no,
        wants_alternative=bool(signals.get("wants_alternative")),
    )
    readiness = stage_mod.readiness_of(signals, events, [o.type for o in objections])

    query_bits = [text]
    for fact in facts:
        if fact.key in ("occasion", "style", "material", "brand") and fact.polarity == "like" and fact.source == "explicit":
            query_bits.append(fact.value)
    search = recommend.search(products, requirements, query=" ".join(query_bits), limit=8, shown_skus=shown)
    engaged = _engaged_product(ctx, page_product, merchant)
    interested = _interested_product(ctx, merchant, signals)
    focus = interested or engaged
    price_signal = (
        any(o.type in ("price", "competitor_price") for o in objections)
        or signals.get("hesitation_reason") == "price"
        or bool(signals.get("wants_alternative"))
    )
    if price_signal and focus is not None:
        # "فيه أرخص؟": الأرخص من منتجه أولاً (بالأرخص فالأرخص)، مش بالصلة النصية — وإلا البائع ما بيشوف أرخص قطعة.
        base = focus.price
        search.candidates.sort(key=lambda c: (c.product.price >= base, c.product.price, c.product.sku))

    known_slots = set()
    for fact in facts:
        if fact.source == "explicit" and fact.polarity == "like" and fact.key in SLOT_KEYS and fact.subject in ("self", "gift"):
            known_slots.add(SLOT_KEYS[fact.key])
    assistant_questions = sum(
        1 for content in Message.objects.filter(conversation=conversation, role="assistant").values_list("content", flat=True)
        if "?" in content or "؟" in content
    )
    # اعتراض سعري "حالي" = اقتباسه برسالة الزبون هالدور؛ اعتراض قديم أعاده المحلّل مع السياق ما بيعدّ.
    price_now = bool(validated) and any(
        o.get("type") in policy.PRICE_OBJECTIONS and o.get("current", True) for o in validated.objections
    )
    strategy = policy.decide(
        stage=stage, signals=signals, readiness=readiness,
        objections=[(o.type, o.sku) for o in objections],
        engaged_product=interested, turn_no=turn_no,
        shown_count=shown_count, known_slots=known_slots,
        questions_asked_in_conversation=assistant_questions, requirements=requirements,
        discount_codes=list(merchant.discount_codes.all()), offer_history=memory.offer_history(ctx.profile, conversation),
        search_result=search, conversation_state=state, now=timezone.now(),
        price_now=price_now, shown_skus=frozenset(shown),
    )
    approved = None
    if strategy.discount.allowed and strategy.discount.code is not None:
        approved = {"code": strategy.discount.code.code, "description": strategy.discount.code.description}
    constraints = memory.compile_constraints(ctx, merchant.currency)
    memory_section = memory.build_memory_section(ctx, constraints, merchant.currency)
    shared = None
    if approved is None and conversation.discount_code and signals.get("asks_discount"):
        # عُرض كود بهالمحادثة والزبون بيسأل عن خصم هالدور: بيضل مسموح نذكره (بنفس وصف التاجر) بدون عرض جديد؛ بغير هيك ما بنعرضه للموديل.
        code = next((d for d in merchant.discount_codes.all() if d.code == conversation.discount_code), None)
        if code is not None:
            shared = {"code": code.code, "description": code.description}
    return Plan(requirements, strategy, search, memory_section, approved, stage, readiness, interested, shared,
                bool(signals.get("asks_discount"))), constraints


NEEDS_SEARCH_INTENTS = frozenset({"search", "compare", "reject", "browse"})


def _needs_search(validated, analyzer_ok):
    """هل الزبون ممكن يحتاج منتجات جديدة/إضافية؟ (وقتها أداة البحث لازم تبقى ظاهرة.) بنفترض نعم
    لو التحليل فشل أو ما فهمنا النية — الإخفاء مسموح بس لما نكون متأكدين."""
    if not analyzer_ok or validated is None:
        return True
    sig = validated.signals or {}
    return (
        sig.get("intent") in NEEDS_SEARCH_INTENTS or sig.get("intent") in (None, "", "other")
        or bool(sig.get("wants_alternative"))
        or any(r["type"] == "rejected" for r in validated.reactions)
    )


def _run_seller(ctx, merchant, plan, *, window, lang, shown, page_product, page_note, customer_texts,
                allow_search=True, reshow=frozenset()):
    from . import seller_v2

    return seller_v2.run_seller(
        contracts.SellerInput(
            merchant=merchant, window=window, lang=lang, memory_section=plan.memory_section,
            strategy=plan.strategy, requirements=plan.requirements, prefetched=plan.search,
            shown_skus=list(shown), page_product=page_product, page_note=page_note or "",
            store_info=merchant.store_info, currency=merchant.currency, approved_offer=plan.approved_offer,
            shared_offer=plan.shared_offer, asks_discount=plan.asks_discount, gender=ctx.profile.gender_signal or "",
            customer_texts=customer_texts, allow_search=allow_search, reshow_skus=frozenset(reshow),
        )
    )


def _reshow(validated, hidden_before):
    """منتجات كانت مخفية (مرفوضة) والزبون طلبها صراحة هالدور ← بنعيد عرض كرتها."""
    asked = {r["sku"] for r in (validated.reactions if validated else []) if r["type"] in ("asked", "liked")}
    return asked & set(hidden_before)


def _merge_state(state, updates):
    for key, value in (updates or {}).items():
        if isinstance(value, dict) and isinstance(state.get(key), dict):
            state[key] = {**state[key], **value}
        else:
            state[key] = value
    return state


def _process_loop(ctx, merchant, text, client_msg_id, page_url, started):
    conversation = ctx.conversation
    now = timezone.now()
    timings = {}

    user_message = memory.find_user_message(conversation, client_msg_id)
    if user_message is None:
        memory.add_message(conversation, "user", text, client_msg_id=client_msg_id)
        from ..models import Conversation

        Conversation.objects.filter(pk=conversation.pk).update(turn_count=F("turn_count") + 1, last_activity_at=now)
        conversation.turn_count += 1
    turn_no = conversation.turn_count

    page_product = wv._find_page_product(merchant, page_url)
    if page_product is not None:
        memory.record_view(ctx, page_product, turn_no)
    if page_product is not None and turn_no == 1 and not ProductReaction.objects.filter(
        conversation=conversation, sku=page_product.sku, reaction="shown"
    ).exists():
        # الودجت فتح المحادثة بكرت هالمنتج (ترحيب صفحة المنتج): معروض للزبون، ما نكرّره.
        memory.record_reaction(ctx, page_product, "shown", turn_no)

    window = memory.recent_window(conversation)
    lang = wv._customer_lang(window)
    customer_texts = [m["content"] for m in window if m["role"] == "user"]
    shown = memory.shown_skus(conversation)
    products = list(merchant.products.filter(in_stock=True))
    page_note = None
    if page_product is not None and not ctx.new_conversation and turn_no > 1:
        page_note = (
            f"\nThe customer is now viewing the product page of {page_product.sku} ({page_product.title}). "
            "It may not be the item you were discussing: treat \"it\"/\"this\" as this product only when their "
            f"message points to it. Don't recommend {page_product.sku} again unless they want to buy it.\n"
        )
    kw = dict(window=window, lang=lang, shown=shown, page_product=page_product, page_note=page_note,
              customer_texts=customer_texts)

    before_constraints = memory.compile_constraints(ctx, merchant.currency)
    refs = memory.recent_product_refs(ctx, page_product)
    products_by_sku = {p.sku: p for p in merchant.products.filter(sku__in=list(refs))}
    viewing_line = ""
    if page_product is not None:
        opened_after = memory.viewed_after(conversation, page_product.sku, memory.last_assistant_time(conversation))
        viewing_line = (
            f"{page_product.sku} — {page_product.title} — {page_product.price}"
            + (" (OPENED AFTER the assistant's last message)" if opened_after else " (already open during the assistant's last message)")
        )
    memory_lines = memory.memory_lines_for_analyzer(ctx, before_constraints)
    hidden_before = set(before_constraints.excluded)
    product_lines = "\n".join(
        f"{sku} — {title} — {price}" + (" [REJECTED earlier by the customer, hidden]" if sku in hidden_before else "")
        for sku, (title, price) in refs.items()
    )
    analyzer_provider = llm.get_provider(role="analyzer")
    worth = _worth_analyzing(text)

    def analyze():
        t0 = time.monotonic()
        raw = understanding.analyze(analyzer_provider, window, memory_lines, product_lines, viewing_line) if worth else None
        timings["analyzer_ms"] = int((time.monotonic() - t0) * 1000)
        return raw

    plan_args = dict(window=window, text=text, page_product=page_product, shown=shown, turn_no=turn_no, products=products)
    speculative = settings.V2_SPECULATIVE and worth
    early_plan = early_out = early_error = future = None
    try:
        if speculative:
            early_plan, _ = _build_plan(ctx, merchant, validated=None, **plan_args)
            future = _ANALYZER_POOL.submit(analyze)
            t0 = time.monotonic()
            try:
                early_out = _run_seller(ctx, merchant, early_plan, **kw)
            except llm.LLMError as exc:
                early_error = exc
            timings["early_seller_ms"] = int((time.monotonic() - t0) * 1000)
            raw = future.result()
        else:
            raw = analyze()

        validated = understanding.Validated()
        if raw is not None:
            validated = understanding.validate(
                raw, user_texts=customer_texts, products_by_sku=products_by_sku, merchant_currency=merchant.currency,
                current_text=text,
            )
        changes = memory.apply_validated(ctx, validated, products_by_sku, turn_no)
        sig = validated.signals or {}
        if sig.get("readiness") == "ready" or sig.get("intent") in ("buy", "accept"):
            # قرّر يشتري: اعتراضاته القديمة انحلّت (المحلّل ما بيسكّرها بنفسه دايماً)؛ يلي قاله بهالدور ("آخذه بس غالي") بيضل مفتوح.
            memory.resolve_open_objections(ctx.conversation, turn_no, keep_current=True)
        plan, constraints = _build_plan(ctx, merchant, validated=validated, **plan_args)

        reran = True
        if early_out is not None and early_plan.same_as(plan):
            out, reran = early_out, False
        elif early_error is not None and early_plan.same_as(plan):
            raise early_error
        else:
            t0 = time.monotonic()
            out = _run_seller(
                ctx, merchant, plan, allow_search=_needs_search(validated, raw is not None),
                reshow=_reshow(validated, hidden_before), **kw,
            )
            timings["seller_ms"] = int((time.monotonic() - t0) * 1000)
    except llm.LLMError:
        # المزوّد واقع: رسالة الزبون محفوظة، والإعادة بنفس client_msg_id بتكمل بدون تكرار.
        logger.warning("v2_seller_unavailable merchant=%s", merchant.pk, exc_info=True)
        return None, ("assistant_unavailable", 502)
    timings.setdefault("seller_ms", timings.get("early_seller_ms", 0))

    cards = out.recommendations
    product_map = {p.sku: p for p in products if p.sku in {c["sku"] for c in cards}}

    # "بديل أرخص أولاً" بيتحسب بس لما بطاقته انعرضت فعلاً بالرد يلي وصل للزبون — مش لما قررنا نطلبها من الموديل
    # (قرار سياسة، استدعاء أداة فاشل، رفض duplicate، حذف المدقّق أو عطل المزوّد ما بتعدّ). وبنحفظ العلامة
    # بنفس المعاملة يلي بتحفظ الرد.
    updates = plan.strategy.state_updates
    alt_shown = bool(plan.strategy.alternative_first) and policy_mod.alternative_shown(plan.engaged, product_map.values())
    if alt_shown:
        updates = _merge_state(dict(updates), {"sales": {"alt_first_done": True}})
    state = _merge_state(dict(conversation.state or {}), updates)
    state["stage"], state["readiness"] = plan.stage, plan.readiness
    sales_state = state.get("sales") if isinstance(state.get("sales"), dict) else {}
    alt_attempts = sales_state.get("alt_first_attempts", 0)
    alt_failed_repeatedly = bool(
        plan.strategy.alternative_first and not sales_state.get("alt_first_done") and alt_attempts >= ALT_FIRST_WARN_AFTER
    )
    if alt_failed_repeatedly:
        logger.warning("alt_first_not_shown merchant=%s conversation=%s attempts=%s", merchant.pk, conversation.pk, alt_attempts)

    with transaction.atomic():
        for card in cards:
            memory.record_reaction(ctx, product_map.get(card["sku"]) or card["sku"], "shown", turn_no, title=card.get("title", ""))
        payload = {
            "ok": True, "reply": out.reply,
            "recommendation": cards[0] if cards else None, "recommendations": cards,
            "discount_offer": out.discount_offer,
            "size_advice": {k: v for k, v in out.size_advice.items() if k != "text"} if out.size_advice else None,
        }
        stored = {k: payload[k] for k in ("reply", "recommendation", "recommendations", "discount_offer", "size_advice")}
        memory.add_message(conversation, "assistant", out.reply, meta={"reply_to": client_msg_id, "response": stored})
        skus = list(conversation.recommended_skus)
        skus.extend(c["sku"] for c in cards if c["sku"] not in skus)
        conversation.recommended_skus = skus
        conversation.state = state
        fields = ["recommended_skus", "state"]
        if out.discount_offer and not conversation.discount_code:
            conversation.discount_code = out.discount_offer["code"]
            fields.append("discount_code")
        conversation.save(update_fields=fields)

    total_ms = int((time.monotonic() - started) * 1000)
    timings.update({"total_ms": total_ms, "steps": out.steps, **{k: v for k, v in out.timings.items() if k != "seller_ms"}})
    req = plan.requirements
    verdict = plan.strategy.discount
    memory.save_trace(
        ctx, turn_no,
        analyzer={"summary": validated.summary(), "analyzer_called": worth, "analyzer_ok": raw is not None, "timings": timings},
        memory_changes=changes,
        constraints={
            "stage": plan.stage, "readiness": plan.readiness,
            "discount": {"allowed": verdict.allowed, "reason": verdict.reason, "trigger": verdict.trigger,
                         "rule_trace": verdict.rule_trace},
            "requirements": {
                "budget_max": str(req.budget_max) if req.budget_max is not None else None,
                "colors": list(req.colors), "avoid_colors": list(req.avoid_colors),
                "sizes": list(req.sizes), "categories": list(req.categories),
                "excluded": sorted(req.excluded_skus), "exempt": sorted(req.exempt_skus),
            },
            "candidates": [c.product.sku for c in plan.search.candidates],
            "search": {"exact": plan.search.exact_count, "relaxed": list(plan.search.relaxed_fields),
                       "dropped": plan.search.dropped, "note": plan.search.note},
            "speculative": {"enabled": bool(speculative), "reused": speculative and not reran},
            "tool_events": out.tool_events,
            "alt_first": {"decided": bool(plan.strategy.alternative_first), "shown": alt_shown,
                          "attempts": alt_attempts, "repeated_failure": alt_failed_repeatedly},
        },
        guard=out.guard,
        llm_calls=(1 if worth else 0) + (out.llm_calls if not reran else (early_out.llm_calls if early_out else 0) + out.llm_calls),
        latency_ms=total_ms,
        models_used={"seller": getattr(llm.get_provider(), "model", ""), "analyzer": getattr(analyzer_provider, "model", "")},
    )
    return {**payload, **memory.issue_tokens(ctx), "turn": turn_no}, None
