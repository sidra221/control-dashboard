from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .models import ApiToken


@staff_member_required
def dashboard(request):
    new_token = None

    if request.method == "POST":
        label = request.POST.get("label", "").strip()
        quota_raw = request.POST.get("image_quota", "").strip()
        period = request.POST.get("period", ApiToken.Period.MONTHLY)

        error = None
        if not label:
            error = "لازم تكتب اسم للتعريف"
        elif not quota_raw.isdigit() or int(quota_raw) <= 0:
            error = "عدد الصور لازم يكون رقم أكبر من صفر"
        elif period not in ApiToken.Period.values:
            error = "فترة غير صحيحة"

        if error:
            messages.error(request, error)
        else:
            new_token = ApiToken.objects.create(
                label=label,
                image_quota=int(quota_raw),
                period=period,
                created_by=request.user,
            )
            messages.success(request, "تم إنشاء التوكن — انسخه وابعتو للمستخدم")

    tokens = ApiToken.objects.all()
    return render(
        request,
        "apitokens/dashboard.html",
        {
            "tokens": tokens,
            "new_token": new_token,
            "periods": ApiToken.Period.choices,
        },
    )


@staff_member_required
@require_http_methods(["POST"])
def toggle_active(request, pk):
    token = get_object_or_404(ApiToken, pk=pk)
    token.is_active = not token.is_active
    token.save(update_fields=["is_active"])
    return redirect("apitokens:dashboard")
