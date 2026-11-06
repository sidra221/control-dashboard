"""عقود مشتركة بين مكوّنات محرّك المبيعات (v2). أشكال بيانات بس — بدون منطق.

recommend.py   ← بيبني Requirements من الذاكرة وبيرجّع SearchResult
stage.py/policy.py ← بيقرر Strategy (مرحلة البيع + الخصم) من الإشارات والذاكرة
seller_v2.py   ← بياخد كل هاد وبيشغّل حلقة الأدوات وبيرجّع SellerOutput
v2_turn.py     ← بيربط الكل

ما حدا يعدّل هالملف بدون تنسيق: ثلاث وحدات بتعتمد عليه بنفس الوقت.
"""

from dataclasses import dataclass, field
from decimal import Decimal

STAGES = ("discovery", "recommendation", "objection_handling", "persuasion", "closing")
READINESS = ("browsing", "interested", "hesitant", "ready")


# ------------------------------------------------------------------ توصيات

@dataclass(frozen=True)
class Requirements:
    """متطلبات الزبون الصريحة — بتنفرض بالكود (فلاتر صلبة)، مش بالبرومبت."""

    budget_max: Decimal = None
    budget_min: Decimal = None
    colors: tuple = ()            # ألوان مطلوبة بأسماء قياسية إنجليزية: ("black",)
    avoid_colors: tuple = ()
    sizes: tuple = ()             # مقاسات مطلوبة قياسية: ("M",) أو ("42",)
    categories: tuple = ()        # فئات قياسية: ("dress",)
    excluded_skus: frozenset = frozenset()   # مرفوضة
    exempt_skus: frozenset = frozenset()     # طلبها الزبون صراحة هالدور ← بتتجاوز فلتر الميزانية/الرفض
    include_out_of_stock: bool = False


@dataclass
class Reason:
    """حقيقة وحدة من بيانات الكتالوج بتبرّر الترشيح (مش إنشاء حر)."""

    kind: str   # color | size | category | budget | sale | tag | stock | similar | price_gap
    text: str   # جملة إنجليزية قصيرة مبنية من حقول المنتج فقط


@dataclass
class Candidate:
    product: object
    score: float = 0.0
    reasons: list = field(default_factory=list)      # list[Reason]
    exact: bool = True                               # حقق كل المتطلبات؟
    relaxed: tuple = ()                              # متطلبات تساهلنا فيها: ("color",)
    warnings: list = field(default_factory=list)     # مثلاً "color blue not available; shown in black"


@dataclass
class SearchResult:
    candidates: list = field(default_factory=list)   # list[Candidate] مرتبة
    exact_count: int = 0
    relaxed_fields: tuple = ()
    considered: int = 0
    dropped: dict = field(default_factory=dict)      # {"budget": n, "color": n, "size": n, "category": n, "rejected": n, "stock": n}
    note: str = ""                                   # "" | "no_exact_match" | "nothing_matches"


# ------------------------------------------------------------------ مبيعات

@dataclass
class DiscountVerdict:
    allowed: bool = False
    code: object = None          # DiscountCode (فعّال وضمن تواريخه) لو allowed
    reason: str = "no_trigger"   # معرّف القاعدة: ok | no_codes | expired | too_early | already_offered | not_engaged | no_trigger | alternative_first | ready_no_price_concern
    trigger: str = ""            # price_objection | hesitation | asked | ""
    rule_trace: dict = field(default_factory=dict)


@dataclass
class Strategy:
    stage: str = "discovery"
    readiness: str = "browsing"
    directives: list = field(default_factory=list)   # أسطر إنجليزية للبائع: شو يعمل هالدور
    ask_slot: str = None            # سؤال توضيحي واحد كحد أقصى: category|occasion|color|size|budget|None
    forbid: list = field(default_factory=list)       # ["ask_question", "push_discount", "upsell"]
    alternative_first: bool = False
    stay_sku: str = ""              # إذا تعبّى: البائع يرفض أي كرت غير هالقطعة
    hold_cards: bool = False        # الزبون ما قال شو بده: ممنوع أي كرت
    discount: DiscountVerdict = field(default_factory=DiscountVerdict)
    state_updates: dict = field(default_factory=dict)  # بتنحفظ بـ Conversation.state


# ------------------------------------------------------------------ البائع

@dataclass
class SellerInput:
    merchant: object
    window: list                    # [{"role","content"}] آخر رسائل (بتنتهي برسالة زبون)
    lang: str                       # "ar" | "en"
    memory_section: str             # من memory.build_memory_section
    strategy: Strategy
    requirements: Requirements
    prefetched: SearchResult        # مرشّحين جاهزين (بتوفّر جولة أدوات)
    shown_skus: list = field(default_factory=list)
    page_product: object = None
    page_note: str = ""
    store_info: str = ""
    currency: str = ""
    approved_offer: dict = None     # {"code","description"} بعد ما السياسة وافقت؛ None = ممنوع أي ذكر لخصم
    # كود انعرض للزبون أصلاً بهالمحادثة (مش عرض جديد): بس للتذكير لو سأل عن خصم أكبر/الكود، والمدقّق بيسمح بذكره.
    shared_offer: dict = None
    asks_discount: bool = False     # الزبون سأل عن خصم/كود بهالرسالة (المحلّل): بنضمن جواب واضح وصادق
    gender: str = ""               # "female" | "male" | "" (ما كشفه الزبون بكلامه)
    customer_texts: list = field(default_factory=list)   # لتدقيق المخرجات (أرقام الزبون)
    deadline_seconds: float = 25.0
    # true = أداة البحث لازم تبقى ظاهرة للموديل (الزبون محتاج منتجات جديدة/إضافية، أو ما قدرنا نفهم رسالته)
    allow_search: bool = False
    # منتجات كانت مخفية (مرفوضة) وطلبها الزبون صراحة هالدور: بس هدول مسموح نعيد عرض كرتهم. سؤال عادي عن
    # منتج معروض ("كم الشحن له؟") ما بيعيد كرته — كرته أصلاً على الشاشة.
    reshow_skus: frozenset = frozenset()


@dataclass
class SellerOutput:
    reply: str = ""
    recommendations: list = field(default_factory=list)   # كروت (نفس شكل widget_views._product_card + reason, tryon_*)
    discount_offer: dict = None
    size_advice: dict = None
    llm_calls: int = 0
    steps: int = 0
    tool_events: list = field(default_factory=list)       # [{"name","input","status"}]
    guard: dict = field(default_factory=dict)             # {"guard_first": [...], "guard_strip": [...], "dialect_substituted": [...]}
    timings: dict = field(default_factory=dict)           # {"seller_ms": int, "steps_ms": [..]}
