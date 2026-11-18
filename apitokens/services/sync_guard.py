"""حماية الكتالوج من المسح بالغلط وقت المزامنة الكاملة.

المزامنة بتحذف المنتجات اللي ما رجعت من المنصة (يعني انحذفت هناك). بس إذا الجلب نفسه ناقص
(سقف صفحات، هيدر عدد الصفحات ضاع، خلل مؤقت رجّع قائمة فاضية)، الحذف بيمسح منتجات حقيقية.
- CatalogIncomplete: الجالب بيرميها لما يعرف إنه ما جاب كل شي (بعد ما يرجّع يلي جابه).
- should_prune: ما منحذف إذا الجلب ناقص، أو إذا رجع أقل من نص المنتجات اللي عنا (أو صفر).
  حذف منتج واحد من التاجر بيضل يوصل عبر الـ webhook متل ما هو.
"""
import logging

logger = logging.getLogger(__name__)

# فوق هالعدد منطبّق قاعدة "النص"؛ تحته (متجر صغير) بس الصفر بيوقف الحذف.
SHRINK_GUARD_MIN_EXISTING = 20
SHRINK_GUARD_RATIO = 0.5


class CatalogIncomplete(Exception):
    """الجلب ما وصل لآخر الكتالوج. partial = اللي انجاب (للجالبين اللي برجّعوا قائمة)."""

    def __init__(self, reason, partial=()):
        super().__init__(reason)
        self.reason = reason
        self.partial = list(partial)


def should_prune(merchant, existing, fetched, complete, source):
    """هل آمن نحذف المنتجات اللي ما رجعت؟ existing = منتجات المزامنة عنا هلأ، fetched = اللي رجع."""
    if not complete:
        logger.warning("catalog_prune_skipped merchant=%s source=%s reason=incomplete existing=%s fetched=%s",
                       merchant.pk, source, existing, fetched)
        return False
    if existing and not fetched:
        logger.error("catalog_prune_skipped merchant=%s source=%s reason=empty_fetch existing=%s",
                     merchant.pk, source, existing)
        return False
    if existing >= SHRINK_GUARD_MIN_EXISTING and fetched < existing * SHRINK_GUARD_RATIO:
        logger.error("catalog_prune_skipped merchant=%s source=%s reason=sharp_drop existing=%s fetched=%s",
                     merchant.pk, source, existing, fetched)
        return False
    return True
