"""بيسأل LightOTP عن حالة رسائل التحقق اللي لسا ما انتهت (200 OK = بالطابور مو وصلت).

بياخد أكواد آخر 24 ساعة اللي إلها message_id وحالتها فاضية/Pending/Sent، وبيحدّث message_status
(Delivered/Read/Failed...)، ولو Failed بيحفظ failureCode/failureReason بـ send_error. خطأ صف واحد
ما بيوقف الباقي. شغّلها من cron كل كم دقيقة:

    python manage.py check_otp_status

ما بتطبع أرقام ولا أكواد، بس عدّاد.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apitokens.models import PhoneOtp
from apitokens.services import lightotp

OPEN_STATUSES = ("", "Pending", "Sent")


class Command(BaseCommand):
    help = "Poll LightOTP for the delivery status of recent portal OTP messages"

    def handle(self, *args, **options):
        since = timezone.now() - timedelta(hours=24)
        rows = (
            PhoneOtp.objects.filter(created_at__gte=since, message_status__in=OPEN_STATUSES)
            .exclude(message_id="")
            .exclude(message_id__startswith="console-")  # القناة المحلية: ما في شي نسأل عنه
            .order_by("pk")
        )
        checked = updated = errors = 0
        for otp in rows:
            try:
                data = lightotp.check_status(message_id=otp.message_id)
                status = str(data.get("messageStatus") or "")[:20]
            except lightotp.LightOtpNotConfigured:
                self.stderr.write("LightOTP is not configured: stopping")
                errors += 1
                break
            except lightotp.LightOtpError as exc:
                errors += 1
                self.stderr.write(f"otp #{otp.pk}: {str(exc.code)[:80]} (HTTP {exc.status})")
                continue
            except Exception as exc:  # رد غير متوقع (JSON ناقص...): نكمّل الباقي
                errors += 1
                self.stderr.write(f"otp #{otp.pk}: unexpected {type(exc).__name__}")
                continue
            checked += 1
            fields = []
            if status and status != otp.message_status:
                otp.message_status = status
                fields.append("message_status")
            if status == "Failed":
                reason = " - ".join(str(data[k]) for k in ("failureCode", "failureReason") if data.get(k))
                if reason:
                    otp.send_error = reason[:200]
                    fields.append("send_error")
            if fields:
                otp.save(update_fields=fields)
                updated += 1
        self.stdout.write(f"checked={checked} updated={updated} errors={errors}")
