from django.contrib import admin
from django.urls import include, path

from apitokens import salla_webhooks, shopify_webhooks, widget_views

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
    path(
        "webhooks/shopify/<str:token>/",
        shopify_webhooks.product_webhook,
        name="shopify-webhook",
    ),
    path("webhooks/salla/", salla_webhooks.webhook, name="salla-webhook"),
    path("", include("apitokens.urls")),
]
