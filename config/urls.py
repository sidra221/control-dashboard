from django.contrib import admin
from django.urls import include, path

from apitokens import salla_webhooks, shopify_webhooks, widget_views, zid_views, zid_webhooks

admin.site.site_header = "Control — kie.ai Tokens"
admin.site.site_title = "Control"
admin.site.index_title = "لوحة التحكم"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("apitokens.api_urls")),
    path("widget/chat/", widget_views.chat, name="widget-chat"),
    path("widget/tryon/", widget_views.tryon, name="widget-tryon"),
    path("widget/event/", widget_views.event, name="widget-event"),
    path("widget/page-product/", widget_views.page_product, name="widget-page-product"),
    path("widget/salla-key/<str:store_id>/", widget_views.salla_store_key, name="widget-salla-key"),
    path(
        "webhooks/shopify/<str:token>/",
        shopify_webhooks.product_webhook,
        name="shopify-webhook",
    ),
    path("webhooks/salla/", salla_webhooks.webhook, name="salla-webhook"),
    path("zid/install/", zid_views.install, name="zid-install"),
    path("zid/callback/", zid_views.callback, name="zid-callback"),
    path("zid/app/", zid_views.app, name="zid-app"),
    path("privacy/", zid_views.privacy, name="privacy"),
    path("webhooks/zid/app/", zid_webhooks.app_webhook, name="zid-app-webhook"),
    path("webhooks/zid/store/<str:store_id>/<str:event>/", zid_webhooks.store_webhook, name="zid-store-webhook"),
    path("", include("apitokens.urls")),
]
