"""فحص وتصحيح الرد العربي: لهجة سعودية، ومخاطبة بالمذكّر لحد ما الزبون يبيّن إنها بنت.

الموديل بيغلط بشغلتين بشكل متكرر حتى مع تعليمات صريحة:
- بادئة «هال» الشامية (هالفستان) — منصلّحها بالكود: «هذا الفستان» / «هذي العباية».
- بيخاطب الزبون بالمؤنث لأنه المنتج فستان (ارفعي، تقدرين) — منصلّح الأفعال الشائعة بالكود.
وأي كلمة شامية/مصرية واضحة أو فعل مؤنث مو معروف بيرجع كـ "مشكلة" لإعادة صياغة وحدة.
"""

import re

_WORD = re.compile(r"[ء-يـً-ْ]+")
_DIACRITICS = re.compile(r"[ـً-ْ]")

# الزبونة حكت عن حالها بالمؤنث
_FEMALE_SELF = re.compile(
    r"(?:^|\s)(?:متأكده|متاكده|محتاره|متردده|عايزه|عاوزه|حابه|ناويه|رايحه|مدعوه|مبسوطه|جاهزه|متحمسه|"
    r"خايفه|مستعجله|معزومه|لابسه|ابغاها لنفسي|ابيها لنفسي|انا بنت|انا مره|انا امراه|انا عروس|"
    r"اني بنت|كنت حابه|بدي ياه لالي|لحالي ابغاه)(?=\s|$|[،.!؟?])"
)

# فعل مؤنث ← مذكّر (بعد شيل التشكيل). منضيف و/ف قبلها تلقائياً.
_MASCULINE = {
    "ارفعي": "ارفع", "جربي": "جرب", "جربيه": "جربه", "جربيها": "جربها", "شوفي": "شوف",
    "شوفيه": "شوفه", "شوفيها": "شوفها", "استخدمي": "استخدم", "اختاري": "اختار", "خذي": "خذ",
    "خذيه": "خذه", "اطلبي": "اطلب", "اطلبيه": "اطلبه", "كملي": "كمل", "اضغطي": "اضغط",
    "قولي": "قول", "عطيني": "عطني", "ارسلي": "ارسل", "تاكدي": "تاكد", "تأكدي": "تأكد",
    "اكتبي": "اكتب", "البسيه": "البسه", "تقدرين": "تقدر", "تشوفين": "تشوف", "تشوفينه": "تشوفه",
    "تحتاجين": "تحتاج", "تبينه": "تبيه", "ترجعينه": "ترجعه", "ترجعين": "ترجع",
    "تطلبينه": "تطلبه", "تطلبين": "تطلب", "تجربينه": "تجربه", "تجربين": "تجرب", "تحبين": "تحب", "تحبينه": "تحبه", "تشوفينها": "تشوفها", "تلبسينها": "تلبسها",
    "تختارين": "تختار", "تلبسينه": "تلبسه", "تلبسين": "تلبس", "تطمنين": "تطمن", "تكملين": "تكمل",
    "تدخلين": "تدخل", "ترفعين": "ترفع", "تضغطين": "تضغط", "تبغين": "تبغى",
    "تعرفين": "تعرف", "تفضلين": "تفضل", "تخلينه": "تخليه", "تستاهلين": "تستاهل", "تبغينه": "تبغاه",
}

# كلمات شامية/مصرية واضحة ما بيقولها سعودي
NON_SAUDI = {
    "شو", "كتير", "كتيره", "هلق", "هلأ", "هيك", "منيح", "منيحه", "بدك", "بدي", "عنجد",
    "هاد", "هادا", "هيدا", "هيدي", "عايز", "عايزه", "ازاي", "إزاي", "كده", "كدا", "دلوقتي", "اوي",
    "أوي", "بتاعك", "بتاعتك", "قديش", "ليكي", "شلونك",
}
# كلمات بشكل t…ين وهي مو أفعال مؤنثة
_NOT_VERBS = {
    "تأمين", "تامين", "تلوين", "تكوين", "تخزين", "تحسين", "تزيين", "تعيين", "تخمين", "تسخين",
    "تدوين", "تموين", "تحصين", "تمرين", "تسعين", "تبيين", "تعليين", "تبين", "وتبين", "يتبين",
}
_FEM_VERB = re.compile(r"^(?:[وف]?ب?)ت[ء-ي]{2,}ين(?:ه|ها)?$")
_HAL = re.compile(r"(?<![ء-ي])([وف]?)هال([ء-ي]{2,})")


def _plain(word):
    return _DIACRITICS.sub("", word)


def _norm(text):
    text = _DIACRITICS.sub("", text or "")
    return text.replace("ة", "ه").replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")


_MALE_SELF = re.compile(
    r"(?:^|\s)(?:انا|اني)\s+(?:رجال|رجل|ذكر|شب|شاب|ولد|رجّال)(?=\s|$|[،.!؟?])"
    r"|(?:^|\s)(?:انا|اني)\s+(?:بدي|ابي|ابغى|ابغي|اريد|بدور على|ادور)\s+(?:\S+\s+)?"
    r"(?:ذكوري|ذكوريه|رجالي|رجاليه|شبابي|للرجال|للشباب)(?=\s|$|[،.!؟?])"
    r"|\b(?:i'?m|i am)\s+(?:a\s+)?(?:man|guy|male|dude)\b",
    re.IGNORECASE,
)


def male_revealed(messages):
    """الزبون قال عن حاله إنه رجال/شب (انا رجال، انا ذكر، I'm a man)."""
    return any(m["role"] == "user" and _MALE_SELF.search(_norm(m["content"]).lower()) for m in messages)


def female_revealed(messages):
    """الزبونة استخدمت صيغة مؤنثة عن حالها بأي رسالة."""
    return any(m["role"] == "user" and _FEMALE_SELF.search(_norm(m["content"])) for m in messages)


def _fix_hal(match):
    prefix, noun = match.group(1), match.group(2)
    if noun == "حين":
        return prefix + "الحين"
    demonstrative = "هذي" if noun.endswith("ة") or noun.endswith("ه") else "هذا"
    return f"{prefix}{demonstrative} ال{noun}"


def _fix_word(match):
    word = match.group(0)
    plain = _plain(word)
    if word.endswith("كِ"):  # عليكِ، لكِ ← عليك، لك
        return word[:-1]
    for prefix in ("", "و", "ف", "ب", "وب", "فب"):
        if prefix and not plain.startswith(prefix):
            continue
        stem = plain[len(prefix):]
        if stem in _MASCULINE:
            return _drop_letters(word, plain, prefix + _MASCULINE[stem])
    return word


def _drop_letters(word, plain, target):
    """بيحذف من الكلمة الأصلية الحروف اللي مو بالهدف (ي/ين المؤنث) ومحافظ على تشكيلها
    (جرّبيه ← جرّبه). إذا الهدف مو حذف بسيط (تبغين ← تبغى) بيرجّع الهدف بلا تشكيل."""
    keep, j = [], 0
    for ch in plain:
        if j < len(target) and ch == target[j]:
            keep.append(True)
            j += 1
        else:
            keep.append(False)
    if j != len(target):
        return target
    out, letter = [], -1
    for ch in word:
        if _DIACRITICS.match(ch):
            if letter >= 0 and keep[letter]:
                out.append(ch)
            continue
        letter += 1
        if keep[letter]:
            out.append(ch)
    return "".join(out)


def fix(text, female=False):
    """بيصلّح «هال» دايماً، والأفعال المؤنثة الشائعة إذا الزبون ما بيّن إنها بنت."""
    if not text:
        return text
    text = _HAL.sub(_fix_hal, text)
    if not female:
        text = re.sub(r"[ء-يً-ْـ]+ِ?", _fix_word, text)
    return text


def issues(text, female=False):
    """اللي ضل بعد fix: كلمات مو سعودية، وأفعال مؤنثة ما عرفنا نصلحها."""
    found = []
    for raw in _WORD.findall(text or ""):
        word = _plain(raw)
        bare = word[1:] if word[:1] in ("و", "ف") and len(word) > 3 else word
        if word in NON_SAUDI or bare in NON_SAUDI:
            found.append(word)
        elif not female and _FEM_VERB.match(word) and bare not in _NOT_VERBS and word not in _NOT_VERBS:
            found.append(word)
    return found
