"""قلب البائع v2: حلقة أدوات محدودة وحتمية. الموديل بيقدر ينادي أدوات (بحث، تفاصيل، مقارنة،
عرض كرت، مقاس)، بياخد النتائج، وبعدين بس بيكتب رد الزبون النهائي.

القواعد (كلها بالكود، مش بالبرومبت):
- أقصى 3 خطوات LLM، 6 استدعاءات أدوات، وميزانية وقت (deadline_seconds). آخر خطوة بـ
  tool_choice="none" وأي أداة بترجع فيها بتنتجاهل.
- الكرت ما بيوصل للزبون إلا عبر show_product (تحقق بالكود: موجود، بالمخزون، مو مكرر، بيحقق
  متطلبات الزبون، أقصى 3 كروت).
- مختصر الحلقة: نص + show_product كلها "shown" ← نخلّص بدون استدعاء LLM إضافي (المسار السعيد = استدعاء واحد).
- بعد الحلقة: شبكة اللغة + مدقّق المخرجات (guard) + لهجة سعودية، بإعادة توليد وحدة كحد أقصى.
- الخصم ما إله أداة: الكود قرر قبلنا (approved_offer). لو موجود، الكود لازم يطلع بالرد.
- محرّك التوصيات (recommend.py) بيتستورد بكسل — راجع prompts_v2.get_recommend.
"""

import dataclasses
import json
import logging
import re
import time
from decimal import Decimal, InvalidOperation

from .. import prompts_v2
from .. import widget_views as wv
from . import contracts, guard, llm, saudi

logger = logging.getLogger(__name__)

MAX_STEPS = 3
MAX_TOOL_CALLS = 6
MAX_CARDS = 1

# "جرّبه عليّ" من منظور البائع غلط: الزر للزبون ← "عليك"
# (بس آخر كلمة بالزر، حتى ما نلمس "على أختك")
_SELF_LABEL_RE = re.compile(r"(?<![\u0600-\u06FF])عليّ?(?=[^\u0600-\u06FF]*$)")


def _self_label(label):
    return _SELF_LABEL_RE.sub("عليك", label or "")
MAX_SEARCH_LIMIT = 5
MAX_REASON_CHARS = 400
FULL_DESCRIPTION_CHARS = 800


def _now():
    return time.monotonic()


# ------------------------------------------------------------------ تعريف الأدوات

SEARCH_TOOL = {
    "name": "search_products",
    "description": (
        "Search the store's in-stock catalog. Use ONLY when the candidates already listed in the prompt don't "
        "fit. The customer's confirmed requirements (budget, rejected items, sizes, colors) are always enforced "
        "by the system: your arguments can only narrow the search further, never loosen it. Returns up to "
        "`limit` products with facts, `exact_count` and a `note`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Free-text description of what to look for (style, occasion, item)."},
            "category": {"type": "string", "description": "Standard English category such as dress, abaya, shirt."},
            "color": {"type": "string", "description": "One standard English color name, e.g. black."},
            "max_price": {"type": "number", "description": "Upper price bound; can only lower the customer's budget."},
            "size": {"type": "string", "description": "One size the product must be available in, e.g. M."},
            "limit": {"type": "integer", "description": "How many results (1-5, default 5)."},
        },
    },
}

GET_PRODUCT_TOOL = {
    "name": "get_product",
    "description": (
        "Full facts about ONE product by sku: description, all options, price, whether it fits the customer's "
        "confirmed requirements. Use it to answer a specific question about a product."
    ),
    "parameters": {
        "type": "object",
        "properties": {"sku": {"type": "string", "description": "Exact sku, copied from the data."}},
        "required": ["sku"],
    },
}

COMPARE_TOOL = {
    "name": "compare_products",
    "description": "Compare 2 or 3 products side by side (price, options, differences). Use when the customer is torn between items.",
    "parameters": {
        "type": "object",
        "properties": {
            "skus": {
                "type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3,
                "description": "The 2-3 exact skus to compare.",
            },
        },
        "required": ["skus"],
    },
}

SHOW_TOOL = {
    "name": "show_product",
    "description": (
        "Show the customer ONE product card. This is the ONLY way a product reaches the customer: naming a "
        "product in text without calling this shows nothing. Call it in the same step as your reply text. "
        "If you write no separate text, the `reason` of the FIRST card is sent as your complete reply to the "
        "customer (1-3 short sentences in the customer's language, may include the one allowed question), so write it that way. "
        "The system validates it (exists, in stock, not already shown, meets the customer's confirmed "
        "requirements, max 1 card per reply) and returns a status: shown, duplicate, violates_requirement, "
        "unknown_sku, out_of_stock or card_limit. Calling it is low-commitment: it only displays a card."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sku": {"type": "string", "description": "Exact sku, copied from the data."},
            "reason": {
                "type": "string",
                "description": (
                    "Short benefit-led reason this product fits THIS customer, built only from the product's "
                    "real facts, in the customer's language (Saudi dialect if Arabic), plain text with no "
                    "markdown. No discounts, codes or invented numbers."
                ),
            },
            "for_someone_else": {
                "type": "boolean",
                "description": (
                    "True only if the customer said the product is FOR another person (a gift, \"أبيه لأختي\"). "
                    "Someone else's occasion is not a gift: \"لعرس أختي\" means the customer wears it (false). "
                    "Keep passing true on every later call once established."
                ),
            },
            "tryon_button_label": {
                "type": "string",
                "description": "Very short label (2-6 words, may end with 📸) for the photo try-on button, in the customer's language; for someone else, word it for that person.",
            },
        },
        "required": ["sku", "reason"],
    },
}

TOOLS = [SEARCH_TOOL, GET_PRODUCT_TOOL, COMPARE_TOOL, SHOW_TOOL, wv.SIZE_TOOL]
INFO_TOOLS = frozenset({"search_products", "get_product", "compare_products", "recommend_size"})

MISMATCH_CORRECTION = (
    "\n\nCORRECTION — your previous reply was written in the wrong language. The customer's last message is: "
    "{message}. Reply ONLY in that exact language and script (Saudi dialect if it's Arabic)."
)
CARDS_NOTE = (
    "\n\nThe product cards for these skus are already on the customer's screen with their own reasons: {skus}. "
    "Write only the text reply; do not call any tool."
)


# ------------------------------------------------------------------ مساعدات

def _norm_value(value):
    if isinstance(value, str):
        return " ".join(value.split()).lower()
    if isinstance(value, (list, tuple)):
        items = [_norm_value(v) for v in value]
        return sorted(items) if all(isinstance(i, str) for i in items) else items
    return value


def _norm_args(args):
    """مفتاح التكرار: اسم الأداة + وسائط مطبّعة (بدون الفاضي، حروف صغيرة، مسافات موحّدة)."""
    cleaned = {k: _norm_value(v) for k, v in args.items() if v not in (None, "", [], ())}
    return json.dumps(cleaned, sort_keys=True, ensure_ascii=False, default=str)


def _text(value):
    return str(value).strip() if value is not None else ""


def _decimal(value):
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return dec if dec.is_finite() else None


def _clamp_int(value, low, high, default):
    try:
        return max(low, min(high, int(float(value))))
    except (TypeError, ValueError):
        return default


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _status_of(result):
    if result.get("status"):
        return result["status"]
    return "ok" if result.get("ok") else result.get("error", "error")


# ------------------------------------------------------------------ تنفيذ الأدوات

class _Turn:
    """حالة دور واحد: الكروت المعروضة، الكاش، عدّاد الأدوات، مقاس محسوب. كل التحقق هون."""

    def __init__(self, inp, recommend):
        self.inp = inp
        self.rec = recommend
        self.req = inp.requirements
        self._products = None
        self._by_sku = None
        self.cards = []
        # إعادة عرض الكرت بس لمنتج كان مخفي (مرفوض) وطلبه الزبون صراحة؛ أي منتج تاني معروض = ما بنكرره.
        self.shown = set(inp.shown_skus or []) - set(inp.reshow_skus or ())
        self.cache = {}
        self.executed = 0
        self.events = []
        self.size_advice = None
        self.size_calls = []
        self.blocked_reason = ""   # reason تبع كرت مكرر: أحياناً الموديل بيحط جوابه كله فيه
        # بدائل قريبة صرّحت وحدة التوصيات إنها خفّفت فيها متطلب (لون/فئة) لأنه ما في تطابق تام:
        # بتنعرض مع تنبيه. الميزانية والمقاس والرفض والمخزون ما بتتخفّف أبداً، فمحجوبة دايماً.
        self.relaxed = {}
        self._note_relaxed(getattr(inp.prefetched, "candidates", None) or [])
        self.handlers = {
            "search_products": self._search,
            "get_product": self._get_product,
            "compare_products": self._compare,
            "show_product": self._show,
            "recommend_size": self._size,
        }

    def _note_relaxed(self, candidates):
        for cand in candidates:
            if not cand.exact and cand.relaxed:
                self.relaxed.setdefault(cand.product.sku, set()).update(cand.relaxed)

    def _unmet(self, product):
        """متطلبات الزبون يلي المنتج ما بيحققها (بعد استثناء ما خُفّف صراحة بقائمة المرشّحين)."""
        allowed = self.relaxed.get(product.sku, ())
        return [v for v in self.rec.check_product(product, self.req) if v not in allowed]

    @property
    def products(self):
        """كون الأدوات: منتجات التاجر المتوفرة (بيتحمّل مرة وحدة، عند أول استعمال)."""
        if self._products is None:
            self._products = list(self.inp.merchant.products.filter(in_stock=True))
        return self._products

    @property
    def by_sku(self):
        if self._by_sku is None:
            self._by_sku = {p.sku: p for p in self.products}
        return self._by_sku

    # ---- التنفيذ العام (كاش + سقف + حماية من الاستثناءات)
    def execute(self, call):
        args = call.input if isinstance(call.input, dict) else {}
        key = (call.name, _norm_args(args))
        cached = False
        if key in self.cache:
            result, cached = {**self.cache[key], "note": "already_executed"}, True
        elif call.name not in self.handlers:
            result = {"ok": False, "error": "unknown_tool"}
        elif self.executed >= MAX_TOOL_CALLS:
            result = {
                "ok": False, "error": "tool_limit_reached",
                "message": "No more tool calls are available this turn: write your final reply now.",
            }
        else:
            self.executed += 1
            try:
                result = self.handlers[call.name](args)
            except Exception:  # الأداة ما بتكسر الدور أبداً
                logger.exception("seller_tool_failed tool=%s", call.name)
                result = {"ok": False, "error": "internal"}
            self.cache[key] = result
        event = {"name": call.name, "input": args, "status": _status_of(result)}
        if cached:
            event["cached"] = True
        self.events.append(event)
        return result

    # ---- بحث
    def _tighten(self, args):
        """بيدمج وسائط الموديل بمتطلبات الزبون بالتضييق فقط (ما بيشيل ميزانية/رفض/مقاس)."""
        req, changes, ignored = self.req, {}, []

        category = _text(args.get("category")).lower()
        if category:
            if not req.categories or category in {c.lower() for c in req.categories}:
                changes["categories"] = (category,)
            else:
                ignored.append("category")
        color = _text(args.get("color")).lower()
        if color:
            if color in {c.lower() for c in req.avoid_colors}:
                ignored.append("color")
            elif not req.colors or color in {c.lower() for c in req.colors}:
                changes["colors"] = (color,)
            else:
                ignored.append("color")
        size = _text(args.get("size")).upper()
        if size:
            if not req.sizes or size in {s.upper() for s in req.sizes}:
                changes["sizes"] = (size,)
            else:
                ignored.append("size")
        if args.get("max_price") not in (None, ""):
            price = _decimal(args.get("max_price"))
            if price is None or price <= 0:
                ignored.append("max_price")
            elif req.budget_max is None or price < req.budget_max:
                changes["budget_max"] = price
            elif price > req.budget_max:
                ignored.append("max_price")   # حاول يوسّع الميزانية
        return (dataclasses.replace(req, **changes) if changes else req), ignored

    def _search(self, args):
        req, ignored = self._tighten(args)
        limit = _clamp_int(args.get("limit"), 1, MAX_SEARCH_LIMIT, MAX_SEARCH_LIMIT)
        result = self.rec.search(
            self.products, req, query=_text(args.get("query"))[:200], limit=limit,
            shown_skus=tuple(self.shown), relax=True,
        )
        self._note_relaxed(result.candidates)
        out = {
            "ok": True,
            "results": self.rec.describe_candidates(list(result.candidates)[:limit], self.inp.currency),
            "exact_count": result.exact_count,
            "note": result.note,
        }
        if ignored:
            out["ignored_filters"] = ignored
            out["message"] = (
                "Ignored filters that would loosen the customer's confirmed requirements: " + ", ".join(ignored)
            )
        return out

    # ---- منتج / مقارنة
    def _lookup(self, sku):
        """(product, error). error = unknown_sku | out_of_stock."""
        product = self.by_sku.get(sku) if sku else None
        if product is not None:
            return product, None
        exists = bool(sku) and self.inp.merchant.products.filter(sku=sku).exists()
        return None, ("out_of_stock" if exists else "unknown_sku")

    def _visible(self, sku):
        product, error = self._lookup(sku)
        if error:
            return None, error
        if sku in self.req.excluded_skus and sku not in self.req.exempt_skus:
            return None, "hidden_by_customer_rejection"
        return product, None

    _ERROR_MESSAGES = {
        "unknown_sku": "No product with this sku exists in the catalog.",
        "out_of_stock": "This product is out of stock; do not offer it.",
        "hidden_by_customer_rejection": "The customer rejected this product earlier; do not bring it back.",
    }

    def _get_product(self, args):
        sku = _text(args.get("sku"))
        product, error = self._visible(sku)
        if error:
            return {"ok": False, "error": error, "message": self._ERROR_MESSAGES[error]}
        violations = self._unmet(product)
        candidate = contracts.Candidate(
            product=product, exact=not violations,
            reasons=self.rec.explain(product, self.req, ""),
            warnings=[f"does not meet the customer's requirement: {v}" for v in violations],
        )
        row = dict(self.rec.describe_candidates([candidate], self.inp.currency)[0])
        row.update(
            full_description=(product.description or "")[:FULL_DESCRIPTION_CHARS],
            options=product.options or {},
            already_shown=sku in self.shown,
            can_show=not violations,
            violations=violations,
        )
        return {"ok": True, "product": row}

    def _compare(self, args):
        skus = args.get("skus")
        if not isinstance(skus, (list, tuple)):
            return {"ok": False, "error": "invalid_skus", "message": "Pass 2 or 3 skus."}
        skus = list(dict.fromkeys(_text(s) for s in skus))
        if not 2 <= len(skus) <= 3:
            return {"ok": False, "error": "invalid_skus", "message": "Pass 2 or 3 different skus."}
        products, problems = [], {}
        for sku in skus:
            product, error = self._visible(sku)
            if error:
                problems[sku] = error
            else:
                products.append(product)
        if problems:
            return {"ok": False, "error": "invalid_skus", "problems": problems}
        return {"ok": True, **self.rec.compare(products, self.req)}

    # ---- عرض كرت (الطريق الوحيد للكرت)
    def _show(self, args):
        sku = _text(args.get("sku"))
        reason = _text(args.get("reason"))[:MAX_REASON_CHARS]

        def refused(status, message, **extra):
            return {"ok": False, "status": status, "sku": sku, "message": message, **extra}

        product, error = self._lookup(sku)
        if error:
            return refused(error, self._ERROR_MESSAGES[error])
        stay = (getattr(self.inp.strategy, "stay_sku", "") or "").strip()
        if stay and sku != stay:
            return refused(
                "stay_on_product",
                f"The customer is staying on sku {stay}. Do not show {sku}. Answer about {stay}. "
                "If their size is not available on it, say so honestly and do not switch products.",
            )
        if sku in self.shown:
            self.blocked_reason = self.blocked_reason or reason
            return refused(
                "duplicate",
                "This card is already on the customer's screen: do not show it again. Answer in text and, "
                "if they want to buy it, point them to its buy button.",
            )
        if len(self.cards) >= MAX_CARDS:
            return refused("card_limit", "One card per reply already shown. Do not show more.")
        violations = self._unmet(product)
        if violations:
            detail = "This product breaks what the customer confirmed (" + ", ".join(violations) + "). "
            if stay:
                message = detail + "Tell the customer honestly and do not show a different product."
            else:
                message = detail + (
                    "Choose another candidate or tell the customer honestly; do not describe this product as a fit."
                )
            return refused("violates_requirement", message, violations=violations)
        for_someone_else = _truthy(args.get("for_someone_else"))
        self.cards.append({
            **wv._product_card(product, self.inp.currency),
            "reason": reason,
            "tryon_supported": product.supports_tryon,
            "tryon_button_label": _self_label(
                wv._tryon_label(args.get("tryon_button_label"), self.inp.lang, for_someone_else)
            ),
        })
        self.shown.add(sku)
        return {"ok": True, "status": "shown", "sku": sku, "cards_shown_this_turn": len(self.cards)}

    # ---- مقاس
    def _size(self, args):
        self.size_calls.append({"name": "recommend_size", "input": args})
        sku = _text(args.get("sku"))
        product, error = self._lookup(sku)
        if error:
            return {"ok": False, "error": error, "message": self._ERROR_MESSAGES[error]}
        advice = wv._size_advice(
            [{"name": "recommend_size", "input": args}], self.inp.merchant, self.products,
            self.inp.page_product, list(self.shown), self.inp.lang,
        )
        if advice is None:
            return {
                "ok": False, "error": "no_size_advice",
                "message": (
                    "Could not compute a size: measurements are missing or out of range, or the size chart does "
                    "not apply to this product. Ask for height and weight (waist if known) in one short "
                    "question, or use the product's available options."
                ),
            }
        self.size_advice = self.size_advice or advice
        return {
            "ok": True, "sku": advice["sku"], "size": advice["size"], "available": advice["available"],
            "alternative": advice["alternative"], "approximate": advice["approximate"],
            "note": (
                "The system appends the exact size sentence to your reply automatically. Do NOT state or repeat "
                "the size yourself: write one short warm line about how the cut will sit, plus a nudge to buy."
            ),
        }


# ------------------------------------------------------------------ التجميع النهائي

MAX_DISCOUNT = {
    "ar": "هذا أقصى خصم معتمد عندنا: كود {code}{description} 🎁",
    "en": "This is the maximum discount we have approved: code {code}{description} 🎁",
}
NO_VALID_CODE = {"ar": "ما فيه كود خصم متاح حالياً.", "en": "There is no discount code available right now."}
NOT_ELIGIBLE = {"ar": "ما أقدر أقدم لك خصم حالياً.", "en": "I can't offer you a discount right now."}
KEEP_HELPING = {
    "ar": "وأقدر أساعدك تختار القطعة الأنسب لك.", "en": "I'm happy to help you pick the best piece for you.",
}


def _max_discount(offer, lang):
    description = str(offer.get("description", "") or "").strip()
    return MAX_DISCOUNT[lang].format(code=offer["code"], description=f" ({description})" if description else "")


def _compose(text, offer, size_advice, lang, asks=False):
    """نفس wv._compose_reply بس بفحص الكود بدون حساسية حروف (الموديل ممكن يكتبه صغير). لو الزبون سأل عن خصم
    والكود معتمد: الجملة بتوضّح إنه أقصى خصم معتمد (مش عرض عام)."""
    if offer and text and offer["code"].lower() not in text.lower():
        if asks:
            text = text + "\n\n" + _max_discount(offer, lang)
        else:
            appendix = wv.DISCOUNT_APPENDIX[lang].format(code=offer["code"], description=offer.get("description", ""))
            text = text + "\n\n" + (appendix if offer.get("description") else appendix.replace(" ()", ""))
    if size_advice and size_advice["text"] not in text:
        text = text + "\n\n" + size_advice["text"]
    return text


# جمل بتنفي وجود الأكواد أصلاً ("ما عندي/فيه كود خصم"، "no discount code") — صحيحة بس لما فعلاً ما في كود صالح.
_DENIES_CODES_EXIST = re.compile(
    r"(?:ما|مافي|ماعندي|ماعندنا)\s*(?:في|فيه|عندي|عندنا|عندك|عندكم)?\s*(?:أي|اي)?\s*(?:كود|كوبون|عروض|عرض)"
    r"|لا\s+(?:يوجد|توجد)|there\s+(?:is|are)\s+no\b|\bno\s+(?:discount\s+)?(?:code|coupon)s?\b|don'?t\s+have\s+(?:any\s+)?(?:discount\s+)?(?:code|coupon)",
    re.IGNORECASE,
)


def _drop_denials(sentence):
    """بيشيل بس الجمل الفرعية اللي بتنفي وجود الأكواد ("ما عندي كود خصم أقدّمه لك الحين،") وبيبقي الباقي ("بس القماش ممتاز")."""
    parts = re.split(r"(?<=[،,;؛])\s*", sentence)
    kept = [p for p in parts if not _DENIES_CODES_EXIST.search(guard.normalize(p))]
    if not kept:
        return ""
    out = " ".join(kept).strip()
    out = re.sub(r"^(?:بس|لكن|لاكن)\s+", "", out)
    return out.strip("،, ")


def _ensure_discount_answer(text, inp, offer, lang):
    """الزبون سأل عن خصم/كود: لازم ينقال بوضوح وبصدق أي وحدة من التلات، حتى لو الموديل تهرّب أو المدقّق حذف جملته:
    (1) ما في كود صالح، (2) في كود بس الزبون مش مؤهل حالياً (بدون كشفه ولا الإيحاء إنه مش موجود)،
    (3) الكود المتاح (المعروض هلق أو من قبل) هو أقصى خصم معتمد. الجمل ثابتة وآمنة بالمدقّق بالتصميم."""
    if not inp.asks_discount:
        return text
    empty = not text.strip() or text.strip() == guard.SAFE_REPLY[lang]
    shared = offer or inp.shared_offer
    if shared:
        if shared["code"].lower() not in text.lower():
            base = "" if empty else text + "\n\n"
            return base + _max_discount(shared, lang)
        return text
    reason = inp.strategy.discount.reason
    no_valid = reason in ("no_codes", "expired")
    answer = NO_VALID_CODE[lang] if no_valid else NOT_ELIGIBLE[lang]
    if empty:
        return f"{answer} {KEEP_HELPING[lang]}"
    sentences = guard.sentences(text)
    if not no_valid:
        # في كود صالح والزبون مش مؤهل بعد: ممنوع الإيحاء إنه ما في أكواد ("ما عندي كود خصم"): نشيل هالجمل ونحط الصيغة الصحيحة
        text = " ".join(filter(None, (_drop_denials(x) for x in sentences)))
        sentences = guard.sentences(text)
    if any(guard.is_truthful_no_discount(guard.normalize(sentence)) for sentence in sentences):
        return text
    return f"{answer} {text}".strip()


def _allowed_latin(inp, turn, codes):
    """كلمات لاتينية مشروعة بالرد: SKU، عناوين/فئات/خيارات المنتجات، معلومات المتجر، الأكواد."""
    words = set()
    texts = [inp.store_info or "", " ".join(codes)]
    for product in turn.products:
        texts.extend([product.sku, product.title or "", getattr(product, "category", "") or ""])
        for values in (getattr(product, "options", None) or {}).values():
            texts.extend(str(v) for v in (values if isinstance(values, (list, tuple)) else [values]))
    for chunk in texts:
        words.update(w.lower() for w in re.findall(r"[A-Za-z]{4,}", chunk))
    return words


# كلمات شامية/مصرية ما إلها بديل بـ saudi.to_saudi — بنكمّل عليها محلياً (نفس فكرة to_saudi).
_EXTRA_SUBSTITUTIONS = {
    "بدو": "يبي", "بدها": "تبي", "بدكن": "تبون", "هيدا": "هذا", "هيدي": "هذي", "حابب": "تبي", "حابة": "تبي",
    "عايزة": "تبي", "ايمتى": "متى",
}


def _saudi_text(text):
    text = saudi.to_saudi(text)
    text = saudi._ARABIC_TOKEN.sub(
        lambda m: _EXTRA_SUBSTITUTIONS.get(saudi._plain(m.group(0)), m.group(0)), text
    )
    text = saudi._AFTER_AM.sub(lambda m: m.group(0)[2:].lstrip(), text)  # "عم يشتغل" ← "يشتغل"
    if saudi.find_non_saudi(text):
        # كلمة بقيت بلا بديل آمن: بنحذف الجملة اللي فيها بدل ما توصل للزبون.
        text = " ".join(s for s in guard.sentences(text) if not saudi.find_non_saudi(s)).strip()
    return text


def _fix_dialect(text, cards, lang, gender=""):
    """استبدال حتمي: كلمات غير سعودية + صيغة المخاطَب (مذكر/مؤنث) حسب اللي كشفه الزبون. بيرجّع اللي انصلّح (للسجل)."""
    found = set(saudi.find_non_saudi(text))
    for card in cards:
        found.update(saudi.find_non_saudi(card["reason"]))
        found.update(saudi.find_non_saudi(card["tryon_button_label"]))
    if found:
        text = _saudi_text(text) or guard.SAFE_REPLY[lang]
        for card in cards:
            card["reason"] = _saudi_text(card["reason"])
            label = _saudi_text(card["tryon_button_label"])
            card["tryon_button_label"] = label or wv.TRYON_BUTTON_LABELS[lang]["self"]
    fixed = sorted(found)
    if lang == "ar":
        text, changes = saudi.fix_gender(text, gender)
        for card in cards:
            card["reason"], more = saudi.fix_gender(card["reason"], gender)
            changes += more
            card["tryon_button_label"], more = saudi.fix_gender(card["tryon_button_label"], gender)
            changes += more
        fixed += [f"gender:{c}" for c in changes]
    return text, fixed


MIN_UNSHOWN_TO_HIDE_SEARCH = 2


def _tools_for_turn(inp):
    """أداة البحث بتنخبّى بس لما نكون متأكدين إنها مش لازمة: في مرشّحين كفاية ما انعرضوا،
    وتطابقهم تام (بدون note)، والمنسّق ما طلب إبقاءها (`allow_search`: الزبون بدو منتجات جديدة أو
    إضافية، أو ما قدرنا نفهم رسالته). أي شك ← الأداة ظاهرة."""
    if inp.allow_search:
        return TOOLS
    prefetched = inp.prefetched
    if getattr(prefetched, "note", ""):
        return TOOLS
    candidates = list(getattr(prefetched, "candidates", None) or [])
    shown = set(inp.shown_skus or [])
    unshown = [c for c in candidates if c.product.sku not in shown and c.exact]
    if len(unshown) >= MIN_UNSHOWN_TO_HIDE_SEARCH:
        return [t for t in TOOLS if t["name"] != "search_products"]
    return TOOLS


def run_seller(inp, *, provider=None):
    """يشغّل حلقة البائع ويرجّع contracts.SellerOutput. بيرمي llm.LLMError لو المزوّد واقع
    من أول خطوة (وقتها ما في شي نعرضه)؛ بالخطوات اللاحقة بنختم بردّ ثابت بدل الفشل."""
    started = _now()
    provider = provider or llm.get_provider()
    merchant, lang = inp.merchant, inp.lang
    recommend = prompts_v2.get_recommend()
    turn = _Turn(inp, recommend)
    system_prompt = prompts_v2.build_system_prompt(inp)
    tools_now = _tools_for_turn(inp)
    last_user_text = inp.window[-1]["content"] if inp.window else ""
    offer = dict(inp.approved_offer) if inp.approved_offer else None

    transcript = [dict(m) for m in inp.window]
    steps_ms, llm_calls, steps, text = [], 0, 0, ""
    regen_ms = None

    # ---- الحلقة
    for index in range(MAX_STEPS):
        if _now() - started >= inp.deadline_seconds:
            logger.warning("seller_deadline merchant=%s step=%s", merchant.pk, index + 1)
            break
        final = index == MAX_STEPS - 1 or turn.executed >= MAX_TOOL_CALLS
        step_started = _now()
        try:
            step = llm.chat_step_with_retry(
                provider, system_prompt, transcript, tools_now, tool_choice="none" if final else None
            )
        except llm.LLMError:
            if llm_calls == 0:
                raise
            logger.warning("seller_provider_failed merchant=%s step=%s", merchant.pk, index + 1)
            break
        llm_calls += 1
        steps += 1
        steps_ms.append(int((_now() - step_started) * 1000))
        step_text = (step.text or "").strip()
        calls = list(step.tool_calls or [])
        if final or not calls:
            text = step_text   # خطوة نهائية: أي أداة بترجع بتنتجاهل
            for call in calls:
                turn.events.append({"name": call.name, "input": call.input, "status": "ignored_final_step"})
            break
        results = [turn.execute(call) for call in calls]
        # كرت زيادة مرفوض بسبب الحد (كرت واحد بالرد) ما بيستاهل جولة جديدة للموديل
        all_shown = (
            all(c.name == "show_product" for c in calls)
            and all(r.get("status") in ("shown", "card_limit") for r in results)
            and any(r.get("status") == "shown" for r in results)
        )
        if all_shown and step_text:
            text = step_text   # مختصر: ما في شي نرجّعه للموديل
            break
        if all_shown and turn.cards and (turn.cards[0].get("reason") or "").strip():
            # الموديل (خصوصاً OpenAI) كثير بيرسل show_product بلا نص وبيستنى النتيجة: جولة كاملة (~2 ث)
            # لمجرد كتابة نص. الـ reason بالكرت الأول مكتوب كرد كامل للزبون (انظر وصف الأداة) ← نستخدمه.
            text = turn.cards[0]["reason"].strip()
            turn.events.append({"name": "reason_as_reply", "input": {}, "status": "used"})
            break
        transcript.append(llm.assistant_entry(step))
        for call, result in zip(calls, results):
            transcript.append(llm.tool_entry(call, json.dumps(result, ensure_ascii=False, default=str)))

    cards = turn.cards
    size_advice = turn.size_advice

    # ---- تركيب الرد (وبدائل لو ما كتب الموديل نص)
    if not text:
        text = wv._fallback_reply(
            cards, wv._has_purchase_intent(last_user_text), turn.blocked_reason, offer, size_advice,
            turn.size_calls, lang,
        )
    codes = [c.code for c in merchant.discount_codes.all()]
    allowed_latin = _allowed_latin(inp, turn, codes)
    text, stray = saudi.strip_stray(text, last_user_text, allowed_latin)
    # شبكة اللغة بتفحص نص الموديل قبل ما نلزق عليه جمل ثابتة (كود/مقاس) بلغة الزبون.
    mismatch = wv._script_mismatch(last_user_text, text)
    text = _compose(text, offer, size_advice, lang, inp.asks_discount)

    # كرت بسبب بحروف لغة تانية: منفضّيه (الكرت صالح بدونه، وإعادة التوليد ما بتصلّحه).
    for card in cards:
        if card["reason"] and wv._script_mismatch(last_user_text, card["reason"]):
            card["reason"] = ""

    # ---- شبكة اللغة + المدقّق + اللهجة: إعادة توليد وحدة كحد أقصى لكل هالطبقات مجتمعة
    check_dialect = lang == "ar"

    def grounding():
        return guard.build_context(
            products=turn.products, merchant_codes=codes, approved_offer=offer or inp.shared_offer, cards=cards,
            store_info=inp.store_info, customer_texts=inp.customer_texts,
        )

    trace = {}
    ctx = grounding()
    violations = wv._response_violations(text, cards, ctx, check_dialect)
    if violations:
        trace["guard_first"] = guard.kinds(violations)
        logger.warning("output_guard merchant=%s stage=first kinds=%s", merchant.pk, guard.kinds(violations))
    if (mismatch or violations) and _now() - started < inp.deadline_seconds:
        correction = ""
        if mismatch:
            correction += MISMATCH_CORRECTION.format(message=json.dumps(last_user_text, ensure_ascii=False))
        if violations:
            correction += wv._correction_text(violations)
        if cards:
            correction += CARDS_NOTE.format(skus=", ".join(c["sku"] for c in cards))
        regen_started = _now()
        llm_calls += 1
        try:
            regen = llm.chat_step_with_retry(
                provider, system_prompt + correction, transcript, tools_now, tool_choice="none"
            )
            new_raw, more_stray = saudi.strip_stray((regen.text or "").strip(), last_user_text, allowed_latin)
            stray += more_stray
            # تصحيح لغة ما نجح ← منبقي على القديم (نفس سلوك v1)
            if new_raw and not (mismatch and wv._script_mismatch(last_user_text, new_raw)):
                text = _compose(new_raw, offer, size_advice, lang, inp.asks_discount)
        except llm.LLMError:
            logger.warning("seller_regen_failed merchant=%s", merchant.pk)
        regen_ms = int((_now() - regen_started) * 1000)
        trace["guard_regenerated"] = True
        violations = wv._response_violations(text, cards, ctx, check_dialect)

    promo_left = [v for v in violations if v.kind != wv.DIALECT]
    if promo_left:
        trace["guard_strip"] = guard.kinds(promo_left)
        logger.warning("output_guard merchant=%s stage=strip kinds=%s", merchant.pk, guard.kinds(promo_left))
        text = guard.strip_violations(text, ctx) or guard.SAFE_REPLY[lang]
        text = _compose(text, offer, size_advice, lang, inp.asks_discount)   # لو انشالت جملة الكود
        for card in cards:
            card["reason"] = guard.strip_violations(card["reason"], ctx)
            if guard.find_violations(card["tryon_button_label"], ctx):
                card["tryon_button_label"] = wv.TRYON_BUTTON_LABELS[lang]["self"]
    for card in cards:   # الكروت كمان: سبب الكرت وزر التجربة
        card["reason"], more = saudi.strip_stray(card["reason"], last_user_text, allowed_latin)
        stray += more
        card["tryon_button_label"], more = saudi.strip_stray(card["tryon_button_label"], last_user_text, allowed_latin)
        stray += more
    text, more = saudi.strip_stray(text, last_user_text, allowed_latin)
    stray += more
    if stray:
        trace["stray_removed"] = stray
        logger.warning("seller_stray_text_removed merchant=%s parts=%s", merchant.pk, stray)
        if not text.strip():
            text = guard.SAFE_REPLY[lang]
    text = _ensure_discount_answer(text, inp, offer, lang)
    if check_dialect:
        text, substituted = _fix_dialect(text, cards, lang, inp.gender)
        text = _compose(text, offer, size_advice, lang, inp.asks_discount)
        if substituted:
            trace["dialect_substituted"] = substituted
            logger.warning("saudi_dialect_substituted merchant=%s words=%s", merchant.pk, substituted)

    now = _now()
    timings = {"seller_ms": int((now - started) * 1000), "steps_ms": steps_ms}
    if regen_ms is not None:
        timings["regen_ms"] = regen_ms
    return contracts.SellerOutput(
        reply=text,
        recommendations=cards,
        discount_offer=offer,
        size_advice={k: v for k, v in size_advice.items() if k != "text"} if size_advice else None,
        llm_calls=llm_calls,
        steps=steps,
        tool_events=turn.events,
        guard=trace,
        timings=timings,
    )
