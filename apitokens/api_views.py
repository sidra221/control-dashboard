from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ApiToken


class ConsumeTokenView(APIView):
    """بيستدعيها الـ n8n webhook قبل ما يكمل لـ kie.ai: بيتحقق من التوكن
    وبيخصم صورة (أو أكتر) من رصيد الفترة الحالية، أو برجع خطأ إذا ما
    في رصيد / التوكن موقوف / مش موجود أصلاً.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        # دائماً HTTP 200 والنتيجة الحقيقية بحقل "ok" + "error" — حتى
        # عقدة HTTP Request بـ n8n ما توقف التنفيذ على 401/403/429 (تفعيل
        # "Never Error" بيختلف اسم الباراميتر بين نسخ n8n، فمنتجنبه كلياً).
        token_value = request.headers.get("X-Api-Token", "").strip()
        if not token_value:
            return Response({"ok": False, "error": "missing_token"})

        try:
            count = int(request.data.get("count", 1))
        except (TypeError, ValueError):
            return Response({"ok": False, "error": "invalid_count"})
        if count <= 0:
            return Response({"ok": False, "error": "invalid_count"})

        with transaction.atomic():
            token = (
                ApiToken.objects.select_for_update()
                .filter(token=token_value)
                .first()
            )
            if token is None:
                return Response({"ok": False, "error": "invalid_token"})
            if not token.is_active:
                return Response({"ok": False, "error": "token_disabled"})

            period_end = token.period_start + timedelta(days=token.period_days)
            if timezone.now() >= period_end:
                token.images_used = 0
                token.period_start = timezone.now()

            if token.images_used + count > token.image_quota:
                token.save(update_fields=["images_used", "period_start"])
                return Response(
                    {
                        "ok": False,
                        "error": "quota_exceeded",
                        "remaining": max(token.image_quota - token.images_used, 0),
                    }
                )

            token.images_used += count
            token.save(update_fields=["images_used", "period_start"])

        return Response(
            {
                "ok": True,
                "remaining": token.image_quota - token.images_used,
                "quota": token.image_quota,
                "period": token.period,
            }
        )
