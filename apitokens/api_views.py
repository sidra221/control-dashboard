from django.db import transaction
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ApiToken
from .services import consume_auth, ratelimit


def _limited_response(decision):
    # دايماً HTTP 200 (عقد n8n) — النتيجة بحقل "error".
    return Response(
        {"ok": False, "error": "rate_limit_unavailable" if decision.unavailable else "rate_limited"}
    )


class ConsumeTokenView(APIView):
    """بيستدعيها الـ n8n webhook قبل ما يكمل لـ kie.ai: بيتحقق من الاعتماد
    وبيخصم صورة (أو أكتر) من رصيد الفترة الحالية، أو برجع خطأ إذا ما
    في رصيد / التاجر موقوف / الاعتماد مش صالح.

    الاعتماد: X-Consume-Key (سرّي، الأساسي) أو التوكن العلني القديم لمصدر داخلي
    بس (services/consume_auth.py). مفتاح الودجت لوحدو ما بيفوّض هالعملية.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        # دائماً HTTP 200 والنتيجة الحقيقية بحقل "ok" + "error" — حتى
        # عقدة HTTP Request بـ n8n ما توقف التنفيذ على 401/403/429 (تفعيل
        # "Never Error" بيختلف اسم الباراميتر بين نسخ n8n، فمنتجنبه كلياً).
        # حد بالـ IP قبل أي بحث بقاعدة البيانات (تخمين المفاتيح).
        decision = ratelimit.check(request, [("consume_ip", ratelimit.client_ip(request))])
        if not decision.allowed:
            return _limited_response(decision)

        auth = consume_auth.authenticate(request)
        if auth.error:
            return Response({"ok": False, "error": auth.error})
        merchant = auth.merchant

        decision = ratelimit.check(request, [("consume_token", str(merchant.pk))])
        if not decision.allowed:
            return _limited_response(decision)

        try:
            count = int(request.data.get("count", 1))
        except (TypeError, ValueError):
            return Response({"ok": False, "error": "invalid_count"})
        if count <= 0:
            return Response({"ok": False, "error": "invalid_count"})

        with transaction.atomic():
            token = ApiToken.objects.select_for_update().filter(pk=merchant.pk).first()
            if token is None:
                return Response({"ok": False, "error": "invalid_token"})
            if not token.is_active:
                return Response({"ok": False, "error": "token_disabled"})

            result = token.consume_quota(count)

        return Response(result)
