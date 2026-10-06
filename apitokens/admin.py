from django.contrib import admin

from .models import ApiToken, Product


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    list_display = (
        "label",
        "token",
        "image_quota",
        "period",
        "images_used",
        "is_active",
        "created_by",
        "created_at",
    )
    list_filter = ("period", "is_active")
    search_fields = ("label", "token")
    readonly_fields = ("token", "images_used", "period_start", "created_at")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("title", "merchant", "category", "price", "created_at")
    list_filter = ("merchant", "category")
    search_fields = ("title", "sku", "tags")
