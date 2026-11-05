"""سياسة العروض — الكود هو اللي بيقرر إذا مسموح نعرض كود خصم، مش الموديل.

هاد النسخة الأولى (v1) بتعتمد على اللي بيوصل بالطلب فقط (تاريخ المحادثة
وsku المعروضة من الودجت، وكلها من العميل فبتقدر تقلّل العروض بس ما بتحمي
من تزوير). النسخة الكاملة بحالة سيرفر ومراحل البيع بتيجي بمرحلة لاحقة وبتحل
محل هالدالة.
"""

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
