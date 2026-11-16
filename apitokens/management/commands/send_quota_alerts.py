"""شبكة أمان (cron) لتنبيهات الرصيد: بيمرّ على كل المتاجر الفعّالة وبينادي maybe_send_quota_alerts.
التنبيه الأساسي بيطلع بعد كل تجربة ناجحة؛ هالأمر بيلحق أي تنبيه فات (مزوّد واقع، تجديد فترة...).
التكرار آمن: كل عتبة بتنرسل مرة وحدة بالفترة. شغّله مثلاً كل ساعة:

    python manage.py send_quota_alerts

بيطبع عدّادات بس: ما في أرقام هواتف ولا أسماء.
"""
from django.core.management.base import BaseCommand

from apitokens.models import ApiToken
from apitokens.services.quota_alerts import maybe_send_quota_alerts


class Command(BaseCommand):
    help = "Send pending WhatsApp quota alerts (80% / 100%) for every active store"

    def handle(self, *args, **options):
        checked = sent = 0
        for merchant in ApiToken.objects.filter(is_active=True).order_by("pk"):
            checked += 1
            sent += len(maybe_send_quota_alerts(merchant))
        self.stdout.write(f"checked={checked} alerts_sent={sent}")
