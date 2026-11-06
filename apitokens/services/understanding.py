"""فهم رسالة الزبون (v2): استدعاء "محلّل" صغير بيطلّع معلومات منظّمة (تفضيلات، ميزانية،
ردّ فعل على منتج، اعتراضات...) + تحقق صارم بالكود قبل ما أي شي يدخل الذاكرة.

مبدأ: الموديل بيقترح، الكود بيقرر. الاقتباس ما لازم يكون حرفياً: إعادة صياغة بنفس
الكلمات (بترتيب مختلف) بتضل صريحة. الميزانية والمقاس وقيود المنتج (لون، فئة، ستايل...)
مسموح تنحفظ inferred لما الزبون عبّر عنها وما قدرنا نقتبسها حرفياً، بشرط إن القيمة
نفسها (الرقم أو المقاس) ظاهرة بكلامه — رقم مخترع بينرفض. المستلم والرفض الصريح
لسا بدهم اقتباس، لأن الرفض بيخفي المنتج.
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
# قيود البحث يجوز استنتاجها لما الزبون عبّر عنها بصياغة ثانية. المستلم لأ: ما بيتحزر.
INFERABLE = frozenset({
    "budget_max", "budget_min", "size", "color", "style", "occasion", "category", "material", "brand", "fit",
})
# هدول بالذات لازم قيمتهم (الرقم أو المقاس) تكون بكلام الزبون، مش بس تخمين الموديل.
GROUNDED_INFERRED = frozenset({"budget_max", "budget_min", "size"})
REACTIONS = ("liked", "rejected", "asked", "neutral")
REASONS = ("price", "style", "color", "fit", "quality", "material", "other", "unknown")
OBJECTIONS = ("price", "competitor_price", "trust", "quality", "fit", "shipping", "timing", "other")
INTENTS = ("browse", "search", "ask_info", "compare", "object", "reject", "accept", "buy", "ask_discount", "chitchat", "other")
SIGNAL_READINESS = ("browsing", "interested", "hesitant", "ready")
HESITATION_REASONS = ("price", "style", "fit", "trust", "timing", "other")
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
            "gender": {
                "type": "string", "enum": ["female", "male", "unknown"],
                "description": "ONLY if the customer's own words reveal their gender through grammatical gender about "
                               "THEMSELVES (e.g. مترددة/محتارة/متأكدة = female, متردد/محتار/متأكد = male). Never from what "
                               "they shop for (a dress), never from gender-neutral words like أبي/أبغى/هلا. Otherwise unknown.",
            },
            "gender_evidence": {"type": "string", "description": "The exact gendered word(s) the customer used about themselves."},
            "signals": {
                "type": "object",
                "description": "How the customer is doing right now, judged from the MEANING of the latest message in context.",
                "properties": {
                    "intent": {"type": "string", "enum": list(INTENTS), "description": "What the latest message is mainly doing."},
                    "readiness": {"type": "string", "enum": list(SIGNAL_READINESS)},
                    "hesitation_reason": {
                        "type": "string", "enum": list(HESITATION_REASONS) + [""],
                        "description": "Why the customer is unsure about buying; empty for plain questions, comparisons, rejections and when not hesitating.",
                    },
                    "asks_discount": {"type": "boolean", "description": "The customer asks for a discount, a code, a better price or a deal."},
                    "wants_alternative": {"type": "boolean", "description": "The customer wants to see something different or other options."},
                },
            },
        },
    },
}

ANALYZER_SYSTEM = """You are the memory analyzer of an online store's sales assistant. You never talk \
to the customer. You read the conversation and record ONLY what the customer's own words establish.

Rules
- Only the customer's words count — never the assistant's suggestions. Earlier messages are only for \
resolving references like "it", "this one", "the second one".
- source="explicit" when the customer's words establish it. Copy those words into `evidence`. \
Word order and light paraphrasing are fine; it does not have to be a character-for-character quote. \
If you cannot point at their words, use source="inferred". Inferred IS allowed for a budget, a size, \
and a product constraint (color, category, style, occasion, material, brand, fit) when the customer \
expressed it and you are only normalizing it (digits for a budget, a standard size or color name). \
NEVER invent a number, size, color or other constraint they did not express. NEVER infer a recipient. \
polarity="avoid" is allowed ONLY as explicit, with their words refusing that thing ("ما أبي كاروهات"). \
Never infer a dislike, a banned pattern or a taste.
- Budget: budget_max for "up to / under / max / not more than / around N", budget_min for "at least N". \
value = digits only. Fill `currency` only if the customer named one (ريال/ريال سعودي → SAR, دولار → USD ...).
- Corrections ("actually", "no I meant", "change it to", "بدّلت رأيي", "لا قصدي") → emit the new \
preference as explicit; if it does not simply replace the old one (e.g. colors), also emit a \
retraction for the old value. "Forget that / ignore my budget" → retractions.
- Something for someone else (gift, family member) → subject="gift" and a `recipient` preference \
(value = the relation, e.g. "sister"), ONLY when the customer says the item itself is FOR that person \
("هدية لأختي", "أبي فستان لأختي", "لبنتي", "for my mom"). An occasion that belongs to someone else is NOT a \
gift: "فستان لعرس أختي", "لحفلة صديقتي", "for my friend's wedding" = the customer will wear it → subject="self", \
no `recipient`; the occasion is just an occasion (e.g. wedding).
- `reaction`: only if the latest message reacts to ONE specific product from the products list: \
liked / rejected (+ reason_type) / asked (a question about it) / neutral. Copy its sku exactly. \
A price remark is NOT a rejection. "حلو بس غالي", "عجبني بس سعره مرتفع", "حلو بس مو متأكد بسبب السعر", \
"حلو بس غالي علي شوي" = the customer LIKES it: reaction type liked (evidence: the liking words, e.g. "حلو"), \
plus an objection of type price, plus signals readiness hesitant / hesitation_reason price. Use "rejected" ONLY \
when the customer says they do not want THIS product or do not like it ("ما أبيه", "مو حلو", "ما عجبني"), even \
if the price is also mentioned ("غالي وما أبيه" = rejected, reason price). Asking for something cheaper or \
complaining about the price in general ("غالية! عندكم أرخص؟") is not a rejection of the product: use neutral or \
no reaction. \
Resolving "it / this / هذا": if a product is marked "OPENED AFTER the assistant's last message", the customer \
is reacting to THAT page; if the page was already open, or none is marked, use the product the assistant \
just showed; if the customer names a product, use the named one.
- Products tagged "[REJECTED earlier by the customer, hidden]" are hidden from the customer. If the customer asks to see one \
again, bring it back, or says they changed their mind about it (even without naming it: "the first one", "the one I \
rejected", "الأول اللي رفضته"), emit reaction type "asked" (or "liked") for that sku: an explicit request lifts the rejection.
- Rejection reasons (only for a real rejection): price (they refuse it because of the price), style \
(not my taste), color, fit (size/cut), quality, material, other, unknown.
- objections: price, competitor_price (found it cheaper elsewhere), trust, quality, fit, shipping, \
timing ("I'll think about it"), other — with the customer's exact words as evidence. \
resolved_objections: types the customer now says are settled.
- questions_asked: topics the customer asked about in the latest message.
- Understand every Arabic dialect (Saudi, Gulf, Levantine/Syrian, Egyptian, Maghrebi), MSA, Arabizi and \
other languages. Quote in the original language.
- `signals` (always fill it, judging the MEANING of the latest message in context — keywords are not enough):
  - intent: browse (just looking), search (asks for something specific), ask_info (a question about \
product/shipping/size/payment), compare (weighs options), object (a concern or doubt), reject (does not want \
what was shown), accept (agrees / likes / wants it), buy (wants to order or pay now), ask_discount (asks for a \
discount/code/better price), chitchat (greetings, thanks, small talk), other.
  - readiness: "ready" ONLY when the customer clearly decided to buy now (asks how to pay or order, says \
they'll take it, asks for the payment/checkout link) and voices no remaining doubt. "hesitant" ONLY when the \
customer is interested in a specific product but is unsure about BUYING it, judged from the whole conversation: \
a price worry ("حلو بس غالي", "تمام آخذه بس غالي"), doubt ("مو متأكدة إذا أشتريه", "عجبني بس محتارة", "مترددة"), \
"I'll think about it" ("بفكر"), "let me ask my husband/sister". Plain questions are NOT hesitation, even when \
they sound like weighing options: "وش أحسن واحد؟", "قارن لي بين هذا وهذا", "وش القماش؟", "متى يوصل؟", \
"كم الشحن؟", "عندكم مقاس كبير؟" — these are ask_info / compare / browse and the customer is "interested" or \
"browsing" unless the SAME message also voices doubt about buying. Disliking a product ("مو حلو") is a \
reject, not hesitation. "interested" when positive but no decision yet. "browsing" otherwise (just looking, \
greetings, general questions).
  - hesitation_reason (only when readiness is hesitant, i.e. real hesitation as defined above): price | style | \
fit | trust | timing | other; otherwise "" — leave it empty for plain questions and for rejections.
  - asks_discount: true only if the customer asks for a discount, a code or a lower price.
  - wants_alternative: true if the customer asks for other options or something different.
  Leave a field out if you cannot tell. Never infer "ready" from politeness or from a compliment.
- `gender` + `gender_evidence`: fill ONLY when the customer's own words reveal their gender through grammatical \
gender about themselves: feminine "مترددة / محتارة / متأكدة / مبسوطة / جاهزة / أنا بنت" ⇒ female; masculine \
"متردد / محتار / متأكد / مبسوط / جاهز" ⇒ male. Copy the gendered word exactly as `gender_evidence`. It is NOT a signal: \
what they shop for (a dress, an abaya), greetings, and gender-neutral verbs like أبي / أبغى / أدور / وريني. \
Otherwise leave gender out (unknown). Never guess.
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


_FRESH_START = (
    "نبلش من جديد", "نبدا من جديد", "ابدا من جديد", "ابدي من جديد",
    "خلينا نبلش", "خلينا نبدا", "نبلش من الصفر", "نبدا من الصفر", "ابدا من الصفر",
    "انسى كل شي", "انسى اللي فات", "انسى اللي صار", "انسى الحكي",
    "start over", "start again", "new chat",
)
_ASKS_TO_SEE = (
    "ورجيني", "وريني", "شوفني", "شو عندك", "شو عندكم", "ايش عندك", "ايش عندكم",
    "وش عندك", "وش عندكم", "ابي اشوف", "بدي اشوف", "ابغى اشوف", "ابغا اشوف",
    "show me", "what do you have",
)
_DENIES_STOCK = re.compile(
    r"ما عندي|ما فيه خيار|ما في خيار|لا يوجد|nothing available|don't have|do not have|no match",
    re.IGNORECASE,
)


def wants_fresh_start(text):
    """الزبون طلب يمسح الحكي ويبلّش من جديد. مطابقة حرفية بعد تطبيع، مش استنتاج الموديل."""
    hay = norm_text(text)
    return any(phrase in hay for phrase in _FRESH_START)


_GREETINGS = ("مرحبا", "مرحبتين", "هلا", "اهلين", "اهلا", "هاي", "hello", "hi", "السلام")
_UNSPECIFIED = (
    "ماقلتك", "ماقلتلك", "ماقلتي", "ما قلتلك", "ما قلتك", "ما قلتي",
    "عن شو عم دور", "شو عم دور", "ما عم دور", "ما حددت", "ما طلبت", "ما قلت شو",
)


def should_ask_before_showing(text):
    """تحية أو «ما قلت شو بدي»: نسأل قبل ما نعرض كرت. طلب صريح يشوف المنتجات ما ينحجب."""
    if asks_to_see(text):
        return False
    hay = norm_text(text)
    if any(phrase in hay for phrase in _UNSPECIFIED):
        return True
    words = [w for w in hay.split() if w not in ("يا", "بس", "طيب")]
    return bool(words) and len(words) <= 3 and all(w in _GREETINGS or w in ("والله", "فيك") for w in words)


def asks_to_see(text):
    """طلب يشوف المنتجات الموجودة، حتى لو المحلّل ما سمّى النية بحث."""
    hay = norm_text(text)
    return any(phrase in hay for phrase in _ASKS_TO_SEE)


def denies_availability(text):
    return bool(_DENIES_STOCK.search(text or ""))


_PARAPHRASE_COVERAGE = 0.6


def quote_in(evidence, texts):
    """اقتباس حرفي بعد التطبيع، أو إعادة صياغة: أغلب كلمات الدليل موجودة برسالة الزبون."""
    needle = norm_text(evidence)
    if len(needle) < 2:
        return False
    haystacks = [norm_text(t) for t in texts if t]
    if any(needle in hay for hay in haystacks):
        return True
    tokens = [t for t in needle.split() if len(t) >= 2]
    if len(tokens) < 2:
        return False
    for hay in haystacks:
        words = set(hay.split())
        hits = sum(1 for token in tokens if token in words)
        if hits / len(tokens) >= _PARAPHRASE_COVERAGE:
            return True
    return False


def value_grounded(key, value, user_texts):
    """رقم الميزانية أو رمز المقاس ظاهر بكلام الزبون، حتى لو الدليل معاد صياغته بلغة ثانية."""
    blob = " ".join(norm_text(t) for t in user_texts if t)
    if key in BUDGET_KEYS:
        digits = re.sub(r"\D", "", guard.normalize(str(value or "")))
        return bool(digits) and digits in re.sub(r"\D", "", blob)
    token = norm_text(value)
    return bool(token) and token in blob.split()


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
    # إشارات الحالة (نيّة/جاهزية/تردّد): دايماً dict بالمفاتيح الخمسة؛ القيم غير المعروفة = الافتراضي
    signals: dict = field(default_factory=lambda: default_signals())

    def summary(self):
        return {
            "preferences": [{k: p[k] for k in ("key", "value", "polarity", "source", "subject")} for p in self.preferences],
            "retractions": [{k: r[k] for k in ("key", "value", "subject")} for r in self.retractions],
            "reactions": [{k: r[k] for k in ("sku", "type", "reason_type")} for r in self.reactions],
            "objections": [{"type": o["type"], "sku": o["sku"]} for o in self.objections],
            "resolved": self.resolved, "topics": self.topics, "gender": self.gender,
            "pending": self.pending, "dropped": self.dropped,
            "signals": dict(self.signals),
        }


def default_signals():
    return {"intent": "other", "readiness": "", "hesitation_reason": "",
            "asks_discount": False, "wants_alternative": False}


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes")
    return False


def sanitize_signals(raw, dropped=None):
    """إشارات المحلّل ← dict ثابت الشكل. أي قيمة خارج القائمة بتنرمي (بتنزل للافتراضي).
    ما في اقتباس مطلوب: هدول إشارات مساعدة للسياسة، والقرار الحقيقي (خصم/مرحلة) بيتخذ بالكود
    وبيتقاطع مع أحداث موثوقة (نقرة شراء، سلة...)."""
    out = default_signals()
    if not isinstance(raw, dict):
        return out
    for key, allowed in (("intent", INTENTS), ("readiness", SIGNAL_READINESS), ("hesitation_reason", HESITATION_REASONS)):
        value = raw.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, str) and value.strip().lower() in allowed:
            out[key] = value.strip().lower()
        elif dropped is not None:
            dropped.append(f"signals:bad_{key}")
    for key in ("asks_discount", "wants_alternative"):
        out[key] = _as_bool(raw.get(key))
    return out


def _as_list(value):
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return [value] if isinstance(value, dict) else []


# كلمات مش دليل على جنس الزبون: أفعال رغبة/تحية محايدة، ونوع القطعة (فستان لا يعني امرأة).
_NEUTRAL_WORDS = frozenset(norm_text(w) for w in (
    "ابي", "ابغى", "ابغا", "ابى", "بدي", "اريد", "ادور", "وريني", "ورني", "هلا", "اهلا", "مرحبا", "السلام", "عليكم",
    "فستان", "فساتين", "عباية", "عبايات", "شنطة", "شنط", "قميص", "بنطلون", "جاكيت", "حذاء", "جزمة", "عرس", "زواج",
    "سهرة", "حفلة", "دوام", "هدية", "لون", "مقاس", "الاسود", "اسود", "ابيض", "احمر", "اخضر", "ازرق", "بيج", "وردي",
    # أقارب ومعارف: "عرس أختي" بيحكي عن الأخت، مش عن جنس الزبون نفسه
    "اختي", "امي", "بنتي", "زوجتي", "صديقتي", "صاحبتي", "خالتي", "عمتي", "جدتي", "اخوي", "ابوي", "ولدي",
    "زوجي", "صديقي", "صاحبي", "خالي", "عمي",
))


def _only_neutral_words(evidence, products_by_sku):
    """الدليل كله كلمات محايدة أو من عناوين المنتجات؟ ← مش دليل جنس."""
    titles = set()
    for product in (products_by_sku or {}).values():
        titles.update(norm_text(getattr(product, "title", "")).split())
        titles.update(norm_text(getattr(product, "category", "") or "").split())
    known = _NEUTRAL_WORDS | titles

    def neutral(token):
        # سوابق شائعة: و/ف/ب/ل/ك + ال ("للعرس"، "بالفستان"، "والفستان")
        variants = {token}
        for cut in (1, 2, 3):
            if len(token) > cut + 1:
                variants.add(token[cut:])
        if token.startswith("لل"):
            variants.add("ال" + token[2:])
        return bool(variants & known) or any(v.startswith("ال") and v[2:] in known for v in variants)

    tokens = norm_text(evidence).split()
    return bool(tokens) and all(neutral(t) for t in tokens)


# ---- جنس الزبون من نفس الرسالة (شبكة حتمية فوق المحلّل، بدون انتظار الدور التالي)
# أوصاف الزبون لنفسه بصيغة واضحة (مذكر/مؤنث). كلمات ملتبسة ("جاهز" = الطلب جاهز؟) مستبعدة عمداً.
_SELF_GENDER_PAIRS = (
    ("متردد", "مترددة"), ("محتار", "محتارة"), ("متأكد", "متأكدة"), ("مقتنع", "مقتنعة"), ("مبسوط", "مبسوطة"),
    ("مهتم", "مهتمة"), ("متحمس", "متحمسة"), ("مستعجل", "مستعجلة"), ("زعلان", "زعلانة"), ("فرحان", "فرحانة"),
    ("تعبان", "تعبانة"), ("محرج", "محرجة"), ("ضايع", "ضايعة"), ("مرتبك", "مرتبكة"), ("خايف", "خايفة"),
    ("قلقان", "قلقانة"), ("متشوق", "متشوقة"), ("منبهر", "منبهرة"),
)
_SELF_GENDER = {}
for _m, _f in _SELF_GENDER_PAIRS:
    _SELF_GENDER[norm_text(_m)] = "male"
    _SELF_GENDER[norm_text(_f)] = "female"
_SELF_PHRASES = (("انا بنت", "female"), ("انا امراه", "female"), ("انا مره", "female"), ("انا رجل", "male"),
                 ("انا شاب", "male"), ("انا ولد", "male"))
_ADDRESSING = frozenset(norm_text(w) for w in ("انت", "انتي", "انتم", "إنت", "أنت", "أنتِ", "هل"))


def detect_self_gender(text):
    """(gender, الكلمة) من وصف الزبون لنفسه بنفس الرسالة، أو None. بيتجاهل: سؤال موجّه للمساعد ("أنت متأكد؟")،
    ورسالة فيها الصيغتين (متضاربة)."""
    found = []
    for sentence in re.findall(r"[^.!?؟\n]+[.!?؟]*", text or ""):
        tokens = norm_text(sentence).split()
        asking = sentence.strip().endswith(("؟", "?"))
        for i, token in enumerate(tokens):
            base = token if token in _SELF_GENDER else (token[1:] if token[:1] in "وف" and token[1:] in _SELF_GENDER else None)
            if base is None:
                continue
            if any(t in _ADDRESSING for t in tokens[max(0, i - 3):i]) or (asking and len(tokens) <= 2):
                continue
            found.append((_SELF_GENDER[base], base))
    norm_all = norm_text(text)
    for phrase, gender in _SELF_PHRASES:
        if phrase in norm_all:
            found.append((gender, phrase))
    genders = {g for g, _w in found}
    return found[0] if len(genders) == 1 else None


# نوايا بتناقض "رفض المنتج": المحلّل نفسه صنّف الرسالة كاعتراض/سؤال مش رفض.
NOT_A_REJECTION_INTENTS = frozenset({"object", "ask_discount", "compare", "ask_info", "search", "browse", "accept", "chitchat"})


def _reconcile_price_rejections(out):
    """ملاحظة على السعر مش رفض للمنتج ("حلو بس غالي"). إذا المحلّل سجّل rejected(price) بس صنّف الرسالة بنفسه
    كاعتراض/سؤال (intent مش reject): ما منخفي المنتج. إذا كان متردد (readiness hesitant = مهتم بس مش محسوم) ← liked
    (اهتمام حقيقي)، وإلا neutral. اعتراض السعر بيضل مسجّل. الرفض الحقيقي ("غالي وما أبيه") intent=reject وما بيتأثر."""
    sig = out.signals
    if sig.get("intent") not in NOT_A_REJECTION_INTENTS:
        return
    for r in out.reactions:
        if r["type"] == "rejected" and r["reason_type"] == "price":
            r["type"] = "liked" if sig.get("readiness") == "hesitant" else "neutral"
            r["reason_type"] = ""
            out.dropped.append(f"reaction:price_remark_is_not_a_rejection:{r['sku']}")
            if not any(o["type"] == "price" for o in out.objections):
                out.objections.append({"type": "price", "sku": r["sku"], "text": r.get("reason_text", ""), "current": True})


def validate(raw, *, user_texts, products_by_sku, merchant_currency, current_text=None):
    """raw: وسائط أداة المحلّل. user_texts: رسائل الزبون (للتحقق من الاقتباسات).
    current_text: رسالة الزبون الحالية — اعتراض اقتباسه مش فيها (الموديل أعاد اعتراض قديم) بيتعلّم current=False."""
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
        if polarity == "avoid" and source != "explicit":
            out.dropped.append(f"pref:{key}:avoid_must_be_quoted")
            continue
        if source == "inferred" and key not in INFERABLE:
            out.dropped.append(f"pref:{key}:not_inferable")
            continue
        if source == "inferred" and key in GROUNDED_INFERRED and not value_grounded(key, value, user_texts):
            out.dropped.append(f"pref:{key}:ungrounded")
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
        out.objections.append({
            "type": kind, "sku": sku if sku in products_by_sku else "", "text": evidence,
            # "حالي": اقتباسه موجود برسالة الزبون هالدور (مش اعتراض قديم أعاده المحلّل مع سياق المحادثة)
            "current": quote_in(evidence, [current_text]) if current_text is not None else True,
        })

    out.resolved = [o for o in (raw.get("resolved_objections") or []) if o in OBJECTIONS]
    out.topics = [t for t in (raw.get("questions_asked") or []) if t in TOPICS]
    gender = raw.get("gender")
    out.gender = gender if gender in ("female", "male") else ""
    if out.gender:
        evidence = clean_value(raw.get("gender_evidence"), 120)
        if not quote_in(evidence, user_texts):
            out.dropped.append(f"gender:{out.gender}:no_quote")      # بدون كلمة الزبون الحرفية = تخمين
            out.gender = ""
        elif _only_neutral_words(evidence, products_by_sku):
            out.dropped.append(f"gender:{out.gender}:evidence_not_gendered")   # "أبي فستان" مش إشارة جنس
            out.gender = ""
    if current_text is not None:
        detected = detect_self_gender(current_text)
        if detected and detected[0] != out.gender:
            # صيغة واضحة بنفس الرسالة ← بتُسجَّل فوراً (الصيغة الصريحة بتغلب تخمين/تأخّر المحلّل)
            out.dropped.append(f"gender:detected_in_message:{detected[0]}:{detected[1]}")
            out.gender = detected[0]
    out.signals = sanitize_signals(raw.get("signals"), out.dropped)
    _reconcile_price_rejections(out)
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
