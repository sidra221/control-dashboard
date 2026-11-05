"""دور محادثة v2 كامل: جلسة موقّعة ← رسالة محفوظة ← تحليل + تحقق ← ذاكرة ← قيود ← إحاطة ←
رد (نفس محرّك v1 `_run_turn` بالمنطق والتدقيق) ← حفظ.

السيرفر هو مصدر الحقيقة: التاريخ، المنتجات المعروضة، التفضيلات، الرفض، وحالة الخصم كلها
من قاعدة البيانات — ما بنصدّق ولا بنقرا أي تاريخ/هوية من العميل.
"""

import logging
import re
import time

from django.db.models import F
from django.utils import timezone

from .. import widget_views as wv
from ..models import ProductReaction
from . import llm, memory, understanding

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
        payload, error = _process(ctx, merchant, text, client_msg_id, page_url, started)
        return payload, error, (memory.issue_tokens(ctx) if error else None)
    finally:
        memory.release_turn(conversation)


def _process(ctx, merchant, text, client_msg_id, page_url, started):
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
