"""اللهجة السعودية — مصدر واحد للبرومبتات والفحص الحتمي.

المطلوب: بياع ملابس سعودي محترف — ودود، واثق، مقنع، طبيعي، مو رسمي ولا روبوتي.
مو شامي ولا مصري ولا فصحى متكلّفة. الفحص هون شبكة أمان بالكود فوق البرومبت:
- find_non_saudi: كلمات شامية/مصرية ما بتنقال بالسعودي.
- to_saudi: استبدال حتمي لأشهر الكلمات لو الموديل زلّ بعد إعادة المحاولة.
"""

import re

STYLE_BLOCK = """SAUDI ARABIC — mandatory whenever the customer writes Arabic (any dialect, MSA or Arabizi)
You are a professional Saudi salesperson in a clothing store, texting a customer on WhatsApp: warm, \
quick, confident, friendly and persuasive — a real person, never a script.
- Write natural everyday Saudi (Gulf/Najdi-Hejazi) in Arabic letters. Typical, natural words and \
phrases (use a few per reply, never all of them, never forced): يا هلا، حيّاك الله، أبشر، أبشر بالخير، \
وش (what)، ليش، زين، مرّة (very)، الحين، أبي / تبي / تبغى، ودّك، وش رايك، عاد، كذا، عشان، طيب، تمام، \
يجنّن، فخم، يستاهل، ما عليك، على راحتك، لا تشيل هم.
- NEVER Syrian/Levantine: شو، هلق، هلأ، كتير، بدي، بدك، بدنا، منيح، هيك، لسا، إلك، يلي، مشان، هاد، هاي، \
حابب، قديش، رح (future)، عم + verb، بكرا. NEVER Egyptian (دلوقتي، إزاي، عايز، كده، لسه). \
NEVER stiff Modern Standard Arabic (لقد، سوف، إنني، يسرّنا، نودّ إعلامك، بإمكانك) — say it the way a \
Saudi shop assistant says it out loud.
- Address the customer neutrally/masculine (تبي، ودّك، جرّب، شوف) until THEY reveal otherwise in their own words — never guess feminine from what they shop for.
- Short, lively sentences. At most one or two emojis. No lectures, no corporate phrases, and never \
repeat the same opening line, closing line or compliment twice in a conversation — vary how you \
sound like a person would.
- This applies to EVERYTHING the customer sees: replies, questions, objections, discount lines, \
card reasons, button labels, apologies and error-ish moments (e.g. you don't have an answer: say it \
in warm Saudi, not in a canned formal sentence)."""

# كلمات ما بتنقال بالسعودي. بنقارن token كامل (بعد شيل التشكيل) — مش substring.
_NON_SAUDI = {
    # شامي
    "شو", "هلق", "هلأ", "هلئ", "كتير", "كتيير", "بدي", "بدك", "بدنا", "بدو", "بدها", "بدكن", "منيح",
    "هيك", "لسا", "إلك", "الك", "يلي", "مشان", "منشان", "هاد", "هادا", "هاي", "هيدا", "هيدي",
    "حابب", "حابة", "قديش", "أديش", "اديش", "رح", "بكرا", "كلشي", "ازا", "ليكي", "ايمتى",
    # مصري — بس الكلمات المميّزة. كلمات مشتركة مع الحجازي أو أصلها فصيح (هادي = calm،
    # كده/كدا، لسه، فين، إيه) ما بنعتبرها غلط حتى ما نشوّه كلام سعودي سليم.
    "دلوقتي", "دلوقتى", "إزاي", "ازاي", "عايز", "عايزة", "أوي", "اوي",
}
_AFTER_AM = re.compile("(?<![\u0621-\u065f\u0670-\u06d3])عم\\s+[يتنأا]")  # "عم يشتغل" شامي، "عم" لوحدها (عمّي) لأ
_DIACRITICS = re.compile(r"[ً-ٰٟـ]")
# حروف + تشكيل بس (بدون علامات الترقيم العربية مثل ؟ ، ؛ وبدون الأرقام)
_ARABIC_TOKEN = re.compile("[\u0621-\u065f\u0670-\u06d3]+")

_SUBSTITUTIONS = {
    "شو": "وش", "هلق": "الحين", "هلأ": "الحين", "هلئ": "الحين", "كتير": "مرّة", "كتيير": "مرّة",
    "بدي": "أبي", "بدك": "تبي", "بدنا": "نبي", "منيح": "زين", "هيك": "كذا", "لسا": "للحين",
    "إلك": "لك", "الك": "لك", "يلي": "اللي", "مشان": "عشان", "منشان": "عشان", "هاد": "هذا",
    "هادا": "هذا", "هاي": "هذي", "ازا": "إذا", "قديش": "كم", "أديش": "كم",
    "اديش": "كم", "رح": "راح", "بكرا": "باكر", "كلشي": "كل شي",
    "دلوقتي": "الحين", "دلوقتى": "الحين", "إزاي": "كيف", "ازاي": "كيف",
    "عايز": "أبي", "أوي": "مرّة", "اوي": "مرّة",
}


def _plain(token):
    return _DIACRITICS.sub("", token)


def find_non_saudi(text):
    """قائمة الكلمات غير السعودية الموجودة بالنص."""
    hits = []
    for raw in _ARABIC_TOKEN.findall(text or ""):
        token = _plain(raw)
        if token in _NON_SAUDI and token not in hits:
            hits.append(token)
    if _AFTER_AM.search(text or "") and "عم" not in hits:
        hits.append("عم")
    return hits


def to_saudi(text):
    """استبدال حتمي لكلمات شامية/مصرية شائعة. ما بيلمس الباقي (والكلمات يلي
    ما إلها بديل آمن بتنذكر بالسجل مش بتنغيّر)."""
    def swap(match):
        raw = match.group(0)
        return _SUBSTITUTIONS.get(_plain(raw), raw)

    return _ARABIC_TOKEN.sub(swap, text or "")


def unmapped(text):
    return [w for w in find_non_saudi(text) if w not in _SUBSTITUTIONS]


# ---------------------------------------------------------------- صيغة المذكر/المؤنث بخطاب الزبون
# شبكة أمان حتمية فوق البرومبت: قائمة مغلقة من أفعال البيع الشائعة بصيغة المخاطَب.
# - جنس الزبون مجهول أو ذكر ← أي صيغة مؤنثة بتنقلب مذكر (ما منفترض إنه امرأة من نوع القطعة).
# - الزبون كشف إنه أنثى بكلامه ← أي صيغة مذكرة بتنقلب مؤنث، حتى ما تتخلط الصيغ.
# بتشتغل على كلمات كاملة (مع سابقة و/ف وضمير متصل ه/ها/ك/لي/لك). ما بتلمس غير القائمة.
_GENDER_PAIRS = (  # (مذكر، مؤنث) بدون تشكيل
    ("جرب", "جربي"), ("شوف", "شوفي"), ("خذ", "خذي"), ("كمل", "كملي"), ("اختر", "اختاري"), ("ارفع", "ارفعي"),
    ("اطلب", "اطلبي"), ("تعال", "تعالي"), ("فكر", "فكري"), ("ارجع", "ارجعي"), ("قارن", "قارني"),
    ("اضغط", "اضغطي"), ("استخدم", "استخدمي"), ("تأكد", "تأكدي"), ("خلك", "خليك"),
    ("تقدر", "تقدرين"), ("تبي", "تبين"), ("تبغى", "تبغين"), ("تحتاج", "تحتاجين"), ("تختار", "تختارين"),
    ("تشوف", "تشوفين"), ("تلبس", "تلبسين"), ("تطلب", "تطلبين"), ("تكمل", "تكملين"), ("تحب", "تحبين"),
    ("تفضل", "تفضلين"), ("ترتاح", "ترتاحين"), ("تتهنى", "تتهنين"), ("تجرب", "تجربين"), ("تفكر", "تفكرين"),
    ("تقرر", "تقررين"), ("تاخذ", "تاخذين"), ("تضغط", "تضغطين"), ("تعرف", "تعرفين"), ("تقارن", "تقارنين"),
    ("تتأكد", "تتأكدين"), ("تستخدم", "تستخدمين"), ("تدور", "تدورين"), ("تنتظر", "تنتظرين"), ("تبدا", "تبدين"),
    ("تضيف", "تضيفين"), ("تختارها", "تختارينها"),
)
_FEM_TO_MASC = {f: m for m, f in _GENDER_PAIRS}
_MASC_TO_FEM = {m: f for m, f in _GENDER_PAIRS}
_PREFIXES = ("", "و", "ف")
_SUFFIXES = ("", "ه", "ها", "ك", "لي", "لك")


def _swap_gender_token(raw, table):
    plain = _plain(raw)
    for prefix in _PREFIXES:
        if prefix and not plain.startswith(prefix):
            continue
        body = plain[len(prefix):]
        for suffix in _SUFFIXES:
            if suffix and not body.endswith(suffix):
                continue
            stem = body[: len(body) - len(suffix)] if suffix else body
            if stem in table:
                return prefix + table[stem] + suffix
    return None


def fix_gender(text, gender):
    """(نص مصحّح، قائمة التغييرات). gender: "female" | "male" | "" (مجهول = مذكر عام)."""
    female = gender == "female"
    table = _MASC_TO_FEM if female else _FEM_TO_MASC
    changes = []

    def swap(match):
        new = _swap_gender_token(match.group(0), table)
        if new is None:
            return match.group(0)
        changes.append(f"{_plain(match.group(0))}->{new}")
        return new

    return _ARABIC_TOKEN.sub(swap, text or ""), changes


# ---------------------------------------------------------------- أجزاء غريبة بالرد
# أحياناً الموديل بيلصق بآخر الرد كلمة بحروف تانية ("оставка")، أو صيغة نداء أداة ("show_product(sku=...)")، أو
# رموز تالفة. هدول ما بيوصلوا للزبون. أبجديات غير العربية/اللاتينية بتنشال إلا لو الزبون نفسه كاتب فيها.
_FOREIGN_SCRIPTS = re.compile(
    "[\u0370-\u03ff\u0400-\u052f\u0530-\u058f\u0590-\u05ff\u0900-\u0dff\u0e00-\u0eff\u1100-\u11ff"
    "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+"
)
_GARBAGE_CHARS = re.compile("[\ufffd\u200b-\u200f\u202a-\u202e\u2060\ufeff\x00-\x08\x0b\x0c\x0e-\x1f]")
_TOOL_CALL = re.compile(
    r"\b(?:show_product|search_products|get_product|compare_products|recommend_size|update_customer_profile|"
    r"offer_discount_code|recommend_product)\s*\([^)]*\)?",
    re.IGNORECASE,
)
# كلمة لاتينية (4 حروف وأكثر) ملزوقة بحرف/علامة عربية بدون مسافة: ".ostavka" — بقايا، مش اسم منتج.
_GLUED_LATIN = re.compile("(?<=[\u0600-\u06ff.،؟!؛:])([A-Za-z]{4,})(?![A-Za-z0-9_-])")


def strip_stray(text, customer_text="", allowed_latin=frozenset()):
    """(نص نظيف، قائمة اللي انشال). allowed_latin: كلمات لاتينية مشروعة (SKU، عناوين منتجات، أكواد، معلومات المتجر)."""
    original = text or ""
    removed = []
    customer_scripts = set(_FOREIGN_SCRIPTS.findall(customer_text or ""))

    def drop(match):
        if customer_scripts:           # الزبون كاتب بهالأبجدية: ما منلمسها
            return match.group(0)
        removed.append(match.group(0))
        return ""

    text = _GARBAGE_CHARS.sub("", original)
    text = _FOREIGN_SCRIPTS.sub(drop, text)

    def drop_call(match):
        removed.append(match.group(0))
        return ""

    text = _TOOL_CALL.sub(drop_call, text)

    allowed = {w.lower() for w in allowed_latin}

    def drop_latin(match):
        if match.group(1).lower() in allowed:
            return match.group(0)
        removed.append(match.group(1))
        return ""

    text = _GLUED_LATIN.sub(drop_latin, text)
    if removed or text != original:
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\s+([،.؟!؛:])", r"\1", text).strip()
    return text, removed
