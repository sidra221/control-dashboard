"""فهم رسالة الزبون (v2): استدعاء "محلّل" صغير بيطلّع معلومات منظّمة (تفضيلات، ميزانية،
ردّ فعل على منتج، اعتراضات...) + تحقق صارم بالكود قبل ما أي شي يدخل الذاكرة.

مبدأ: الموديل بيقترح، الكود بيقرر. أي معلومة "صريحة" لازم يجي معها اقتباس حرفي من كلام
الزبون وبنتحقق منه — وإلا بتنزل لـ inferred (أو بتنرفض). الميزانية والمقاس والمستلم
ما بيدخلوا أبداً كاستنتاج. الرفض بيتطلب اقتباس (لأنه بيخفي المنتج عن الزبون).
"""

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal

from . import guard, llm

logger = logging.getLogger(__name__)

PREF_KEYS = (
    "budget_max", "budget_min", "category", "occasion", "color", "style", "material",
    "size", "fit", "brand", "recipient",
)
BUDGET_KEYS = frozenset({"budget_max", "budget_min"})
# قيمة وحدة فعّالة لكل (subject, key): القيمة الجديدة بتحل محل القديمة
SINGLE_VALUED = frozenset({"budget_max", "budget_min", "occasion", "recipient", "size", "fit"})
# بس هدول بيجوز استنتاجهم — الباقي (ميزانية، مقاس، مستلم) لازم الزبون يقوله
INFERABLE = frozenset({"color", "style", "occasion", "category", "material", "brand", "fit"})
REACTIONS = ("liked", "rejected", "asked", "neutral")
REASONS = ("price", "style", "color", "fit", "quality", "material", "other", "unknown")
OBJECTIONS = ("price", "competitor_price", "trust", "quality", "fit", "shipping", "timing", "other")
TOPICS = ("shipping", "returns", "payment", "size_guide", "materials", "stock", "discount", "try_on", "other")

MAX_VALUE_CHARS = 80
MAX_BUDGET = Decimal("1000000")

CURRENCY_ALIASES = {
    "SAR": "SAR", "SR": "SAR", "RIYAL": "SAR", "RIYALS": "SAR", "ريال": "SAR", "ريال سعودي": "SAR", "ر.س": "SAR",
    "USD": "USD", "$": "USD", "DOLLAR": "USD", "DOLLARS": "USD", "دولار": "USD",
    "AED": "AED", "درهم": "AED", "EUR": "EUR", "€": "EUR", "GBP": "GBP", "£": "GBP",
    "QAR": "QAR", "KWD": "KWD", "BHD": "BHD", "OMR": "OMR", "EGP": "EGP", "JOD": "JOD",
}
AMBIGUOUS_CURRENCY = {"دينار", "جنيه", "ليرة", "DINAR", "POUND", "LIRA"}


ANALYZE_TOOL = {
    "name": "update_customer_profile",
    "description": (
        "Record what the customer's own words establish in their latest message: preferences, "
        "budget, reaction to a specific product, objections, topics asked, withdrawn facts. "
        "Call it exactly once. Use empty lists when there is nothing new."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "preferences": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "enum": list(PREF_KEYS)},
                        "value": {"type": "string", "description": "Budgets: digits only (e.g. 200)."},
                        "currency": {"type": "string", "description": "Budgets only, and only if the customer named one (SAR for ريال, USD for دولار...)."},
                        "polarity": {"type": "string", "enum": ["like", "avoid"]},
                        "source": {"type": "string", "enum": ["explicit", "inferred"]},
                        "evidence": {"type": "string", "description": "Exact words copied from a customer message. Required for explicit."},
                        "subject": {"type": "string", "enum": ["self", "gift"]},
                    },
                    "required": ["key", "value", "source"],
                },
            },
            "retractions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "enum": list(PREF_KEYS)},
                        "value": {"type": "string", "description": "The withdrawn value; empty = every value of this key."},
                        "subject": {"type": "string", "enum": ["self", "gift"]},
                        "evidence": {"type": "string", "description": "Exact customer words withdrawing it."},
                    },
                    "required": ["key", "evidence"],
                },
            },
            "reaction": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string", "description": "Copied from the products list."},
                    "type": {"type": "string", "enum": list(REACTIONS)},
                    "reason_type": {"type": "string", "enum": list(REASONS)},
                    "reason_text": {"type": "string"},
                    "evidence": {"type": "string", "description": "Exact customer words (required for liked/rejected)."},
                },
                "required": ["sku", "type"],
            },
            "objections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": list(OBJECTIONS)},
                        "sku": {"type": "string"},
                        "evidence": {"type": "string", "description": "Exact customer words."},
                    },
                    "required": ["type", "evidence"],
                },
            },
            "resolved_objections": {"type": "array", "items": {"type": "string", "enum": list(OBJECTIONS)}},
            "questions_asked": {"type": "array", "items": {"type": "string", "enum": list(TOPICS)}},
            "gender": {"type": "string", "enum": ["female", "male", "unknown"]},
        },
    },
}

ANALYZER_SYSTEM = """You are the memory analyzer of an online store's sales assistant. You never talk \
to the customer. You read the conversation and record ONLY what the customer's own words establish.

Rules
- Only the customer's words count — never the assistant's suggestions. Earlier messages are only for \
resolving references like "it", "this one", "the second one".
- source="explicit" when the customer said it. You MUST copy the exact words into `evidence` \
(verbatim, in the customer's original script and spelling). If you cannot quote it, use \
source="inferred" — allowed only for color, style, occasion, category, material, brand, fit. NEVER \
infer a budget, a size or a recipient.
- Budget: budget_max for "up to / under / max / not more than / around N", budget_min for "at least N". \
value = digits only. Fill `currency` only if the customer named one (ريال/ريال سعودي → SAR, دولار → USD ...).
- Corrections ("actually", "no I meant", "change it to", "بدّلت رأيي", "لا قصدي") → emit the new \
preference as explicit; if it does not simply replace the old one (e.g. colors), also emit a \
retraction for the old value. "Forget that / ignore my budget" → retractions.
- Something for someone else (gift, family member) → subject="gift" and a `recipient` preference \
(value = the relation, e.g. "sister").
- `reaction`: only if the latest message reacts to ONE specific product from the products list: \
liked / rejected (+ reason_type) / asked (a question about it) / neutral. Copy its sku exactly. \
Resolving "it / this / هذا": if a product is marked "OPENED AFTER the assistant's last message", the customer \
is reacting to THAT page; if the page was already open, or none is marked, use the product the assistant \
just showed; if the customer names a product, use the named one.
- Rejection reasons: price (too expensive), style (not my taste), color, fit (size/cut), quality, \
material, other, unknown.
- objections: price, competitor_price (found it cheaper elsewhere), trust, quality, fit, shipping, \
timing ("I'll think about it"), other — with the customer's exact words as evidence. \
resolved_objections: types the customer now says are settled.
- questions_asked: topics the customer asked about in the latest message.
- Understand every Arabic dialect (Saudi, Gulf, Levantine/Syrian, Egyptian, Maghrebi), MSA, Arabizi and \
other languages. Quote in the original language.
- Nothing new → return empty lists. Never invent."""


# ---------------------------------------------------------------- تحقق

_DIACRITICS = re.compile("[ً-ٰٟـ]")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮]")


def norm_text(text):
    text = guard.normalize(text or "")
    text = _DIACRITICS.sub("", text)
    text = re.sub("[أإآٱ]", "ا", text)
    text = text.replace("ى", "ي").replace("ة", "ه")
    text = _NON_WORD.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def quote_in(evidence, texts):
    needle = norm_text(evidence)
    if len(needle) < 2:
        return False
    return any(needle in norm_text(t) for t in texts)


def clean_value(value, limit=MAX_VALUE_CHARS):
    """قيمة بتنحط لاحقاً ببرومبت الموديل: بنشيل أسطر وحروف تحكم ونقصّ الطول
    (كلام الزبون = بيانات مش تعليمات)."""
    value = _CONTROL.sub(" ", str(value or ""))
    return re.sub(r"\s+", " ", value).strip()[:limit]


def normalize_currency(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    key = raw.upper()
    if key in AMBIGUOUS_CURRENCY or raw in AMBIGUOUS_CURRENCY:
        return "?"
    return CURRENCY_ALIASES.get(key) or CURRENCY_ALIASES.get(raw) or "?"


@dataclass
class Validated:
    preferences: list = field(default_factory=list)
    retractions: list = field(default_factory=list)
    reactions: list = field(default_factory=list)
    objections: list = field(default_factory=list)
    resolved: list = field(default_factory=list)
    topics: list = field(default_factory=list)
    gender: str = ""
    pending: object = None  # {"type": "currency", "amount": "500", "currency": "USD"}
    dropped: list = field(default_factory=list)

    def summary(self):
        return {
            "preferences": [{k: p[k] for k in ("key", "value", "polarity", "source", "subject")} for p in self.preferences],
            "retractions": [{k: r[k] for k in ("key", "value", "subject")} for r in self.retractions],
            "reactions": [{k: r[k] for k in ("sku", "type", "reason_type")} for r in self.reactions],
            "objections": [{"type": o["type"], "sku": o["sku"]} for o in self.objections],
            "resolved": self.resolved, "topics": self.topics, "gender": self.gender,
            "pending": self.pending, "dropped": self.dropped,
        }


def _as_list(value):
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return [value] if isinstance(value, dict) else []


def validate(raw, *, user_texts, products_by_sku, merchant_currency):
    """raw: وسائط أداة المحلّل. user_texts: رسائل الزبون (للتحقق من الاقتباسات)."""
    out = Validated()
    if not isinstance(raw, dict):
        return out

    for item in _as_list(raw.get("preferences")):
        key = item.get("key")
        if key not in PREF_KEYS:
            out.dropped.append(f"pref:bad_key:{key}")
            continue
        subject = "gift" if item.get("subject") == "gift" else "self"
        polarity = "avoid" if item.get("polarity") == "avoid" else "like"
        source = "explicit" if item.get("source") == "explicit" else "inferred"
        value = clean_value(item.get("value"))
        if not value:
            continue
        evidence = clean_value(item.get("evidence"), 300)
        if source == "explicit" and not quote_in(evidence, user_texts):
            out.dropped.append(f"pref:{key}:explicit_without_quote->inferred")
            source = "inferred"
        if source == "inferred" and key not in INFERABLE:
            out.dropped.append(f"pref:{key}:not_inferable")
            continue

        if key in BUDGET_KEYS:
            amount = guard.to_decimal(guard.normalize(value).replace(" ", ""))
            if amount is None or not (Decimal("1") <= amount <= MAX_BUDGET):
                out.dropped.append(f"pref:{key}:bad_amount")
                continue
            polarity = "like"
            value = format(amount.normalize(), "f")
            currency = normalize_currency(item.get("currency"))
            store_currency = (merchant_currency or "").upper()
            if currency and store_currency and currency != store_currency:
                # عملة غير عملة المتجر وما عنا تحويل ← منسأل الزبون بدل ما نفترض
                out.pending = {"type": "currency", "key": key, "amount": value, "currency": currency}
                out.dropped.append(f"pref:{key}:currency_mismatch")
                continue
        out.preferences.append(
            {
                "key": key, "value": value, "polarity": polarity, "source": source, "subject": subject,
                "confidence": 1.0 if source == "explicit" else 0.6, "evidence": evidence,
            }
        )

    for item in _as_list(raw.get("retractions")):
        key = item.get("key")
        if key not in PREF_KEYS:
            continue
        if not quote_in(clean_value(item.get("evidence"), 300), user_texts):
            out.dropped.append(f"retraction:{key}:no_quote")
            continue
        out.retractions.append(
            {"key": key, "value": clean_value(item.get("value")),
             "subject": "gift" if item.get("subject") == "gift" else "self"}
        )

    for item in _as_list(raw.get("reaction")):
        sku = str(item.get("sku") or "").strip()
        kind = item.get("type")
        if sku not in products_by_sku:
            out.dropped.append(f"reaction:unknown_sku:{sku[:20]}")
            continue
        if kind not in REACTIONS:
            continue
        if kind in ("rejected", "liked") and not quote_in(clean_value(item.get("evidence"), 300), user_texts):
            out.dropped.append(f"reaction:{kind}:no_quote")
            continue
        reason = item.get("reason_type") if item.get("reason_type") in REASONS else ""
        if kind == "rejected" and not reason:
            reason = "unknown"
        out.reactions.append(
            {"sku": sku, "type": kind, "reason_type": reason if kind == "rejected" else "",
             "reason_text": clean_value(item.get("reason_text"), 200)}
        )

    for item in _as_list(raw.get("objections")):
        kind = item.get("type")
        if kind not in OBJECTIONS:
            continue
        evidence = clean_value(item.get("evidence"), 200)
        if not quote_in(evidence, user_texts):
            out.dropped.append(f"objection:{kind}:no_quote")
            continue
        sku = str(item.get("sku") or "").strip()
        out.objections.append({"type": kind, "sku": sku if sku in products_by_sku else "", "text": evidence})

    out.resolved = [o for o in (raw.get("resolved_objections") or []) if o in OBJECTIONS]
    out.topics = [t for t in (raw.get("questions_asked") or []) if t in TOPICS]
    gender = raw.get("gender")
    out.gender = gender if gender in ("female", "male") else ""
    return out


# ---------------------------------------------------------------- استدعاء

def build_input(window, memory_lines, product_lines, viewing_line=""):
    convo = "\n".join(
        ("CUSTOMER: " if m["role"] == "user" else "ASSISTANT: ") + m["content"] for m in window[-8:]
    )
    return (
        "Already remembered about this customer:\n" + (memory_lines or "(nothing yet)") + "\n\n"
        + ("CURRENTLY VIEWING: " + viewing_line + "\n\n" if viewing_line else "")
        + "Products the customer has seen or is viewing (sku — title — price):\n"
        + (product_lines or "(none)") + "\n\n"
        "Conversation (most recent last):\n" + convo + "\n\n"
        "Analyze the LAST customer message."
    )


def analyze(provider, window, memory_lines, product_lines, viewing_line=""):
    """بيرجّع وسائط الأداة (dict) أو None لو فشل. دايماً محاولة وحدة: التحليل
    تحسين، مش شرط — فشله ما لازم يأخّر أو يوقف الرد."""
    if not provider.is_configured():
        return None
    try:
        result = llm.chat_with_retry(
            provider, ANALYZER_SYSTEM, [{"role": "user", "content": build_input(window, memory_lines, product_lines, viewing_line)}],
            [ANALYZE_TOOL], force_tool=ANALYZE_TOOL["name"], max_attempts=1,
        )
    except llm.LLMError:
        logger.warning("analyzer_failed", exc_info=True)
        return None
    call = next((c for c in result.tool_calls if c["name"] == ANALYZE_TOOL["name"]), None)
    return call["input"] if call else None
