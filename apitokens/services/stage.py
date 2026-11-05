"""آلة مراحل البيع (v2) — حتمية بالكامل، بدون موديل وبدون DB.

المراحل (contracts.STAGES): discovery ← recommendation ← objection_handling / persuasion ← closing.
المدخلات كلها بيانات جاهزة (إشارات المحلّل + أحداث موثوقة من الودجت + اعتراضات مفتوحة)،
فالدالة قابلة للاختبار بجداول.

القواعد (بالترتيب — أول قاعدة بتنطبق بتحسم):

 R0  تطبيع: previous غير معروف ← discovery. الإشارات الناقصة ← الافتراضي.
 R1  حدث موثوق (buy_click / add_to_cart) ← closing، إلا إذا في اعتراض مفتوح ← objection_handling.
     الأحداث أقوى من المحلّل: ما بيقدر يلغيها غير (أ) اعتراض مفتوح، (ب) رفض/طلب بديل صريح هالدور
     (الزبون حطّ بالسلة وبعدين قال "وريني غيره" ← recommendation).
 R2  اعتراض مفتوح ← objection_handling. (الاعتراض أقوى من طلب البديل: "غالي، عندك أرخص؟" بتنعالج
     بمرحلة الاعتراض اللي بتعرض البديل الأرخص الحقيقي.)
 R3  رفض هالدور (intent=reject) أو طلب بديل (wants_alternative) ← recommendation،
     أو discovery إذا ما في شي معروف (ولا منتج انعرض ولا تفاعل).
 R4  تردّد هالدور (readiness=hesitant أو hesitation_reason) بعد ما انعرض منتج ← objection_handling.
     بدون منتج معروض بيضل بـ discovery (ما في شي نعالج اعتراضه).
 R5  جاهز (readiness=ready أو intent=buy) وفي منتج معروض/تفاعل ← closing.
     بدون منتج: intent=search ← recommendation، غير هيك ← discovery.
 R6  مهتم بعد العرض (liked/asked/tried_on، أو liked_or_asked_count>0، أو readiness=interested،
     أو intent=accept) وبدون اعتراض ← persuasion (إلا لو previous=closing: R7 بتسبقها).
 R7  هستيريسيس ("لا رفرفة"): لو الإشارة محايدة (دردشة، سؤال معلومات، browsing) بنضل بالمرحلة السابقة
     إذا كانت persuasion أو closing. من closing ما بنرجع إلا باعتراض مفتوح (R2) أو رفض/طلب بديل (R3)
     أو تردّد صريح (R4). objection_handling بتخلص لما الاعتراض ينحل: persuasion إذا في اهتمام، وإلا
     recommendation.
 R8  الافتراضي: ما انعرض شي ← discovery (إلا intent=search ← recommendation)؛ انعرض شي ← recommendation.
"""

from .contracts import READINESS, STAGES

BUY_EVENTS = frozenset({"buy_click", "add_to_cart"})
ENGAGED_EVENTS = frozenset({"liked", "asked", "tried_on"})
HESITANT_REASONS = frozenset({"price", "style", "fit", "trust", "timing", "other"})


def _norm_signals(signals):
    s = signals if isinstance(signals, dict) else {}
    readiness = s.get("readiness") or ""
    return {
        "intent": s.get("intent") or "other",
        "readiness": readiness if readiness in READINESS else "",
        "hesitation_reason": s.get("hesitation_reason") or "",
        "wants_alternative": bool(s.get("wants_alternative")),
    }


def _clean(events, open_objections):
    return frozenset(events or ()), [o for o in (open_objections or ()) if o]


def next_stage(*, previous, signals, open_objections, shown_count, liked_or_asked_count, events, turn_no,
               wants_alternative):
    prev = previous if previous in STAGES else "discovery"
    sig = _norm_signals(signals)
    events, objections = _clean(events, open_objections)

    explicit_reject = sig["intent"] == "reject"
    alt = bool(wants_alternative) or sig["wants_alternative"]
    hesitating = sig["readiness"] == "hesitant" or sig["hesitation_reason"] in HESITANT_REASONS
    wants_to_buy = sig["readiness"] == "ready" or sig["intent"] == "buy"
    shown = shown_count > 0
    known_something = shown or liked_or_asked_count > 0 or bool(events)
    engaged = bool(events & ENGAGED_EVENTS) or liked_or_asked_count > 0

    # R1 — أحداث موثوقة
    if events & BUY_EVENTS and not (explicit_reject or alt):
        return "objection_handling" if objections else "closing"

    # R2 — اعتراض مفتوح
    if objections:
        return "objection_handling"

    # R3 — رفض / بديل
    if explicit_reject or alt:
        return "recommendation" if known_something else "discovery"

    # R4 — تردّد
    if hesitating:
        return "objection_handling" if (shown or engaged) else "discovery"

    # R5 — جاهز
    if wants_to_buy:
        if shown or engaged:
            return "closing"
        return "recommendation" if sig["intent"] == "search" else "discovery"

    # R7a — من closing ما في رجوع على إشارة محايدة/إيجابية (حتى لو في liked/asked قديمة)
    if prev == "closing" and (shown or engaged):
        return "closing"

    # R6 — اهتمام
    interested = engaged or sig["readiness"] == "interested" or sig["intent"] == "accept"
    if interested and (shown or engaged):
        return "persuasion"

    # R7b — persuasion بتضل على إشارة محايدة
    if prev == "persuasion" and (shown or engaged):
        return prev

    # R8 — الافتراضي
    if not shown:
        return "recommendation" if sig["intent"] == "search" else "discovery"
    return "recommendation"


def readiness_of(signals, events, open_objections):
    """جاهزية الزبون (contracts.READINESS) بنفس فلسفة next_stage: الأحداث الموثوقة والاعتراضات
    المفتوحة أقوى من تخمين المحلّل؛ والرفض الصريح بيسقط "ready"."""
    sig = _norm_signals(signals)
    events, objections = _clean(events, open_objections)
    rejecting = sig["intent"] == "reject" or sig["wants_alternative"]

    if objections:
        return "hesitant"
    if events & BUY_EVENTS and not rejecting:
        return "ready"
    if sig["readiness"] == "hesitant" or sig["hesitation_reason"] in HESITANT_REASONS:
        return "hesitant"
    if not rejecting and (sig["readiness"] == "ready" or sig["intent"] == "buy"):
        return "ready"
    if events & ENGAGED_EVENTS or sig["readiness"] == "interested" or sig["intent"] == "accept":
        return "interested"
    return "browsing"
