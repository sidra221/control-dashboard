from django.contrib import admin

from .models import ApiToken, Conversation, Product, ShopifyConnection


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
    list_display = ("title", "merchant", "category", "price", "external_id", "created_at")
    list_filter = ("merchant", "category")
    search_fields = ("title", "sku", "tags", "external_id")


@admin.register(ShopifyConnection)
class ShopifyConnectionAdmin(admin.ModelAdmin):
    list_display = ("shop_domain", "merchant", "is_active", "last_synced_at", "created_at")
    exclude = ("access_token", "token_expires_at")
    list_filter = ("is_active",)
    search_fields = ("shop_domain", "merchant__label")
    readonly_fields = ("last_synced_at", "created_at")


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = (
        "merchant", "session_id", "updated_at", "tryons", "buy_clicks", "add_to_carts",
        "discount_code", "order_id", "order_total",
    )
    list_filter = ("merchant",)
    search_fields = ("session_id", "order_id")
    readonly_fields = ("started_at", "updated_at")
