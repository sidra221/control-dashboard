from django.contrib import admin

from .models import ApiToken, Conversation, MerchantAccount, Product, ShopifyConnection, WooCommerceConnection


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


@admin.register(WooCommerceConnection)
class WooCommerceConnectionAdmin(admin.ModelAdmin):
    list_display = ("site_url", "merchant", "is_active", "last_synced_at", "created_at")
    exclude = ("consumer_key", "consumer_secret")
    list_filter = ("is_active",)
    search_fields = ("site_url", "merchant__label")
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


@admin.register(MerchantAccount)
class MerchantAccountAdmin(admin.ModelAdmin):
    """Merchant portal logins. The password hash is never shown or editable here: the model field is
    editable=False (so no form field) and it is excluded explicitly; staff only see whether one is set."""

    list_display = ("phone", "name", "merchant", "phone_verified", "is_active", "last_login_at", "created_at")
    list_filter = ("phone_verified", "is_active")
    search_fields = ("phone", "name")
    list_select_related = ("merchant",)
    exclude = ("password",)
    readonly_fields = ("password_status", "phone_verified_at", "last_login_at", "created_at")

    @admin.display(description="كلمة المرور")
    def password_status(self, obj):
        return "مُعيَّنة (مُشفَّرة، لا تُعرض)" if obj.password else "غير مُعيَّنة"
