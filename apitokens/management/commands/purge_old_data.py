"""بيحذف بيانات الزبائن الأقدم من DATA_RETENTION_DAYS (الافتراضي 365 يوم):

- المحادثات (Conversation) حسب updated_at: آخر نشاط فيها (رسالة، تجربة، سلة، أو ربط طلب)، فالمحادثة
  بتنحسب من نهايتها مو من بدايتها، وما بتنحذف محادثة لسا شغّالة أو انربط فيها طلب مؤخراً.
- التجارب الافتراضية (TryOnEvent) واستهلاك الشات اليومي (LlmUsage).
- أكواد التحقق (PhoneOtp) المستخدمة أو المنتهية للحسابات الموثّقة الأقدم من 30 يوم (الحسابات غير
  الموثّقة بيتكفّل فيها purge_unverified_accounts).

ما بيلمس المنتجات ولا المتاجر ولا الحسابات ولا أكواد الخصم. الحذف على دفعات حتى ما يطوّل القفل،
وبيطبع أعداد بس (بدون أي بيانات شخصية). شغّلها من cron يومياً:

    python manage.py purge_old_data [--dry-run] [--batch-size 5000]
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from apitokens.models import Conversation, LlmUsage, PhoneOtp, TryOnEvent

OTP_RETENTION_DAYS = 30


def _batched_delete(queryset, batch_size):
    """بيحذف على دفعات بالـ pk (كل دفعة استعلام قصير لحاله). برجّع عدد الصفوف المحذوفة."""
    model = queryset.model
    total = 0
    while True:
        pks = list(queryset.order_by("pk").values_list("pk", flat=True)[:batch_size])
        if not pks:
            return total
        _, per_model = model.objects.filter(pk__in=pks).delete()
        total += per_model.get(model._meta.label, 0)


class Command(BaseCommand):
    help = "Delete shopper data (conversations, try-ons, chat usage, used OTPs) past the retention period"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="print counts only, delete nothing")
        parser.add_argument("--batch-size", type=int, default=5000, help="rows per delete batch (default 5000)")

    def handle(self, *args, **options):
        days = settings.DATA_RETENTION_DAYS
        batch_size = options["batch_size"]
        if days < 1:
            raise CommandError("DATA_RETENTION_DAYS must be >= 1")
        if batch_size < 1:
            raise CommandError("--batch-size must be >= 1")

        now = timezone.now()
        cutoff = now - timedelta(days=days)
        otp_cutoff = now - timedelta(days=OTP_RETENTION_DAYS)
        targets = [
            ("conversations", Conversation.objects.filter(updated_at__lt=cutoff)),
            ("try-on events", TryOnEvent.objects.filter(created_at__lt=cutoff)),
            ("chat usage rows", LlmUsage.objects.filter(day__lt=timezone.localdate(cutoff))),
            ("verification codes", PhoneOtp.objects.filter(
                Q(consumed_at__isnull=False) | Q(expires_at__lt=now),
                account__phone_verified=True, created_at__lt=otp_cutoff,
            )),
        ]

        verb = "would delete" if options["dry_run"] else "deleted"
        for name, queryset in targets:
            count = queryset.count() if options["dry_run"] else _batched_delete(queryset, batch_size)
            self.stdout.write(f"{verb} {count} {name}")
        self.stdout.write(
            f"retention: {days} day(s) for shopper data, {OTP_RETENTION_DAYS} day(s) for used codes"
            + (" (dry run)" if options["dry_run"] else "")
        )
